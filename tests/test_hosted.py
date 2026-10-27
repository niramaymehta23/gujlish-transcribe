"""Hosted mode: password, per-visitor privacy, limits, chunked uploads and retention."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import DeferredExecutor
from web.server import create_app
from web.settings import Settings

SECRET = "x" * 40


class HostedBase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / "talk.wav"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=12",
                "-ar",
                "16000",
                self.source,
            ],
            check=True,
        )
        for name, value in (("RECORDINGS", self.root / "recordings"), ("TRANSCRIPTS", self.root / "transcripts")):
            patcher = patch(f"asr.storage.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        uploads = patch("web.server.tempfile.gettempdir", return_value=str(self.root / "tmp"))
        uploads.start()
        self.addCleanup(uploads.stop)

    def app(self, **overrides):
        options = dict(
            hosted=True, secret_key=SECRET, max_upload_mb=5, max_minutes=90, retention_hours=24, allow_cloud_llm=False
        )
        options.update(overrides)
        self.executor = DeferredExecutor()
        app = create_app(executor=self.executor, settings=Settings(**options))
        app.config["SESSION_COOKIE_SECURE"] = False  # the test client uses plain http
        return app

    def upload(self, client, data: bytes, piece: int = 2 * 1024**2) -> str:
        upload_id = client.post("/api/uploads").json["id"]
        for offset in range(0, len(data), piece):
            response = client.put(f"/api/uploads/{upload_id}?offset={offset}", data=data[offset : offset + piece])
            self.assertEqual(response.status_code, 200, response.json)
        return upload_id

    def start_job(self, client, model="flex", name="talk.wav"):
        upload_id = self.upload(client, self.source.read_bytes())
        return client.post("/api/jobs", data={"upload_id": upload_id, "name": name, "model": model})


class TestSettings(unittest.TestCase):
    def test_hosted_defaults_and_validation(self):
        with patch.dict("os.environ", {"GUJLISH_HOSTED": "1", "GUJLISH_SECRET_KEY": SECRET}, clear=True):
            settings = Settings.from_env()
        self.assertEqual((settings.max_upload_mb, settings.max_minutes, settings.retention_hours), (300, 90, 24))
        self.assertFalse(settings.allow_cloud_llm)
        with patch.dict("os.environ", {"GUJLISH_HOSTED": "1"}, clear=True), self.assertRaises(ValueError):
            Settings.from_env()  # no secret key
        env = {"GUJLISH_MODELS": "gujlish,whisper"}
        with patch.dict("os.environ", env, clear=True), self.assertRaises(ValueError):
            Settings.from_env()
        with patch.dict("os.environ", {}, clear=True):
            self.assertEqual(Settings.from_env(), Settings())  # local use needs no settings


class TestPassword(HostedBase):
    def test_password_gate(self):
        client = self.app(password="open sesame").test_client()
        self.assertEqual(client.get("/").status_code, 302)
        self.assertEqual(client.get("/api/history").status_code, 401)
        self.assertEqual(client.get("/healthz").status_code, 200)
        with patch("web.server.time.sleep") as slowed:
            self.assertEqual(client.post("/login", data={"password": "wrong"}).status_code, 401)
        slowed.assert_called_once()
        self.assertEqual(client.post("/login", data={"password": "open sesame"}).status_code, 302)
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/api/history").status_code, 200)

    def test_no_password_means_open(self):
        client = self.app().test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/login").status_code, 302)


class TestUploadsAndPrivacy(HostedBase):
    def test_chunked_upload_rebuilds_the_file(self):
        client = self.app().test_client()
        response = self.start_job(client)
        self.assertEqual(response.status_code, 202, response.json)
        job = self.executor.tasks[0][1]
        saved = self.root / "recordings" / "talk.wav"
        self.assertEqual(saved.read_bytes(), self.source.read_bytes())
        self.assertEqual(Path(job[2], ".owner").exists(), True)
        self.assertEqual(list((self.root / "tmp" / "gujlish-uploads").glob("*")), [])

    def test_out_of_order_and_oversized_pieces(self):
        client = self.app(max_upload_mb=1).test_client()
        upload_id = client.post("/api/uploads").json["id"]
        response = client.put(f"/api/uploads/{upload_id}?offset=10", data=b"abc")
        self.assertEqual((response.status_code, response.json["received"]), (409, 0))
        self.assertEqual(client.put(f"/api/uploads/{upload_id}?offset=0", data=b"abc").json["received"], 3)
        self.assertEqual(client.put(f"/api/uploads/{upload_id}?offset=3", data=b"x" * 1024**2).status_code, 413)
        self.assertEqual(client.put(f"/api/uploads/{upload_id}?offset=0", data=b"abc").status_code, 404)  # discarded
        other = self.app().test_client()
        upload_id = client.post("/api/uploads").json["id"]
        self.assertEqual(other.put(f"/api/uploads/{upload_id}?offset=0", data=b"a").status_code, 404)

    def test_visitors_see_only_their_own_runs(self):
        app = self.app()
        owner, stranger = app.test_client(), app.test_client()
        job_id = self.start_job(owner).json["id"]
        job = app.extensions["asr_jobs"][job_id]
        txt = Path(job["run_dir"]) / "talk.flex.txt"
        txt.write_text("આજે machine learning\n", encoding="utf-8")
        (txt.with_suffix(".json")).write_text(
            '{"source": "talk.wav", "model": "flex", "mode": "mixed", "duration": 12, "segments": []}', encoding="utf-8"
        )
        job.update(status="complete", txt=str(txt), json=str(txt.with_suffix(".json")))
        self.assertEqual(owner.get(f"/api/jobs/{job_id}").status_code, 200)
        self.assertNotIn("owner", owner.get(f"/api/jobs/{job_id}").json)
        for path in (f"/api/jobs/{job_id}", f"/api/jobs/{job_id}/download/txt"):
            self.assertEqual(stranger.get(path).status_code, 404)
        self.assertEqual(stranger.post(f"/api/jobs/{job_id}/retry").status_code, 404)
        self.assertEqual(len(owner.get("/api/history").json), 1)
        self.assertEqual(stranger.get("/api/history").json, [])

    def test_limits(self):
        client = self.app(max_minutes=0.1).test_client()  # 6 seconds
        response = self.start_job(client)
        self.assertEqual(response.status_code, 400)
        self.assertIn("0 minutes", response.json["error"])
        self.assertEqual(list((self.root / "recordings").glob("*")), [])
        client = self.app(models=("gujlish",)).test_client()
        self.assertEqual(self.start_job(client, model="flex").status_code, 400)
        self.assertEqual(self.start_job(client, model="gujlish").status_code, 202)
        self.assertEqual(self.start_job(client, model="gujlish").status_code, 429)  # one active run per visitor
        page = client.get("/").get_data(as_text=True)
        self.assertNotIn('value="flex"', page)
        self.assertNotIn("Bodhan", page)


class TestHostedLLM(HostedBase):
    def test_local_models_are_hidden_and_cloud_needs_permission(self):
        client = self.app().test_client()
        with patch.dict("os.environ", {"ANTHROPIC_API_KEY": "test"}):
            caps = client.get("/api/capabilities").json
            self.assertEqual((caps["local"], caps["claude"], caps["gemini"]), (False, False, False))
            caps = self.app(allow_cloud_llm=True).test_client().get("/api/capabilities").json
            self.assertTrue(caps["claude"])
        job_id = self.start_job(client).json["id"]
        response = client.post(f"/api/jobs/{job_id}/translate", json={"llm": "local"})
        self.assertEqual(response.status_code, 400)


class TestRetentionAndPage(HostedBase):
    def test_old_runs_are_deleted(self):
        transcripts = self.root / "transcripts"
        old = transcripts / "old" / "flex-mixed" / "20200101-000000-000000"
        old.mkdir(parents=True)
        (old / "old.flex.txt").write_text("x")
        client = self.app().test_client()
        self.start_job(client)
        self.assertFalse(old.exists())
        self.assertFalse((transcripts / "old").exists())
        self.assertEqual(len(list(transcripts.glob("talk/flex-mixed/*"))), 1)  # the fresh run is kept

    def test_page_wording(self):
        page = self.app().test_client().get("/").get_data(as_text=True)
        self.assertIn("Online workspace", page)
        self.assertIn("Built with Indic-Transcribe Flex from Bodhan AI / AI4Bharat.", page)
        self.assertIn("deleted after 24 hours", page)
        self.assertNotIn("Processed on this computer", page)


if __name__ == "__main__":
    unittest.main()
