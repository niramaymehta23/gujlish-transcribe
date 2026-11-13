"""Production safety: passing storage errors never remove talks or recordings, stale upload pieces leave memory, the
site's sign-in and response headers, and the GPU job and its follower surviving failed writes, lost status calls and
an earlier job's late write."""

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import DeferredExecutor, background_check_done, stop_search_at_end, wait_for_background_check
from tests.test_hosted import HostedBase
from tests.test_web_gpu_jobs import EXECUTION, FakeJobs
from transcriber.speech.cli import build_parser, run
from web.cloudjobs import FOLLOW_GRACE_SECONDS, events_file, run_remote
from web.server import RUN_FILE, create_app, run_id
from web.settings import Settings

OLD = 1_600_000_000  # a modification time well past the leftover age


class StorageBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recordings, self.transcripts = self.root / "recordings", self.root / "transcripts"
        for name, value in (("RECORDINGS", self.recordings), ("TRANSCRIPTS", self.transcripts), ("DATA", self.root)):
            patcher = patch(f"transcriber.files.storage.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def start(self):
        """A hosted site's start: with no catalogue file yet it reads every run folder, then merges recordings."""
        app = create_app(executor=DeferredExecutor(), settings=Settings(hosted=True, secret_key="x" * 40))
        app.config["SESSION_COOKIE_SECURE"] = False
        wait_for_background_check(self, app)
        stop_search_at_end(self, app)
        app.test_client().get("/health")
        return app

    def recording(self, name, data):
        path = self.recordings / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        os.utime(path, (OLD, OLD))
        return path

    def add_run(self, source, when):
        run_dir = self.transcripts / Path(source).stem / "flex-mixed" / when
        run_dir.mkdir(parents=True)
        stem = f"{Path(source).stem}.flex"
        data = {"source": source, "model": "flex", "mode": "mixed", "duration": 5.0, "segments": []}
        (run_dir / f"{stem}.json").write_text(json.dumps(data), encoding="utf-8")
        (run_dir / f"{stem}.txt").write_text("આજે\n", encoding="utf-8")
        meta = {"source": source, "name": source, "model": "flex", "mode": "mixed", "duration": 5.0}
        (run_dir / RUN_FILE).write_text(json.dumps(meta), encoding="utf-8")
        return run_dir


class TestPassingStorageErrors(StorageBase):
    def test_a_talk_that_cannot_be_read_at_a_start_keeps_its_files_and_recording(self):
        recording = self.recording("talk.m4a", b"audio")
        run_dir = self.add_run("talk.m4a", "20261101-100000-000001")
        real = Path.read_text

        def failing(path, *args, **kwargs):
            if str(path).startswith(str(run_dir)):
                raise OSError(5, "Input/output error")  # what the storage mount gives for a refused read
            return real(path, *args, **kwargs)

        with patch("pathlib.Path.read_text", failing):
            app = self.start()
        self.assertTrue((run_dir / RUN_FILE).exists())  # not taken for a leftover upload
        self.assertTrue(recording.exists())  # nor its recording for an unused one
        self.assertNotIn(run_id(run_dir), app.extensions["asr_jobs"])  # it comes back when it can be read

    def test_nothing_is_merged_while_a_folder_cannot_be_listed(self):
        kept = self.recording("talk.m4a", b"same audio")
        copy = self.recording("talk-2.m4a", b"same audio")
        self.add_run("talk.m4a", "20261101-100000-000001")
        self.add_run("talk-2.m4a", "20261102-100000-000001")
        real, unlistable = os.scandir, str(self.transcripts / "talk-2")

        def failing(path=".", *args, **kwargs):
            if str(path) == unlistable:
                raise OSError(5, "Input/output error")
            return real(path, *args, **kwargs)

        with patch("os.scandir", failing):
            self.start()
        self.assertTrue(kept.exists())
        self.assertTrue(copy.exists())  # its run may not have been read: merged at a later start

    def test_a_listing_error_never_empties_the_catalogue(self):
        run_dir = self.add_run("talk.m4a", "20261101-100000-000001")
        self.recording("talk.m4a", b"audio")
        self.start()  # writes the catalogue file
        with patch("os.scandir", side_effect=OSError(5, "Input/output error")):
            app = self.start()  # from the catalogue file; its background check can't list the folders
            self.assertTrue(background_check_done(app))
        self.assertIn(run_id(run_dir), app.extensions["asr_jobs"])

    def test_a_merged_talk_whose_state_cannot_be_read_keeps_the_copy_it_uses(self):
        kept = self.recording("talk.m4a", b"same audio")
        run_dir = self.add_run("talk-2.m4a", "20261102-100000-000001")  # its own copy was merged into talk.m4a
        meta = json.loads((run_dir / RUN_FILE).read_text(encoding="utf-8"))
        (run_dir / RUN_FILE).write_text(json.dumps({**meta, "recording": "talk.m4a"}), encoding="utf-8")
        real = Path.read_text

        def failing(path, *args, **kwargs):
            if path == run_dir / RUN_FILE:
                raise OSError(5, "Input/output error")
            return real(path, *args, **kwargs)

        with patch("pathlib.Path.read_text", failing):
            self.start()
        self.assertTrue(kept.exists())  # read from its transcript alone, the talk would have named talk-2.m4a

    def test_old_folders_without_a_run_are_still_removed_and_block_nothing(self):
        self.add_run("talk.m4a", "20261101-100000-000001")
        self.recording("talk.m4a", b"audio")
        forgotten = self.recording("forgotten.m4a", b"nobody uses this")
        leftover = self.transcripts / "lost" / "flex-mixed" / "20261102-110000-000001"
        leftover.mkdir(parents=True)
        (leftover / ".run.json.tmp").write_text("{", encoding="utf-8")  # a state file whose write was cut short
        cut_short = self.transcripts / "gone" / "flex-mixed" / "20261101-090000-000001"
        cut_short.mkdir(parents=True)
        (cut_short / "gone.flex.summary.json").write_text("{}", encoding="utf-8")  # what a cut-short delete left
        self.start()
        self.assertFalse(leftover.exists())
        self.assertFalse(cut_short.exists())
        self.assertFalse(forgotten.exists())

    def test_a_local_site_removes_a_deleted_talks_recording_as_before(self):
        recording = self.recording("talk.m4a", b"audio")
        run_dir = self.add_run("talk.m4a", "20261101-100000-000001")
        busy = self.transcripts / "other" / "flex-mixed" / "20261014-110000-000001"
        busy.mkdir(parents=True)
        (busy / ".other.flex.partial.json").write_text("{}", encoding="utf-8")  # a command-line run under way
        app = create_app(executor=DeferredExecutor(), settings=Settings())
        stop_search_at_end(self, app)
        client = app.test_client()
        self.assertEqual(client.post(f"/api/jobs/{run_id(run_dir)}/delete").status_code, 200)
        deadline = time.monotonic() + 5
        while recording.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(recording.exists())


class TestUploadPieces(HostedBase):
    def test_pieces_of_an_abandoned_upload_are_removed(self):
        client = self.app().test_client()
        pieces = self.root / "tmp" / "vaani-uploads"
        pieces.mkdir(parents=True)
        stale, recent = pieces / f"{'a' * 32}-{'b' * 32}.part", pieces / f"{'a' * 32}-{'c' * 32}.part"
        stale.write_bytes(b"x" * 1024)
        recent.write_bytes(b"y")
        os.utime(stale, (time.time() - 2 * 3600,) * 2)
        self.assertEqual(client.post("/api/uploads").status_code, 201)
        self.assertFalse(stale.exists())
        self.assertTrue(recent.exists())  # may still be coming in


class TestSignInAndHeaders(HostedBase):
    def test_changing_the_password_signs_every_browser_out(self):
        first = self.app(password="old one").test_client()
        self.assertEqual(first.post("/login", data={"password": "old one"}).status_code, 302)
        cookie = first.get_cookie("session").value
        for password, status in (("old one", 200), ("new one", 401)):
            with self.subTest(password):
                browser = self.app(password=password).test_client()
                browser.set_cookie("session", cookie)
                self.assertEqual(browser.get("/api/history").status_code, status)

    def test_every_response_carries_the_security_headers(self):
        client = self.app(password="secret").test_client()
        for path in ("/login", "/health", "/api/history"):
            with self.subTest(path):
                headers = client.get(path).headers
                self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
                self.assertEqual(headers["X-Frame-Options"], "DENY")


class TestFollowingTheGpuJob(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name)
        self.jobs = FakeJobs()

    def follow(self, sleep, saved=None):
        return run_remote(
            self.jobs, ["python", "scripts/transcribe.py", "talk.wav", "flex"], lambda event: None,
            run_dir=self.run_dir, step="transcribe", timeout_seconds=1800, saved=saved, sleep=sleep,
        )  # fmt: skip

    def unreachable(self, execution):
        raise OSError("Cloud Run did not answer")

    def test_it_cancels_and_stops_following_once_the_job_must_have_ended(self):
        self.jobs.status = self.unreachable
        began = self.jobs.clock.now

        def sleep(seconds):
            self.jobs.clock.now += seconds
            if self.jobs.clock.now - began > 2 * 3600:
                raise AssertionError("still following a job long past its time limit")

        code, logs = self.follow(sleep)
        self.assertEqual(code, 1)
        self.assertLessEqual(self.jobs.clock.now - began, 1800 + FOLLOW_GRACE_SECONDS + 5)
        self.assertTrue(logs and logs[0].startswith("Error:"))
        self.assertEqual(self.jobs.cancelled, [EXECUTION])  # never left running untracked

    def test_a_job_cloud_run_still_calls_running_is_followed_past_its_limit(self):
        began = self.jobs.clock.now

        def sleep(seconds):
            self.jobs.clock.now += seconds
            if self.jobs.clock.now - began > 1800 + FOLLOW_GRACE_SECONDS + 600:
                self.jobs.running = False  # e.g. it started late: it ends by its own limit

        code, _ = self.follow(sleep)
        self.assertEqual(code, 1)  # ended without writing a result
        self.assertGreater(self.jobs.clock.now - began, 1800 + FOLLOW_GRACE_SECONDS + 600)
        self.assertEqual(self.jobs.cancelled, [])

    def test_a_saved_job_that_cannot_be_checked_is_followed_not_started_again(self):
        progress = events_file(self.run_dir, "transcribe")
        self.jobs.status = self.unreachable
        done = {"count": 0, "events": [], "done": True, "code": 0, "logs": ["Done:"]}
        code, _ = self.follow(lambda _: progress.write_text(json.dumps(done), encoding="utf-8"), saved=EXECUTION)
        self.assertEqual(code, 0)
        self.assertEqual(self.jobs.started, [])  # no second GPU job on the same run

    def test_a_late_write_from_an_earlier_job_is_ignored(self):
        progress = events_file(self.run_dir, "transcribe")
        writes = iter([
            {"count": 0, "events": [], "done": True, "code": -15, "logs": ["Stopped"], "execution": "gpu-old"},
            {"count": 0, "events": [], "done": True, "code": 0, "logs": ["Done:"], "execution": EXECUTION.split("/")[-1]},
        ])  # fmt: skip
        code, logs = self.follow(lambda _: progress.write_text(json.dumps(next(writes)), encoding="utf-8"))
        self.assertEqual((code, logs), (0, ["Done:"]))


def load_job_script():
    path = Path(__file__).resolve().parents[1] / "scripts" / "run_job.py"
    spec = importlib.util.spec_from_file_location("vaani_run_job", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestGpuJobWrites(unittest.TestCase):
    def test_refused_writes_do_not_end_the_job(self):
        job = load_job_script()
        with tempfile.TemporaryDirectory() as temp:
            progress = Path(temp) / ".job-transcribe.json"
            worker = 'print(\'ASR_EVENT \' + \'{"stage": "loading", "device": "cuda"}\'); print(\'Done:\')'
            env = {"VAANI_JOB_COMMAND": json.dumps([sys.executable, "-c", worker]), "VAANI_JOB_EVENTS": str(progress),
                   "CLOUD_RUN_EXECUTION": "gpu-xyz"}  # fmt: skip
            real, refusals = job.write_json, [3]

            def refusing(path, value):
                if refusals[0]:
                    refusals[0] -= 1
                    raise OSError(5, "Input/output error")
                real(path, value)

            with (
                patch.dict(os.environ, env),
                patch.object(job, "write_json", refusing),
                patch.object(job.usage, "record", side_effect=OSError(5, "Input/output error")),
                patch.object(job.time, "sleep"),
            ):
                code = job.main()
            self.assertEqual(code, 0)
            state = json.loads(progress.read_text(encoding="utf-8"))
            self.assertEqual((state["done"], state["code"], state["execution"]), (True, 0, "gpu-xyz"))


class TestFinishedTranscript(unittest.TestCase):
    def test_a_finished_run_is_reported_rather_than_transcribed_again(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "talk.wav"
            subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=2", str(source)], check=True
            )
            output = root / "run"
            output.mkdir()
            (output / "talk.flex.txt").write_text("આજે\n", encoding="utf-8")
            data = {"source": "talk.wav", "model": "flex", "duration": 2.0, "segments": []}
            (output / "talk.flex.json").write_text(json.dumps(data), encoding="utf-8")
            argv = [str(source), "flex", "--device", "cpu", "--chunking", "fixed", "--output-dir", str(output)]
            events = []
            with (
                patch("transcriber.speech.cli.choose_device", return_value="cpu"),
                patch("transcriber.speech.cli.load_backend", side_effect=AssertionError("transcribed again")),
            ):
                txt, transcript, _ = run(build_parser().parse_args(argv), events.append)
            self.assertEqual(
                events, [{"stage": "complete", "processed": 2.0, "txt": str(txt), "json": str(transcript)}]
            )


if __name__ == "__main__":
    unittest.main()
