"""The correction dictionary and word edits: matching, punctuation, fix positions and what gets remembered."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from transcriber.speech.dictionary import (
    MAX_EXAMPLES,
    apply_corrections,
    forget,
    reject,
    remember,
    set_mode,
    shift_fixes,
    split_edges,
    with_edges,
)
from transcriber.speech.edits import matches, replace_words, transcript_text


def rule(heard, correct, id="r1", mode="always"):
    return {"id": id, "heard": heard, "correct": correct, "mode": mode}


class TestApplyCorrections(unittest.TestCase):
    def test_replaces_words_and_keeps_punctuation(self):
        segments = [{"text": "નીરમય, આજે નીરમય."}]
        self.assertEqual(apply_corrections(segments, [rule("નીરમય", "નિરામય")]), (2, 0))
        self.assertEqual(segments[0]["text"], "નિરામય, આજે નિરામય.")
        self.assertEqual(
            [(f["start"], f["end"], f["heard"]) for f in segments[0]["fixes"]], [(0, 1, "નીરમય,"), (2, 3, "નીરમય.")]
        )

    def test_longest_phrase_wins_and_fix_ranges_follow_the_new_words(self):
        segments = [{"text": "હરિ સેના ની જય"}]
        corrections = [rule("સેના", "સૈના", "short"), rule("હરિ સેના", "હરિસેના", "long")]
        apply_corrections(segments, corrections)
        self.assertEqual(segments[0]["text"], "હરિસેના ની જય")
        self.assertEqual(segments[0]["fixes"], [{"start": 0, "end": 1, "heard": "હરિ સેના", "rule": "long"}])
        segments = [{"text": "એક ભૂલ શબ્દ પછી ભૂલ"}]
        apply_corrections(segments, [rule("ભૂલ", "સાચો આખો")])
        self.assertEqual([(f["start"], f["end"]) for f in segments[0]["fixes"]], [(1, 3), (5, 7)])

    def test_case_is_ignored_and_case_only_rules_are_skipped(self):
        segments = [{"text": "Unique assembly kavi"}]
        corrections = [rule("UNIQUE ASSEMBLY", "Unique gathering"), rule("Kavi", "kavi")]
        self.assertEqual(apply_corrections(segments, corrections), (1, 0))
        self.assertEqual(segments[0]["text"], "Unique gathering kavi")
        self.assertEqual(apply_corrections([{"text": ""}], [rule("x", "y")]), (0, 0))

    def test_ask_rules_only_suggest_and_a_rule_without_a_mode_asks(self):
        segments = [{"text": "અમારી વાત, અમારી."}]
        old = {"id": "old", "heard": "અમારી", "correct": "આમાંથી", "correct_gujlish": "aamaathi"}  # saved before modes
        self.assertEqual(apply_corrections(segments, [old]), (0, 2))
        self.assertEqual(segments[0]["text"], "અમારી વાત, અમારી.")
        self.assertNotIn("fixes", segments[0])
        self.assertEqual(
            segments[0]["suggestions"],
            [
                {"start": 0, "end": 1, "correct": "આમાંથી", "correct_gujlish": "aamaathi", "rule": "old"},
                {"start": 2, "end": 3, "correct": "આમાંથી", "correct_gujlish": "aamaathi", "rule": "old"},
            ],
        )

    def test_suggestion_ranges_follow_fixes_made_in_the_same_line(self):
        segments = [{"text": "હરિ સેના અમારી"}]
        corrections = [rule("હરિ સેના", "હરિસેના", "a"), rule("અમારી", "આમાંથી", "b", mode="ask")]
        self.assertEqual(apply_corrections(segments, corrections), (1, 1))
        self.assertEqual(segments[0]["text"], "હરિસેના અમારી")
        self.assertEqual([(s["start"], s["end"]) for s in segments[0]["suggestions"]], [(1, 2)])


class TestRemember(unittest.TestCase):
    def setUp(self):
        self.data = {"corrections": [], "gujlish": {}}

    def test_new_correction_with_spellings_and_example(self):
        saved = remember(self.data, "કવિ", "ખરેખર", "kavi", "kharekhar", {"context": "x"})
        self.assertEqual(saved["correct"], "ખરેખર")
        self.assertEqual(saved["examples"], [{"context": "x"}])
        self.assertEqual(self.data["gujlish"], {"ખરેખર": "kharekhar"})

    def test_same_mistake_updates_the_existing_correction(self):
        for n in range(MAX_EXAMPLES + 2):
            remember(self.data, "કવિ.", "ખરેખર" if n < 3 else "ખરે", "", "", {"n": n})
        self.assertEqual(len(self.data["corrections"]), 1)
        self.assertEqual(self.data["corrections"][0]["correct"], "ખરે")
        self.assertEqual([e["n"] for e in self.data["corrections"][0]["examples"]], list(range(2, MAX_EXAMPLES + 2)))

    def test_spelling_only_fix_keeps_no_correction(self):
        self.assertEqual(remember(self.data, "આત્મીય", "આત્મીય", "aatmiy", "aatmiya", {}), {})
        self.assertEqual(self.data, {"corrections": [], "gujlish": {"આત્મીય": "aatmiya"}})

    def test_new_corrections_ask_unless_always_and_always_is_never_undone_here(self):
        saved = remember(self.data, "કવિ", "કેવી", "", "", {})
        self.assertEqual(saved["mode"], "ask")
        self.assertEqual(remember(self.data, "કવિ", "કેવી", "", "", {}, always=True)["mode"], "always")
        self.assertEqual(remember(self.data, "કવિ", "કેવી", "", "", {})["mode"], "always")
        self.assertTrue(set_mode(self.data, saved["id"], "ask"))
        self.assertEqual(saved["mode"], "ask")
        self.assertFalse(set_mode(self.data, saved["id"], "sometimes"))
        self.assertFalse(set_mode(self.data, "nope", "ask"))

    def test_rejections_are_kept_as_evidence(self):
        saved = remember(self.data, "કવિ", "કેવી", "", "", {})
        for n in range(MAX_EXAMPLES + 1):
            self.assertIs(reject(self.data, saved["id"], {"n": n}), saved)
        self.assertEqual([e["n"] for e in saved["rejected"]], list(range(1, MAX_EXAMPLES + 1)))
        self.assertEqual(reject(self.data, "nope", {}), {})

    def test_clips_of_dropped_examples_are_deleted(self):
        with tempfile.TemporaryDirectory() as folder, patch("transcriber.files.storage.DATA", Path(folder)):
            clips = Path(folder) / "dictionary-clips"
            clips.mkdir()
            for n in range(MAX_EXAMPLES + 1):
                (clips / f"r-{n}.ogg").write_bytes(b"x")
                remember(self.data, "કવિ", "કેવી", "", "", {"clip": f"r-{n}.ogg"})
            self.assertEqual(
                sorted(p.name for p in clips.iterdir()), [f"r-{n}.ogg" for n in range(1, MAX_EXAMPLES + 1)]
            )

    def test_forget(self):
        saved = remember(self.data, "કવિ", "ખરેખર", "kavi", "kharekhar", {})
        self.assertTrue(forget(self.data, correction=saved["id"]))
        self.assertFalse(forget(self.data, correction=saved["id"]))
        self.assertTrue(forget(self.data, gujlish="ખરેખર"))
        self.assertEqual(self.data, {"corrections": [], "gujlish": {}})


class TestEdits(unittest.TestCase):
    def test_edges(self):
        self.assertEqual(split_edges("(જય)."), ("(", "જય", ")."))
        self.assertEqual(split_edges("..."), ("", "...", ""))
        self.assertEqual(with_edges(["(જય", "જય)."], ["હરિ"]), ["(હરિ)."])

    def test_replace_everywhere_keeps_gujlish_and_fixes_in_step(self):
        segments = [
            {"text": "નીરમય આવ્યો, નીરમય!", "fixes": [{"start": 1, "end": 2, "heard": "x", "rule": "a"}]},
            {"text": "હા નીરમય"},
        ]
        gujlish = [["nirmay", "aavyo,", "nirmay!"], ["ha", "nirmay"]]
        places = matches(segments, "નીરમય")
        self.assertEqual(places, [(0, 0), (0, 2), (1, 1)])
        latin = replace_words(segments, gujlish, places, 1, "નિ રામય", "ni ramay")
        self.assertEqual(latin, ["ni", "ramay"])
        self.assertEqual(segments[0]["text"], "નિ રામય આવ્યો, નિ રામય!")
        self.assertEqual(gujlish[0], ["ni", "ramay", "aavyo,", "ni", "ramay!"])
        self.assertEqual(segments[0]["fixes"], [{"start": 2, "end": 3, "heard": "x", "rule": "a"}])
        self.assertEqual(transcript_text(segments), "નિ રામય આવ્યો, નિ રામય! હા નિ રામય")

    def test_replacing_a_fixed_word_drops_its_fix(self):
        segment = {"text": "a b c", "fixes": [{"start": 1, "end": 2}]}
        shift_fixes(segment, 1, 2, 1)
        self.assertNotIn("fixes", segment)

    def test_accepting_a_suggestion_drops_it_and_moves_the_ones_after(self):
        segment = {"text": "a b c d", "suggestions": [{"start": 1, "end": 2}, {"start": 3, "end": 4}]}
        shift_fixes(segment, 1, 2, 2)
        self.assertEqual(segment["suggestions"], [{"start": 4, "end": 5}])


if __name__ == "__main__":
    unittest.main()
