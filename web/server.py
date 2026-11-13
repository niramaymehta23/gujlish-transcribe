"""The upload server. Transcription runs on a GPU queue, one recording at a time; Gujlish, English and summaries run
on a separate queue, so a translation never waits for a transcription or the reverse.

Each run keeps a small state file in its folder (RUN_FILE). A restarted server still lists every run, opens it by the
same id, and picks up work a restart cut short. Hosted on Cloud Run, the machine can be replaced at any time.

Hosted, the folders sit in a storage bucket where every file check, listing and read is a network request. There the
server keeps a catalogue of every run in memory, saved to CATALOGUE_FILE, and keeps finished talks' files in a memory
cache, so opening the talk list or a finished talk touches no files."""

import hashlib
import hmac
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlparse
from uuid import uuid4

from flask import Flask, abort, g, jsonify, redirect, render_template, request, send_file, session, url_for

import transcriber.files.storage as storage
from transcriber.english.captions import captions_current, captions_srt
from transcriber.english.llm import LLMError, gemini_available
from transcriber.english.summarize import UNTITLED
from transcriber.english.youtube import shorten
from transcriber.files.storage import ROOT, new_run, run_paths, stage_paths
from transcriber.files.subtitles import to_srt
from transcriber.files.textutil import fmt_time, write_json
from transcriber.speech import align
from transcriber.speech.dictionary import (
    clip_audio,
    clips_dir,
    dictionary_path,
    forget,
    mode,
    reject,
    remember,
    save_clip,
    set_mode,
)
from transcriber.speech.dictionary import load as load_dictionary
from transcriber.speech.dictionary import save as save_dictionary
from transcriber.speech.edits import load_gujlish, matches, replace_words, save_transcript
from transcriber.speech.gujlish import KnownWordsOnly, Transliterator, XlitEngine, write_gujlish, xlit_installed
from web import usage
from web.cloudjobs import CloudRunJobs, RemoteRun, run_remote
from web.documents import markdown_docx, transcript_docx
from web.recordings import FINGERPRINT, fingerprint, fingerprints
from web.recordings import find as find_recording
from web.search import service as search_service
from web.search.gemini import Embedder
from web.search.gemini import answer as answer_question
from web.settings import Settings

LOG_TAIL_LINES = 12  # recent subprocess output kept for error messages
UPLOAD_CHUNK_BYTES = 8 * 1024**2  # Cloud Run caps a request body at 32 MiB, so large files arrive in pieces
HEX_ID = re.compile(r"[0-9a-f]{32}")
ACTIVE = {"queued", "loading", "transcribing"}
SWEEP_EVERY_SECONDS = 600
RUN_FILE = ".run.json"  # the recording, its settings, and the steps still to finish
MAX_RESUMES = 3  # restarts in a row after which cut-short work waits for you instead of resuming itself
STOP_GRACE_SECONDS = 10  # a stopped worker is killed if it is still running after this
WORD_PAD = 0.4  # seconds of audio played around words whose times are known (a little of the next word helps)
ROUGH_PAD = 1.0  # and around words whose times are estimated
LEFTOVER_SECONDS = 3600  # unused recordings and empty run folders younger than this may belong to an upload under way
EVENT_PREFIX = "ASR_EVENT "
CATALOGUE_FILE = ".catalogue.json"  # every run, in the transcripts folder (create_app's catalogue)
# A catalogue file of any other version is rebuilt from the run folders. Version 2 added the talks' names.
CATALOGUE_VERSION = 2
# What the catalogue keeps of each run as it is, besides its paths and the fields catalogue_entry() adds.
CATALOGUE_FIELDS = ("name", "model", "mode", "status", "progress", "message", "duration", "processed", "size")
CATALOGUE_FIELDS += ("sha256", "started")
TITLE_LIMIT = 120  # characters in a talk's name
# Characters of finished talks' files kept in memory; parsed, they take about twice that in bytes (Nov 2026 talks).
VIEW_CACHE_CHARS = 64 * 1024**2
SEARCH_LIMIT = 500  # characters in a search or a question
MOMENT_SECONDS = 30  # audio played for a search result when the page names no end
MOMENT_LIMIT = 45  # and never more
STOPPED = "Stopped. Start it again to continue where it left off."
GPU_LIMIT_MESSAGE = (
    "This month's GPU limit ({hours:g} hours) is used up, so no new transcriptions can start. "
    "It resets on the 1st (UTC), or the site owner can raise it."
)
BUSY_MESSAGE = "A recording is already being transcribed. Pause or cancel it, or wait for it to finish."
# Direction overrides and isolates: in a name they would turn the text shown after it around.
BIDI_CONTROLS = frozenset("‪‫‬‭‮⁦⁧⁨⁩")
# Sent with every response: no framing (clickjacking), no MIME sniffing, and only this site's own scripts, styles,
# fonts and media (the page has no inline script or style; word and moment audio play from blob: URLs).
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; media-src 'self' blob:; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


@dataclass(frozen=True)
class Stage:
    """One stage after transcription. Each goes by several names: job key ("translation"), URL (/translate), worker
    script, output file, and progress event ("translating")."""

    script: str
    output: str  # key in transcriber.files.storage.stage_paths
    verb: str


STAGES = {
    "gujlish": Stage("scripts/transliterate.py", "gujlish_json", "Writing Gujlish"),
    "translation": Stage("scripts/translate.py", "translation_txt", "Translating"),
    "captions": Stage("scripts/captions.py", "captions_json", "Timing the English captions"),
    "summary": Stage("scripts/summarize.py", "summary_md", "Writing the summary & YouTube details"),
}
PROGRESS_EVENTS = {"transliterating", "translating", "aligning", "captioning", "summarizing"}


def stage_output(transcript_json: str, key: str) -> Path:
    return stage_paths(Path(transcript_json))[STAGES[key].output]


def run_worker(command: list[str], on_event, on_start=None) -> tuple[int, list[str]]:
    """Run a worker script, passing each progress event to `on_event`; return its exit code and last log lines.
    `on_start` gets the process as soon as it exists, so it can be stopped."""
    logs: list[str] = []
    with subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,  # its own process group, so stopping it also stops the ffmpeg it starts
    ) as worker:
        if on_start:
            on_start(worker)
        for line in worker.stdout:
            logs = [*logs, line.strip()][-LOG_TAIL_LINES:]
            if line.startswith(EVENT_PREFIX):
                on_event(json.loads(line[len(EVENT_PREFIX) :]))
        return worker.wait(), logs


def stop_worker(worker) -> None:
    """Stop a worker and everything it started; kill them if they are still running after a grace period."""
    if isinstance(worker, RemoteRun):  # a Cloud Run GPU job: cancelling it stops the worker inside
        Thread(target=worker.stop, daemon=True).start()
        return

    def send(sig):
        try:
            os.killpg(worker.pid, sig)
        except OSError:
            pass  # already finished

    send(signal.SIGTERM)

    def force():
        try:
            worker.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL)

    Thread(target=force, daemon=True).start()


def last_error(logs: list[str], fallback: str) -> str:
    """The worker's final "Error: ..." line, or `fallback` when it printed none."""
    return next((line.removeprefix("Error: ") for line in reversed(logs) if line.startswith("Error:")), fallback)


def probe_audio(path: Path) -> float | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("This file could not be decoded. Choose a supported audio or video file.")
    data = json.loads(result.stdout)
    if not data.get("streams"):
        raise ValueError("This file does not contain an audio track.")
    try:
        duration = float(data.get("format", {}).get("duration", 0))
        return duration if duration > 0 else None
    except (TypeError, ValueError):
        return None


def clean_filename(name: str) -> str:
    """Preserve Gujarati/English filenames, stripping paths and unsafe characters."""
    raw_name = unicodedata.normalize("NFC", Path(name.replace("\\", "/")).name)
    return "".join(
        char if char in " .()-_" or unicodedata.category(char)[0] in "LNM" else "_" for char in raw_name
    ).strip(" .")[:180]


def clean_title(text: str) -> str:
    """A talk's name as it is kept, shown and searched: NFC, control characters and lone surrogates as spaces, no
    direction overrides, single spaces, and empty when nothing in it shows. Joiners and other format characters stay,
    as Gujarati spelling needs them."""
    text = unicodedata.normalize("NFC", text)
    kept = "".join(
        " " if unicodedata.category(char) in {"Cc", "Cs"} else "" if char in BIDI_CONTROLS else char for char in text
    )
    if not any(unicodedata.category(char)[0] in "LNPS" for char in kept):  # only spaces, joiners or marks
        return ""
    return " ".join(kept.split())


def summary_title(transcript_json) -> str | None:
    """The short title the Gemini summary gave the talk (transcriber.english.summarize), as a name for it: None when
    there is no summary, it can't be read, or the model gave no title."""
    try:
        data = json.loads(stage_paths(Path(transcript_json))["summary_json"].read_text(encoding="utf-8"))
        title = clean_title(data["summary"]["title"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return shorten(title, TITLE_LIMIT) if title and title != UNTITLED else None


def given_title(meta: dict) -> str | None:
    """The name you gave a run, from its RUN_FILE."""
    title = meta.get("title")
    return title if isinstance(title, str) and title else None


def shown_title(job: dict) -> tuple[str | None, str | None]:
    """The name a talk shows and where it came from: the one you gave it ("user"), else its summary's ("summary").
    With neither, (None, None): the page shows the file name."""
    if job.get("title"):
        return job["title"], "user"
    if job.get("auto_title"):
        return job["auto_title"], "summary"
    return None, None


def run_id(run_dir: Path) -> str:
    """A run's id comes from its folder, so it is the same after a restart and a page opened before one still works."""
    return hashlib.sha256("/".join(Path(run_dir).parts[-3:]).encode()).hexdigest()[:32]


def read_meta(run_dir: Path) -> dict:
    try:
        return json.loads((Path(run_dir) / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def started(run_dir: Path) -> str | None:
    """When the run began, with the server's time zone, so the browser can show it in yours."""
    try:
        return datetime.strptime(Path(run_dir).name, "%Y%m%d-%H%M%S-%f").astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


def find_transcript(run_dir: Path, meta: dict) -> Path | None:
    """The run's transcript JSON once transcription has finished, else None."""
    if meta.get("source") and meta.get("model"):
        txt, data = run_paths(Path(meta["source"]), meta["model"], run_dir)[:2]
        return data if data.exists() and txt.exists() else None
    # Runs from before RUN_FILE: the transcript has the shortest name, since stage files add to it.
    candidates = [p for p in run_dir.glob("*.json") if not p.name.startswith(".") and p.with_suffix(".txt").exists()]
    for path in sorted(candidates, key=lambda p: len(p.name)):
        try:
            if not json.loads(path.read_text(encoding="utf-8")).get("kind"):
                return path
        except (OSError, ValueError):
            continue
    return None


def stopped_at(job: dict) -> str:
    done, total = job.get("processed") or 0, job.get("duration")
    return f"{fmt_time(done)} of {fmt_time(total)}" if total else fmt_time(done)


def gpu_timeout(duration: float | None) -> int:
    """Longest a GPU job may run for a recording, so a stuck job cannot run on: 10 minutes for start-up and model
    loading, plus a third of the recording's length, within 15 minutes to 1 hour. In Nov 2026 a 12-minute talk took
    3.5 minutes in all (about 1 to start, then about 6 times faster than real time), so this leaves twice that."""
    return int(min(3600, max(15 * 60, 10 * 60 + (duration or 3 * 3600) / 3)))


def unsettled(meta: dict, status: str | None) -> bool:
    """Whether recover() has to look at a run after a restart: steps still to run, a GPU job to follow or end, or a
    transcription that was cut short. `meta` is its RUN_FILE."""
    cut_short = status != "complete" and not meta.get("paused") and not meta.get("failed")
    return bool(meta.get("pending") or meta.get("execution") or cut_short)


def outputs(transcript_json) -> tuple[str, ...]:
    """The stages whose output is on disk for a finished transcript (one file check each)."""
    if not transcript_json:
        return ()
    return tuple(key for key in STAGES if stage_output(transcript_json, key).exists())


def holds_run(run_dir: Path) -> bool:
    """Whether a folder holds a run, judged by its file names alone, so that a file which is there but can't be read
    just now (a passing storage error) still counts: its state file (RUN_FILE), a transcription checkpoint, or a
    transcript (a .json beside a .txt of the same name). Raises OSError when the folder can't be listed.

    A folder without any of these (an upload whose transcription never started, or what a delete cut short left)
    names no recording and has nothing to resume."""
    with os.scandir(run_dir) as entries:
        names = {entry.name for entry in entries}
    return (
        RUN_FILE in names
        or any(name.endswith(".partial.json") for name in names)
        or any(name.endswith(".json") and f"{name[:-5]}.txt" in names for name in names)
    )


def leftover(run_dir: Path, cutoff: float) -> bool:
    """A run folder made before `cutoff` that holds no run (holds_run): left by an upload whose transcription never
    started, or by a delete cut short. A folder that can't be listed just now is never taken for one."""
    began = started(run_dir)
    if not began or datetime.fromisoformat(began).timestamp() >= cutoff:
        return False
    try:
        return not holds_run(run_dir)
    except OSError:
        return False


def relative(path, base: Path) -> str | None:
    """`path` relative to `base` when it is inside it (so a catalogue survives the storage moving), else as it is."""
    if not path:
        return None
    try:
        return str(Path(path).relative_to(base))
    except ValueError:
        return str(path)


class ViewCache:
    """Finished talks' files (talk_files), least recently used dropped first once they pass `budget` characters.

    Each talk has a generation, raised whenever the talk changes. A load passes the generation it started at to put(),
    which keeps the files only if nothing changed meanwhile: otherwise they may be from before the change."""

    def __init__(self, budget: int):
        self.budget, self.used = budget, 0
        self.entries: OrderedDict[str, dict] = OrderedDict()
        # One number per talk that ever changed while this server ran, so it grows only as fast as the catalogue.
        self.generations: dict[str, int] = {}
        self.lock = Lock()

    def get(self, key: str) -> tuple[dict | None, int]:
        """The talk's cached files (or None) and its generation, for put() after loading them."""
        with self.lock:
            files = self.entries.get(key)
            if files is not None:
                self.entries.move_to_end(key)
            return files, self.generations.get(key, 0)

    def put(self, key: str, generation: int, files: dict) -> None:
        with self.lock:
            if self.generations.get(key, 0) != generation or files["size"] > self.budget:
                return
            self._drop(key)
            self.entries[key] = files
            self.used += files["size"]
            while self.used > self.budget:
                self._drop(next(iter(self.entries)))

    def forget(self, key: str) -> None:
        with self.lock:
            self.generations[key] = self.generations.get(key, 0) + 1
            self._drop(key)

    def _drop(self, key: str) -> None:
        files = self.entries.pop(key, None)
        if files is not None:
            self.used -= files["size"]


def talk_files(job: dict, made) -> dict:
    """Everything a finished talk's page shows from its files, read once: the transcript, and the outputs of the stages
    in `made` (no others are read). `size` counts the characters read, for the cache."""
    sizes = []

    def text(path: Path) -> str:
        content = path.read_text(encoding="utf-8")
        sizes.append(len(content))
        return content

    def optional(path: Path) -> str | None:
        try:
            return text(path)
        except FileNotFoundError:
            return None

    def stat(path: Path):
        try:
            return path.stat()
        except FileNotFoundError:
            return None

    files = {"text": text(Path(job["txt"])), "translation_text": None, "english": {}, "captions_current": False}
    files.update(summary=None, cacheable=True)
    if not job.get("json"):
        files["size"] = sum(sizes)
        return files
    transcript_json = Path(job["json"])
    paths = stage_paths(transcript_json)
    segments = json.loads(text(transcript_json)).get("segments", [])
    gujlish = load_gujlish(paths["gujlish_json"], segments)[0] if "gujlish" in made else None
    stamps = (stat(transcript_json), stat(paths["gujlish_json"]) if "gujlish" in made else None)
    files.update(
        segments=[
            {
                "start": s["start"],
                "end": s["end"],
                "text": s.get("text", ""),
                "fixes": s.get("fixes", []),
                "suggestions": s.get("suggestions", []),
            }
            for s in segments
        ],
        gujlish_tokens=gujlish,
        rev="-".join(str(stamp.st_mtime_ns) for stamp in stamps if stamp),
        gujlish_text=None if gujlish is None else text(paths["gujlish_txt"]),
    )
    if gujlish is not None:
        sizes.append(sum(len(token) for tokens in gujlish for token in tokens))  # read by load_gujlish
    # Output that should be there but did not load or match may be a passing read error: not cached, read next time.
    files["cacheable"] = "gujlish" not in made or gujlish is not None
    if "translation" in made:
        files["translation_text"] = text(paths["translation_txt"])
        english = optional(paths["translation_json"])
        if english is not None:
            # The page shows each line's English under it (None where there is none).
            lines, stale = search_service.english_lines(segments, json.loads(english).get("segments", []))
            files["english"] = {"english_stale": stale, "english_segments": lines}
        files["captions_current"] = "captions" in made and captions_current(transcript_json)
        files["cacheable"] &= "captions" not in made or files["captions_current"]
    if "summary" in made:
        markdown = text(paths["summary_md"])
        youtube = optional(paths["youtube_json"])  # missing for a summary from before this step wrote them
        if youtube is not None:
            youtube = json.loads(youtube)
            youtube = {key: youtube.get(key, "") for key in ("title", "description")}
        english_at = stat(paths["translation_json"])
        older = english_at is not None and paths["summary_md"].stat().st_mtime < english_at.st_mtime
        files["summary"] = {"markdown": markdown, "youtube": youtube, "older": older}
    files["size"] = sum(sizes)
    return files


def transcript_view(files: dict, state: dict) -> dict:
    """Segments for the word editor, the Gujlish words lined up with them, the English and summary texts, and whether
    those were made before the latest word fixes: `files` (talk_files) as far as the stages' states in `state` allow."""
    view = {key: files[key] for key in ("segments", "gujlish_tokens", "rev")}
    if files["gujlish_tokens"] is not None:
        view["gujlish_text"] = files["gujlish_text"]
    if state["translation"]["status"] == "complete":
        view["translation_text"] = files["translation_text"]
        view.update(files["english"])
        # A translation made before the caption step, or whose captions were not redone after it changed.
        view["captions_stale"] = state["captions"]["status"] != "running" and not files["captions_current"]
    summary = files["summary"]
    if state["summary"]["status"] == "complete" and summary is not None:
        view["summary_markdown"] = summary["markdown"]
        view["youtube_missing"] = summary["youtube"] is None
        view["summary_stale"] = view.get("english_stale", False) or view["youtube_missing"] or summary["older"]
        if summary["youtube"] is not None:
            view["youtube"] = summary["youtube"]
    return view


def create_app(
    *, executor=None, settings: Settings | None = None, gpu_jobs=None, embedder=None, answer_llm=None
) -> Flask:
    """`embedder` and `answer_llm` stand in for Gemini in search (tests); otherwise Gemini is used where allowed."""
    settings = settings or Settings.from_env()
    # Hosted with a GPU job: transcription runs there and this website needs no GPU (web/cloudjobs.py).
    if gpu_jobs is None and settings.gpu_job:
        gpu_jobs = CloudRunJobs(settings.project, settings.region, settings.gpu_job)
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = int(settings.max_upload_mb * 1024**2) + 1024**2
    app.config.update(
        SECRET_KEY=settings.secret_key or os.urandom(32).hex(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.hosted,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    jobs = {}
    lock = Lock()
    meta_lock = Lock()  # RUN_FILE is read, changed and written back from several threads
    edit_lock = Lock()  # one word fix at a time: each rewrites the transcript files and may save the dictionary
    align_lock = Lock()  # one line aligned at a time, on the CPU, with one copy of the model
    aligner: dict = {}  # "model": the Aligner (None when it can't load), loaded the first time a word is played
    aligned: dict = {}  # word times of lines aligned here, by transcript and line
    xlit = []  # the IndicXlit engine for editor suggestions, loaded on first use
    # Transcription needs the GPU, one recording at a time. Gujlish and the Gemini stages need neither.
    gpu = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-gpu")
    text = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr-text")
    workers = {}  # (job id, step) -> its running process; step is "transcribe" or a stage key
    tasks = {}  # (job id, step) -> the queued task allowed to run it; stopping or queueing again changes it
    stopping = {}  # (job id, step) -> why its process is being stopped: "pause", "stop" or "delete"
    deleted = set()  # run folders being removed in the background
    claims = set()  # requests let through to start a transcription on a hosted site, until they end (take_the_place)
    uploads_dir = Path(tempfile.gettempdir()) / "vaani-uploads"  # local disk, not the mounted bucket
    last_sweep = [0.0]
    sequence = [0]
    recovered = [False]
    recover_lock = Lock()
    # Trusted: the folders change only through this server, and through its GPU job, which the server follows to the
    # end. So the server can know every run and every finished talk's files from memory: the catalogue below and
    # `views`. A hosted site is trusted. Locally the command line writes to the same folders, so they are read live.
    trusted = settings.hosted
    # The catalogue is `jobs`, saved after each change of state (not of progress) to one file, so a start reads one
    # file instead of every run folder. Its place is fixed here, so a late background save cannot land elsewhere.
    transcripts = storage.TRANSCRIPTS
    catalogue_path = transcripts / CATALOGUE_FILE
    save_lock = Lock()
    saving = {"dirty": False, "writing": False}
    outputs_lock = Lock()  # one run's stage outputs checked and noted at a time (note_outputs)
    views = ViewCache(VIEW_CACHE_CHARS)

    # Search across all talks (web/search). The hooks below only queue work: a worker thread reads the files.
    def search_names(job: dict) -> dict:
        return {"given": job.get("title") or "", "auto": job.get("auto_title") or "", "file": job.get("name") or ""}

    def search_job(talk: str) -> dict | None:
        """A copy of the record of a talk with a transcript, or None. (Its status is not asked: a transcript is taken
        in before its run is marked complete.)"""
        with lock:
            job = jobs.get(talk)
            return dict(job) if job and job.get("json") else None

    def search_talks() -> dict:
        finished = [job for job in all_runs() if job.get("json")]
        with lock:
            return {job["id"]: search_names(job) for job in finished}

    def search_talk_names(talk: str) -> dict | None:
        job = search_job(talk)
        return search_names(job) if job else None

    def search_read(talk: str):
        job = search_job(talk)
        return search_service.read_talk(Path(job["json"]), search_names(job)) if job else None

    def search_stamp(talk: str) -> str | None:
        job = search_job(talk)
        return search_service.talk_stamp(Path(job["json"])) if job else None

    searcher = search_service.SearchService(
        folder=transcripts.parent / ".search",  # beside the transcripts folder, in the same storage
        talks=search_talks,
        names=search_talk_names,
        read=search_read,
        stamp=search_stamp,
        embedder=embedder if embedder is not None else Embedder(),
        meaning=(lambda: True) if embedder is not None else (lambda: gemini_ready()),
        trusted=trusted,
    )
    app.extensions["asr_search"] = searcher
    app.extensions["asr_jobs"] = jobs
    app.extensions["asr_settings"] = settings
    app.extensions["asr_reconcile"] = None  # the thread checking the catalogue against the folders after a start

    def gpu_limit_reached() -> bool:
        """True once this month's GPU job time has reached the limit (only when transcription runs on a GPU job)."""
        if gpu_jobs is None or not settings.gpu_hours_per_month:
            return False
        return usage.month_total()["seconds"] >= settings.gpu_hours_per_month * 3600

    def gpu_job_running(execution: str) -> bool:
        if gpu_jobs is None:
            return False
        try:
            return not gpu_jobs.status(execution)["done"]
        except (OSError, ValueError, KeyError):
            return False

    def log_gpu(execution: str, status: dict, run_dir: Path) -> None:
        """Record the GPU time Cloud Run reports for a finished job (what Google bills), over the job's own estimate."""
        if status.get("started") and status.get("ended"):
            try:
                usage.record(execution, status["started"], status["ended"], run=Path(run_dir).name)
            except OSError:
                pass  # the job's own record stays

    def browser_tag() -> str:
        """A per-browser id kept in the signed session cookie, so that only the browser sending a recording in pieces
        can add to it. Talks themselves are everyone's: on a site with a password, being signed in is the account."""
        if "visitor" not in session:
            session.permanent = True
            session["visitor"] = uuid4().hex
        return hashlib.sha256(session["visitor"].encode()).hexdigest()[:32]

    def run_dirs(root: Path | None = None, strict: bool = False) -> list[Path]:
        """transcripts/<recording>/<model>/<run>/ folders. Listed level by level, so one deleted in the meantime is
        skipped (Python 3.10's glob fails on it instead). Any other listing error leaves that folder's runs out, or,
        when `strict`, raises OSError: for decisions that must not miss a run."""

        def folders(parent):
            try:
                if strict:  # a folder entry that can't be checked raises here too, instead of reading as "no folder"
                    with os.scandir(parent) as entries:
                        return [Path(entry.path) for entry in entries if entry.is_dir()]
                return [path for path in parent.iterdir() if path.is_dir()]
            except FileNotFoundError:
                return []
            except OSError:
                if strict:
                    raise
                return []

        runs = [
            run for name in folders(root or storage.TRANSCRIPTS) for label in folders(name) for run in folders(label)
        ]
        return [run for run in runs if str(run) not in deleted]

    def load_run(run_dir: Path) -> dict | None:
        """A job record rebuilt from a run folder: finished, paused, or cut short by a restart. None when there is no
        run, or its RUN_FILE is there but can't be read just now: read without it, a run would lose its name and
        which stored recording it uses (after a merge), and a later merge could remove that recording."""
        try:
            meta = json.loads((Path(run_dir) / RUN_FILE).read_text(encoding="utf-8"))
        except FileNotFoundError:
            meta = {}  # a run from before RUN_FILE
        except (OSError, ValueError):
            return None
        if not isinstance(meta, dict):
            return None
        record = {"id": run_id(run_dir), "run_dir": str(run_dir), "started": started(run_dir)}
        try:
            transcript = find_transcript(run_dir, meta)
            if transcript:
                data = json.loads(transcript.read_text(encoding="utf-8"))
                record.update(name=data["source"], model=data["model"], mode=data.get("mode"))
                record.update(duration=data["duration"], status="complete", progress=100, message="Transcript ready")
                record.update(txt=str(transcript.with_suffix(".txt")), json=str(transcript))
            elif meta.get("source"):
                record.update(name=meta["source"], model=meta["model"], mode=meta.get("mode"))
                checkpoint = run_paths(Path(meta["source"]), meta["model"], run_dir)[3]
                done = json.loads(checkpoint.read_text(encoding="utf-8"))["segments"] if checkpoint.exists() else []
                record.update(duration=meta.get("duration"), processed=done[-1]["end"] if done else 0)
                if record["duration"]:
                    record["progress"] = min(99, round(record["processed"] / record["duration"] * 100))
                if meta.get("paused"):
                    record.update(
                        status="paused", message=f"Paused at {stopped_at(record)}. Resume continues from there."
                    )
                else:
                    reason = meta.get("failed") or f"Stopped at {stopped_at(record)} when the server restarted."
                    record.update(status="error", message=reason)
            else:
                return None
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None
        # "name" is what you called the file; "recording" points at the stored copy when duplicates were merged.
        recording = storage.RECORDINGS / (meta.get("recording") or record["name"])
        record.update(name=meta.get("name") or record["name"], source=str(recording), sha256=meta.get("sha256"))
        record["size"] = meta.get("size") or (recording.stat().st_size if recording.is_file() else None)
        if trusted:
            record.update(done=outputs(record.get("json")), unsettled=unsettled(meta, record["status"]))
        # Its names: the one you gave it, from RUN_FILE, and its summary's (read only when there is a summary).
        summarised = "summary" in record["done"] if trusted else bool(record.get("json"))
        record.update(title=given_title(meta), auto_title=summary_title(record["json"]) if summarised else None)
        return record

    def restore(job_id: str) -> dict | None:
        """Load a run this server has not seen yet, e.g. one a page opened before a restart. The caller holds the lock.
        A trusted server has every run in its catalogue, so there is nothing to look for."""
        if trusted:
            return None
        for run_dir in run_dirs():
            if run_id(run_dir) == job_id:
                record = load_run(run_dir)
                if record:
                    jobs[job_id] = record
                return record
        return None

    def find_job(job_id: str) -> dict:
        """The job dict (the caller holds the lock), or 404 for an unknown run. Everyone who may use the site may open
        every run (guard_requests)."""
        job = jobs.get(job_id) or (restore(job_id) if HEX_ID.fullmatch(job_id) else None)
        if not job:
            abort(404)
        return job

    def all_runs() -> list[dict]:
        """Every run, newest first: from the catalogue when trusted, else from disk, loading the ones this server has
        not seen yet."""
        if trusted:
            with lock:
                return sorted(jobs.values(), key=lambda job: os.path.basename(job["run_dir"]), reverse=True)
        found = []
        for run_dir in sorted(run_dirs(), key=lambda path: path.name, reverse=True):
            job_id = run_id(run_dir)
            with lock:
                job = jobs.get(job_id)
            if job is None:
                record = load_run(run_dir)
                if not record:
                    continue
                with lock:
                    job = jobs.setdefault(job_id, record)
            found.append(job)
        return found

    def update_meta(run_dir, change) -> bool:
        """Read the run's RUN_FILE, let `change` edit it, write it back; False when skipped because the run is being
        deleted, its folder is gone, or its RUN_FILE is there but can't be read just now (written back from nothing,
        it would lose the run's recording, model and pending work). The run's record follows the file (follow_meta),
        and the catalogue is saved when that changed it."""
        with meta_lock:
            if str(run_dir) in deleted or not Path(run_dir).is_dir():
                return False
            try:
                meta = json.loads((Path(run_dir) / RUN_FILE).read_text(encoding="utf-8"))
            except FileNotFoundError:
                meta = {}  # a run from before RUN_FILE
            except (OSError, ValueError):
                return False
            if not isinstance(meta, dict):
                return False
            change(meta)
            write_json(Path(run_dir) / RUN_FILE, meta)
            changed = follow_meta(run_dir, meta)
        if changed:
            save_catalogue()
        return True

    def follow_meta(run_dir, meta: dict) -> bool:
        """Bring the run's record in line with its RUN_FILE, as just read or written (the caller holds meta_lock, so
        no other change of the file comes in between): the name you gave it and, when trusted, whether a restart
        must look at it. True when the catalogue changed."""
        with lock:
            job = jobs.get(run_id(run_dir))
            if job is None:
                return False
            before = (job.get("title"), job.get("unsettled"))
            job["title"] = given_title(meta)
            if job["title"] != before[0]:
                searcher.rename(job["id"])
            if trusted:
                job["unsettled"] = unsettled(meta, job.get("status"))
            return trusted and (job.get("title"), job.get("unsettled")) != before

    def save_catalogue() -> None:
        """Write the catalogue file (trusted only). A save asked for while another is being written is left to that
        writer, which writes once more when it is done: a burst of changes costs two writes, never a queue of them.
        A failed write is retried by the next save; each write holds every run, so none is lost meanwhile."""
        if not trusted:
            return
        with save_lock:
            saving["dirty"] = True
            if saving["writing"]:
                return
            saving["writing"] = True
        while True:
            with save_lock:
                if not saving["dirty"]:
                    saving["writing"] = False
                    return
                saving["dirty"] = False
            try:
                with lock:
                    runs = [catalogue_entry(job) for job in jobs.values()]
                catalogue = {"version": CATALOGUE_VERSION, "runs": runs}
                try:
                    write_json(catalogue_path, catalogue)
                except FileNotFoundError:  # a new site has no transcripts folder until its first run
                    catalogue_path.parent.mkdir(exist_ok=True)  # not its parents: they are the storage itself
                    write_json(catalogue_path, catalogue)
            except BaseException as error:
                with save_lock:
                    saving.update(dirty=True, writing=False)
                if not isinstance(error, OSError):
                    raise
                print(f"Could not save the talk catalogue ({error}); the next change saves it again.", flush=True)
                return

    def catalogue_entry(job: dict) -> dict:
        """What the catalogue keeps of a run: what history and the run's page show without opening its folder, and
        whether recover() has to look at it after a restart. Paths are kept relative to their folders."""
        run_dir = Path(job["run_dir"])
        entry = {key: job[key] for key in CATALOGUE_FIELDS if key in job}
        if entry.get("status") == "complete":
            entry.pop("processed", None)  # as for a finished run read from its folder (load_run)
        entry.update(
            run=relative(run_dir, transcripts),
            source=relative(job.get("source"), storage.RECORDINGS),
            txt=relative(job.get("txt"), run_dir),
            json=relative(job.get("json"), run_dir),
            done=list(job.get("done", ())),
            unsettled=bool(job.get("unsettled", True)) or job.get("status") in ACTIVE,
            # Its names, as plain NFC text for search: the one you gave it (also in RUN_FILE) and its summary's.
            title=job.get("title"),
            auto_title=job.get("auto_title"),
        )
        return entry

    def catalogue_record(entry: dict) -> dict:
        """A job record from a catalogue entry. Anything malformed raises, and the file is rebuilt."""
        if not isinstance(entry.get("run"), str) or not entry["run"] or not isinstance(entry.get("status"), str):
            raise ValueError("a run without a folder or a status")
        if entry["status"] == "complete" and not (entry.get("txt") and entry.get("json")):
            raise ValueError("a finished run without its transcript")
        if any(not isinstance(entry.get(key), (str, type(None))) for key in ("title", "auto_title")):
            raise ValueError("a name that is not text")
        run_dir = transcripts / entry["run"]
        record = {key: entry[key] for key in CATALOGUE_FIELDS if key in entry}
        record.update(id=run_id(run_dir), run_dir=str(run_dir), title=entry.get("title") or None)
        record["auto_title"] = entry.get("auto_title") or None
        record["done"] = tuple(key for key in entry["done"] if key in STAGES)
        record["unsettled"] = bool(entry["unsettled"]) or entry["status"] in ACTIVE  # cut short if it was running
        for key, base in (("source", storage.RECORDINGS), ("txt", run_dir), ("json", run_dir)):
            record[key] = str(base / entry[key]) if entry.get(key) else None
        return record

    def open_catalogue() -> bool:
        """Load every run from the catalogue file; True when it could be used. A missing or damaged file, or one of
        another version, is rebuilt from the run folders instead (False). Runs recover() has to look at are read again
        from their folders, as their state may have moved on after the file was saved."""
        try:
            data = json.loads(catalogue_path.read_text(encoding="utf-8"))
            if data.get("version") != CATALOGUE_VERSION:
                raise ValueError(f"version {data.get('version')!r}, not {CATALOGUE_VERSION}")
            records = [catalogue_record(entry) for entry in data["runs"]]
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as error:
            print(f"Building the talk catalogue from the run folders ({type(error).__name__}: {error}).", flush=True)
            rebuild_catalogue()
            return False
        for record in records:
            if record["unsettled"]:
                record = load_run(Path(record["run_dir"]))
                if record is None:
                    continue  # its folder is gone or unreadable; reconcile() adds it back if it is there after all
            with lock:
                jobs.setdefault(record["id"], record)
        return True

    def rebuild_catalogue() -> None:
        """Fill the catalogue by reading every run folder, and save it. Run folders left by uploads whose transcription
        never started are removed on the way."""
        cutoff = time.time() - LEFTOVER_SECONDS
        for run_dir in run_dirs(transcripts):
            record = load_run(run_dir)
            if record:
                with lock:
                    jobs.setdefault(record["id"], record)
            elif leftover(run_dir, cutoff):
                shutil.rmtree(run_dir, ignore_errors=True)
        save_catalogue()

    def reconcile(root: Path) -> None:
        """After a start from the catalogue file, bring it in line with the run folders, in the background. Then a start
        ends where the old folder scan did even when the file missed something (a save that failed, two versions of
        the site running during a deploy, or an older version run in between): add the runs it lacks and pick up their
        work, refresh the runs it has, drop the runs whose folders are gone, and remove leftover run folders. A folder
        that can't be listed raises OSError before anything changes: a partial listing would drop the runs it missed."""
        cutoff = time.time() - LEFTOVER_SECONDS
        listed = set()
        for run_dir in run_dirs(root, strict=True):
            job_id = run_id(run_dir)
            listed.add(job_id)
            with lock:
                job = jobs.get(job_id)
            if job is not None:
                refresh(job, run_dir)
                continue
            record = load_run(run_dir)
            if record is None:
                if leftover(run_dir, cutoff):
                    shutil.rmtree(run_dir, ignore_errors=True)
                continue
            with lock:
                added = str(run_dir) not in deleted and jobs.setdefault(job_id, record) is record
            if added and record["status"] == "complete":
                searcher.touch(job_id)
            if added and record["unsettled"]:
                resume_run(run_dir)  # nobody could see it until now, so nothing else can be doing its work
        with lock:
            unlisted = [job for job_id, job in jobs.items() if job_id not in listed and job["status"] not in ACTIVE]
        # Confirmed one by one: a run made after the listing began is not in it either.
        gone = [job for job in unlisted if not Path(job["run_dir"]).is_dir()]
        with lock:
            for job in gone:
                if jobs.get(job["id"]) is job:
                    del jobs[job["id"]]
        for job in gone:
            views.forget(job["run_dir"])
            searcher.forget(job["id"])
        save_catalogue()

    def refresh(job: dict, run_dir: Path) -> None:
        """Take in what changed in a known run's folder without this server: the name you gave it, which stage outputs
        it has (and so its summary's name), and work its RUN_FILE says a restart should pick up. A run with work under
        way is left alone."""
        with lock:
            if jobs.get(job["id"]) is not job or job["status"] in ACTIVE or busy(job, *STAGES):
                return
            settled = not job.get("unsettled")
        with meta_lock:  # so a rename made meanwhile is not undone by the name read before it
            meta = read_meta(run_dir)
            if meta:  # else unreadable for now: keep what the catalogue knows
                with lock:
                    renamed = job.get("title") != given_title(meta)
                    job["title"] = given_title(meta)
                if renamed:
                    searcher.rename(job["id"])
        if job["status"] == "complete" and note_outputs(job)[1]:
            views.forget(job["run_dir"])
            searcher.touch(job["id"])
            name_from_summary(job)
        if settled and unsettled(meta, job["status"]):
            with lock:
                job["unsettled"] = True
            resume_run(run_dir)

    def check_catalogue(root: Path) -> None:
        """The background half of a start from the catalogue file: reconcile it, then merge duplicate recordings, which
        must know every run first so that no recording a run uses is taken for unused."""
        try:
            reconcile(root)
        except OSError as error:  # the storage did not answer: the catalogue stays as loaded, the next start checks
            print(f"Could not check the talk catalogue against the run folders ({error}).", flush=True)
            return
        tidy_recordings()

    def tidy_recordings() -> None:
        """merge_recordings() on a hosted site. A storage error leaves everything as it is until the next start."""
        if not settings.hosted:
            return
        try:
            merge_recordings()
        except OSError as error:  # e.g. a recording removed meanwhile, or a folder that could not be listed
            print(f"Could not merge duplicate recordings ({error}).", flush=True)

    def every_run_known() -> bool:
        """Whether every run in the storage is in the catalogue. Checked on a hosted site before a recording is removed,
        so that a run whose folder could not be read just now (a passing storage error) never loses its recording.
        Folders that hold no run (holds_run) don't count. False, saying which folder, when one can't be checked."""
        try:
            listed = run_dirs(strict=True)
            with lock:
                unknown = [run for run in listed if run_id(run) not in jobs and str(run) not in deleted]
            for run_dir in unknown:
                if holds_run(run_dir):
                    print(f"Recordings kept for now: the run in {run_dir} could not be read.", flush=True)
                    return False
        except OSError as error:
            print(f"Recordings kept for now: the run folders could not all be listed ({error}).", flush=True)
            return False
        return True

    def note_outputs(job: dict) -> tuple[tuple[str, ...], bool]:
        """Check which stage outputs the run has and, when trusted, note them; also whether that changed. One run at a
        time, so two stages ending together cannot each note the other's output as missing."""
        with outputs_lock:
            made = outputs(job.get("json"))
            with lock:
                changed = job.get("done") != made
                if trusted:
                    job["done"] = made
        return made, changed

    def took_in(job: dict) -> tuple[str, ...]:
        """The run's files changed under it: a transcript or a stage's output came in. Note which stage outputs it has
        now and drop what is cached of it, both before its new state shows, so no page sees the state without the
        files. Returns the outputs."""
        made = note_outputs(job)[0]
        views.forget(job["run_dir"])
        searcher.touch(job["id"])
        return made

    def name_from_summary(job: dict) -> None:
        """Take the talk's name from its summary, after the summary changed. Only its summary's name: the one you gave
        it is kept apart (in RUN_FILE) and shows first, so this never replaces it."""
        title = summary_title(job["json"]) if job.get("json") and output_exists(job, "summary") else None
        with lock:
            job["auto_title"] = title
        searcher.rename(job["id"])

    def drop_meta(run_dir, *keys):
        update_meta(run_dir, lambda meta: [meta.pop(key, None) for key in keys])

    def finish_pending(run_dir, keys, progressed=False):
        def change(meta):
            meta["pending"] = [key for key in meta.get("pending", []) if key not in keys]
            if not meta["pending"]:
                del meta["pending"]
            if progressed:
                meta.pop("resumes", None)

        update_meta(run_dir, change)

    def sweep():
        """Delete runs and recordings older than the retention period (hosted sites)."""
        if not settings.retention_hours or time.time() - last_sweep[0] < SWEEP_EVERY_SECONDS:
            return
        last_sweep[0] = time.time()
        cutoff = datetime.now() - timedelta(hours=settings.retention_hours)
        with lock:
            busy = {job.get("run_dir") for job in jobs.values() if job.get("status") in ACTIVE}
            busy |= {job.get("source") for job in jobs.values() if job.get("status") in ACTIVE}
        removed = []
        for run_dir in storage.TRANSCRIPTS.glob("*/*/*/"):
            try:
                started_at = datetime.strptime(run_dir.name, "%Y%m%d-%H%M%S-%f")
            except ValueError:
                continue
            if started_at < cutoff and str(run_dir) not in busy:
                shutil.rmtree(run_dir, ignore_errors=True)
                with lock:
                    for job_id in [k for k, job in jobs.items() if job.get("run_dir") == str(run_dir)]:
                        removed.append(jobs.pop(job_id))
        for job in removed:
            views.forget(job["run_dir"])
            searcher.forget(job["id"])
        if removed:
            save_catalogue()
        for folder in [*storage.TRANSCRIPTS.glob("*/*/"), *storage.TRANSCRIPTS.glob("*/")]:
            try:
                folder.rmdir()  # only succeeds once empty
            except OSError:
                pass
        for path in [*storage.RECORDINGS.glob("*"), *uploads_dir.glob("*")]:
            try:
                old = datetime.fromtimestamp(path.stat().st_mtime) < cutoff
            except OSError:
                continue
            if old and path.is_file() and str(path) not in busy and not path.name.startswith("."):
                path.unlink(missing_ok=True)

    def started_worker(job_id, step, worker):
        """Keep a running process so it can be stopped, and stop it at once if that was asked before it started."""
        with lock:
            workers[(job_id, step)] = worker
            asked = (job_id, step) in stopping
        if asked:
            stop_worker(worker)

    def finished_worker(job_id, step, token):
        """Forget the step's process and task; return why it was stopped, if it was."""
        with lock:
            workers.pop((job_id, step), None)
            if tasks.get((job_id, step)) == token:
                del tasks[(job_id, step)]
            return stopping.pop((job_id, step), None)

    def queue_transcription(job_id, message):
        token = uuid4().hex
        with lock:
            sequence[0] += 1
            tasks[(job_id, "transcribe")] = token
            jobs[job_id].update(status="queued", message=message, seq=sequence[0])
        gpu.submit(process, job_id, token)

    def process(job_id, token):
        with lock:
            job = jobs.get(job_id)
            if not job or tasks.get((job_id, "transcribe")) != token:
                return  # deleted, paused or queued again since
            job.update(status="loading", message="Loading the model. The first run may download model weights.")
            run_dir = Path(job["run_dir"])
            command = [sys.executable, str(ROOT / "scripts" / "transcribe.py"), job["source"], job["model"]]
            command += ["--mode", job.get("mode") or "mixed", "--output-dir", str(run_dir)]
            command += ["--dictionary", str(dictionary_path()), "--progress-json"]

        def change(**fields):
            with lock:
                job.update(fields)

        def on_event(event):
            if event["stage"] == "transcribing":
                processed, total = event["processed"], job.get("duration")
                percent = min(99, round(processed / total * 100)) if total else None
                change(
                    status="transcribing",
                    processed=processed,
                    progress=percent,
                    message=f"Transcribing · {event['chunks']} chunks completed",
                )
            elif event["stage"] == "complete":
                change(txt=event["txt"], json=event["json"])
            elif event["stage"] == "loading":
                change(device=event["device"])

        on_start = lambda worker: started_worker(job_id, "transcribe", worker)  # noqa: E731
        if gpu_jobs is None:
            code, logs = run_worker(command, on_event, on_start)
        elif gpu_limit_reached():
            code, logs = 1, [f"Error: {GPU_LIMIT_MESSAGE.format(hours=settings.gpu_hours_per_month)}"]
        else:
            try:
                code, logs = run_remote(
                    gpu_jobs,
                    command,
                    on_event,
                    on_start,
                    run_dir=run_dir,
                    step="transcribe",
                    timeout_seconds=gpu_timeout(job.get("duration")),
                    saved=read_meta(run_dir).get("execution"),
                    on_execution=lambda name: update_meta(
                        run_dir, lambda meta: meta.update(execution=name) if name else meta.pop("execution", None)
                    ),
                    on_finish=lambda name, status: log_gpu(name, status, run_dir),
                )
            except (OSError, ValueError, RuntimeError) as error:
                code, logs = 1, [f"Error: Could not start the transcription job ({error})."]
        reason = finished_worker(job_id, "transcribe", token)
        if reason == "delete":
            shutil.rmtree(run_dir, ignore_errors=True)  # in case it wrote after the folder was removed
            return
        if not code and job.get("txt"):
            # The transcript is in, written here or by the GPU job, which has ended (run_remote returned).
            took_in(job)
            drop_meta(run_dir, "resumes", "failed", "paused")
            # Gujlish follows straight away; English and the summary wait until you have fixed the words.
            queue_stages(job_id, ("gujlish",))
            change(status="complete", progress=100, message="Transcript ready")
        elif reason == "pause":
            change(status="paused", message=f"Paused at {stopped_at(job)}. Resume continues from there.")
            update_meta(run_dir, lambda meta: meta.update(paused=True))
        else:
            message = last_error(logs, "Transcription stopped. You can retry to resume completed chunks.")
            change(status="error", message=message)
            update_meta(run_dir, lambda meta: meta.update(failed=message))
        save_catalogue()

    def stage_command(transcript_json, key, engine="auto", recording=None):
        command = [sys.executable, str(ROOT / STAGES[key].script), transcript_json, "--progress-json"]
        if key == "gujlish":
            command += ["--dictionary", str(dictionary_path()), "--engine", engine]
        if key == "translation":
            command.append("--no-captions")  # the captions run as their own step, with their own progress
        if key == "captions" and recording:
            command += ["--audio", recording]  # to time the captions to the speech
        return command

    def set_stage(job, key, **fields):
        with lock:
            job.setdefault(key, {}).update(fields)

    def queue_stages(job_id, keys, engine="auto"):
        token = uuid4().hex
        with lock:
            job = jobs[job_id]
            for key in keys:
                tasks[(job_id, key)] = token
                job[key] = {"status": "queued", "progress": None, "message": "Waiting to start…"}
            steps = [(key, stage_command(job["json"], key, engine, job.get("source"))) for key in keys]

        def change(meta):
            meta["pending"] = sorted({*meta.get("pending", []), *keys})
            meta["engine"] = engine

        update_meta(job["run_dir"], change)
        text.submit(run_stages, job_id, steps, token)

    def run_stage(job_id, key, command, token) -> bool:
        """Run a stage's worker script as a subprocess, mirroring its progress events into the job. True when done."""
        with lock:
            job = jobs.get(job_id)
            if not job or tasks.get((job_id, key)) != token:
                return False
        verb = STAGES[key].verb
        set_stage(job, key, status="running", progress=None, message=f"{verb}…")

        def on_event(event):
            if event["stage"] in PROGRESS_EVENTS and event["total"]:
                percent = min(99, round(event["done"] / event["total"] * 100))
                set_stage(job, key, progress=percent, message=f"{verb} · {event['done']} of {event['total']}")

        try:
            code, logs = run_worker(command, on_event, lambda worker: started_worker(job_id, key, worker))
        except OSError as error:
            code, logs = 1, [f"Error: {error}"]
        reason = finished_worker(job_id, key, token)
        if reason == "delete":
            shutil.rmtree(job["run_dir"], ignore_errors=True)
            return False
        made = took_in(job)  # however it ended, it may have written files
        if key == "summary":
            # Named from the summary on disk, however the stage ended: the summary is written before the YouTube
            # details, so it may be new even when those failed or were stopped. Before it shows, so no page sees it
            # without its name.
            name_from_summary(job)
        if reason == "stop":
            set_stage(job, key, status="idle", progress=None, message=STOPPED)
        elif not code and key in made:
            set_stage(job, key, status="complete", progress=100, message="Ready")
        else:
            set_stage(job, key, status="error", message=last_error(logs, f"{verb} stopped. Start it again to resume."))
        done = job[key]["status"] == "complete"
        finish_pending(job["run_dir"], {key}, progressed=done)
        save_catalogue()
        return done

    def run_stages(job_id, steps, token):
        """Run stages in order (translation, then the summary), stopping at the first that does not finish."""
        for index, (key, command) in enumerate(steps):
            if run_stage(job_id, key, command, token):
                continue
            with lock:
                job = jobs.get(job_id)
                later = [key for key, _ in steps[index + 1 :] if tasks.get((job_id, key)) == token]
                for key in later:
                    del tasks[(job_id, key)]
            if job and later:
                for key in later:
                    set_stage(job, key, status="idle", progress=None, message="")
                finish_pending(job["run_dir"], set(later))
            return

    def merge_recordings():
        """Keep one stored copy of each recording. Runs that used a duplicate point at the kept copy instead. Run
        folders left by uploads whose transcription never started, and recordings no run uses, are removed once they
        are LEFTOVER_SECONDS old. Copies an unfinished run still reads from are left alone. A recording is removed only
        while no run uses it, runs made meanwhile included, as this can run alongside requests. (Leftover run folders
        go while the catalogue is built or checked, just before this.)

        Nothing is merged or removed unless every run folder is in the catalogue: a run missing from it (its folder
        could not be read at the start) would lose its recording. It is tried again at the next start."""
        cutoff = time.time() - LEFTOVER_SECONDS

        def unused(path: Path) -> bool:
            with lock:
                return all(job.get("source") != str(path) for job in jobs.values())

        if not every_run_known():
            return
        users = {}
        for job in all_runs():
            users.setdefault(job.get("source"), []).append(job)
        copies = {}
        for name, value in fingerprints().items():
            copies.setdefault(value, []).append(storage.RECORDINGS / name)
        for paths in copies.values():
            if len(paths) < 2 or any(job["status"] != "complete" for p in paths for job in users.get(str(p), [])):
                continue
            keep = max(paths, key=lambda path: (len(users.get(str(path), [])), -path.stat().st_mtime))
            for path in paths:
                if path == keep:
                    continue
                for job in users.pop(str(path), []):
                    update_meta(job["run_dir"], lambda meta: meta.update(recording=keep.name))
                    with lock:
                        job["source"] = str(keep)
                    views.forget(job["run_dir"])
                    users.setdefault(str(keep), []).append(job)
                if unused(path):
                    path.unlink(missing_ok=True)
        for name in fingerprints():
            path = storage.RECORDINGS / name
            try:
                old = path.stat().st_mtime < cutoff
            except FileNotFoundError:
                continue  # removed meanwhile
            if old and str(path) not in users and unused(path):
                path.unlink(missing_ok=True)

    def recover():
        """Once per server start, before any request is answered, load the runs (the catalogue, when trusted) and
        resume the work a restart cut short. Requests that arrive meanwhile wait, so none sees the runs half loaded."""
        if recovered[0]:
            return
        with recover_lock:
            if recovered[0]:
                return
            try:
                resume_work()
            finally:
                recovered[0] = True
                searcher.start()  # in the background: restores the saved index and catches up with the talks

    def resume_work():
        """Resume the work a restart cut short. Hosted sites also merge duplicate recordings.

        A trusted server looks only at the runs its catalogue says need it. When the catalogue came from its file, a
        background thread then checks it against the folders, picks up what it finds, and only then merges."""
        checked = trusted and open_catalogue()
        if not checked:
            tidy_recordings()
        if trusted:
            with lock:
                waiting = sorted(Path(job["run_dir"]) for job in jobs.values() if job.get("unsettled"))
        else:
            waiting = run_dirs()
        for run_dir in waiting:
            resume_run(run_dir)
        if trusted:
            save_catalogue()
        if checked:
            thread = Thread(target=check_catalogue, args=(transcripts,), daemon=True, name="asr-reconcile")
            app.extensions["asr_reconcile"] = thread
            thread.start()

    def resume_run(run_dir: Path) -> None:
        """Pick up what a restart cut short in one run, from its RUN_FILE. After MAX_RESUMES restarts in a row it waits
        for you instead, so a recording that brings the server down cannot keep doing so."""
        meta = read_meta(run_dir)
        if meta.get("execution") and (meta.get("paused") or meta.get("failed")):
            # Paused or failed, but its GPU job may still be running (this website restarted mid-stop): end it.
            if gpu_job_running(meta["execution"]):
                Thread(target=RemoteRun(gpu_jobs, meta["execution"]).stop, daemon=True).start()
            drop_meta(run_dir, "execution")
            return
        unfinished = meta.get("source") and not meta.get("paused") and not meta.get("failed")
        if not meta.get("pending") and not unfinished:
            return
        with lock:
            job = jobs.get(run_id(run_dir)) or restore(run_id(run_dir))
        if not job:
            return
        if meta.get("execution") and job["status"] == "complete":
            # Its GPU job finished while this website was asleep: carry on as if it had just finished here.
            took_in(job)
            drop_meta(run_dir, "execution", "resumes", "failed", "paused")
            if not output_exists(job, "gujlish"):
                queue_stages(job["id"], ("gujlish",))
            return
        if job["status"] == "error" and meta.get("execution") and gpu_job_running(meta["execution"]):
            # Still transcribing on the GPU job: follow it again. Not a restart of the work, so not counted.
            queue_transcription(job["id"], "Transcribing")
            return
        transcribe = job["status"] == "error"
        pending = [key for key in meta.get("pending", []) if key in STAGES] if job["status"] == "complete" else []
        if not transcribe and not pending:
            return
        if meta.get("resumes", 0) >= MAX_RESUMES:
            update_meta(run_dir, lambda meta: meta.pop("pending", None))
            with lock:
                if transcribe:
                    job["message"] = f"Stopped after {MAX_RESUMES} server restarts in a row. Resume to try again."
                for key in pending:
                    job[key] = {
                        "status": "error",
                        "message": "Stopped after several server restarts. Start it again.",
                    }
            return
        update_meta(run_dir, lambda meta: meta.update(resumes=meta.get("resumes", 0) + 1))
        if transcribe:
            queue_transcription(job["id"], "Continuing after a server restart")
        else:
            queue_stages(job["id"], pending, meta.get("engine", "auto"))

    def busy(job, *keys):
        return any((job.get(key) or {}).get("status") in {"queued", "running"} for key in keys)

    def transliterator(overrides):
        if not xlit and xlit_installed():
            xlit.append(XlitEngine())
        return Transliterator(xlit[0] if xlit else KnownWordsOnly(), overrides)

    def stage_view(job, key):
        """Stage state for the browser; files already on disk count as complete (e.g. after a server restart)."""
        state = dict(job.get(key) or {})
        if state.get("status") in {"queued", "running", "error"}:
            return state
        if job.get("json") and output_exists(job, key):
            return {"status": "complete", "progress": 100, "message": "Ready"}
        return state or {"status": "idle"}

    def output_exists(job, key) -> bool:
        """Whether the stage's output is on disk: known from the catalogue when trusted, else checked."""
        return key in job.get("done", ()) if trusted else stage_output(job["json"], key).exists()

    @app.before_request
    def guard_requests():
        recover()
        # Reject cross-site upload/POST requests from other pages.
        if request.method in {"POST", "PUT"} and request.headers.get("Origin"):
            if urlparse(request.headers["Origin"]).netloc != request.host:
                abort(403)
        if settings.password and not signed_in() and request.endpoint not in {"login", "static", "health"}:
            if request.path.startswith("/api/"):
                return jsonify(error="Sign in again: your session has ended."), 401
            return redirect(url_for("login"))

    # A signed-in browser keeps a mark of the password it signed in with, keyed with the secret key (so the cookie
    # reveals nothing about the password). Changing the password signs every browser out.
    password_mark = hmac.new(app.secret_key.encode(), settings.password.encode(), "sha256").hexdigest()

    def signed_in() -> bool:
        mark = session.get("authed")
        return isinstance(mark, str) and hmac.compare_digest(mark, password_mark)

    @app.after_request
    def secure_headers(response):
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        return response

    @app.get("/health")  # Cloud Run reserves /healthz
    def health():
        return "ok"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not settings.password:
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(), settings.password.encode()):
                session.permanent = True
                session["authed"] = password_mark
                return redirect(url_for("index"))
            time.sleep(1)  # slows down password guessing
            error = "That password is not right."
        return render_template("login.html", error=error), 401 if error else 200

    @app.post("/logout")
    def logout():
        """Sign this browser out: the whole session goes, so the site asks for the password again."""
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return render_template(
            "index.html",
            hosted=settings.hosted,
            has_password=bool(settings.password),
            models=settings.models,
            max_upload_mb=round(settings.max_upload_mb),
            upload_limit=f"{settings.max_upload_mb / 1024:g} GB"
            if settings.max_upload_mb >= 1024
            else f"{settings.max_upload_mb:g} MB",
            max_minutes=round(settings.max_minutes),
            retention_hours=round(settings.retention_hours),
            chunk_bytes=UPLOAD_CHUNK_BYTES,
        )

    def site_busy() -> bool:
        """Whether a recording is being transcribed or waits to be, or a request let through is about to add one.
        The caller holds lock."""
        return bool(claims) or any(job["status"] in ACTIVE for job in jobs.values())

    def take_the_place() -> bool:
        """On a hosted site one recording is transcribed or waits to be at a time, whichever browser started it: False
        while one is. A request let through holds the site's place until it ends (free_place), by when it has queued
        its run or given up, so two requests at once are never both let through. Work a restart picks up again is
        not asked here."""
        if not settings.hosted:
            return True
        with lock:
            if site_busy():
                return False
            g.claim = object()
            claims.add(g.claim)
        return True

    @app.teardown_request
    def free_place(_error=None):
        claim = g.pop("claim", None)
        if claim is not None:
            with lock:
                claims.discard(claim)

    def upload_path(upload_id: str) -> Path:
        if not HEX_ID.fullmatch(upload_id):
            abort(404)
        return uploads_dir / f"{browser_tag()}-{upload_id}.part"

    @app.post("/api/uploads")
    def start_upload():
        if settings.hosted:  # said before the file is sent, not after
            with lock:
                busy = site_busy()
            if busy:
                return jsonify(error=BUSY_MESSAGE), 429
        uploads_dir.mkdir(parents=True, exist_ok=True)
        drop_stale_uploads()
        upload_id = uuid4().hex
        upload_path(upload_id).write_bytes(b"")
        return jsonify(id=upload_id), 201

    def drop_stale_uploads() -> None:
        """Remove pieces of uploads that stopped coming in an hour ago. On Cloud Run this folder is in memory, and the
        retention sweep, which also clears it, is off when recordings are kept."""
        cutoff = time.time() - LEFTOVER_SECONDS
        for path in uploads_dir.glob("*.part"):
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink(missing_ok=True)
            except OSError:
                pass  # finished or removed meanwhile

    @app.put("/api/uploads/<upload_id>")
    def upload_chunk(upload_id):
        """Append one piece. `offset` must equal the bytes received so far, so a resent piece is never doubled."""
        path = upload_path(upload_id)
        if not path.exists():
            abort(404)
        size = path.stat().st_size
        offset = request.args.get("offset", type=int)
        if offset != size:
            return jsonify(error="Upload out of order.", received=size), 409
        data = request.get_data(cache=False)
        if len(data) > UPLOAD_CHUNK_BYTES or size + len(data) > settings.max_upload_mb * 1024**2:
            path.unlink(missing_ok=True)
            return jsonify(error=f"This file exceeds the {round(settings.max_upload_mb)} MB upload limit."), 413
        with path.open("ab") as handle:
            handle.write(data)
        return jsonify(received=size + len(data))

    def store_upload(upload, partial, filename) -> tuple[Path, bool, str]:
        """Save an uploaded recording, or reuse the stored copy of the same audio. Returns the file, whether it is
        new, and its fingerprint."""
        if partial is not None:
            sha = fingerprint(partial)
            same = find_recording(sha)
            if same:
                partial.unlink(missing_ok=True)
                return same, False, sha
        source = storage.RECORDINGS / filename
        base = source
        suffix = 2
        while True:
            try:
                with source.open("xb") as target:
                    if upload:
                        upload.save(target)
                    else:
                        with partial.open("rb") as handle:
                            shutil.copyfileobj(handle, target, 1024**2)
                break
            except FileExistsError:
                source = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
                suffix += 1
        if partial is not None:
            partial.unlink(missing_ok=True)
            return source, True, sha
        sha = fingerprint(source)
        same = find_recording(sha, exclude=source.name)
        if same:
            source.unlink()
            return same, False, sha
        return source, True, sha

    @app.get("/api/recordings/<sha>")
    def recording_status(sha):
        """Whether this audio is stored already (so it need not be uploaded), and the runs of it."""
        stored = find_recording(sha) if FINGERPRINT.fullmatch(sha) else None
        runs = []
        for job in all_runs():
            with lock:
                if stored and (job.get("sha256") == sha or job.get("source") == str(stored)):
                    runs.append({key: job.get(key) for key in ("id", "name", "started", "status")})
                    runs[-1]["title"] = shown_title(job)[0]
        return jsonify(stored=stored is not None, runs=runs)

    @app.post("/api/jobs")
    def create_job():
        sweep()
        model, mode = request.form.get("model", settings.models[0]), request.form.get("mode", "mixed")
        upload, upload_id = request.files.get("audio"), request.form.get("upload_id")

        def refuse(message, status=400):
            """Turn the request down, removing the file it sent in pieces: to try again, the browser sends it again."""
            if upload_id and not upload and HEX_ID.fullmatch(upload_id):
                upload_path(upload_id).unlink(missing_ok=True)
            return jsonify(error=message), status

        known = request.form.get("fingerprint", "")  # a recording already stored: nothing was uploaded
        name = upload.filename if upload else request.form.get("name", "")
        if model not in settings.models or mode not in {"mixed", "native"}:
            return refuse("Choose a valid model and output mode.")
        if not (upload or upload_id or known) or not name:
            return refuse("Choose an audio file first.")
        filename = clean_filename(name)
        if not filename:
            return refuse("Choose a file with a valid filename.")
        if gpu_limit_reached():
            return refuse(GPU_LIMIT_MESSAGE.format(hours=settings.gpu_hours_per_month), 429)
        if not take_the_place():
            return refuse(BUSY_MESSAGE, 429)
        partial = upload_path(upload_id) if upload_id and not upload else None
        if partial is not None and not partial.exists():
            return jsonify(error="The upload did not finish. Please try again."), 400
        storage.RECORDINGS.mkdir(parents=True, exist_ok=True)
        if upload or partial is not None:
            source, new, sha = store_upload(upload, partial, filename)
        else:
            source, new, sha = find_recording(known) if FINGERPRINT.fullmatch(known) else None, False, known
            if source is None:
                return jsonify(error="This recording is not stored here yet.", upload=True), 409
        try:
            duration = probe_audio(source)
            if settings.max_minutes and duration and duration > settings.max_minutes * 60:
                raise ValueError(f"Recordings up to {round(settings.max_minutes)} minutes are accepted here.")
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            if new:
                source.unlink(missing_ok=True)
            return jsonify(error=str(error)), 400
        run_dir = new_run(source, model, mode)
        size = source.stat().st_size
        meta = {"source": source.name, "name": filename, "model": model, "mode": mode, "duration": duration}
        write_json(run_dir / RUN_FILE, {**meta, "size": size, "sha256": sha})
        job_id = run_id(run_dir)
        with lock:
            jobs[job_id] = {
                "id": job_id,
                "name": filename,
                "sha256": sha,
                "model": model,
                "mode": mode,
                "status": "queued",
                "progress": 0,
                "duration": duration,
                "processed": 0,
                "message": "Queued for transcription",
                "source": str(source),
                "run_dir": str(run_dir),
                "size": size,
                "started": started(run_dir),
                "title": None,
                "auto_title": None,
            }
            if trusted:
                jobs[job_id].update(done=(), unsettled=True)
        save_catalogue()
        queue_transcription(job_id, "Queued for transcription")
        return jsonify(id=job_id), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        with lock:
            job = dict(find_job(job_id))
            if job["status"] == "queued" and job.get("seq"):
                job["ahead"] = sum(
                    1 for other in jobs.values() if other["status"] in ACTIVE and other.get("seq", 0) < job["seq"]
                )
        # The catalogue's own fields stay on the server, so the page sees the same fields as before it existed.
        hidden = {"source", "run_dir", "txt", "json", "seq", "done", "unsettled", "auto_title", *STAGES}
        result = {k: v for k, v in job.items() if k not in hidden}
        result["run"] = Path(job["run_dir"]).name
        result["title"], result["title_from"] = shown_title(job)
        for key in STAGES:
            result[key] = stage_view(job, key)
        if result["status"] == "complete":
            files = finished_files(job, result)
            result["text"] = files["text"]
            if job.get("json"):
                result.update(transcript_view(files, result))
        return jsonify(result)

    def finished_files(job: dict, state: dict) -> dict:
        """A finished talk's files for its page (talk_files). When trusted they stay in memory until the talk changes
        (views); otherwise they are read on every request, since the command line may change them."""
        if not trusted:
            # As before: the Gujlish is always looked for, the English and summary only once their stage is complete.
            return talk_files(
                job, {"gujlish", "captions", *(key for key in STAGES if state[key]["status"] == "complete")}
            )
        files, generation = views.get(job["run_dir"])
        if files is None:
            # The outputs as of now, after the generation: a stage that ends meanwhile then discards this load.
            with lock:
                made = (jobs.get(job["id"]) or job).get("done", ())
            files = talk_files(job, made)
            if files["cacheable"]:
                views.put(job["run_dir"], generation, files)
        return files

    def gemini_ready() -> bool:
        # Hosted sites use Gemini only when the owner allows it, because it runs on the owner's key.
        return gemini_available() and (not settings.hosted or settings.allow_gemini)

    @app.get("/api/capabilities")
    def capabilities():
        # Whether translation/summaries can run. The key itself stays in the server's environment.
        return jsonify(hosted=settings.hosted, gemini=gemini_ready())

    @app.get("/api/usage")
    def gpu_usage():
        """This month's GPU job time and its estimated cost, for the page. Empty when there is no GPU job."""
        if gpu_jobs is None:
            return jsonify(gpu=False)
        summary = usage.summary(settings.gpu_hours_per_month, settings.gpu_pounds_per_hour)
        return jsonify(gpu=True, **summary)

    def start_stage(job_id, *keys, engine="auto"):
        with lock:
            find_job(job_id)
        if (set(keys) - {"gujlish"} or engine == "gemini") and not gemini_ready():
            if settings.hosted:
                return jsonify(error="Translation and summaries are not enabled on this site."), 400
            return jsonify(error="Set GEMINI_API_KEY in the terminal before starting the app (see the README)."), 400
        with lock:
            job = find_job(job_id)
            if job.get("status") != "complete" or not job.get("json"):
                return jsonify(error="Finish the transcript first."), 409
            if keys in {("summary",), ("captions",)} and stage_view(job, "translation")["status"] != "complete":
                return jsonify(error="Translate the transcript first."), 409
            if busy(job, *keys):
                return jsonify(error="This step is already running."), 409
        queue_stages(job_id, keys, engine)
        return jsonify(id=job_id), 202

    @app.post("/api/jobs/<job_id>/translate")
    def translate_job(job_id):
        return start_stage(job_id, "translation", "captions")

    @app.post("/api/jobs/<job_id>/captions")
    def captions_job(job_id):
        """Re-break and re-time the English subtitles, e.g. for a translation made before this step existed."""
        return start_stage(job_id, "captions")

    @app.post("/api/jobs/<job_id>/summarize")
    def summarize_job(job_id):
        return start_stage(job_id, "summary")

    @app.post("/api/jobs/<job_id>/english")
    def english_job(job_id):
        """Translate, then write the summary and YouTube details: the step you take once the words are right."""
        return start_stage(job_id, "translation", "captions", "summary")

    @app.post("/api/jobs/<job_id>/gujlish")
    def gujlish_job(job_id):
        engine = (request.get_json(silent=True) or {}).get("engine", "auto")
        if engine not in {"auto", "gemini"}:
            return jsonify(error="Choose a valid Gujlish engine."), 400
        return start_stage(job_id, "gujlish", engine=engine)

    @app.post("/api/jobs/<job_id>/stop")
    def stop_stage(job_id):
        """Stop the Gujlish, or the English and summary. Finished parts are kept, so starting again continues."""
        keys = {"gujlish": ("gujlish",), "english": ("translation", "captions", "summary")}.get(
            (request.get_json(silent=True) or {}).get("stage")
        )
        if not keys:
            return jsonify(error="Choose a step to stop."), 400
        running = []
        with lock:
            job = find_job(job_id)
            for key in keys:
                state = job.get(key) or {}
                if state.get("status") == "running":
                    stopping[(job_id, key)] = "stop"
                    state["message"] = "Stopping…"
                    if (job_id, key) in workers:
                        running.append(workers[(job_id, key)])
                elif state.get("status") == "queued":
                    tasks.pop((job_id, key), None)
                    job[key] = {"status": "idle", "progress": None, "message": STOPPED}
        for worker in running:
            stop_worker(worker)
        finish_pending(job["run_dir"], set(keys))
        return jsonify(stopped=True)

    @app.post("/api/jobs/<job_id>/pause")
    def pause_job(job_id):
        """Pause a transcription. Finished chunks are kept on disk; resuming continues after them."""
        worker = None
        with lock:
            job = find_job(job_id)
            if job["status"] not in ACTIVE:
                return jsonify(error="Only a recording being transcribed can be paused."), 409
            if job["status"] == "queued":
                tasks.pop((job_id, "transcribe"), None)
                job.update(status="paused", message=f"Paused at {stopped_at(job)}. Resume continues from there.")
            else:
                stopping[(job_id, "transcribe")] = "pause"
                job["message"] = "Pausing…"
                worker = workers.get((job_id, "transcribe"))  # not started yet: it is stopped as soon as it starts
        if isinstance(worker, RemoteRun):
            # Saved at once: if this website restarts before the GPU job has stopped, the pause still holds and
            # recover() cancels the job instead of following it again.
            update_meta(job["run_dir"], lambda meta: meta.update(paused=True))
        if worker:
            stop_worker(worker)
        elif job["status"] == "paused":
            update_meta(job["run_dir"], lambda meta: meta.update(paused=True))
        return jsonify(paused=True)

    @app.post("/api/jobs/<job_id>/resume")
    @app.post("/api/jobs/<job_id>/retry")
    def resume_job(job_id):
        with lock:
            job = find_job(job_id)
            if job["status"] not in {"error", "paused"}:
                return jsonify(error="Only a paused or stopped recording can be resumed."), 409
        if gpu_limit_reached():
            return jsonify(error=GPU_LIMIT_MESSAGE.format(hours=settings.gpu_hours_per_month)), 429
        if not take_the_place():
            return jsonify(error=BUSY_MESSAGE), 429
        drop_meta(job["run_dir"], "paused", "failed", "resumes")
        queue_transcription(job_id, "Queued to continue from where it stopped")
        return jsonify(id=job_id), 202

    def remove_run(job: dict, running: list) -> None:
        """Wait for the run's processes to stop, then delete its folder and, if no other run uses it, its recording."""
        for worker in running:
            try:
                worker.wait(timeout=STOP_GRACE_SECONDS + 5)
            except subprocess.TimeoutExpired:
                pass
        run_dir = Path(job["run_dir"])
        shutil.rmtree(run_dir, ignore_errors=True)
        for folder in (run_dir.parent, run_dir.parent.parent):
            try:
                folder.rmdir()  # only succeeds once empty
            except OSError:
                pass
        recording = Path(job.get("source") or "")
        if recording.is_file() and all(other.get("source") != str(recording) for other in all_runs()):
            # Hosted, kept while a run could not be read: it may use it (the next start's merge removes it if not).
            if not settings.hosted or every_run_known():
                recording.unlink(missing_ok=True)

    @app.post("/api/jobs/<job_id>/delete")
    def delete_job(job_id):
        """Cancel or delete a run: stop whatever it is doing, then remove its files in the background."""
        running = []
        # meta_lock too, so no RUN_FILE write (a rename, say) is under way as the folder goes: one that ended after the
        # removal began could leave the file behind, and a restart would find the talk again.
        with meta_lock, lock:
            job = find_job(job_id)
            del jobs[job_id]
            deleted.add(job["run_dir"])
            for step in ("transcribe", *STAGES):
                tasks.pop((job_id, step), None)
                started_step = job["status"] in {"loading", "transcribing"} if step == "transcribe" else busy(job, step)
                if started_step or (job_id, step) in workers:
                    stopping[(job_id, step)] = "delete"
                if (job_id, step) in workers:
                    running.append(workers[(job_id, step)])
        for worker in running:
            stop_worker(worker)
        views.forget(job["run_dir"])
        searcher.forget(job_id)
        save_catalogue()
        Thread(target=remove_run, args=(job, running), daemon=True).start()
        return jsonify(deleted=True)

    @app.post("/api/jobs/<job_id>/rename")
    def rename_job(job_id):
        """Give the talk a name every device shows, kept in its RUN_FILE. An empty name gives it back its automatic one:
        its summary's title, else its file name."""
        body = request.get_json(silent=True)
        title = body.get("title") if isinstance(body, dict) else None
        if not isinstance(title, str):
            return jsonify(error="Type a name for this talk."), 400
        title = clean_title(title)
        if len(title) > TITLE_LIMIT:
            return jsonify(error=f"Keep the name to {TITLE_LIMIT} characters."), 400
        with lock:
            job = find_job(job_id)

        def change(meta):
            if title:
                meta["title"] = title
            else:
                meta.pop("title", None)

        if not update_meta(job["run_dir"], change):
            with lock:
                if jobs.get(job_id) is not job:  # deleted meanwhile
                    abort(404)
            return jsonify(error="The name could not be saved just now. Try again."), 503
        with lock:
            shown, source = shown_title(job)
        return jsonify(title=shown, title_from=source)

    @app.post("/api/jobs/<job_id>/edit")
    def edit_job(job_id):
        """Replace words in the transcript (and the same words elsewhere in it, if asked), keep the Gujlish in step,
        and remember the fix for future recordings unless told not to."""
        with lock:
            job = dict(find_job(job_id))
        body = request.get_json(silent=True) or {}
        try:
            number, start, end = int(body["segment"]), int(body["start"]), int(body["end"])
            heard = str(body["heard"])
            correct = " ".join(str(body["correct"]).split())
            correct_gujlish = " ".join(str(body.get("correct_gujlish") or "").split())
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Choose the words to replace."), 400
        if not correct:
            return jsonify(error="Type the correct word."), 400
        if job.get("status") != "complete" or not job.get("json"):
            return jsonify(error="Finish the transcript first."), 409
        if busy(job, "gujlish"):
            return jsonify(error="Gujlish is still being written. Try again in a moment."), 409
        with edit_lock:
            transcript_json = Path(job["json"])
            data = json.loads(transcript_json.read_text(encoding="utf-8"))
            segments = data["segments"]
            tokens = segments[number].get("text", "").split() if 0 <= number < len(segments) else []
            if not 0 <= start < end <= len(tokens) or " ".join(tokens[start:end]) != heard:
                return jsonify(error="This part of the transcript has changed. Reload the page and try again."), 409
            context, segment = segments[number]["text"], segments[number]
            paths = stage_paths(transcript_json)
            gujlish, engine = load_gujlish(paths["gujlish_json"], segments)
            heard_gujlish = " ".join(gujlish[number][start:end]) if gujlish else ""
            places = [(number, start)]
            if body.get("everywhere"):
                size = end - start
                places += [(n, s) for n, s in matches(segments, heard) if n != number or s + size <= start or s >= end]
            dictionary = load_dictionary()
            latin = replace_words(
                segments, gujlish, places, end - start, correct, correct_gujlish, transliterator(dictionary["gujlish"])
            )
            try:
                save_transcript(transcript_json, data)
                if gujlish is not None:
                    write_gujlish(transcript_json, data, gujlish, engine)
            finally:
                views.forget(job["run_dir"])  # also after a write that failed partway
                searcher.touch(job_id)
            clip = None
            if body.get("remember", True):
                example = evidence(data, transcript_json, segment, context, start)
                always = bool(body.get("always"))
                rule = remember(dictionary, heard, correct, heard_gujlish, " ".join(latin), example, always)
                clip = keep_clip(job, rule, example)
                save_dictionary(dictionary)
        if clip:
            Thread(target=save_clip, args=clip, daemon=True).start()
        return jsonify(replaced=len(places), remembered=bool(body.get("remember", True)))

    def evidence(data: dict, transcript_json: Path, segment: dict, context: str, start: int) -> dict:
        """Where a fix (or a rejected suggestion) happened: the recording, the sentence as heard and roughly when the
        word was said (estimated from its place in the sentence: the recognizer gives no word times)."""
        tokens, seconds = context.split(), segment["end"] - segment["start"]
        return {
            "recording": data.get("source"),
            "run": transcript_json.parent.name,
            "start": segment["start"],
            "end": segment["end"],
            "at": round(segment["start"] + seconds * len(" ".join(tokens[:start])) / max(1, len(context)), 2),
            "context": context,
        }

    def keep_clip(job: dict, rule: dict, example: dict):
        """Name the audio clip for this example and return what save_clip needs to cut it, or None."""
        recording = Path(job.get("source") or "")
        if not rule or not recording.is_file():
            return None
        example["clip"] = f"{rule['id']}-{uuid4().hex[:6]}.ogg"
        return recording, example["start"], example["end"], clips_dir() / example["clip"]

    @app.post("/api/jobs/<job_id>/dismiss")
    def dismiss_suggestion(job_id):
        """Reject a dictionary suggestion in one place: the words stay as heard, and the dictionary keeps this place
        (sentence and audio clip) as a time the fix was wrong."""
        with lock:
            job = dict(find_job(job_id))
        body = request.get_json(silent=True) or {}
        try:
            number, start, end, rule_id = int(body["segment"]), int(body["start"]), int(body["end"]), str(body["rule"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Choose the suggestion to dismiss."), 400
        if job.get("status") != "complete" or not job.get("json"):
            return jsonify(error="Finish the transcript first."), 409
        clip = None
        with edit_lock:
            transcript_json = Path(job["json"])
            data = json.loads(transcript_json.read_text(encoding="utf-8"))
            segments = data["segments"]
            segment = segments[number] if 0 <= number < len(segments) else {}
            suggestions = segment.get("suggestions", [])
            found = next(
                (s for s in suggestions if (s["start"], s["end"], s.get("rule")) == (start, end, rule_id)), None
            )
            if found is None:
                return jsonify(error="This part of the transcript has changed. Reload the page and try again."), 409
            suggestions.remove(found)
            if not suggestions:
                segment.pop("suggestions", None)
            try:
                save_transcript(transcript_json, data)
            finally:
                views.forget(job["run_dir"])  # also after a write that failed partway
                searcher.touch(job_id)
            dictionary = load_dictionary()
            example = evidence(data, transcript_json, segment, segment.get("text", ""), start)
            rule = reject(dictionary, rule_id, example)
            if rule:
                clip = keep_clip(job, rule, example)
                save_dictionary(dictionary)
        if clip:
            Thread(target=save_clip, args=clip, daemon=True).start()
        return jsonify(dismissed=True)

    def line_word_times(transcript_json: Path, segment: dict, recording: Path) -> list[dict] | None:
        """When each word of a transcript line is said: from the caption step's alignment, else by aligning the line
        now (about half a second on the CPU, plus loading the model the first time), else None."""
        saved = align.load_word_times(transcript_json, [segment])[0]
        if saved:
            return saved
        key = json.dumps([str(transcript_json), segment["start"], segment["end"], segment["text"]], ensure_ascii=False)
        with align_lock:
            if key not in aligned:
                if "model" not in aligner:
                    try:
                        aligner["model"] = align.Aligner()
                    except (ImportError, OSError, RuntimeError, ValueError):
                        aligner["model"] = None
                times = None
                if aligner["model"] is not None:
                    start, end = float(segment["start"]), float(segment["end"])
                    try:
                        samples = align.read_audio(recording, start, end)
                        times = aligner["model"].align(samples, start, end, segment["text"])
                    except (OSError, RuntimeError, ValueError):
                        times = None
                if len(aligned) >= 1000:
                    aligned.clear()
                aligned[key] = times
            return aligned[key]

    @app.get("/api/jobs/<job_id>/word-audio")
    def word_audio(job_id):
        """The moment words [start, end) of a line are said, as a short WAV, so a fix or a dictionary suggestion can
        be judged by ear. When the words can't be timed, their place in the line is estimated and more audio is
        kept around them (the X-Word-Timing header says which)."""
        with lock:
            job = dict(find_job(job_id))
        try:
            number, start, end = (int(request.args[name]) for name in ("segment", "start", "end"))
        except (KeyError, ValueError):
            return jsonify(error="Choose the words to play."), 400
        recording = Path(job.get("source") or "")
        if job.get("status") != "complete" or not job.get("json") or not recording.is_file():
            return jsonify(error="The recording isn't available."), 404
        transcript_json = Path(job["json"])
        segments = json.loads(transcript_json.read_text(encoding="utf-8")).get("segments", [])
        segment = segments[number] if 0 <= number < len(segments) else {}
        if not 0 <= start < end <= len(segment.get("text", "").split()):
            return jsonify(error="This part of the transcript has changed. Reload the page and try again."), 409
        times = line_word_times(transcript_json, segment, recording)
        exact = times is not None
        if not exact:
            times = align.estimate_times(segment["text"], float(segment["start"]), float(segment["end"]))
        pad = WORD_PAD if exact else ROUGH_PAD
        first, last = max(0.0, round(times[start]["start"] - pad, 3)), round(times[end - 1]["end"] + pad, 3)
        audio = clip_audio(recording, first, last)
        if audio is None:
            return jsonify(error="Could not cut this part of the recording."), 500
        response = send_file(BytesIO(audio), mimetype="audio/wav", download_name="words.wav")
        response.headers["X-Word-Timing"] = "aligned" if exact else "estimated"
        response.headers["Cache-Control"] = "private, max-age=300"
        return response

    @app.get("/api/jobs/<job_id>/moment")
    def moment_audio(job_id):
        """A moment of a finished talk, as a short WAV, for a search result's play button: from `start` to `end`
        seconds, at most MOMENT_LIMIT seconds and never past the talk's end."""
        with lock:
            job = dict(find_job(job_id))
        try:
            start = float(request.args["start"])
            end = float(request.args.get("end", start + MOMENT_SECONDS))
        except (KeyError, ValueError):
            return jsonify(error="Choose the moment to play."), 400
        if not (math.isfinite(start) and math.isfinite(end)) or start < 0 or end <= start:
            return jsonify(error="Choose the moment to play."), 400
        recording = Path(job.get("source") or "")
        if job.get("status") != "complete" or not recording.is_file():
            return jsonify(error="The recording isn't available."), 404
        end = min(end, start + MOMENT_LIMIT, float(job.get("duration") or math.inf))
        if end <= start:
            return jsonify(error="This moment is past the end of the talk."), 400
        audio = clip_audio(recording, start, end)
        if audio is None:
            return jsonify(error="Could not cut this part of the recording."), 500
        response = send_file(BytesIO(audio), mimetype="audio/wav", download_name="moment.wav")
        response.headers["Cache-Control"] = "private, max-age=300"
        return response

    @app.get("/api/search")
    def search_endpoint():
        """Talks matching the words typed, best first, each with its best moments. `live=1` while typing (the last
        word may be unfinished); `meaning=1` also searches by meaning where Gemini is available. Reads no files."""
        query = request.args.get("q", "")
        if len(query) > SEARCH_LIMIT:
            return jsonify(error=f"Keep the search to {SEARCH_LIMIT} characters."), 400
        found = searcher.search(query, live=request.args.get("live") == "1", meaning=request.args.get("meaning") == "1")
        talks = []
        with lock:
            for talk in found["talks"]:
                job = jobs.get(talk["talk"])
                if not job or not job.get("json"):
                    continue  # deleted meanwhile
                title, title_from = shown_title(job)
                hits = [
                    {
                        **hit,
                        "time": search_service.clock(hit["start"]),
                        "link": search_service.link(job["id"], hit["start"]),
                    }
                    for hit in talk["hits"]
                ]
                talks.append({
                    "id": job["id"], "title": title, "title_from": title_from, "name": job.get("name"),
                    "duration": job.get("duration"), "started": job.get("started"), "name_hit": talk["name_hit"],
                    "more": talk["more"], "hits": hits,
                })  # fmt: skip
        return jsonify(
            state=found["state"],
            meaning=found["meaning"],
            indexing=found["indexing"],
            total=found["total"],
            talks=talks,
        )

    def answers_ready() -> bool:
        return answer_llm is not None or gemini_ready()

    @app.post("/api/ask")
    def ask_endpoint():
        """Answer a question from the talks: the passages that best match it are sent to Gemini, which answers from
        them alone and cites them; each citation links to its talk and moment."""
        body = request.get_json(silent=True)
        question = body.get("question") if isinstance(body, dict) else None
        question = " ".join(question.split()) if isinstance(question, str) else ""
        if not question:
            return jsonify(error="Type a question."), 400
        if len(question) > SEARCH_LIMIT:
            return jsonify(error=f"Keep the question to {SEARCH_LIMIT} characters."), 400
        if not answers_ready():
            if settings.hosted:
                return jsonify(error="Questions are not enabled on this site."), 400
            return jsonify(error="Set GEMINI_API_KEY in the terminal before starting the app (see the README)."), 400
        passages = searcher.retrieve(question)
        if passages is None:
            return jsonify(error="Search is still starting. Try again in a moment."), 503
        sources = []
        with lock:
            for passage in passages:
                job = jobs.get(passage["talk"])
                if job:
                    title = shown_title(job)[0]
                    sources.append({
                        **passage, "n": len(sources) + 1, "title": title, "file": job.get("name"),
                        "name": title or job.get("name") or "", "time": search_service.clock(passage["start"]),
                    })  # fmt: skip
        if not sources:
            return jsonify(answer="Nothing in the talks seems to answer this.", citations=[], model=None, consulted=0)
        try:
            result = answer_question(question, sources, make_llm=answer_llm)
        except LLMError as error:
            print(f"Could not answer a question ({error}).", flush=True)
            return jsonify(error="Gemini could not answer just now. Try again in a moment."), 502
        numbered = {source["n"]: source for source in sources}
        citations = [
            {
                "n": n, "id": source["talk"], "title": source["title"], "name": source["file"], "kind": source["kind"],
                "label": source["label"], "time": source["time"], "start": source["start"], "seg": source["seg"],
                "link": search_service.link(source["talk"], source["start"]),
            }
            for n in result["cited"]
            for source in [numbered[n]]
        ]  # fmt: skip
        return jsonify(answer=result["answer"], citations=citations, model=result["model"], consulted=len(sources))

    @app.get("/api/transliterate")
    def transliterate_text():
        """Suggestions for the word editor: Gujarati for typed Gujlish (to=gu), or Gujlish for typed Gujarati."""
        text, to = request.args.get("text", "")[:200], request.args.get("to")
        if to not in {"gu", "latin"}:
            abort(400)
        try:
            words = transliterator(load_dictionary()["gujlish"])
            options = words.gujarati_options(text) if to == "gu" else [words.latin_text(text)] if text.strip() else []
        except (ImportError, OSError, RuntimeError, ValueError):
            options = []
        return jsonify(options=[option for option in options if option.strip()])

    @app.get("/api/dictionary")
    def dictionary_entries():
        data = load_dictionary()
        fields = ("id", "heard", "heard_gujlish", "correct", "correct_gujlish", "added")
        corrections = [
            {
                **{key: rule.get(key) for key in fields},
                "mode": mode(rule),
                "examples": len(rule.get("examples", [])),
                "rejected": len(rule.get("rejected", [])),
            }
            for rule in reversed(data["corrections"])
        ]
        return jsonify(corrections=corrections, gujlish=data["gujlish"])

    @app.post("/api/dictionary/forget")
    def dictionary_forget():
        body = request.get_json(silent=True) or {}
        with edit_lock:
            data = load_dictionary()
            if not forget(data, correction=body.get("correction"), gujlish=body.get("gujlish")):
                abort(404)
            save_dictionary(data)
        return jsonify(ok=True)

    @app.post("/api/dictionary/mode")
    def dictionary_mode():
        """Switch a correction between asking before each fix ("ask") and fixing without asking ("always")."""
        body = request.get_json(silent=True) or {}
        with edit_lock:
            data = load_dictionary()
            if not set_mode(data, str(body.get("correction", "")), str(body.get("mode", ""))):
                abort(404)
            save_dictionary(data)
        return jsonify(ok=True)

    @app.get("/api/jobs/<job_id>/download/<kind>")
    def download(job_id, kind):
        with lock:
            job = dict(find_job(job_id))
        if job.get("status") != "complete":
            abort(404)
        txt = Path(job["txt"])
        docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if kind == "txt":
            return send_file(txt, as_attachment=True, download_name=txt.name, mimetype="text/plain; charset=utf-8")
        if kind == "docx":
            return send_file(
                transcript_docx(txt.read_text(encoding="utf-8"), txt.stem),
                as_attachment=True,
                download_name=txt.with_suffix(".docx").name,
                mimetype=docx_type,
            )
        if kind in {"srt", "en-srt"} and job.get("json"):
            # Built from the timed segments, so runs saved before SRT files existed can still be exported. The English
            # uses the caption step's breaks and times while they match the current translation.
            source = Path(job["json"]) if kind == "srt" else stage_paths(Path(job["json"]))["translation_json"]
            if not source.exists():
                abort(404)
            name = source.with_suffix(".srt").name
            if kind == "en-srt" and captions_current(Path(job["json"])):
                captions = stage_paths(Path(job["json"]))["captions_json"].read_text(encoding="utf-8")
                text = captions_srt(json.loads(captions).get("captions", []))
            else:
                text = to_srt(json.loads(source.read_text(encoding="utf-8")).get("segments", []))
            return send_file(
                BytesIO(text.encode("utf-8")),
                as_attachment=True,
                download_name=name,
                mimetype="application/x-subrip; charset=utf-8",
            )
        # Gujlish, English and summary files sit next to the transcript.
        paths = stage_paths(Path(job["json"])) if job.get("json") else {}
        wanted = {
            "gujlish-txt": ("gujlish_txt", False),
            "gujlish-docx": ("gujlish_txt", True),
            "gujlish-srt": ("gujlish_srt", False),
            "en-txt": ("translation_txt", False),
            "en-docx": ("translation_txt", True),
            "summary-md": ("summary_md", False),
            "summary-docx": ("summary_md", True),
        }.get(kind)
        if not wanted or not paths.get(wanted[0]) or not paths[wanted[0]].exists():
            abort(404)
        path, as_docx = paths[wanted[0]], wanted[1]
        if not as_docx:
            mimetype = {".md": "text/markdown", ".srt": "application/x-subrip"}.get(path.suffix, "text/plain")
            return send_file(path, as_attachment=True, download_name=path.name, mimetype=f"{mimetype}; charset=utf-8")
        text = path.read_text(encoding="utf-8")
        document = markdown_docx(text, path.stem) if path.suffix == ".md" else transcript_docx(text, path.stem)
        return send_file(document, as_attachment=True, download_name=path.with_suffix(".docx").name, mimetype=docx_type)

    @app.get("/api/history")
    def history():
        """Every run, newest first, with its name and what each one is doing right now."""
        sweep()
        fields = ("id", "name", "model", "mode", "status", "progress", "message", "duration", "size", "started")
        runs = []
        for job in all_runs():
            with lock:
                entry = {key: job.get(key) for key in fields}
                entry["run"] = Path(job["run_dir"]).name
                entry["title"], entry["title_from"] = shown_title(job)
                for key in STAGES:
                    state = job.get(key) or {}
                    if state.get("status") in {"queued", "running", "error"}:
                        entry[key] = {"status": state["status"], "progress": state.get("progress")}
            if entry["status"] == "complete":
                # For the talk list's "Summary ready" / "English ready": the furthest finished step. Trusted, the
                # catalogue knows; otherwise with as few file checks as possible.
                for key in ("summary", "translation"):
                    if key in entry:  # queued, running or failed: look one step back
                        continue
                    if stage_view(job, key)["status"] == "complete":
                        entry[key] = {"status": "complete", "progress": 100}
                        break
            runs.append(entry)
            if len(runs) >= 100:
                break
        return jsonify(runs)

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error=f"This file exceeds the {round(settings.max_upload_mb)} MB upload limit."), 413

    return app
