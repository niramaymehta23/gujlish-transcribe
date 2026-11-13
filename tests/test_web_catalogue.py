"""The hosted site's talk catalogue and finished-talk cache. On the hosted site every file check, listing and read is a
request to the storage bucket, so: the talk list and an opened finished talk touch no files once warm, whatever the
number of talks; every change the site makes shows on the next open; and the catalogue survives restarts, rebuilding
itself when its file is missing, damaged or from another version."""

import builtins
import hashlib
import io
import json
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from tests.helpers import (
    DeferredExecutor,
    background_check_done,
    stop_search,
    stop_search_at_end,
    wait_for_background_check,
)
from tests.test_web_gpu_jobs import EXECUTION, FakeJobs
from transcriber.english.captions import english_key
from web.server import CATALOGUE_FILE, CATALOGUE_VERSION, RUN_FILE, ViewCache, create_app
from web.settings import Settings

VISITOR, STRANGER = "a" * 32, "b" * 32
SEGMENTS = [{"start": 0, "end": 5, "text": "આજે વાત"}, {"start": 5, "end": 10, "text": "હા નીરમય"}]
GUJLISH = [["aaje", "vaat"], ["ha", "nirmay"]]
ENGLISH = {"kind": "translation", "segments": [
    {"start": 0, "end": 5, "source": "આજે વાત", "text": "Today a talk"},
    {"start": 5, "end": 10, "source": "હા નીરમય", "text": "Yes Nirmay"},
]}  # fmt: skip
ALL_STAGES = ("gujlish", "translation", "captions", "summary")


def owner_of(visitor: str) -> str:
    """The owner tag the site gave a browser before talks were shared, as left in old run folders' .owner files."""
    return hashlib.sha256(visitor.encode()).hexdigest()[:32]


class StorageTrips:
    """Counts what this thread asks of the storage folder, as the bucket would see it: listings (a folder's contents),
    reads (opening or checking a file) and writes. Other threads, such as the background reconcile, are not counted."""

    def __init__(self, root: Path):
        self.root = str(root)

    @contextmanager
    def counting(self):
        thread, trips = threading.get_ident(), []
        real = {"scandir": os.scandir, "listdir": os.listdir, "stat": os.stat, "lstat": os.lstat, "open": io.open}

        def note(kind, path):
            try:
                name = os.fsdecode(os.fspath(path))
            except TypeError:
                return  # a file descriptor
            if threading.get_ident() == thread and name.startswith(self.root):
                trips.append((kind, name))

        def listing(name):
            def call(path=".", *args, **kwargs):
                note("listing", path)
                return real[name](path, *args, **kwargs)

            return call

        def check(name):
            def call(path, *args, **kwargs):
                note("read", path)
                return real[name](path, *args, **kwargs)

            return call

        def opened(file, mode="r", *args, **kwargs):
            note("write" if set(mode) & set("wax+") else "read", file)
            return real["open"](file, mode, *args, **kwargs)

        counted = {"scandir": listing("scandir"), "listdir": listing("listdir"), "stat": check("stat"), "open": opened}
        # Python 3.10's pathlib calls these through its own references, taken when it was imported.
        accessor = getattr(pathlib, "_normal_accessor", None)
        with ExitStack() as stack:
            stack.enter_context(patch("os.lstat", check("lstat")))
            stack.enter_context(patch("builtins.open", opened))
            for name, call in counted.items():
                stack.enter_context(patch(f"{'io' if name == 'open' else 'os'}.{name}", call))
                if accessor is not None and hasattr(accessor, name):
                    stack.enter_context(patch.object(accessor, name, call))
            yield Trips(trips)


class Trips:
    def __init__(self, trips: list):
        self.trips = trips

    def count(self, kind: str, under: Path | None = None) -> int:
        return sum(1 for k, path in self.trips if k == kind and (under is None or path.startswith(str(under))))

    def summary(self) -> dict:
        return {kind: self.count(kind) for kind in ("listing", "read", "write")}

    def paths(self, kind: str, under: Path) -> list[str]:
        return [path for k, path in self.trips if k == kind and path.startswith(str(under))]


class CatalogueBase(unittest.TestCase):
    def setUp(self):
        self.use_new_storage()
        no_engine = patch("web.server.xlit_installed", return_value=False)  # Gujlish suggestions from known words
        no_engine.start()
        self.addCleanup(no_engine.stop)

    def use_new_storage(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.recordings, self.transcripts = self.root / "recordings", self.root / "transcripts"
        self.recordings.mkdir()
        self.catalogue = self.transcripts / CATALOGUE_FILE
        for name, value in (("RECORDINGS", self.recordings), ("TRANSCRIPTS", self.transcripts), ("DATA", self.root)):
            patcher = patch(f"transcriber.files.storage.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.storage = StorageTrips(self.root)

    def start(self, gpu_jobs=None, **overrides):
        """A server on the storage, as when Cloud Run starts one (again)."""
        options = dict(hosted=True, secret_key="x" * 40, retention_hours=0, allow_gemini=True, gpu_hours_per_month=0)
        options.update(overrides)
        self.settings = Settings(**options)
        self.executor = DeferredExecutor()
        self.app = create_app(executor=self.executor, settings=self.settings, gpu_jobs=gpu_jobs)
        self.app.config["SESSION_COOKIE_SECURE"] = False  # the test client uses plain http
        wait_for_background_check(self, self.app)
        stop_search_at_end(self, self.app)
        self.client = self.browser(VISITOR)
        return self.app

    def browser(self, visitor, app=None):
        client = (app or self.app).test_client()
        with client.session_transaction() as session:
            session["visitor"] = visitor
        return client

    def join_reconcile(self, app=None):
        self.assertTrue(background_check_done(app or self.app))

    def add_talk(self, number, *, stages=(), visitor=VISITOR, finished=True, meta=None, run=None, suggestion=False):
        """A run folder as the site leaves it: its RUN_FILE, the transcript, and the outputs of `stages`; also an
        .owner file, as the site left before talks were shared."""
        name = f"talk{number:03d}"
        run_dir = self.transcripts / name / "flex-mixed" / (run or f"20261101-120000-{number:06d}")
        run_dir.mkdir(parents=True)
        (self.recordings / f"{name}.wav").write_bytes(f"RIFF {number}".encode())  # each a different recording
        state = {"source": f"{name}.wav", "name": f"{name}.wav", "model": "flex", "mode": "mixed", "duration": 10.0}
        (run_dir / RUN_FILE).write_text(json.dumps({**state, "size": 4, **(meta or {})}), encoding="utf-8")
        (run_dir / ".owner").write_text(owner_of(visitor), encoding="utf-8")
        if finished:
            segments = [dict(segment) for segment in SEGMENTS]
            if suggestion:
                segments[1]["suggestions"] = [
                    {"start": 1, "end": 2, "correct": "નિરામય", "correct_gujlish": "", "rule": "r1"}
                ]
            self.write_transcript(run_dir, segments)
        for stage in stages:
            self.write_output(run_dir, stage)
        return run_dir

    def transcript(self, run_dir, suffix=".json") -> Path:
        """The run's transcript file (`suffix` ".txt" for its text), named as the transcriber names it."""
        return run_dir / f"{run_dir.parts[-3]}.flex{suffix}"

    def write_transcript(self, run_dir, segments):
        data = {"source": f"{run_dir.parts[-3]}.wav", "model": "flex", "mode": "mixed", "duration": 10.0}
        self.transcript(run_dir).write_text(json.dumps({**data, "segments": segments}), encoding="utf-8")
        text = " ".join(segment["text"] for segment in segments)
        self.transcript(run_dir, ".txt").write_text(text + "\n", encoding="utf-8")

    def write_output(self, run_dir, stage, tokens=GUJLISH):
        """What a stage's worker script writes."""
        files = {
            "gujlish": {
                ".gujlish.json": json.dumps(
                    {
                        "kind": "gujlish",
                        "engine": "indicxlit",
                        "segments": [
                            {"start": s["start"], "end": s["end"], "tokens": t} for s, t in zip(SEGMENTS, tokens)
                        ],
                    }
                ),  # fmt: skip
                ".gujlish.txt": " ".join(" ".join(t) for t in tokens) + "\n",
            },
            "translation": {".en.json": json.dumps(ENGLISH), ".en.txt": "Today a talk Yes Nirmay\n"},
            "captions": {
                ".en.captions.json": json.dumps(
                    {
                        "kind": "captions",
                        "english": english_key(ENGLISH),
                        "captions": [{"start": 0, "end": 10, "text": "Today a talk. Yes Nirmay"}],
                    }
                ),  # fmt: skip
            },
            "summary": {
                ".summary.md": "# The talk\n",
                ".youtube.json": json.dumps({"kind": "youtube", "title": "A talk", "description": "About it"}),
            },
        }[stage]
        for suffix, content in files.items():
            self.transcript(run_dir, suffix).write_text(content, encoding="utf-8")

    def history(self, client=None):
        response = (client or self.client).get("/api/history")
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def open(self, job_id, client=None):
        response = (client or self.client).get(f"/api/jobs/{job_id}")
        self.assertEqual(response.status_code, 200, response.json)
        return response.json

    def fresh_open(self, job_id):
        """The talk as a server that has cached nothing shows it: a second server on the same storage."""
        other = create_app(executor=DeferredExecutor(), settings=self.settings)
        other.config["SESSION_COOKIE_SECURE"] = False
        try:
            return self.open(job_id, self.browser(VISITOR, other))
        finally:
            self.join_reconcile(other)
            stop_search(self, other)

    def saved_runs(self) -> list[dict]:
        data = json.loads(self.catalogue.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], CATALOGUE_VERSION)
        return data["runs"]

    def run_task(self, worker_writes):
        """Run the oldest queued task; `worker_writes(command)` stands in for the worker script's output."""
        function, args = self.executor.tasks.pop(0)

        def worker(command, on_event, on_start=None):
            worker_writes(command)
            return 0, ["Done:"]

        with patch("web.server.run_worker", side_effect=worker):
            function(*args)


class TestStorageTrips(CatalogueBase):
    def test_the_counter_sees_every_kind_of_storage_request(self):
        run_dir = self.add_talk(1)
        requests = {  # exact numbers differ between Python versions (3.12's glob checks the folder first)
            "listing": {"iterdir": lambda: list(run_dir.iterdir()), "glob": lambda: list(run_dir.glob("*.json"))},
            "read": {
                "read_text": lambda: (run_dir / RUN_FILE).read_text(encoding="utf-8"),
                "exists": lambda: (run_dir / "missing").exists(),
                "is_dir": run_dir.is_dir,
                "stat": (run_dir / RUN_FILE).stat,
                "open": lambda: builtins.open(run_dir / RUN_FILE, encoding="utf-8").close(),
            },
            "write": {"write_text": lambda: (run_dir / "new.txt").write_text("x", encoding="utf-8")},
        }
        for kind, calls in requests.items():
            for name, call in calls.items():
                with self.subTest(name):
                    with self.storage.counting() as trips:
                        call()
                    self.assertGreater(trips.count(kind), 0)
                    if kind == "read":
                        self.assertEqual(trips.count("write"), 0)
        with self.storage.counting() as trips:
            other = threading.Thread(target=lambda: list(run_dir.iterdir()))
            other.start()
            other.join()
        self.assertEqual(trips.summary(), {"listing": 0, "read": 0, "write": 0})  # another thread: not counted


class TestWarmRequests(CatalogueBase):
    def trips_with(self, talks: int) -> dict:
        """Storage requests per request on a site with `talks` finished talks, each with every stage done."""
        self.use_new_storage()
        for number in range(talks):
            self.add_talk(number, stages=ALL_STAGES)
        self.start()
        listed = self.history()  # the first request after a start builds the catalogue from the folders
        self.assertEqual(len(listed), talks)
        self.assertEqual((listed[0]["summary"]["status"], listed[-1]["summary"]["status"]), ("complete", "complete"))
        trips = {}
        with self.storage.counting() as counted:
            self.assertEqual(self.history(), listed)
        trips["warm history"] = counted.summary()
        job_id = listed[talks // 2]["id"]
        with self.storage.counting() as counted:
            first = self.open(job_id)
        trips["first open"] = counted.summary()
        with self.storage.counting() as counted:
            again = self.open(job_id)
        trips["open again"] = counted.summary()
        self.assertEqual(again, first)  # the cached talk shows exactly what reading its files showed
        self.assertEqual(first["translation_text"], "Today a talk Yes Nirmay\n")
        self.assertEqual(first["youtube"], {"title": "A talk", "description": "About it"})
        self.assertFalse(first["captions_stale"] or first["summary_stale"] or first["english_stale"])
        self.assertEqual(again, self.fresh_open(job_id))
        return trips

    def test_the_talk_list_and_a_cached_talk_touch_no_files_however_many_talks(self):
        few, many = self.trips_with(3), self.trips_with(40)
        nothing = {"listing": 0, "read": 0, "write": 0}
        self.assertEqual(few["warm history"], nothing)
        self.assertEqual(few["open again"], nothing)
        self.assertEqual(few["first open"]["listing"], 0)
        self.assertGreater(few["first open"]["read"], 0)  # the first open reads the talk's files, once
        self.assertEqual(many, few)


class TestChangesShow(CatalogueBase):
    def setUp(self):
        super().setUp()
        self.run_dir = self.add_talk(1, stages=("gujlish",), suggestion=True)
        self.start()
        self.job_id = self.history()[0]["id"]
        self.before = self.open(self.job_id)  # now cached

    def english_writes(self, command):
        """What the translation and caption scripts write."""
        self.write_output(self.run_dir, "translation" if command[1].endswith("translate.py") else "captions")

    def test_a_word_fix(self):
        body = {"segment": 0, "start": 1, "end": 2, "heard": "વાત", "correct": "વાર્તા", "correct_gujlish": "vaarta"}
        response = self.client.post(f"/api/jobs/{self.job_id}/edit", json={**body, "remember": False})
        self.assertEqual(response.status_code, 200, response.json)
        after = self.open(self.job_id)
        self.assertEqual(after["segments"][0]["text"], "આજે વાર્તા")
        self.assertEqual(after["gujlish_tokens"][0], ["aaje", "vaarta"])
        self.assertTrue(after["text"].startswith("આજે વાર્તા"))
        self.assertEqual(after, self.fresh_open(self.job_id))

    def test_a_dismissed_suggestion(self):
        self.assertEqual(self.before["segments"][1]["suggestions"][0]["rule"], "r1")
        body = {"segment": 1, "start": 1, "end": 2, "rule": "r1"}
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/dismiss", json=body).json, {"dismissed": True})
        after = self.open(self.job_id)
        self.assertEqual(after["segments"][1]["suggestions"], [])
        self.assertEqual(after, self.fresh_open(self.job_id))

    def test_a_stage_that_ends(self):
        self.assertEqual(self.before["translation"]["status"], "idle")
        with patch.dict("os.environ", {"GEMINI_API_KEY": "g"}):
            self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/translate").status_code, 202)
        self.assertEqual(self.open(self.job_id)["translation"]["status"], "queued")
        self.run_task(self.english_writes)
        after = self.open(self.job_id)
        self.assertEqual((after["translation"]["status"], after["captions"]["status"]), ("complete", "complete"))
        self.assertEqual(after["translation_text"], "Today a talk Yes Nirmay\n")
        self.assertFalse(after["captions_stale"])
        self.assertEqual(self.history()[0]["translation"], {"status": "complete", "progress": 100})
        self.assertEqual(after, self.fresh_open(self.job_id))
        self.assertEqual(self.saved_runs()[0]["done"], ["gujlish", "translation", "captions"])

    def test_a_stage_that_rewrites_the_gujlish(self):
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/gujlish", json={}).status_code, 202)
        tokens = [["aaje", "vaat."], ["haa", "nirmay"]]
        self.run_task(lambda command: self.write_output(self.run_dir, "gujlish", tokens))
        after = self.open(self.job_id)
        self.assertEqual(after["gujlish_tokens"], tokens)
        self.assertEqual(after, self.fresh_open(self.job_id))

    def test_a_load_that_races_with_a_change_is_not_kept(self):
        """A talk opened while a word fix is being saved may read the files from before the fix: that response may
        show the old words, but the cache must not keep them."""
        self.client.post(f"/api/jobs/{self.job_id}/dismiss", json={"segment": 1, "start": 1, "end": 2, "rule": "r1"})
        real = __import__("web.server", fromlist=["load_gujlish"]).load_gujlish
        fixed = []

        def load_gujlish(path, segments):  # called after the transcript was read: fix a word now
            if not fixed:
                fixed.append(True)
                body = {"segment": 0, "start": 1, "end": 2, "heard": "વાત", "correct": "વાર્તા", "remember": False}
                self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/edit", json=body).status_code, 200)
            return real(path, segments)

        with patch("web.server.load_gujlish", side_effect=load_gujlish):
            raced = self.open(self.job_id)
        self.assertEqual(raced["segments"][0]["text"], "આજે વાત")  # read before the fix
        self.assertEqual(self.open(self.job_id)["segments"][0]["text"], "આજે વાર્તા")

    def test_a_word_fix_that_fails_partway(self):
        body = {"segment": 0, "start": 1, "end": 2, "heard": "વાત", "correct": "વાર્તા", "remember": False}
        with (
            patch("transcriber.speech.edits.write_srt", side_effect=OSError("storage unreachable")),
            patch.object(self.app.logger, "error"),  # Flask logs the failure
        ):
            self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/edit", json=body).status_code, 500)
        after = self.open(self.job_id)
        self.assertEqual(after["segments"][0]["text"], "આજે વાર્તા")  # saved before the failure
        self.assertEqual(after, self.fresh_open(self.job_id))

    def test_a_stage_that_ends_while_the_talk_is_opened(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "g"}):
            self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/translate").status_code, 202)
        real_get, ran = ViewCache.get, []

        def get(cache, key):  # the stage ends after this open read the talk's state, before it reads its files
            if not ran:
                ran.append(True)
                self.run_task(self.english_writes)
            return real_get(cache, key)

        with patch.object(ViewCache, "get", get):
            self.assertEqual(self.open(self.job_id)["translation"]["status"], "queued")
        after = self.open(self.job_id)
        self.assertEqual(after["translation_text"], "Today a talk Yes Nirmay\n")
        self.assertEqual(after, self.fresh_open(self.job_id))

    def test_output_that_does_not_load_is_read_again_next_time(self):
        self.client.post(f"/api/jobs/{self.job_id}/dismiss", json={"segment": 1, "start": 1, "end": 2, "rule": "r1"})
        with patch("web.server.load_gujlish", return_value=(None, "")):  # e.g. a passing storage error
            self.assertIsNone(self.open(self.job_id)["gujlish_tokens"])
        self.assertEqual(self.open(self.job_id)["gujlish_tokens"], GUJLISH)


class TestGpuResults(CatalogueBase):
    def gpu_writes(self, run_dir, segments):
        """Stands in for run_remote: the GPU job writes the transcript into the run folder and reports it finished."""

        def remote(jobs, command, on_event, on_start=None, **options):
            options["on_execution"](EXECUTION)
            self.write_transcript(run_dir, segments)
            txt, data = self.transcript(run_dir, ".txt"), self.transcript(run_dir)
            on_event({"stage": "complete", "txt": str(txt), "json": str(data)})
            options["on_finish"](EXECUTION, {"started": time.time() - 60, "ended": time.time()})
            options["on_execution"](None)
            return 0, ["Done:"]

        return remote

    def run_transcription(self, run_dir, segments):
        function, args = self.executor.tasks.pop(0)
        with patch("web.server.run_remote", side_effect=self.gpu_writes(run_dir, segments)):
            function(*args)

    def test_a_transcription_from_the_gpu_job_is_taken_in_when_it_ends(self):
        run_dir = self.add_talk(1, finished=False)
        self.start(gpu_jobs=FakeJobs())
        entry = self.history()[0]  # cut short by the restart: queued again
        self.assertEqual(entry["status"], "queued")
        self.assertTrue(self.saved_runs()[0]["unsettled"])
        self.run_transcription(run_dir, SEGMENTS)
        opened = self.open(entry["id"])
        self.assertEqual((opened["status"], opened["text"]), ("complete", "આજે વાત હા નીરમય\n"))
        self.assertEqual(self.history()[0]["status"], "complete")
        saved = self.saved_runs()[0]
        self.assertEqual((saved["status"], saved["txt"], saved["unsettled"]), ("complete", "talk001.flex.txt", True))
        self.assertNotIn("processed", saved)  # as for a finished run read from its folder
        self.run_task(lambda command: self.write_output(run_dir, "gujlish"))  # the Gujlish that follows
        self.assertFalse(self.saved_runs()[0]["unsettled"])
        # A run finished in this session still shows its `processed` figure; after a restart it does not, as before.
        opened = {key: value for key, value in self.open(entry["id"]).items() if key != "processed"}
        self.assertEqual(opened, self.fresh_open(entry["id"]))

    def test_whatever_the_gpu_job_writes_replaces_a_cached_talk(self):
        run_dir = self.add_talk(1)
        self.start(gpu_jobs=FakeJobs())
        job_id = self.history()[0]["id"]
        self.assertEqual(self.open(job_id)["segments"][0]["text"], "આજે વાત")  # cached
        self.app.extensions["asr_jobs"][job_id]["status"] = "error"  # as if it were to be transcribed again
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/retry").status_code, 202)
        self.run_transcription(run_dir, [{"start": 0, "end": 10, "text": "નવું લખાણ"}])
        opened = self.open(job_id)
        self.assertEqual((opened["status"], opened["text"]), ("complete", "નવું લખાણ\n"))
        self.assertEqual(opened["segments"][0]["text"], "નવું લખાણ")

    def test_a_gpu_job_that_finished_while_the_site_slept_is_taken_in_after_a_restart(self):
        run_dir = self.add_talk(1, finished=False)
        self.start(gpu_jobs=FakeJobs())
        job_id = self.history()[0]["id"]  # queued again, and saved as needing a look after a restart
        # The GPU job starts, and the website stops before it ends: only the RUN_FILE knows the job.
        meta = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
        (run_dir / RUN_FILE).write_text(json.dumps({**meta, "execution": EXECUTION}), encoding="utf-8")
        self.write_transcript(run_dir, SEGMENTS)  # what the GPU job wrote before it ended
        self.start(gpu_jobs=FakeJobs(running=False))
        with self.storage.counting() as trips:
            self.assertEqual(self.history()[0]["status"], "complete")
        self.assertEqual(trips.count("listing", self.transcripts), 0)
        self.assertEqual(self.open(job_id)["text"], "આજે વાત હા નીરમય\n")
        meta = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
        self.assertNotIn("execution", meta)
        self.assertEqual(meta["pending"], ["gujlish"])
        self.assertEqual(len(self.executor.tasks), 1)  # the Gujlish step
        self.assertEqual(self.saved_runs()[0]["status"], "complete")


class TestRestarts(CatalogueBase):
    def test_a_restart_with_a_valid_catalogue_lists_no_folders(self):
        for number in range(5):
            self.add_talk(number, stages=ALL_STAGES[: number % 5])
        self.start()
        listed = self.history()
        self.assertEqual(len(self.saved_runs()), 5)
        self.start()
        with self.storage.counting() as trips:
            self.assertEqual(self.history(), listed)
        self.assertEqual(trips.count("listing"), 0)  # recordings are merged later, in the background, too
        self.assertEqual(trips.paths("read", self.transcripts), [str(self.catalogue)])  # that file and nothing else
        self.join_reconcile()  # it then checks the catalogue against the folders, in the background
        self.assertEqual(self.history(), listed)

    def test_a_missing_damaged_or_other_version_catalogue_is_rebuilt(self):
        for number in range(4):
            self.add_talk(number, stages=("gujlish",))
        self.start()
        listed = self.history()
        self.assertEqual([run["title"] for run in self.saved_runs()], [None] * 4)  # the slot for naming talks
        cases = {
            "missing": None,
            "damaged": "{not json",
            "another version": json.dumps({"version": CATALOGUE_VERSION + 1, "runs": []}),
            "a run without its folder": json.dumps({"version": CATALOGUE_VERSION, "runs": [{"status": "complete"}]}),
        }
        for case, content in cases.items():
            with self.subTest(case):
                if content is None:
                    self.catalogue.unlink()
                else:
                    self.catalogue.write_text(content, encoding="utf-8")
                self.start()
                with patch("builtins.print"), self.storage.counting() as trips:
                    self.assertEqual(self.history(), listed)
                self.assertGreater(trips.count("listing", self.transcripts), 0)  # built from the folders
                self.assertIsNone(self.app.extensions["asr_reconcile"])
                self.assertEqual(len(self.saved_runs()), 4)

    def test_the_background_check_adds_and_drops_runs(self):
        kept = self.add_talk(1)
        removed = self.add_talk(2)
        self.start()
        self.assertEqual(len(self.history()), 2)
        shutil.rmtree(removed)  # deleted outside the site
        made = self.add_talk(3)  # made outside the site, e.g. by the command line
        leftover = self.transcripts / "lost" / "flex-mixed" / "20200101-120000-000001"  # its upload never started
        leftover.mkdir(parents=True)
        self.start()
        with self.storage.counting() as trips:
            self.history()
        self.assertEqual(trips.count("listing", self.transcripts), 0)
        self.join_reconcile()
        self.assertEqual([entry["run"] for entry in self.history()], [made.name, kept.name])
        self.assertEqual(sorted(run["run"].split("/")[0] for run in self.saved_runs()), ["talk001", "talk003"])
        self.assertFalse(leftover.exists())

    def test_a_run_missing_from_the_catalogue_keeps_its_recording_and_its_work(self):
        self.add_talk(1)
        self.start()
        self.history()
        # Made while an older version of the site ran, or saved by a save that failed: not in the catalogue file.
        missing = self.add_talk(2, finished=False)
        recording = self.recordings / "talk002.wav"
        hours_ago = time.time() - 2 * 3600  # old enough to be removed if no run used it
        os.utime(recording, (hours_ago, hours_ago))
        self.start()
        self.history()
        self.join_reconcile()
        self.assertTrue(recording.exists())
        entry = next(entry for entry in self.history() if entry["run"] == missing.name)
        self.assertEqual(entry["status"], "queued")  # cut short: picked up again, as after the old folder scan
        self.assertEqual(len(self.executor.tasks), 1)

    def test_changes_made_without_this_server_show_after_a_restart(self):
        run_dir = self.add_talk(1)
        self.start()
        job_id = self.history()[0]["id"]
        # E.g. an older version of the site, run for a while in between: it made the English and left the summary
        # waiting to resume.
        self.write_output(run_dir, "translation")
        meta = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
        (run_dir / RUN_FILE).write_text(json.dumps({**meta, "pending": ["summary"]}), encoding="utf-8")
        self.start()
        self.history()
        self.join_reconcile()
        self.assertEqual(self.history()[0]["translation"], {"status": "complete", "progress": 100})
        self.assertEqual(self.open(job_id)["translation_text"], "Today a talk Yes Nirmay\n")
        self.assertEqual([key for key, _ in self.executor.tasks[0][1][1]], ["summary"])
        self.assertEqual(self.saved_runs()[0]["done"], ["translation"])


class TestSharingDeletesAndRetention(CatalogueBase):
    def test_every_browser_sees_every_talk(self):
        self.add_talk(1)
        self.add_talk(2, visitor=STRANGER)  # folders from before talks were shared keep an .owner file: ignored
        self.start()
        stranger = self.browser(STRANGER)
        self.assertEqual([entry["name"] for entry in self.history()], ["talk002.wav", "talk001.wav"])
        self.assertEqual(self.history(stranger), self.history())
        for entry in self.history():
            self.assertEqual(self.open(entry["id"], stranger), self.open(entry["id"]))  # cached for everyone
        self.assertNotIn("owner", self.open(entry["id"]))
        self.assertEqual([run.get("owner") for run in self.saved_runs()], [None, None])

    def test_a_deleted_talk_leaves_the_list_the_cache_and_the_catalogue(self):
        gone, kept = self.add_talk(1), self.add_talk(2)
        self.start()
        job_id = next(entry["id"] for entry in self.history() if entry["run"] == gone.name)
        self.open(job_id)
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/delete").status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)
        self.assertEqual([entry["run"] for entry in self.history()], [kept.name])
        self.assertEqual([run["run"].split("/")[0] for run in self.saved_runs()], ["talk002"])
        for _ in range(100):  # its folder is removed in the background
            if not gone.exists():
                break
            time.sleep(0.02)
        self.assertFalse(gone.exists())

    def test_talks_past_the_retention_period_leave_the_catalogue(self):
        old = self.add_talk(1, run="20200101-120000-000001")
        new = self.add_talk(2, run=datetime.now().strftime("%Y%m%d-%H%M%S-%f"))
        self.start(retention_hours=24)
        self.client.get("/health")  # the start: both are in the catalogue
        old_id = next(
            run
            for run in self.app.extensions["asr_jobs"]
            if self.app.extensions["asr_jobs"][run]["run_dir"] == str(old)
        )
        self.open(old_id)
        self.assertEqual([entry["run"] for entry in self.history()], [new.name])  # the list sweeps first
        self.assertFalse(old.exists())
        self.assertEqual(self.client.get(f"/api/jobs/{old_id}").status_code, 404)
        self.assertEqual([run["run"].split("/")[0] for run in self.saved_runs()], ["talk002"])


if __name__ == "__main__":
    unittest.main()
