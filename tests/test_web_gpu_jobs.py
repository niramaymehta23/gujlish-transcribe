"""Hosted transcription on a Cloud Run GPU job (web/cloudjobs.py): the website follows the job, picks it up again after
a restart instead of starting a second one, logs its GPU time, and stops starting jobs at the monthly limit."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import DeferredExecutor, stop_search_at_end
from web import usage
from web.cloudjobs import RemoteRun, events_file, run_remote
from web.server import RUN_FILE, create_app, gpu_timeout
from web.settings import Settings

EXECUTION = "projects/p/locations/r/jobs/gpu/executions/gpu-abc"


class FakeClock:
    def __init__(self):
        self.now = 1_794_000_000.0

    def time(self):
        return self.now


class FakeJobs:
    """Stands in for CloudRunJobs."""

    def __init__(self, running=True):
        self.clock = FakeClock()
        self.started, self.cancelled = [], []
        self.running = running

    def start(self, env, timeout_seconds):
        self.started.append((env, timeout_seconds))
        return EXECUTION

    def status(self, execution):
        done = not self.running
        return {"done": done, "ok": done, "started": self.clock.now - 600, "ended": self.clock.now if done else None,
                "reason": ""}  # fmt: skip

    def cancel(self, execution):
        self.cancelled.append(execution)
        self.running = False


class RunRemoteTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name)
        self.jobs = FakeJobs()
        self.events, self.recorded, self.finished = [], [], []

    def write(self, **state):
        events_file(self.run_dir, "transcribe").write_text(json.dumps(state), encoding="utf-8")

    def go(self, sleep, saved=None):
        return run_remote(
            self.jobs, ["python", "scripts/transcribe.py", "talk.wav", "flex"], self.events.append,
            run_dir=self.run_dir, step="transcribe", timeout_seconds=1800, saved=saved,
            on_execution=self.recorded.append, on_finish=lambda name, status: self.finished.append(name), sleep=sleep,
        )  # fmt: skip

    def test_follows_the_job_and_returns_its_result(self):
        steps = iter([
            dict(count=1, events=[{"stage": "loading", "device": "cuda"}]),
            dict(count=3, events=[{"stage": "loading", "device": "cuda"}, {"stage": "transcribing", "processed": 10},
                                  {"stage": "transcribing", "processed": 20}]),
            dict(count=4, events=[{"stage": "complete", "txt": "a.txt", "json": "a.json"}], done=True, code=0,
                 logs=["Done:"]),
        ])  # fmt: skip
        code, logs = self.go(lambda _: self.write(**next(steps)))
        self.assertEqual((code, logs), (0, ["Done:"]))
        self.assertEqual([e["stage"] for e in self.events], ["loading", "transcribing", "transcribing", "complete"])
        env, timeout = self.jobs.started[0]
        self.assertEqual(json.loads(env["VAANI_JOB_COMMAND"])[1], "scripts/transcribe.py")
        self.assertEqual(env["VAANI_JOB_EVENTS"], str(events_file(self.run_dir, "transcribe")))
        self.assertEqual(timeout, 1800)
        self.assertEqual(self.recorded, [EXECUTION, None])  # kept in the run's state while it runs, then cleared
        self.assertEqual(self.finished, [EXECUTION])

    def test_a_running_job_is_followed_again_not_started_twice(self):
        self.go(lambda _: self.write(count=0, events=[], done=True, code=0, logs=[]), saved=EXECUTION)
        self.assertEqual(self.jobs.started, [])

    def test_a_job_that_ended_long_ago_is_not_followed(self):
        self.jobs.running = False
        self.go(lambda _: self.write(count=0, events=[], done=True, code=0, logs=[]), saved=EXECUTION)
        self.assertEqual(len(self.jobs.started), 1)

    def test_a_job_that_dies_without_a_result_is_an_error(self):
        def sleep(seconds):
            self.jobs.clock.now += seconds
            if self.jobs.clock.now > 1_794_000_030:
                self.jobs.running = False  # killed, e.g. it ran out of time

        code, logs = self.go(sleep)
        self.assertEqual(code, 1)
        self.assertTrue(logs[0].startswith("Error:"))

    def test_stopping_cancels_the_execution(self):
        run = RemoteRun(self.jobs, EXECUTION)
        run.stop(sleep=lambda _: None)
        self.assertEqual(self.jobs.cancelled, [EXECUTION])
        run.wait(timeout=5, sleep=lambda _: None)  # returns once the execution has ended

    def test_stopping_keeps_cancelling_until_the_job_has_ended(self):
        cancels = []

        def cancel(execution):
            cancels.append(execution)
            if len(cancels) == 1:
                raise OSError("Cloud Run unreachable")  # the first cancel is lost
            self.jobs.running = False

        self.jobs.cancel = cancel
        RemoteRun(self.jobs, EXECUTION).stop(sleep=lambda seconds: None)
        self.assertEqual(len(cancels), 2)
        self.assertFalse(self.jobs.running)

    def test_stopping_gives_up_after_five_minutes(self):
        self.jobs.cancel = lambda execution: None  # Cloud Run never confirms

        def sleep(seconds):
            self.jobs.clock.now += seconds

        start = self.jobs.clock.now
        RemoteRun(self.jobs, EXECUTION).stop(sleep=sleep)
        self.assertGreaterEqual(self.jobs.clock.now - start, 300)
        self.assertLess(self.jobs.clock.now - start, 330)

    def test_cloud_run_times(self):
        from web.cloudjobs import _epoch

        self.assertEqual(_epoch("2026-11-11T12:00:00.123456789Z"), _epoch("2026-11-11T12:00:00.123456Z"))
        self.assertEqual(_epoch("2026-11-11T12:00:00Z"), 1_794_398_400.0)
        self.assertIsNone(_epoch(None))

    def test_time_limits(self):
        self.assertEqual(gpu_timeout(60), 15 * 60)
        self.assertEqual(gpu_timeout(716), 15 * 60)  # the 12-minute talk that took 3.5 minutes
        self.assertEqual(gpu_timeout(3600), 30 * 60)
        self.assertEqual(gpu_timeout(6 * 3600), 3600)
        self.assertEqual(gpu_timeout(None), 3600)  # length unknown: the most a job may run


class JobEntryPointTest(unittest.TestCase):
    def test_runs_the_worker_and_reports_progress_and_usage(self):
        import os
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as temp:
            run_dir = Path(temp) / "transcripts" / "talk" / "flex-mixed" / "20261111-120000-000001"
            run_dir.mkdir(parents=True)
            worker = 'print(\'ASR_EVENT \' + \'{"stage": "loading", "device": "cuda"}\'); print(\'Done:\')'
            env = {**os.environ, "VAANI_DATA_DIR": temp, "CLOUD_RUN_EXECUTION": "gpu-xyz",
                   "VAANI_JOB_COMMAND": json.dumps([sys.executable, "-c", worker]),
                   "VAANI_JOB_EVENTS": str(events_file(run_dir, "transcribe"))}  # fmt: skip
            script = Path(__file__).resolve().parents[1] / "scripts" / "run_job.py"
            result = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(events_file(run_dir, "transcribe").read_text(encoding="utf-8"))
            self.assertEqual((state["done"], state["code"], state["count"]), (True, 0, 1))
            self.assertEqual(state["events"][0]["device"], "cuda")
            records = list((Path(temp) / "usage").glob("*/gpu-xyz.json"))
            self.assertEqual(len(records), 1)
            self.assertEqual(json.loads(records[0].read_text(encoding="utf-8"))["run"], run_dir.name)


class ServerGpuJobTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in (("TRANSCRIPTS", root / "transcripts"), ("DATA", root), ("RECORDINGS", root / "recordings")):
            patcher = patch(f"transcriber.files.storage.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / "recordings").mkdir()
        (root / "recordings" / "talk.wav").write_bytes(b"RIFF")
        self.run_dir = root / "transcripts" / "talk" / "flex-mixed" / "20261111-120000-000001"
        self.run_dir.mkdir(parents=True)
        self.jobs = FakeJobs()
        self.start_server()

    def start_server(self, cap=10):
        self.executor = DeferredExecutor()
        self.app = create_app(executor=self.executor, settings=Settings(gpu_hours_per_month=cap), gpu_jobs=self.jobs)
        stop_search_at_end(self, self.app)
        self.client = self.app.test_client()

    def meta(self, **fields):
        state = {"source": "talk.wav", "model": "flex", "mode": "mixed", "duration": 60.0, "size": 4, **fields}
        (self.run_dir / RUN_FILE).write_text(json.dumps(state), encoding="utf-8")

    def saved_meta(self):
        return json.loads((self.run_dir / RUN_FILE).read_text(encoding="utf-8"))

    def finish_transcript(self):
        data = {"source": "talk.wav", "model": "flex", "mode": "mixed", "duration": 60.0,
                "segments": [{"start": 0, "end": 5, "text": "આજે વાત"}]}  # fmt: skip
        (self.run_dir / "talk.flex.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / "talk.flex.txt").write_text("આજે વાત\n", encoding="utf-8")

    def fake_remote(self, calls):
        """Stands in for run_remote: the GPU job writes the transcript and reports it finished."""

        def remote(jobs, command, on_event, on_start=None, **options):
            calls.append(options)
            options["on_execution"](EXECUTION)
            calls.append(dict(self.saved_meta()))
            self.finish_transcript()
            on_event({"stage": "complete", "txt": str(self.run_dir / "talk.flex.txt"),
                      "json": str(self.run_dir / "talk.flex.json")})  # fmt: skip
            options["on_finish"](EXECUTION, {"started": time.time() - 900, "ended": time.time()})
            options["on_execution"](None)
            return 0, ["Done:"]

        return remote

    def test_transcription_runs_on_the_gpu_job_not_here(self):
        self.meta()
        entry = self.client.get("/api/history").json[0]
        calls = []
        function, args = self.executor.tasks.pop(0)
        with (
            patch("web.server.run_remote", side_effect=self.fake_remote(calls)),
            patch("web.server.subprocess.Popen") as popen,
        ):
            function(*args)
        popen.assert_not_called()
        self.assertEqual(calls[0]["step"], "transcribe")
        self.assertEqual(calls[1]["execution"], EXECUTION)  # saved while it runs, for a restart
        self.assertNotIn("execution", self.saved_meta())
        self.assertEqual(self.client.get(f"/api/jobs/{entry['id']}").json["status"], "complete")
        self.assertEqual(self.saved_meta()["pending"], ["gujlish"])
        summary = self.client.get("/api/usage").json
        self.assertEqual((summary["gpu"], summary["runs"], summary["seconds"]), (True, 1, 900))

    def test_a_restart_follows_the_running_job_without_counting_a_restart(self):
        self.meta(execution=EXECUTION)
        self.checkpoint()
        entry = self.client.get("/api/history").json[0]
        self.assertEqual(entry["status"], "queued")
        self.assertNotIn("resumes", self.saved_meta())
        calls = []
        function, args = self.executor.tasks.pop(0)
        with patch("web.server.run_remote", side_effect=self.fake_remote(calls)):
            function(*args)
        self.assertEqual(calls[0]["saved"], EXECUTION)

    def checkpoint(self):
        segments = [{"start": 0.0, "end": 27.5, "text": "આજે"}]
        (self.run_dir / ".talk.flex.partial.json").write_text(json.dumps({"segments": segments}), encoding="utf-8")

    def test_a_job_that_finished_while_the_site_slept_carries_on(self):
        self.jobs.running = False
        self.meta(execution=EXECUTION)
        self.finish_transcript()
        entry = self.client.get("/api/history").json[0]
        self.assertEqual(entry["status"], "complete")
        self.assertNotIn("execution", self.saved_meta())
        self.assertEqual(self.saved_meta()["pending"], ["gujlish"])
        self.assertEqual(len(self.executor.tasks), 1)  # the Gujlish step

    def test_the_monthly_limit_stops_new_transcriptions(self):
        now = time.time()
        usage.record("gpu-old", now - 11 * 3600, now)
        self.meta(paused=True)
        entry = self.client.get("/api/history").json[0]
        response = self.client.post(f"/api/jobs/{entry['id']}/resume")
        self.assertEqual(response.status_code, 429)
        self.assertIn("GPU limit", response.json["error"])
        summary = self.client.get("/api/usage").json
        self.assertTrue(summary["over_cap"])
        self.assertEqual(summary["cap_hours"], 10)

    def test_no_limit_when_set_to_zero(self):
        now = time.time()
        usage.record("gpu-old", now - 11 * 3600, now)
        self.start_server(cap=0)
        self.meta(paused=True)
        entry = self.client.get("/api/history").json[0]
        self.assertEqual(self.client.post(f"/api/jobs/{entry['id']}/resume").status_code, 202)

    def test_local_sites_have_no_gpu_job(self):
        app = create_app(executor=DeferredExecutor(), settings=Settings())
        stop_search_at_end(self, app)
        self.assertEqual(app.test_client().get("/api/usage").json, {"gpu": False})

    def test_a_gpu_job_is_used_only_on_cloud_run(self):
        env = {"VAANI_GPU_JOB": "gpu", "VAANI_PROJECT": "p", "VAANI_REGION": "r"}
        with patch.dict("os.environ", env), patch("builtins.print"):
            self.assertEqual(Settings.from_env().gpu_job, "")  # e.g. python app.py on a Mac
        with patch.dict("os.environ", {**env, "K_SERVICE": "gujarati-transcriber"}):
            self.assertEqual(Settings.from_env().gpu_job, "gpu")

    def test_a_paused_runs_gpu_job_is_cancelled_after_a_restart(self):
        self.meta(execution=EXECUTION, paused=True)
        self.checkpoint()
        with patch("web.cloudjobs.time.sleep"):
            entry = self.client.get("/api/history").json[0]
            for _ in range(50):  # the stop runs in the background
                if not self.jobs.running:
                    break
                time.sleep(0.01)
        self.assertEqual(entry["status"], "paused")
        self.assertEqual(self.jobs.cancelled, [EXECUTION])
        self.assertNotIn("execution", self.saved_meta())
        self.assertEqual(self.executor.tasks, [])  # not followed again

    def test_pausing_is_saved_at_once(self):
        """Saved before the GPU job has even stopped, so a restart in between cannot pick the job up again."""
        import threading

        self.meta()
        self.checkpoint()
        entry = self.client.get("/api/history").json[0]
        function, args = self.executor.tasks.pop(0)
        running, release, seen = threading.Event(), threading.Event(), {}

        def remote(jobs, command, on_event, on_start=None, **options):
            on_start(RemoteRun(self.jobs, EXECUTION))
            on_event({"stage": "transcribing", "processed": 30.0, "chunks": 1})
            running.set()
            release.wait(5)
            return 1, ["Stopped."]

        def stopped(worker):
            seen["paused_when_stopped"] = self.saved_meta().get("paused")
            release.set()

        with patch("web.server.run_remote", side_effect=remote), patch("web.server.stop_worker", side_effect=stopped):
            thread = threading.Thread(target=function, args=args)
            thread.start()
            self.assertTrue(running.wait(5))
            self.assertEqual(self.client.post(f"/api/jobs/{entry['id']}/pause").status_code, 200)
            thread.join(5)
        self.assertTrue(seen["paused_when_stopped"])
        self.assertEqual(self.client.get(f"/api/jobs/{entry['id']}").json["status"], "paused")


if __name__ == "__main__":
    unittest.main()
