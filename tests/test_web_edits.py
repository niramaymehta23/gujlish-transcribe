"""Word fixes from the browser, the dictionary endpoints, Gujlish suggestions and the stages that follow."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import DeferredExecutor
from transcriber.speech import align
from web.server import create_app


class TestEdits(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.run_dir = root / "transcripts" / "talk" / "flex-mixed" / "20261014-120000-000001"
        self.run_dir.mkdir(parents=True)
        self.json = self.run_dir / "talk.flex.json"
        segments = [{"start": 0, "end": 4, "text": "નીરમય આવ્યો, નીરમય!"}, {"start": 4, "end": 6, "text": "હા નીરમય"}]
        self.json.write_text(json.dumps({"source": "talk.wav", "model": "flex", "mode": "mixed", "duration": 6.0,
                                         "segments": segments}, ensure_ascii=False), encoding="utf-8")  # fmt: skip
        (self.run_dir / "talk.flex.txt").write_text("નીરમય આવ્યો, નીરમય! હા નીરમય\n", encoding="utf-8")
        gujlish = {"kind": "gujlish", "engine": "indicxlit", "segments": [
            {"start": 0, "end": 4, "tokens": ["nirmay", "aavyo,", "nirmay!"]}, {"start": 4, "end": 6, "tokens": ["ha", "nirmay"]}
        ]}  # fmt: skip
        (self.run_dir / "talk.flex.gujlish.json").write_text(json.dumps(gujlish), encoding="utf-8")
        (self.run_dir / "talk.flex.gujlish.txt").write_text("nirmay aavyo, nirmay! ha nirmay\n", encoding="utf-8")
        for name, value in (("TRANSCRIPTS", root / "transcripts"), ("DATA", root), ("RECORDINGS", root / "recordings")):
            patcher = patch(f"transcriber.files.storage.{name}", value)
            patcher.start()
            self.addCleanup(patcher.stop)
        no_engine = patch("web.server.xlit_installed", return_value=False)  # suggestions from known words only
        no_engine.start()
        self.addCleanup(no_engine.stop)
        env = patch.dict("os.environ", {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        self.dictionary = root / "dictionary.json"
        self.executor = DeferredExecutor()
        self.app = create_app(executor=self.executor)
        self.client = self.app.test_client()
        self.job_id = self.client.get("/api/history").json[0]["id"]

    def edit(self, **body):
        fields = {"segment": 0, "start": 0, "end": 1, "heard": "નીરમય", "correct": "નિરામય"}
        return self.client.post(f"/api/jobs/{self.job_id}/edit", json={**fields, **body})

    def status(self):
        return self.client.get(f"/api/jobs/{self.job_id}").json

    def test_status_carries_segments_and_aligned_gujlish(self):
        status = self.status()
        self.assertEqual(
            status["segments"][1], {"start": 4, "end": 6, "text": "હા નીરમય", "fixes": [], "suggestions": []}
        )
        self.assertEqual(status["gujlish_tokens"], [["nirmay", "aavyo,", "nirmay!"], ["ha", "nirmay"]])
        self.assertEqual(status["gujlish"]["status"], "complete")

    def test_fix_everywhere_rewrites_files_and_remembers(self):
        response = self.edit(correct_gujlish="Niramay", everywhere=True)
        self.assertEqual(response.json, {"replaced": 3, "remembered": True})
        self.assertEqual(
            (self.run_dir / "talk.flex.txt").read_text(encoding="utf-8"), "નિરામય આવ્યો, નિરામય! હા નિરામય\n"
        )
        self.assertIn("નિરામય આવ્યો", (self.run_dir / "talk.flex.srt").read_text(encoding="utf-8"))
        status = self.status()
        self.assertEqual(status["gujlish_tokens"], [["Niramay", "aavyo,", "Niramay!"], ["ha", "Niramay"]])
        self.assertEqual(status["gujlish_text"], "Niramay aavyo, Niramay! ha Niramay\n")
        saved = json.loads(self.dictionary.read_text(encoding="utf-8"))
        correction = saved["corrections"][0]
        self.assertEqual(
            (correction["heard"], correction["correct"], correction["heard_gujlish"]), ("નીરમય", "નિરામય", "nirmay")
        )
        self.assertEqual(correction["examples"][0]["context"], "નીરમય આવ્યો, નીરમય!")
        self.assertNotIn("clip", correction["examples"][0])  # the recording is not on disk here
        self.assertEqual(saved["gujlish"], {"નિરામય": "Niramay"})

    def test_single_fix_without_remembering(self):
        self.assertEqual(self.edit(start=2, end=3, heard="નીરમય!", remember=False).json["replaced"], 1)
        self.assertEqual(self.status()["segments"][0]["text"], "નીરમય આવ્યો, નિરામય!")
        self.assertFalse(self.dictionary.exists())

    def test_stale_or_invalid_edits_are_refused(self):
        self.assertEqual(self.edit(heard="બીજું").status_code, 409)
        self.assertEqual(self.edit(end=9).status_code, 409)
        self.assertEqual(self.edit(segment=7).status_code, 409)
        self.assertEqual(self.edit(correct="  ").status_code, 400)
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/edit", json={}).status_code, 400)
        self.assertEqual(self.client.post("/api/jobs/nope/edit", json={}).status_code, 404)
        self.app.extensions["asr_jobs"][self.job_id]["gujlish"] = {"status": "running"}
        self.assertEqual(self.edit().status_code, 409)

    def test_english_made_before_a_fix_is_marked_stale(self):
        english = {"kind": "translation", "segments": [
            {"start": 0, "end": 4, "source": "નીરમય આવ્યો, નીરમય!", "text": "Nirmay came, Nirmay!"},
            {"start": 4, "end": 6, "source": "હા નીરમય", "text": "Yes Nirmay"},
        ]}  # fmt: skip
        (self.run_dir / "talk.flex.en.json").write_text(json.dumps(english, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / "talk.flex.en.txt").write_text("Nirmay came", encoding="utf-8")
        self.assertFalse(self.status()["english_stale"])
        self.edit()
        self.assertTrue(self.status()["english_stale"])

    def test_english_of_an_older_transcript_is_current_without_its_cut_stops(self):
        # Transcripts made before cut full stops were dropped still have them; the translator reads them without.
        data = json.loads(self.json.read_text(encoding="utf-8"))
        data["segments"][1]["text"] = "હા નીરમય."
        self.json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        english = {"kind": "translation", "segments": [
            {"start": 0, "end": 4, "source": "નીરમય આવ્યો, નીરમય!", "text": "Nirmay came, Nirmay!"},
            {"start": 4, "end": 6, "source": "હા નીરમય", "text": "Yes Nirmay"},
        ]}  # fmt: skip
        (self.run_dir / "talk.flex.en.json").write_text(json.dumps(english, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / "talk.flex.en.txt").write_text("Nirmay came", encoding="utf-8")
        self.assertFalse(self.status()["english_stale"])

    def test_dictionary_listing_and_forgetting(self):
        self.edit(correct_gujlish="niramay")
        entries = self.client.get("/api/dictionary").json
        self.assertEqual(entries["corrections"][0]["examples"], 1)
        self.assertEqual(entries["gujlish"], {"નિરામય": "niramay"})
        forget = "/api/dictionary/forget"
        self.assertEqual(
            self.client.post(forget, json={"correction": entries["corrections"][0]["id"]}).status_code, 200
        )
        self.assertEqual(self.client.post(forget, json={"gujlish": "નિરામય"}).status_code, 200)
        self.assertEqual(self.client.post(forget, json={"gujlish": "નિરામય"}).status_code, 404)
        self.assertEqual(self.client.get("/api/dictionary").json, {"corrections": [], "gujlish": {}})

    def test_a_fix_asks_in_future_unless_always_and_the_list_can_switch_it(self):
        self.edit()
        entry = self.client.get("/api/dictionary").json["corrections"][0]
        self.assertEqual((entry["mode"], entry["rejected"]), ("ask", 0))
        mode = "/api/dictionary/mode"
        self.assertEqual(self.client.post(mode, json={"correction": entry["id"], "mode": "always"}).status_code, 200)
        self.assertEqual(self.client.get("/api/dictionary").json["corrections"][0]["mode"], "always")
        self.assertEqual(self.client.post(mode, json={"correction": entry["id"], "mode": "x"}).status_code, 404)
        self.assertEqual(self.client.post(mode, json={"correction": "nope", "mode": "ask"}).status_code, 404)
        self.edit(start=2, end=3, heard="નીરમય!", always=True)
        self.client.post(mode, json={"correction": entry["id"], "mode": "ask"})
        self.edit(segment=1, start=1, end=2, heard="નીરમય", always=False)
        self.assertEqual(self.client.get("/api/dictionary").json["corrections"][0]["mode"], "ask")

    def test_rejecting_a_suggestion_keeps_the_words_and_records_where(self):
        self.edit(start=2, end=3, heard="નીરમય!", remember=True)
        rule = json.loads(self.dictionary.read_text(encoding="utf-8"))["corrections"][0]["id"]
        data = json.loads(self.json.read_text(encoding="utf-8"))
        data["segments"][1]["suggestions"] = [
            {"start": 1, "end": 2, "correct": "નિરામય", "correct_gujlish": "", "rule": rule}
        ]
        self.json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.status()["segments"][1]["suggestions"][0]["rule"], rule)
        dismiss = f"/api/jobs/{self.job_id}/dismiss"
        self.assertEqual(
            self.client.post(dismiss, json={"segment": 1, "start": 0, "end": 1, "rule": rule}).status_code, 409
        )
        self.assertEqual(self.client.post(dismiss, json={"segment": 1}).status_code, 400)
        self.assertEqual(
            self.client.post(dismiss, json={"segment": 1, "start": 1, "end": 2, "rule": rule}).json, {"dismissed": True}
        )
        segment = self.status()["segments"][1]
        self.assertEqual((segment["text"], segment["suggestions"]), ("હા નીરમય", []))
        saved = json.loads(self.dictionary.read_text(encoding="utf-8"))["corrections"][0]
        self.assertEqual(saved["rejected"][0]["context"], "હા નીરમય")
        self.assertEqual(self.client.get("/api/dictionary").json["corrections"][0]["rejected"], 1)
        self.assertEqual(
            self.client.post(dismiss, json={"segment": 1, "start": 1, "end": 2, "rule": rule}).status_code, 409
        )

    def test_suggestions_use_your_words(self):
        self.edit(correct_gujlish="niramay")
        response = self.client.get("/api/transliterate", query_string={"to": "gu", "text": "Niramay"})
        self.assertEqual(response.json["options"], ["નિરામય"])
        response = self.client.get("/api/transliterate", query_string={"to": "latin", "text": "નિરામય છે"})
        self.assertEqual(response.json["options"], ["niramay che"])
        self.assertEqual(self.client.get("/api/transliterate?to=xx&text=a").status_code, 400)

    def test_gujlish_and_english_stages_queue_their_scripts(self):
        response = self.client.post(f"/api/jobs/{self.job_id}/gujlish", json={"engine": "gemini"})
        self.assertEqual(response.status_code, 400)  # no Gemini key here
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/gujlish", json={"engine": "x"}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/gujlish", json={}).status_code, 202)
        (key, command), *_ = self.executor.tasks[-1][1][1]
        self.assertTrue(command[1].endswith("transliterate.py"))
        self.assertEqual(command[-4:], ["--dictionary", str(self.dictionary), "--engine", "auto"])
        with patch.dict("os.environ", {"GEMINI_API_KEY": "g"}):
            self.assertEqual(self.client.post(f"/api/jobs/{self.job_id}/english", json={}).status_code, 202)
        steps = self.executor.tasks[-1][1][1]
        self.assertEqual([key for key, _ in steps], ["translation", "captions", "summary"])
        self.assertIn("--no-captions", steps[0][1])
        self.assertTrue(steps[1][1][1].endswith("captions.py"))
        self.assertEqual(self.status()["summary"]["status"], "queued")

    def test_a_failed_translation_does_not_leave_the_summary_queued(self):
        with patch.dict("os.environ", {"GEMINI_API_KEY": "g"}):
            self.client.post(f"/api/jobs/{self.job_id}/english", json={})
        function, args = self.executor.tasks.pop()
        with patch("web.server.subprocess.Popen") as popen:
            popen.return_value.__enter__.return_value.stdout = iter(["Error: quota\n"])
            popen.return_value.__enter__.return_value.wait.return_value = 1
            function(*args)
        self.assertEqual(popen.call_count, 1)
        status = self.status()
        self.assertEqual(status["translation"]["message"], "quota")
        self.assertEqual(status["summary"]["status"], "idle")

    def test_gujlish_downloads(self):
        with self.client.get(f"/api/jobs/{self.job_id}/download/gujlish-txt") as response:
            self.assertEqual(response.get_data(as_text=True), "nirmay aavyo, nirmay! ha nirmay\n")
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}/download/gujlish-docx").status_code, 200)
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}/download/gujlish-srt").status_code, 404)

    def word_audio(self, **args):
        query = "&".join(f"{key}={value}" for key, value in {"segment": 0, "start": 1, "end": 2, **args}.items())
        return self.client.get(f"/api/jobs/{self.job_id}/word-audio?{query}")

    def test_word_audio_plays_the_aligned_words(self):
        recording = Path(self.temp.name) / "recordings" / "talk.wav"
        recording.parent.mkdir()
        recording.write_bytes(b"audio")
        times = [{"start": 0.5, "end": 1.0}, {"start": 1.4, "end": 2.2}, {"start": 2.5, "end": 3.6}]
        aligner = patch("web.server.align.Aligner")
        model = aligner.start()
        self.addCleanup(aligner.stop)
        model.return_value.align.return_value = times
        cut = patch("web.server.clip_audio", return_value=b"RIFF")
        clip_audio = cut.start()
        self.addCleanup(cut.stop)
        with patch("web.server.align.read_audio", return_value=b""):
            response = self.word_audio()
            again = self.word_audio(start=0, end=3)
        self.assertEqual((response.status_code, response.mimetype), (200, "audio/wav"))
        self.assertEqual(response.headers["X-Word-Timing"], "aligned")
        self.assertEqual(response.data, b"RIFF")
        self.assertEqual(again.headers["X-Word-Timing"], "aligned")
        self.assertEqual(clip_audio.call_args_list[0].args, (recording, 1.0, 2.6))  # 0.4 s around the word
        self.assertEqual(clip_audio.call_args_list[1].args, (recording, 0.1, 4.0))
        self.assertEqual((model.call_count, model.return_value.align.call_count), (1, 1))  # loaded and aligned once

    def test_word_audio_estimates_when_the_model_is_missing(self):
        recording = Path(self.temp.name) / "recordings" / "talk.wav"
        recording.parent.mkdir()
        recording.write_bytes(b"audio")
        with (
            patch("web.server.align.Aligner", side_effect=OSError("offline")),
            patch("web.server.clip_audio", return_value=b"RIFF") as clip_audio,
        ):
            response = self.word_audio(start=0, end=1)
        self.assertEqual(response.headers["X-Word-Timing"], "estimated")
        rough = align.estimate_times("નીરમય આવ્યો, નીરમય!", 0, 4)
        self.assertEqual(clip_audio.call_args.args, (recording, 0.0, rough[0]["end"] + 1.0))  # a second around

    def test_word_audio_needs_the_recording_and_current_words(self):
        self.assertEqual(self.word_audio().status_code, 404)  # the recording is not on disk
        recording = Path(self.temp.name) / "recordings" / "talk.wav"
        recording.parent.mkdir()
        recording.write_bytes(b"audio")
        self.assertEqual(self.word_audio(start=2, end=4).status_code, 409)
        self.assertEqual(self.word_audio(segment=5).status_code, 409)
        self.assertEqual(self.word_audio(start="x").status_code, 400)

    def test_estimate_times_shares_the_line_by_length(self):
        self.assertEqual(align.estimate_times("ab cd", 0, 5), [{"start": 0, "end": 2}, {"start": 3, "end": 5}])
        self.assertEqual(align.estimate_times("", 0, 5), [])


if __name__ == "__main__":
    unittest.main()
