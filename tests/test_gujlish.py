"""Gujlish: word-for-word Latin spellings, their priority order, and the files written next to a transcript."""

import json
import tempfile
import unittest
from pathlib import Path

from asr.gujlish import COMMON, GeminiEngine, Transliterator, casual, latin_token, run_gujlish
from asr.llm import Completion


class FakeEngine:
    name = "fake"

    def __init__(self):
        self.asked = []

    def latin(self, words):
        self.asked += words
        return {word: f"<{word}>" for word in words}

    def gujarati_options(self, word, count):
        return [f"[{word}]"]


class TestSpellings(unittest.TestCase):
    def test_casual_spellings(self):
        self.assertEqual(casual("kareene"), "karine")
        self.assertEqual(casual("aneey"), "ane")
        self.assertEqual(casual("aatmeey", "આત્મીય"), "aatmiy")  # a final ય is a real "y"
        self.assertEqual(casual("Pachhee"), "pachi")

    def test_tokens_keep_punctuation_digits_and_english(self):
        spellings = {"જય": "jay", "માં": "ma"}
        self.assertEqual(latin_token("(જય).", spellings), "(jay).")
        self.assertEqual(latin_token("૧૨", spellings), "12")
        self.assertEqual(latin_token("Assemblyમાં", spellings), "Assemblyma")
        self.assertEqual(latin_token("Unique", spellings), "Unique")

    def test_your_spellings_beat_common_words_beat_the_engine(self):
        engine = FakeEngine()
        words = Transliterator(engine, overrides={"છે": "chhe"})
        self.assertEqual(words.latin_text("આ છે નવું."), "aa chhe <નવું>.")
        self.assertEqual(engine.asked, ["નવું"])
        self.assertEqual(COMMON["છે"], "che")

    def test_gujarati_options_put_known_words_first(self):
        words = Transliterator(FakeEngine(), overrides={"નિરામય": "niramay"})
        self.assertEqual(words.gujarati_options("Niramay"), ["નિરામય", "[niramay]"])
        self.assertEqual(words.gujarati_options("che"), ["છે", "[che]"])
        self.assertEqual(words.gujarati_options("kem che"), ["કેમ છે"])
        self.assertEqual(words.gujarati_options("123"), [])

    def test_gemini_engine_keeps_only_latin_answers_for_asked_words(self):
        class LLM:
            def complete(self, system, user, *, max_tokens=2048, temperature=0.2):
                self.user = user
                return Completion('```json\n{"કેમ": "kem", "છો": "છો", "extra": "x"}\n```')

        llm = LLM()
        self.assertEqual(GeminiEngine(llm).latin(["કેમ", "છો"]), {"કેમ": "kem"})
        self.assertEqual(json.loads(llm.user), ["કેમ", "છો"])

    def test_fallback_engine_fills_words_the_engine_skipped(self):
        class Partial(FakeEngine):
            def latin(self, words):
                return {}

        words = Transliterator(Partial(), fallback=FakeEngine())
        self.assertEqual(words.latin_text("નવું"), "<નવું>")


class TestRun(unittest.TestCase):
    def test_writes_aligned_json_text_and_subtitles(self):
        with tempfile.TemporaryDirectory() as folder:
            transcript = Path(folder) / "talk.flex.json"
            segments = [{"start": 0, "end": 2, "text": "આજે વાત, Hello"}, {"start": 2, "end": 3, "text": ""}]
            transcript.write_text(json.dumps({"source": "talk.wav", "segments": segments}), encoding="utf-8")
            events = []
            paths = run_gujlish(transcript, Transliterator(FakeEngine()), events.append)
            data = json.loads(paths["gujlish_json"].read_text(encoding="utf-8"))
            self.assertEqual(data["kind"], "gujlish")
            self.assertEqual(data["engine"], "fake")
            self.assertEqual([s["tokens"] for s in data["segments"]], [["aaje", "vaat,", "Hello"], []])
            self.assertEqual(paths["gujlish_txt"].read_text(encoding="utf-8"), "aaje vaat, Hello\n")
            self.assertIn("aaje vaat, Hello", paths["gujlish_srt"].read_text(encoding="utf-8"))
            self.assertEqual(events[-1], {"stage": "transliterating", "done": 1, "total": 1})


if __name__ == "__main__":
    unittest.main()
