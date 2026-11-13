"""The search service: keeps the index in step with the talks, saves and restores it, and answers searches.

The server's hooks only queue work (`touch` when a talk's files changed, `rename` when only its names did, `forget`
when it is gone), so a request never waits for the index. One worker thread, started when there is work, reads the
changed talk's four files (transcript, Gujlish, English, summary) and lets the index rewrite only what differs, embeds
new texts in batches when meaning search is on, and saves the index.

The index lives in memory. It is saved whole as one file, `<folder>/index.sqlite`: built in a local temporary file and
copied in one sequential write under a temporary name, then renamed, a minute after the last change (ten at most).
Hosted, the folder is in the storage bucket, where a database written in place would rewrite objects at random. A
start restores that file, compares it with the catalogue to re-read only talks that differ, and then checks each
talk's files' sizes and times in the background for changes the last save missed.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from transcriber.files.storage import stage_paths
from transcriber.files.textutil import fmt_time
from transcriber.speech.edits import load_gujlish
from transcriber.speech.postprocess import drop_cut_stop

from .gemini import EMBED_DIMS, EMBED_MODEL
from .index import TalkIndex, name_fields
from .passages import Passage, build_passages

SNAPSHOT = "index.sqlite"
SAVE_AFTER = 60.0  # seconds without changes before the index is saved
SAVE_AT_MOST = 600.0  # and never later than this after the first unsaved change
CHECK_EVERY = 5.0  # a local site re-checks the talks' files before a search at most this often
LOCAL_WAIT = 5.0  # and a search there waits at most this long for changes it found to be indexed
EMBED_BATCH = 100
EMBED_SETTLE = 20.0  # seconds without text changes before new texts are embedded, so a burst goes in few requests
EMBED_RETRY = (30.0, 600.0)  # first and longest wait after Gemini refused or failed an embedding request
LOAD_RETRY = (5.0, 600.0)  # and after the saved index could not be read (the storage did not answer)
SAVE_RETRY = (60.0, 1800.0)  # first and longest wait after a save failed
STALE_TEMP = 3600.0  # a save's temporary file this old was left by a save cut short: removed at the next start
READ_TRIES = 3
QUERY_CACHE = 256  # query embeddings kept, so retyping or paging a search costs nothing


def english_lines(segments: list[dict], english: list[dict]) -> tuple[list[str | None], bool]:
    """The English of each transcript segment (None where there is none) and whether it was made from other words.
    Only segments with text were translated, in order, and a sample translation stops early."""
    # The translator reads each chunk without its cut full stop (transcriber.speech.postprocess.drop_cut_stop).
    current = [drop_cut_stop(s["text"].strip()) for s in segments if s.get("text", "").strip()]
    lines = iter(s.get("text", "") for s in english)
    aligned = [next(lines, None) if s.get("text", "").strip() else None for s in segments]
    return aligned, [s.get("source") for s in english] != current


def talk_paths(transcript_json: Path) -> list[Path]:
    paths = stage_paths(Path(transcript_json))
    return [Path(transcript_json), paths["gujlish_json"], paths["translation_json"], paths["summary_json"]]


def talk_stamp(transcript_json: Path) -> str:
    """The size and time of a talk's four searchable files: different whenever one of them changed."""
    parts = []
    for path in talk_paths(transcript_json):
        try:
            stat = path.stat()
        except FileNotFoundError:
            parts.append("-")
        else:
            parts.append(f"{stat.st_mtime_ns}:{stat.st_size}")
    return "|".join(parts)


@dataclass
class TalkText:
    stamp: str
    names: dict
    passages: list[Passage]


def read_talk(transcript_json: Path, names: dict) -> TalkText:
    """A finished talk's passages from its transcript, Gujlish, English and summary (those that exist). The stamp is
    taken first: a file changed during the read then differs from it, so the next check reads the talk again."""
    stamp = talk_stamp(transcript_json)
    transcript, gujlish_path, english_path, summary_path = talk_paths(transcript_json)
    segments = json.loads(transcript.read_text(encoding="utf-8")).get("segments", [])
    gujlish = load_gujlish(gujlish_path, segments)[0] if gujlish_path.exists() else None
    english = None
    if english_path.exists():
        english = english_lines(segments, json.loads(english_path.read_text(encoding="utf-8")).get("segments", []))[0]
    summary = None
    if summary_path.exists():
        summary = json.loads(summary_path.read_text(encoding="utf-8")).get("summary")
    return TalkText(stamp, names, build_passages(segments, gujlish, english, summary))


def link(talk: str, start) -> str:
    return f"#talk={talk}&t={start:g}" if start is not None else f"#talk={talk}&view=summary"


def clock(start) -> str:
    return fmt_time(start) if start is not None else ""


class SearchService:
    """`talks()` lists every finished talk as {id: names} (it may read the storage: called from the worker only, or
    on a local site); `names(id)` gives one talk's names from memory, or None when it is gone or unfinished;
    `read(id)` reads it (TalkText, or None when it is gone); `stamp(id)` checks its files (or None)."""

    def __init__(self, *, folder: Path, talks, names, read, stamp, embedder=None, meaning=None, trusted=True):
        self.folder = Path(folder)
        self._talks, self._names, self._read, self._stamp = talks, names, read, stamp
        self.embedder = embedder
        self._meaning = meaning or (lambda: False)
        self.trusted = trusted
        self.model = getattr(embedder, "model", EMBED_MODEL)
        self.dims = getattr(embedder, "dims", EMBED_DIMS)
        self.index: TalkIndex | None = None  # None until restored or built
        self.cond = threading.Condition()
        self.save_lock = threading.Lock()
        self.started = self.running = self.stopped = False
        self.worker: threading.Thread | None = None  # the latest worker thread
        self.pending: dict[str, str] = {}  # talk -> "touch", "rename" or "forget", oldest first
        self.generations: dict[str, int] = {}  # raised by touch and forget: a read begun before is not used
        self.tries: dict[str, int] = {}
        self.checks: deque[tuple[str, str]] = deque()  # (talk, stamp in the restored index) still to compare
        self.busy = 0
        self.version = 0  # raised by every change to the index; the save notes the one it saved
        self.saved_version = 0
        self.dirty_since: float | None = None
        self.changed_at = 0.0
        self.save_after = 0.0  # not before this: set when a save failed
        self.save_wait = SAVE_RETRY[0]
        self.load_after, self.load_wait = 0.0, LOAD_RETRY[0]
        self.working: str | None = None  # the talk being indexed now
        self.unembedded = True  # some passages may lack an embedding
        self.embed_after, self.embed_wait = 0.0, EMBED_RETRY[0]
        self.embed_settle, self.texts_changed_at = EMBED_SETTLE, time.monotonic()
        self.check_every, self.checked_at = CHECK_EVERY, 0.0
        self.queries: OrderedDict[tuple[str, bool], list[float]] = OrderedDict()
        self.quiet: set[str] = set()  # problems already reported once

    # ----- hooks: queue only -----

    def start(self) -> None:
        with self.cond:
            self.started = True
            self._wake()

    def touch(self, talk: str) -> None:
        """The talk's files changed (or it is new): read it again."""
        self._queue(talk, "touch", newer=True)

    def rename(self, talk: str) -> None:
        """Only the talk's names changed."""
        self._queue(talk, "rename", newer=False)

    def forget(self, talk: str) -> None:
        self._queue(talk, "forget", newer=True)

    def _queue(self, talk: str, action: str, newer: bool) -> None:
        with self.cond:
            if newer:
                self.generations[talk] = self.generations.get(talk, 0) + 1
                self.tries.pop(talk, None)
            if action != "rename" or self.pending.get(talk) not in ("touch", "forget"):
                self.pending[talk] = action  # a rename never replaces a read or a removal
            self._wake()

    def _wake(self) -> None:
        """Start the worker if it is not running (the caller holds cond). Never after stop()."""
        if not self.started or self.stopped:
            return
        if self.running:
            self.cond.notify_all()
            return
        self.running = True
        self.worker = threading.Thread(target=self._work, daemon=True, name="vaani-search")
        self.worker.start()

    def stop(self, timeout: float = 5.0) -> bool:
        """End the worker once its current task is done, then close the index. Nothing starts a worker afterwards.
        False, with the index left open, when the worker is still busy after `timeout` seconds."""
        with self.cond:
            self.stopped = True
            self.cond.notify_all()
            worker = self.worker
        if worker is not None:
            worker.join(timeout)
            if worker.is_alive():
                return False
        with self.cond:
            index, self.index = self.index, None
        if index is not None:
            index.close()
        return True

    # ----- the worker -----

    def _work(self) -> None:
        while True:
            with self.cond:
                while True:
                    task, wait = self._choose()
                    if task is not None or wait is None:
                        break
                    self.cond.wait(wait)
                if task is None:
                    self.running = False
                    self.cond.notify_all()
                    return
                self.busy += 1
            try:
                task()
            except Exception as error:  # noqa: BLE001 - one talk's problem must not stop the index
                self._report(f"{type(error).__name__}: {error}")
            finally:
                with self.cond:
                    self.busy -= 1
                    self.working = None
                    self.cond.notify_all()

    def _choose(self):
        """The next task and None, or None and how long to wait, or (None, None) when there is nothing left to do.
        The caller holds cond."""
        if self.stopped:
            return None, None
        now = time.monotonic()
        if self.index is None:
            return (self._load, None) if now >= self.load_after else (None, self.load_after - now)
        if self.pending:
            talk = next(iter(self.pending))
            action = self.pending.pop(talk)
            generation = self.generations.get(talk, 0)
            self.working = talk
            return (lambda: self._process(talk, action, generation)), None
        if self.checks:
            talk, stamp = self.checks.popleft()
            return (lambda: self._check(talk, stamp)), None
        # A save that is due goes first, so a long run of embeddings (or Gemini refusing them) never keeps the index
        # from being saved.
        dues = []
        if self.dirty_since is not None:
            dues.append(
                (max(min(self.changed_at + SAVE_AFTER, self.dirty_since + SAVE_AT_MOST), self.save_after), self._save)
            )
        if self.unembedded and self.embedder is not None and self._meaning():
            dues.append((self._embed_due(), self._embed))
        for due, task in dues:
            if now >= due:
                return task, None
        return (None, min(due for due, _ in dues) - now) if dues else (None, None)

    def _embed_due(self) -> float:
        return max(self.embed_after, self.texts_changed_at + self.embed_settle)

    def _changed(self, texts: bool = True) -> None:
        """Note a change to the index, of its texts or only of its embeddings (the caller holds cond)."""
        now = time.monotonic()
        self.version += 1
        self.changed_at = now
        if self.dirty_since is None:
            self.dirty_since = now
        if texts:
            self.texts_changed_at = now
            self.unembedded = True

    def _load(self) -> None:
        try:
            index = self._restore()
        except Exception as error:  # noqa: BLE001 - the storage did not answer (or a fault): try again later, never in a loop
            with self.cond:
                self.load_after = time.monotonic() + self.load_wait
                self.load_wait = min(LOAD_RETRY[1], self.load_wait * 2)
            self._report(f"Could not read the saved search index ({type(error).__name__}: {error}); trying again.")
            return
        self._tidy()
        indexed = index.talks()
        try:
            talks = self._talks()
        except Exception as error:  # noqa: BLE001 - search the snapshot as it is; changes still arrive through hooks
            self._report(f"Could not list the talks for search ({error}).")
            talks = {talk: None for talk in indexed}
        changed = False
        for talk, names in talks.items():
            if names is not None and talk in indexed:
                changed |= bool(index.rename(talk, names))  # names come from the catalogue: no file is read
        with self.cond:
            self.index = index
            for talk in indexed.keys() - talks.keys():
                self.pending.setdefault(talk, "forget")
            for talk in talks.keys() - indexed.keys():
                self.pending.setdefault(talk, "touch")
            # Then, in the background, each talk the snapshot had is checked for files changed after it was saved.
            self.checks.extend((talk, indexed[talk]) for talk in talks if talk in indexed)
            if changed:
                self._changed()
            self.unembedded = True

    def _restore(self) -> TalkIndex:
        """The saved index, or a new one when there is none or it is damaged or of another version. Raises OSError
        when the storage could not be read: a passing fault must not throw away (and later overwrite) a good file."""
        path, local = self.folder / SNAPSHOT, None
        try:
            handle, name = tempfile.mkstemp(suffix=".sqlite", prefix="vaani-search-")
            os.close(handle)
            local = Path(name)
            try:
                shutil.copyfile(path, local)  # one sequential read of the saved file
            except FileNotFoundError:
                return TalkIndex(self.model, self.dims)  # never saved: built from the talks
            try:
                return TalkIndex.load(local, self.model, self.dims)
            except (ValueError, sqlite3.DatabaseError) as error:  # damaged or of another version: rebuilt
                print(f"Rebuilding the search index ({type(error).__name__}: {error}).", flush=True)
                return TalkIndex(self.model, self.dims)
        finally:
            if local is not None:
                local.unlink(missing_ok=True)

    def _tidy(self) -> None:
        """Remove temporary files left by saves cut short (a stop mid-copy); recent ones may be another server's."""
        try:
            for path in self.folder.glob(f".{SNAPSHOT}.*.tmp"):
                if time.time() - path.stat().st_mtime > STALE_TEMP:
                    path.unlink(missing_ok=True)
        except OSError:
            pass  # tidied at a later start

    def _process(self, talk: str, action: str, generation: int) -> None:
        if action == "rename":
            names = self._names(talk)
            if names is None:
                with self.cond:  # gone or unfinished, unless a newer change came in meanwhile
                    if self.generations.get(talk, 0) == generation and talk not in self.pending:
                        self.forget(talk)
                return
            renamed = self.index.rename(talk, names)
            if renamed is None:
                return self.touch(talk)  # not indexed yet: read it whole
            if renamed:
                with self.cond:
                    self._changed()
            return
        text = None
        if action == "touch":
            try:
                text = self._read(talk)
            except FileNotFoundError as error:
                if self._names(talk) is not None:
                    return self._retry(talk, generation, error, quiet=True)  # e.g. a test's storage, removed
                text = None  # its folder is going with it (a delete or a sweep)
            except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
                return self._retry(talk, generation, error)
        with self.cond:
            if self.generations.get(talk, 0) != generation:
                return  # changed or removed while it was read: the newer task does it again
        # Written outside cond, so hooks (some called under the server's locks) never wait for it. A change that comes
        # in meanwhile is queued and done next.
        if text is None:
            if self.index.drop_talk(talk):
                with self.cond:
                    self._changed()
            return
        before = self.index.talks().get(talk)
        change = self.index.put_talk(talk, text.names, text.passages, text.stamp)
        if change.added or change.changed or change.removed or change.renamed or before != text.stamp:
            with self.cond:
                self._changed()

    def _retry(self, talk: str, generation: int, error: Exception, quiet: bool = False) -> None:
        """Read the talk again later, a few times; then leave it as it is until its next change."""
        with self.cond:
            if self.generations.get(talk, 0) != generation:
                return
            self.tries[talk] = self.tries.get(talk, 0) + 1
            if self.tries[talk] < READ_TRIES:
                self.pending.setdefault(talk, "touch")
                return
        if not quiet:
            self._report(f"Could not read a talk for search ({type(error).__name__}: {error}).")

    def _check(self, talk: str, stamp: str) -> None:
        try:
            now = self._stamp(talk)
        except OSError:
            return
        if now is not None and now != stamp:
            self.touch(talk)

    def _embed(self) -> None:
        missing = self.index.missing_vectors(EMBED_BATCH)
        if not missing:
            with self.cond:
                self.unembedded = False
            return
        try:
            vectors = self.embedder.documents([text for _, text in missing])
            sizes = {len(vector) for vector in vectors}
            if len(vectors) != len(missing) or sizes != {self.dims}:  # never sent again at once: a paid request
                raise ValueError(f"{len(vectors)} embeddings of {sorted(sizes)} numbers for {len(missing)} texts")
            stored = self.index.put_vectors(dict(zip((key for key, _ in missing), vectors)))
        except Exception as error:  # noqa: BLE001 - Gemini unreachable or refusing: words still work; try later
            with self.cond:
                self.embed_after = time.monotonic() + self.embed_wait
                self.embed_wait = min(EMBED_RETRY[1], self.embed_wait * 2)
            self._report(f"Meaning search is waiting for Gemini ({type(error).__name__}: {error}).")
            return
        # Whether any text is left is asked outside cond, so hooks never wait for it; a text change meanwhile wins.
        checked = self.texts_changed_at
        left = bool(self.index.missing_vectors(1)) if len(missing) < EMBED_BATCH else None
        with self.cond:
            self.embed_wait = EMBED_RETRY[0]
            if stored:
                self._changed(texts=False)
            if left is not None and self.texts_changed_at == checked:
                self.unembedded = left

    def _save(self) -> None:
        """Save the index now. Quietly gives up when the storage is not there (a test's, removed)."""
        with self.save_lock:
            with self.cond:
                index, version = self.index, self.version
            if index is None:
                return
            local = target = None
            try:
                handle, name = tempfile.mkstemp(suffix=".sqlite", prefix="vaani-search-")
                os.close(handle)
                local = Path(name)
                index.save(local)
                self.folder.mkdir(exist_ok=True)  # never its parents: they are the storage itself
                target = self.folder / f".{SNAPSHOT}.{uuid4().hex[:8]}.tmp"
                shutil.copyfile(local, target)  # one sequential write
                os.replace(target, self.folder / SNAPSHOT)
                target = None
            except FileNotFoundError:
                with self.cond:
                    self.dirty_since = None  # the storage is gone: nothing to save into
                return
            except (OSError, sqlite3.Error) as error:
                self._report(f"Could not save the search index ({error}); trying again later.")
                with self.cond:  # waiting longer each time, so a lasting fault is not a full write every minute
                    self.save_after = time.monotonic() + self.save_wait
                    self.save_wait = min(SAVE_RETRY[1], self.save_wait * 2)
                return
            finally:
                if local is not None:
                    local.unlink(missing_ok=True)
                if target is not None:
                    target.unlink(missing_ok=True)
            with self.cond:
                self.saved_version, self.save_after, self.save_wait = version, 0.0, SAVE_RETRY[0]
                if self.version == version:
                    self.dirty_since = None
                else:
                    self.dirty_since = self.changed_at  # changed while saving: saved again later

    def _report(self, message: str) -> None:
        if message not in self.quiet:
            self.quiet.add(message)
            print(f"Search: {message}", flush=True)

    # ----- requests -----

    def meaning_on(self) -> bool:
        return self.embedder is not None and self._meaning()

    def _query_vector(self, text: str, question: bool):
        key = (" ".join(text.split()).casefold(), question)
        with self.cond:
            vector = self.queries.get(key)
            if vector is not None:
                self.queries.move_to_end(key)
                return vector
        vector = self.embedder.query(text, question=question)
        with self.cond:
            self.queries[key] = vector
            while len(self.queries) > QUERY_CACHE:
                self.queries.popitem(last=False)
        return vector

    def _vector(self, text: str, question: bool):
        """(query vector or None, meaning status: "on", "off" or "failed")."""
        if not self.meaning_on():
            return None, "off"
        try:
            return self._query_vector(text, question), "on"
        except Exception as error:  # noqa: BLE001 - search by words when Gemini cannot embed the query
            self._report(f"Could not embed a search ({type(error).__name__}: {error}).")
            return None, "failed"

    def catch_up(self) -> None:
        """On a local site, the command line changes talks too: look for changes (at most every few seconds) and let
        the search wait a moment for them."""
        if self.trusted or self.index is None:
            return
        with self.cond:
            if time.monotonic() - self.checked_at < self.check_every:
                return
            self.checked_at = time.monotonic()
        talks = self._talks()
        indexed, held = self.index.talks(), self.index.names()
        found = set(indexed.keys() - talks.keys())
        for talk in found:
            self.forget(talk)
        for talk, names in talks.items():
            if talk not in indexed:
                found.add(talk)
                self.touch(talk)
                continue
            try:
                stamp = self._stamp(talk)
            except OSError:
                continue
            if stamp is not None and stamp != indexed[talk]:
                found.add(talk)
                self.touch(talk)
            elif names is not None and held.get(talk) != name_fields(names):
                found.add(talk)
                self.rename(talk)  # the worker reads the names afresh: a rename made meanwhile is never undone
        if found:
            self._wait_for(found, LOCAL_WAIT)

    def _wait_for(self, talks: set[str], timeout: float) -> bool:
        """Wait until `talks` are indexed (not for embeddings or saving)."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while self.working in talks or not talks.isdisjoint(self.pending):
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self.cond.wait(min(left, 0.5))
        return True

    def search(self, text: str, *, live: bool = False, meaning: bool = False, talks: int = 20) -> dict:
        self.catch_up()
        index = self.index
        if index is None:
            return {"state": "loading", "talks": [], "total": 0, "meaning": "off", "indexing": True}
        vector, status = self._vector(text, question=False) if meaning else (None, "skipped")
        result = index.find(text, live=live, vector=vector, talks=talks)
        with self.cond:
            indexing = bool(self.pending or self.busy or self.checks)
        return {**result, "state": "ready", "meaning": status, "indexing": indexing}

    def retrieve(self, question: str, limit: int = 10, per_talk: int = 4) -> list[dict] | None:
        """The passages a question is answered from (None while the index loads)."""
        self.catch_up()
        index = self.index
        if index is None:
            return None
        vector, _ = self._vector(question, question=True)
        return index.context(question, vector, limit=limit, per_talk=per_talk)

    def wait_idle(self, timeout: float = 10.0, vectors: bool = True) -> bool:
        """Wait until every queued change is indexed (and, with `vectors`, embedded while meaning search is on). After
        stop() nothing more is indexed, so it waits only for a task still running."""
        deadline = time.monotonic() + timeout
        with self.cond:
            while True:
                due = vectors and self.unembedded and self.meaning_on() and time.monotonic() >= self._embed_due()
                loading = self.started and self.index is None
                queued = not self.stopped and (loading or self.pending or self.checks or due)
                if not (queued or self.busy):
                    return True
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._wake()
                self.cond.wait(min(left, 0.5))

    def flush(self) -> None:
        """Save the index now instead of after the usual wait."""
        self._save()
