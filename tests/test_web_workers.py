"""The web app's worker functions: how subprocess output becomes job progress, results and errors."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import DeferredExecutor
from web.server import create_app


class FakeWorker:
    """Stands in for subprocess.Popen: yields the given output lines, then exits with `code`."""

    def __init__(self, lines, code=0, on_wait=None):
        self.stdout = iter(line + "\n" for line in lines)
        self.code = code
        self.on_wait = on_wait

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def wait(self):
        if self.on_wait:
            self.on_wait()
        return self.code


def event(**fields) -> str:
    return "ASR_EVENT " + json.dumps(fields)


class TestWorkers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.run_dir = root / "transcripts" / "talk" / "flex-mixed" / "20261014-120000-000001"
        self.run_dir.mkdir(parents=True)
        self.txt = self.run_dir / "talk.flex.txt"
        self.json = self.run_dir / "talk.flex.json"
        self.txt.write_text("આજે વાત", encoding="utf-8")
        transcript = {"source": "talk.wav", "model": "flex", "mode": "mixed", "duration": 10.0, "segments": []}
        self.json.write_text(json.dumps(transcript), encoding="utf-8")
        patcher = patch("asr.storage.TRANSCRIPTS", root / "transcripts")
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", {"GEMINI_API_KEY": "g"})
        env.start()
        self.addCleanup(env.stop)
        self.executor = DeferredExecutor()
        self.app = create_app(executor=self.executor)
        self.client = self.app.test_client()
        self.job_id = self.client.get("/api/history").json[0]["id"]
        self.job = self.app.extensions["asr_jobs"][self.job_id]

    def run_task(self, worker):
        function, args = self.executor.tasks.pop()
        with patch("web.server.subprocess.Popen", return_value=worker) as popen:
            function(*args)
        return popen.call_args.args[0]

    def retry_transcription(self, worker):
        self.job.update(status="error", txt=None, json=None, duration=10.0, source="talk.wav")
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/retry").status_code, 202)
        return self.run_task(worker)

    def test_transcription_progress_and_completion(self):
        seen = []
        lines = [
            event(stage="loading", device="cpu"),
            "some log line",
            event(stage="transcribing", processed=4.0, chunks=2),
        ]

        def record_progress():
            seen.append(dict(self.job))

        lines.append(event(stage="complete", txt=str(self.txt), json=str(self.json)))
        command = self.retry_transcription(FakeWorker(lines, on_wait=record_progress))
        self.assertTrue(command[1].endswith("transcribe.py"))
        self.assertIn("--progress-json", command)
        self.assertEqual(seen[0]["device"], "cpu")
        self.assertEqual(seen[0]["processed"], 4.0)
        self.assertEqual(seen[0]["progress"], 40)
        self.assertEqual(seen[0]["message"], "Transcribing · 2 chunks completed")
        self.assertEqual(self.job["status"], "complete")
        self.assertEqual(self.job["progress"], 100)
        self.assertEqual(self.job["message"], "Transcript ready")
        self.assertEqual(self.job["txt"], str(self.txt))

    def test_transcription_failure_reports_the_last_error_line(self):
        self.retry_transcription(FakeWorker(["Error: first", "noise", "Error: ffmpeg is missing"], code=1))
        self.assertEqual(self.job["status"], "error")
        self.assertEqual(self.job["message"], "ffmpeg is missing")

    def test_transcription_without_output_falls_back_to_a_resume_hint(self):
        self.retry_transcription(FakeWorker(["killed"], code=0))
        self.assertEqual(self.job["status"], "error")
        self.assertEqual(self.job["message"], "Transcription stopped. You can retry to resume completed chunks.")

    def test_only_recent_output_is_searched_for_an_error(self):
        self.retry_transcription(FakeWorker(["Error: early", *[f"line {n}" for n in range(12)]], code=1))
        self.assertEqual(self.job["message"], "Transcription stopped. You can retry to resume completed chunks.")

    def test_a_failing_exit_code_wins_over_produced_output(self):
        done = event(stage="complete", txt=str(self.txt), json=str(self.json))
        self.retry_transcription(FakeWorker([done], code=1))
        self.assertEqual(self.job["status"], "error")
        produced = self.run_dir / "talk.flex.en.txt"
        produced.write_text("Today's talk", encoding="utf-8")
        self.job["status"] = "complete"
        self.start_translation()
        self.run_task(FakeWorker([], code=1))
        self.assertEqual(self.job["translation"]["status"], "error")

    def start_translation(self):
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/translate", json={}).status_code, 202)

    def test_stage_progress_and_completion(self):
        seen = []
        produced = self.run_dir / "talk.flex.en.txt"

        def finish():
            seen.append(dict(self.job["translation"]))
            produced.write_text("Today's talk", encoding="utf-8")

        self.start_translation()
        lines = [event(stage="translating", done=1, total=4), event(stage="translating", done=0, total=0)]
        command = self.run_task(FakeWorker(lines, on_wait=finish))
        self.assertTrue(command[1].endswith("translate.py"))
        self.assertEqual(command[2:], [str(self.json), "--progress-json"])
        self.assertEqual(seen[0], {"status": "running", "progress": 25, "message": "Translating · 1 of 4"})
        self.assertEqual(self.job["translation"], {"status": "complete", "progress": 100, "message": "Ready"})

    def test_stage_failure_reports_error_or_resume_hint(self):
        self.start_translation()
        self.run_task(FakeWorker(["Error: HTTP 429 from Gemini"], code=1))
        self.assertEqual(self.job["translation"]["status"], "error")
        self.assertEqual(self.job["translation"]["message"], "HTTP 429 from Gemini")
        self.start_translation()
        self.run_task(FakeWorker([], code=0))  # exits cleanly but writes nothing
        self.assertEqual(self.job["translation"]["message"], "Translating stopped. Start it again to resume.")

    def test_summary_stage_checks_the_summary_file(self):
        (self.run_dir / "talk.flex.en.txt").write_text("Today's talk", encoding="utf-8")
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/summarize", json={}).status_code, 202)
        summary = self.run_dir / "talk.flex.summary.md"
        lines = [event(stage="summarizing", done=2, total=3)]
        command = self.run_task(FakeWorker(lines, on_wait=lambda: summary.write_text("# T", encoding="utf-8")))
        self.assertTrue(command[1].endswith("summarize.py"))
        self.assertEqual(self.job["summary"]["status"], "complete")


if __name__ == "__main__":
    unittest.main()
