"""One stored copy per recording: identical uploads share a file, a stored recording needs no upload, and a hosted
site merges copies stored before this existed."""

import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import DeferredExecutor
from web.recordings import fingerprint
from web.server import RUN_FILE, create_app, run_id
from web.settings import Settings

OLD = 1_600_000_000  # a modification time well past the leftover age


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.recordings, self.transcripts = self.root / "recordings", self.root / "transcripts"
        for name, value in (("RECORDINGS", self.recordings), ("TRANSCRIPTS", self.transcripts), ("DATA", self.root)):
            patcher = patch(f"asr.storage.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def app(self, **settings):
        self.executor = DeferredExecutor()
        app = create_app(executor=self.executor, settings=Settings(**settings))
        app.config["SESSION_COOKIE_SECURE"] = False
        return app


class TestUploads(Base):
    def tone(self, frequency):
        path = self.root / f"{frequency}.wav"
        command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=3", str(path)]
        subprocess.run(command, check=True)
        return path

    def test_the_same_audio_is_stored_once(self):
        audio = self.tone(440)
        app = self.app()
        client, jobs = app.test_client(), app.extensions["asr_jobs"]

        def send(name, data=None):
            return client.post("/api/jobs", data=data or {"audio": (io.BytesIO(audio.read_bytes()), name)})

        first, second = send("talk.wav").json["id"], send("renamed.wav").json["id"]
        self.assertEqual([jobs[first]["name"], jobs[second]["name"]], ["talk.wav", "renamed.wav"])
        self.assertEqual(jobs[second]["source"], str(self.recordings / "talk.wav"))
        self.assertEqual(sorted(path.name for path in self.recordings.iterdir()), [".fingerprints.json", "talk.wav"])

        sha = fingerprint(audio)
        status = client.get(f"/api/recordings/{sha}").json
        self.assertTrue(status["stored"])
        self.assertEqual(sorted(run["name"] for run in status["runs"]), ["renamed.wav", "talk.wav"])
        response = send(None, {"fingerprint": sha, "name": "third.wav"})  # no upload at all
        self.assertEqual(response.status_code, 202)
        self.assertEqual(jobs[response.json["id"]]["source"], str(self.recordings / "talk.wav"))
        meta = json.loads(Path(jobs[response.json["id"]]["run_dir"], RUN_FILE).read_text(encoding="utf-8"))
        self.assertEqual((meta["name"], meta["source"], meta["sha256"]), ("third.wav", "talk.wav", sha))

        missing = send(None, {"fingerprint": "0" * 64, "name": "x.wav"})
        self.assertEqual((missing.status_code, missing.json["upload"]), (409, True))
        self.assertEqual(client.get("/api/recordings/not-a-fingerprint").json, {"stored": False, "runs": []})
        other = send(None, {"audio": (io.BytesIO(self.tone(880).read_bytes()), "talk.wav")})
        self.assertEqual(jobs[other.json["id"]]["source"], str(self.recordings / "talk-2.wav"))


class TestMerging(Base):
    def recording(self, name, data, old=True):
        path = self.recordings / name
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        if old:
            os.utime(path, (OLD, OLD))
        return path

    def add_run(self, source, when, finished=True, meta=None):
        run_dir = self.transcripts / Path(source).stem / "flex-mixed" / when
        run_dir.mkdir(parents=True)
        if finished:
            stem = f"{Path(source).stem}.flex"
            data = {"source": source, "model": "flex", "mode": "mixed", "duration": 5.0, "segments": []}
            (run_dir / f"{stem}.json").write_text(json.dumps(data), encoding="utf-8")
            (run_dir / f"{stem}.txt").write_text("આજે\n", encoding="utf-8")
        if meta:
            (run_dir / RUN_FILE).write_text(json.dumps(meta), encoding="utf-8")
        return run_dir

    def test_a_hosted_site_merges_copies_and_clears_leftovers(self):
        kept = self.recording("talk.m4a", b"same audio")
        copy = self.recording("talk-2.m4a", b"same audio", old=False)
        self.recording("forgotten.m4a", b"nobody uses this")
        fresh = self.recording("uploading.m4a", b"an upload under way", old=False)
        busy = [self.recording(name, b"in progress") for name in ("a.m4a", "b.m4a")]
        first, second = (
            self.add_run("talk.m4a", "20261101-100000-000001"),
            self.add_run("talk-2.m4a", "20261102-100000-000001"),
        )
        leftover = self.transcripts / "lost" / "flex-mixed" / "20261102-110000-000001"  # its job never started
        leftover.mkdir(parents=True)
        (leftover / ".owner").write_text("someone", encoding="utf-8")
        unfinished = {"source": "b.m4a", "model": "flex", "mode": "mixed", "duration": 5.0, "paused": True}
        self.add_run("b.m4a", "20261102-120000-000001", finished=False, meta=unfinished)
        self.add_run("a.m4a", "20261102-130000-000001")

        app = self.app(hosted=True, secret_key="x" * 40)
        app.test_client().get("/health")  # the first request after a start
        self.assertTrue(kept.exists())
        self.assertFalse(copy.exists())
        self.assertEqual(json.loads((second / RUN_FILE).read_text(encoding="utf-8")), {"recording": "talk.m4a"})
        jobs = app.extensions["asr_jobs"]
        self.assertEqual(jobs[run_id(second)]["source"], str(kept))
        self.assertEqual(jobs[run_id(second)]["name"], "talk-2.m4a")  # still shown under the name it was given
        self.assertEqual(jobs[run_id(first)]["source"], str(kept))
        self.assertFalse((self.recordings / "forgotten.m4a").exists())
        self.assertFalse(leftover.exists())
        self.assertTrue(fresh.exists())
        self.assertTrue(all(path.exists() for path in busy))  # an unfinished run still reads its own copy

    def test_local_use_never_removes_recordings(self):
        copies = [self.recording(name, b"same audio") for name in ("x.m4a", "y.m4a")]
        self.app().test_client().get("/health")
        self.assertTrue(all(path.exists() for path in copies))


if __name__ == "__main__":
    unittest.main()
