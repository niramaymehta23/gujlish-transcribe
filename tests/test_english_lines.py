"""The English under each Gujarati line, and the furthest finished step in the talk list."""

import json
import unittest

from tests import test_web_edits


class TestEnglishLines(unittest.TestCase):
    setUp = test_web_edits.TestEdits.setUp  # the same small finished talk as the edit tests
    status = test_web_edits.TestEdits.status

    def test_status_carries_english_per_segment(self):
        self.assertNotIn("english_segments", self.status())  # nothing translated yet
        # A segment without text is never translated, so the English lines up with the segments that have text.
        data = json.loads(self.json.read_text(encoding="utf-8"))
        data["segments"].insert(1, {"start": 4, "end": 4, "text": " "})
        self.json.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        english = {"kind": "translation", "segments": [
            {"start": 0, "end": 4, "source": "નીરમય આવ્યો, નીરમય!", "text": "Nirmay came, Nirmay!"},
            {"start": 4, "end": 6, "source": "હા નીરમય", "text": "Yes Nirmay"},
        ]}  # fmt: skip
        (self.run_dir / "talk.flex.en.json").write_text(json.dumps(english, ensure_ascii=False), encoding="utf-8")
        (self.run_dir / "talk.flex.en.txt").write_text("Nirmay came", encoding="utf-8")
        self.assertEqual(self.status()["english_segments"], ["Nirmay came, Nirmay!", None, "Yes Nirmay"])
        # A sample translation covers only the first lines; the rest have no English.
        english["segments"] = english["segments"][:1]
        (self.run_dir / "talk.flex.en.json").write_text(json.dumps(english, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.status()["english_segments"], ["Nirmay came, Nirmay!", None, None])

    def test_history_names_the_furthest_finished_step(self):
        entry = lambda: self.client.get("/api/history").json[0]  # noqa: E731
        self.assertNotIn("translation", entry())
        (self.run_dir / "talk.flex.en.txt").write_text("Nirmay came", encoding="utf-8")
        self.assertEqual(entry()["translation"]["status"], "complete")
        (self.run_dir / "talk.flex.summary.md").write_text("# Talk\n", encoding="utf-8")
        self.assertEqual((entry()["summary"]["status"], "translation" in entry()), ("complete", False))

    def test_history_shows_the_english_when_the_summary_failed(self):
        (self.run_dir / "talk.flex.en.txt").write_text("Nirmay came", encoding="utf-8")
        self.app.extensions["asr_jobs"][self.job_id]["summary"] = {"status": "error", "message": "Gemini was busy."}
        entry = self.client.get("/api/history").json[0]
        self.assertEqual((entry["summary"]["status"], entry["translation"]["status"]), ("error", "complete"))


if __name__ == "__main__":
    unittest.main()
