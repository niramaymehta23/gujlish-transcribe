"""Runs that outlive the server, and stopping work: pause, resume, cancel and delete, and stopping the English."""

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import DeferredExecutor, FakeWorker, event
from web.server import RUN_FILE, STOPPED, create_app, run_id


class TestRuns(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        for name, value in (("TRANSCRIPTS", root / "transcripts"), ("DATA", root), ("RECORDINGS", root / "recordings")):
            patcher = patch(f"asr.storage.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.recording = root / "recordings" / "talk.wav"
        self.recording.parent.mkdir()
        self.recording.write_bytes(b"RIFF")
        self.run_dir = root / "transcripts" / "talk" / "flex-mixed" / "20261014-120000-000001"
        self.run_dir.mkdir(parents=True)
        env = patch.dict("os.environ", {"GEMINI_API_KEY": "g"})
        env.start()
        self.addCleanup(env.stop)
        self.stopped = []
        stops = patch("web.server.stop_worker", side_effect=self.stopped.append)
        stops.start()
        self.addCleanup(stops.stop)
        self.start_server()

    def start_server(self):
        """A fresh server on the same storage, as after Cloud Run replaces the machine."""
        self.executor = DeferredExecutor()
        self.app = create_app(executor=self.executor)
        self.client = self.app.test_client()

    def meta(self, **fields):
        state = {"source": "talk.wav", "model": "flex", "mode": "mixed", "duration": 60.0, "size": 4, **fields}
        (self.run_dir / RUN_FILE).write_text(json.dumps(state), encoding="utf-8")

    def saved_meta(self):
        return json.loads((self.run_dir / RUN_FILE).read_text(encoding="utf-8"))

    def checkpoint(self, end=27.5):
        segments = [{"start": 0.0, "end": end, "text": "આજે"}]
        (self.run_dir / ".talk.flex.partial.json").write_text(json.dumps({"segments": segments}), encoding="utf-8")

    def finish_transcript(self):
        data = {"source": "talk.wav", "model": "flex", "mode": "mixed", "duration": 60.0,
                "segments": [{"start": 0, "end": 5, "text": "આજે વાત"}]}  # fmt: skip
        (self.run_dir / "talk.flex.json").write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / "talk.flex.txt").write_text("આજે વાત\n", encoding="utf-8")

    def run_task(self, worker=None):
        function, args = self.executor.tasks.pop(0)
        with patch("web.server.subprocess.Popen", return_value=worker) as popen:
            function(*args)
        return popen.call_count

    def status(self, job_id):
        return self.client.get(f"/api/jobs/{job_id}")

    def test_a_restarted_server_opens_runs_by_the_same_id(self):
        self.finish_transcript()
        job_id = self.client.get("/api/history").json[0]["id"]
        self.assertEqual(job_id, run_id(self.run_dir))
        self.start_server()
        response = self.status(job_id)  # no history request first: the page was open before the restart
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["text"], "આજે વાત\n")
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/english").status_code, 202)
        self.assertEqual(self.status("f" * 32).status_code, 404)

    def test_cut_short_transcription_resumes_after_a_restart(self):
        self.meta()
        self.checkpoint()
        entry = self.client.get("/api/history").json[0]
        self.assertEqual((entry["status"], entry["size"]), ("queued", 4))
        self.assertEqual(self.saved_meta()["resumes"], 1)
        self.finish_transcript()
        done = event(
            stage="complete", txt=str(self.run_dir / "talk.flex.txt"), json=str(self.run_dir / "talk.flex.json")
        )
        self.assertEqual(self.run_task(FakeWorker([done])), 1)
        self.assertEqual(self.status(entry["id"]).json["status"], "complete")
        self.assertNotIn("resumes", self.saved_meta())
        self.assertEqual(self.saved_meta()["pending"], ["gujlish"])

    def test_resuming_by_itself_stops_after_repeated_restarts(self):
        self.meta(resumes=3)
        entry = self.client.get("/api/history").json[0]
        self.assertEqual(entry["status"], "error")
        self.assertIn("3 server restarts", entry["message"])
        self.assertEqual(self.executor.tasks, [])
        self.assertEqual(self.client.post(f"/api/jobs/{entry['id']}/resume").status_code, 202)
        self.assertEqual(len(self.executor.tasks), 1)

    def test_a_failed_run_is_not_retried_by_itself(self):
        self.meta(failed="No speech was detected in this recording.")
        entry = self.client.get("/api/history").json[0]
        self.assertEqual((entry["status"], entry["message"]), ("error", "No speech was detected in this recording."))
        self.assertEqual(self.executor.tasks, [])

    def test_english_cut_short_resumes_after_a_restart(self):
        self.finish_transcript()
        self.meta(pending=["summary", "translation"], engine="auto")
        job_id = run_id(self.run_dir)
        self.assertEqual(self.status(job_id).json["translation"]["status"], "queued")
        _, (_, steps, _) = self.executor.tasks[0]
        self.assertEqual([key for key, _ in steps], ["summary", "translation"])

    def test_pause_and_resume(self):
        self.meta()
        self.checkpoint()
        job_id = self.client.get("/api/history").json[0]["id"]
        pause = f"/api/jobs/{job_id}/pause"
        self.assertEqual(self.client.post(pause).status_code, 200)  # still queued: paused at once
        self.assertEqual(self.status(job_id).json["status"], "paused")
        self.assertTrue(self.saved_meta()["paused"])
        self.assertEqual(self.run_task(), 0)  # the queued task no longer runs
        self.assertEqual(self.client.post(pause).status_code, 409)
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/resume").status_code, 202)
        self.assertNotIn("paused", self.saved_meta())
        progress = event(stage="transcribing", processed=30.0, chunks=1)
        worker = FakeWorker([progress], code=-15, on_wait=lambda: self.client.post(pause))
        self.run_task(worker)
        self.assertEqual(self.stopped, [worker])
        status = self.status(job_id).json
        self.assertEqual(status["status"], "paused")
        self.assertEqual(status["message"], "Paused at 00:30 of 01:00. Resume continues from there.")
        self.start_server()  # a paused run stays paused across a restart
        self.assertEqual(self.status(job_id).json["status"], "paused")
        self.assertEqual(self.executor.tasks, [])

    def wait_until_gone(self, path):
        for _ in range(100):
            if not path.exists():
                return
            time.sleep(0.02)
        self.fail(f"{path} was not deleted")

    def test_cancel_removes_the_run_and_its_recording(self):
        self.meta()
        job_id = self.client.get("/api/history").json[0]["id"]
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/delete").status_code, 200)
        self.assertEqual(self.status(job_id).status_code, 404)
        self.assertEqual(self.client.get("/api/history").json, [])
        self.wait_until_gone(self.run_dir)
        self.wait_until_gone(self.recording)
        self.assertEqual(self.run_task(), 0)

    def test_deleting_keeps_a_recording_another_run_uses(self):
        self.finish_transcript()
        other = self.run_dir.with_name("20261015-120000-000001")
        other.mkdir()
        for path in self.run_dir.iterdir():
            (other / path.name).write_bytes(path.read_bytes())
        job_id = run_id(self.run_dir)
        self.assertEqual(self.client.post(f"/api/jobs/{job_id}/delete").status_code, 200)
        self.wait_until_gone(self.run_dir)
        self.assertTrue(self.recording.exists())
        self.assertEqual([entry["run"] for entry in self.client.get("/api/history").json], [other.name])

    def test_stopping_the_english(self):
        self.finish_transcript()
        job_id = run_id(self.run_dir)
        english, stop = f"/api/jobs/{job_id}/english", f"/api/jobs/{job_id}/stop"
        self.assertEqual(self.client.post(english).status_code, 202)
        self.assertEqual(self.client.post(stop, json={"stage": "english"}).status_code, 200)  # stopped while queued
        status = self.status(job_id).json
        self.assertEqual((status["translation"]["message"], status["summary"]["status"]), (STOPPED, "idle"))
        self.assertNotIn("pending", json.loads((self.run_dir / RUN_FILE).read_text(encoding="utf-8")))
        self.assertEqual(self.run_task(), 0)
        self.assertEqual(self.client.post(english).status_code, 202)
        worker = FakeWorker([event(stage="translating", done=1, total=4)], code=-15)
        worker.on_wait = lambda: self.client.post(stop, json={"stage": "english"})
        self.assertEqual(self.run_task(worker), 1)  # the summary never starts
        self.assertEqual(self.stopped, [worker])
        status = self.status(job_id).json
        self.assertEqual((status["translation"]["status"], status["translation"]["message"]), ("idle", STOPPED))
        self.assertEqual(status["summary"]["status"], "idle")
        self.assertEqual(self.client.post(stop, json={"stage": "x"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
