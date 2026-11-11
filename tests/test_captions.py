"""Word timing by forced alignment, and the caption step that re-breaks and re-times the English subtitles."""

import argparse
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np

from transcriber.english import captions as cap
from transcriber.english.enrich import find_recording, run_stages
from transcriber.english.llm import Completion, LLMError
from transcriber.files.storage import stage_paths
from transcriber.speech import align

# Two recognized chunks that cut a phrase in two, as in the team's example.
GU = [
    ("રાજાનો દીકરો રાજકુમાર", 0.0, 5.0),
    ("રાજ પરિવારમાં જન્મેલો એક દિવસમાં મોટો થાય?", 5.0, 10.0),
]
EN = ["The king's son, a prince", "born into a royal family, can he grow up in one day?"]
# When each Gujarati word is said: a pause after the first chunk's words, and one before "એક".
TIMES = [
    [(0.5, 1.0), (1.1, 1.6), (1.7, 2.5)],
    [(5.5, 5.9), (6.0, 6.8), (6.9, 7.5), (8.0, 8.2), (8.3, 8.9), (9.0, 9.3), (9.4, 9.8)],
]
GOOD = [
    {"gu": 1, "text": "The king's son, a prince born into a royal family,"},
    {"gu": 7, "text": "can he grow up in one day?"},
]


def english_of(user: str) -> list[str]:
    return re.search(r"<english>\n(.*?)\n</english>", user, re.DOTALL).group(1).split()


class FakeLLM:
    label = "gemini:fake"

    def __init__(self, replies=None):
        self.calls = []
        self.replies = list(replies or [])
        self.fail = False

    def prepare(self):
        pass

    def describe(self):
        return "fake"

    def complete(self, system, user, *, max_tokens=2048, temperature=0.2):
        self.calls.append({"system": system, "user": user})
        if self.fail:
            raise LLMError("server went away")
        reply = self.replies.pop(0) if self.replies else {"captions": GOOD}
        if callable(reply):
            reply = reply(user)
        return reply if isinstance(reply, Completion) else Completion(json.dumps(reply, ensure_ascii=False))


class FakeAligner:
    """Gives each segment the TIMES above, looked up by its start."""

    name = "fake"

    def __init__(self):
        self.calls = []

    def align(self, samples, start, end, text):
        self.calls.append(text)
        number = [s for _, s, _ in GU].index(start)
        return [{"start": a, "end": b} for a, b in TIMES[number]]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name)
        self.transcript = self.run_dir / "talk.flex.json"
        self.write_transcript([text for text, _, _ in GU])
        self.paths = stage_paths(self.transcript)
        self.write_english(EN)
        patcher = patch("transcriber.speech.align.read_audio", lambda source, start, end: np.zeros(16000))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.audio = self.run_dir / "talk.wav"
        self.audio.write_bytes(b"")

    def write_transcript(self, texts):
        segments = [{"start": s, "end": e, "text": t} for t, (_, s, e) in zip(texts, GU)]
        segments.insert(1, {"start": 4.9, "end": 5.0, "text": "", "skipped": "no_speech"})
        self.transcript.write_text(
            json.dumps({"source": "talk.wav", "model": "flex", "segments": segments}, ensure_ascii=False),
            encoding="utf-8",
        )

    def write_english(self, texts, sources=None):
        sources = sources or [text for text, _, _ in GU]
        segments = [
            {"start": s, "end": e, "source": source, "text": text}
            for (_, s, e), source, text in zip(GU, sources, texts)
        ]
        self.paths["translation_json"].write_text(
            json.dumps({"kind": "translation", "source": "talk.wav", "segments": segments}, ensure_ascii=False),
            encoding="utf-8",
        )

    def make(self, llm=None, **kwargs):
        kwargs.setdefault("audio", self.audio)
        kwargs.setdefault("aligner", FakeAligner())
        kwargs.setdefault("log", lambda message: None)
        return cap.run_captions(self.transcript, llm, **kwargs)

    def captions(self):
        return json.loads(self.paths["captions_json"].read_text(encoding="utf-8"))["captions"]


class TestForcedAlignment(unittest.TestCase):
    @staticmethod
    def log_probs(frames):
        """Frame log-probabilities from a list of the most likely token per frame (vocabulary of 4, blank 0)."""
        out = np.full((len(frames), 4), np.log(0.02))
        for t, token in enumerate(frames):
            out[t, token] = np.log(0.94)
        return out

    def test_each_token_gets_the_frames_where_it_is_heard(self):
        spans = align.ctc_align(self.log_probs([0, 1, 1, 0, 0, 2, 0, 3, 3, 0]), [1, 2, 3], blank=0)
        self.assertEqual(spans, [(1, 3), (5, 6), (7, 9)])

    def test_repeated_tokens_need_a_blank_between_them(self):
        self.assertIsNone(align.ctc_align(self.log_probs([1, 1]), [1, 1], blank=0))
        self.assertEqual(align.ctc_align(self.log_probs([1, 0, 1]), [1, 1], blank=0), [(0, 1), (2, 3)])

    def test_word_times_skip_pauses_cap_slow_words_and_fill_unknown_ones(self):
        vocab = {"a": 1, "b": 2, "|": 3}
        # 50 frames over 5 s: "a" at frames 2-3, a pause, "b" from 30 to the end (it soaks up the trailing silence).
        frames = [0, 0, 1, 1] + [0] * 20 + [3] + [0] * 5 + [2] * 20
        times = align.word_times(["a", "★", "b"], self.log_probs(frames), vocab, 0, 10.0, 15.0)
        self.assertEqual(times[0], {"start": 10.2, "end": 10.4})
        self.assertGreaterEqual(times[1]["start"], times[0]["end"])  # the unknown word sits between its neighbours
        self.assertLessEqual(times[1]["end"], times[2]["start"])
        self.assertEqual(times[2]["start"], 13.0)
        self.assertAlmostEqual(times[2]["end"], 13.0 + align.WORD_SLACK + align.SECONDS_PER_CHAR)

    def test_text_too_dense_for_the_audio_is_not_timed(self):
        self.assertIsNone(align.word_times(["ab", "ab"], self.log_probs([1, 2]), {"a": 1, "b": 2}, 0, 0.0, 1.0))


class TestWordTimes(Base):
    def test_alignment_is_saved_and_only_changed_segments_are_aligned_again(self):
        aligner = FakeAligner()
        align.run_alignment(self.transcript, self.audio, aligner)
        self.assertEqual(len(aligner.calls), 2)
        segments = json.loads(self.transcript.read_text(encoding="utf-8"))["segments"]
        times = align.load_word_times(self.transcript, segments)
        self.assertEqual(times[0][0], {"start": 0.5, "end": 1.0})
        self.assertIsNone(times[1])  # the skipped chunk has no words
        self.write_transcript([GU[0][0], GU[1][0].replace("મોટો", "મોટા")])
        segments = json.loads(self.transcript.read_text(encoding="utf-8"))["segments"]
        self.assertIsNone(align.load_word_times(self.transcript, segments)[2])  # changed text: old times unused
        again = FakeAligner()
        align.run_alignment(self.transcript, self.audio, again)
        self.assertEqual(again.calls, [GU[1][0].replace("મોટો", "મોટા")])

    def test_the_recognizers_own_word_times_come_first(self):
        segment = {
            "start": 0,
            "end": 2,
            "text": "kem cho",
            "words": [{"start": 0.1, "end": 0.4}, {"start": 0.5, "end": 1}],
        }
        self.assertEqual(align.load_word_times(self.transcript, [segment])[0][1], {"start": 0.5, "end": 1.0})


class TestReplies(unittest.TestCase):
    TEXT = "The king's son, a prince born into a royal family, can he grow up in one day?"

    def texts(self, captions, original=None):
        return [(original or self.TEXT)[a:b] for a, b in (c["chars"] for c in captions)]

    def test_a_reply_that_keeps_every_word_is_used(self):
        result = cap.parse_captions(json.dumps({"captions": GOOD}), self.TEXT, 10)
        self.assertEqual([c["gu"] for c in result], [1, 7])
        self.assertEqual(self.texts(result), [GOOD[0]["text"], GOOD[1]["text"]])

    def test_changed_dropped_or_added_words_are_refused(self):
        changed = json.dumps({"captions": [{"gu": 1, "text": "The king's son, a prince born in a royal family,"}]})
        self.assertIn('near "into"', cap.parse_captions(changed, self.TEXT, 10))
        dropped = json.dumps({"captions": [GOOD[0]]})
        self.assertIn('from "can" on', cap.parse_captions(dropped, self.TEXT, 10))
        extra = json.dumps({"captions": [*GOOD, {"gu": 9, "text": "Jai!"}]})
        self.assertIn("add words", cap.parse_captions(extra, self.TEXT, 10))
        self.assertIsInstance(cap.parse_captions("no json here", self.TEXT, 10), str)

    def test_a_break_inside_a_dashed_word_or_other_quote_marks_keep_the_original_text(self):
        original = "We came here—I\u2019m talking to parents—to live happily…"
        reply = {
            "captions": [
                {"gu": 1, "text": "We came here-"},
                {"gu": 2, "text": "I'm talking to parents— to live happily..."},
            ]
        }
        result = cap.parse_captions(json.dumps(reply, ensure_ascii=False), original, 5)
        self.assertEqual(
            self.texts(result, original), ["We came here—", "I\u2019m talking to parents—to live happily…"]
        )

    def test_a_caption_ending_mid_phrase_is_merged_into_the_next_when_both_fit(self):
        captions = [
            {"text": "Swamiji's. And he knew", "span": (0, 4), "gu": 3},
            {"text": "that not all bricks go into the foundation.", "span": (4, 11), "gu": 5},
            {"text": "We came here—", "span": (11, 14), "gu": 7},
            {"text": "I'm talking to parents,", "span": (13, 17), "gu": 8},
            {"text": "and all of you who have come here today from so many places", "span": (17, 30), "gu": 9},
            {
                "text": "far and near, to listen to what Swamiji has been saying about it for years now.",
                "span": (30, 45),
            },
        ]
        merged = cap.join_split_phrases([{"gu": None, **c} for c in captions])
        self.assertEqual(
            [c["text"] for c in merged],
            [
                "Swamiji's. And he knew that not all bricks go into the foundation.",
                "We came here—",  # ends at a dash: a natural break, kept
                "I'm talking to parents,",
                "and all of you who have come here today from so many places",  # too long to join
                "far and near, to listen to what Swamiji has been saying about it for years now.",
            ],
        )
        self.assertEqual((merged[0]["span"], merged[0]["gu"]), ((0, 11), 3))
        dashed = cap.join_split_phrases(
            [{"text": "We came here", "span": (0, 3), "gu": 1}, {"text": "—I'm here.", "span": (2, 4), "gu": 2}]
        )
        self.assertEqual(dashed[0]["text"], "We came here—I'm here.")

    def test_unusable_start_words_are_dropped_not_fatal(self):
        reply = json.dumps({"captions": [{"gu": "3", "text": GOOD[0]["text"]}, {"gu": 99, "text": GOOD[1]["text"]}]})
        self.assertEqual([c["gu"] for c in cap.parse_captions(reply, self.TEXT, 10)], [3, None])

    def test_windows_end_at_a_sentence_end(self):
        words = (["word"] * 9 + ["end."]) * 40
        with patch.object(cap, "WINDOW_WORDS", 25), patch.object(cap, "MAX_WINDOW_WORDS", 40):
            spans = cap.windows(words)
        self.assertEqual(spans[0], (0, 30))
        self.assertEqual(spans[-1][1], len(words))
        self.assertTrue(all(a == b for (_, a), (b, _) in zip(spans, spans[1:])))

    def test_an_over_long_caption_is_split_at_its_best_break(self):
        text = "When this gathering was first started by Swamiji, it began with just three boys from the town."
        pieces = cap.fit([{"chars": [0, len(text)], "gu": 4}], text)
        texts = self.texts(pieces, text)
        self.assertEqual(" ".join(texts), text)
        self.assertTrue(all(len(piece) <= cap.MAX_CUE_CHARS for piece in texts))
        self.assertTrue(texts[0].endswith("Swamiji,"))
        self.assertEqual([p["gu"] for p in pieces], [4, None])


class TestRunCaptions(Base):
    def test_a_split_phrase_is_joined_and_timed_to_the_speech(self):
        llm = FakeLLM()
        self.make(llm)
        first, second = self.captions()
        self.assertEqual(first["text"], "The king's son, a prince born into a royal family,")
        self.assertEqual(second["text"], "can he grow up in one day?")
        self.assertEqual((first["start"], second["start"]), (0.5, 8.0))  # when "રાજાનો" and "એક" are said
        self.assertAlmostEqual(first["end"], 7.5 + cap.LINGER_SECONDS)  # "જન્મેલો" ends, then a short linger
        self.assertAlmostEqual(second["end"], 9.8 + cap.LINGER_SECONDS)
        self.assertEqual(first["timing"], "spoken")
        srt = self.paths["translation_srt"].read_text(encoding="utf-8")
        self.assertIn("00:00:00,500 --> 00:00:07,900\nThe king's son, a prince\nborn into a royal family,", srt)
        unsplit = self.paths["translation_unsplit_srt"].read_text(encoding="utf-8")
        self.assertIn("The king's son, a prince\n", unsplit)
        prompt = llm.calls[0]["user"]
        self.assertIn("[1]રાજાનો [2]દીકરો", prompt)
        self.assertEqual(english_of(prompt), " ".join(EN).split())
        self.assertTrue(cap.captions_current(self.transcript))

    def test_english_words_get_times_inside_their_caption(self):
        self.make(FakeLLM())
        words = self.captions()[0]["words"]
        self.assertEqual([w["text"] for w in words][:3], ["The", "king's", "son,"])
        self.assertEqual(words[0]["start"], 0.5)
        self.assertTrue(all(a["start"] <= b["start"] for a, b in zip(words, words[1:])))

    def test_start_words_that_go_backwards_or_far_away_fall_back_to_the_timed_text(self):
        llm = FakeLLM([{"captions": [{"gu": 7, "text": GOOD[0]["text"]}, {"gu": 2, "text": GOOD[1]["text"]}]}])
        self.make(llm)
        first, second = self.captions()
        self.assertEqual(first["timing"], "aligned")  # "એક" is said after this caption's words: not its start
        self.assertEqual(second["timing"], "aligned")  # "દીકરો" is said long before these words
        self.assertEqual(first["start"], 0.5)
        self.assertGreater(second["start"], first["start"])
        self.assertLessEqual(first["end"], second["start"])

    def test_an_unusable_reply_is_retried_then_kept_to_the_plain_breaks(self):
        bad = {"captions": [{"gu": 1, "text": "The king's son, a prince born into a royal family"}]}
        llm = FakeLLM([bad, bad])
        notes = []
        self.make(llm, log=notes.append)
        self.assertEqual(len(llm.calls), 2)
        self.assertIn("could not be used", llm.calls[1]["user"])
        self.assertTrue(any("plain caption breaks" in note for note in notes))
        texts = [c["text"] for c in self.captions()]
        self.assertEqual(" ".join(texts), " ".join(EN))  # plain breaks may now cross the chunk
        self.assertEqual(texts[0], "The king's son, a prince born into a royal family, can he grow up in one day?")

    def test_a_rerun_reuses_saved_breaks_and_a_changed_translation_asks_again(self):
        self.make(FakeLLM())
        again = FakeLLM()
        self.make(again)
        self.assertEqual(again.calls, [])
        self.write_english([EN[0], EN[1].replace("grow up", "become grown")])
        self.assertFalse(cap.captions_current(self.transcript))
        changed = FakeLLM([lambda user: {"captions": [{"gu": 1, "text": " ".join(english_of(user))}]}])
        self.make(changed)
        self.assertEqual(len(changed.calls), 1)
        self.assertIn("become grown", self.captions()[0]["text"])

    def test_parts_are_asked_at_the_same_time_and_kept_in_order(self):
        llm = FakeLLM([lambda user: {"captions": [{"text": " ".join(english_of(user))}]}] * 10)
        with patch.object(cap, "WINDOW_WORDS", 4), patch.object(cap, "MAX_WINDOW_WORDS", 6):
            self.make(llm)
        texts = [c["text"] for c in self.captions()]
        self.assertGreater(len(llm.calls), 2)
        self.assertEqual(" ".join(texts), " ".join(EN))
        self.assertTrue(all(len(english_of(call["user"])) <= 6 for call in llm.calls))
        later = next(call["user"] for call in llm.calls if "<before>" in call["user"])
        self.assertIn("The king's", later)

    def test_a_failed_request_stops_the_run_so_it_can_resume(self):
        llm = FakeLLM()
        llm.fail = True
        with self.assertRaisesRegex(RuntimeError, "Captions stopped at part 1/1"):
            self.make(llm)
        self.assertFalse(self.paths["captions_json"].exists())

    def test_without_the_recording_times_are_shared_out_over_each_chunk(self):
        notes = []
        self.make(FakeLLM(), audio=None, log=notes.append)
        first, second = self.captions()
        self.assertEqual(first["timing"], "estimated")
        self.assertEqual(first["start"], 0.0)
        self.assertTrue(any("no recording" in note for note in notes))

    def test_without_a_model_the_plain_breaks_still_cross_chunks(self):
        self.make(None)
        self.assertEqual(len(self.captions()), 1)
        self.assertEqual(self.captions()[0]["start"], 0.5)
        saved = json.loads(self.paths["captions_json"].read_text(encoding="utf-8"))
        self.assertEqual(saved["windows"], [])

    def test_a_word_fixed_after_translating_keeps_the_times(self):
        self.write_transcript([GU[0][0], GU[1][0].replace("મોટો", "મોટા")])
        aligner = FakeAligner()
        self.make(FakeLLM(), aligner=aligner)
        self.assertEqual(self.captions()[1]["start"], 8.0)

    def test_it_needs_a_translation(self):
        self.paths["translation_json"].unlink()
        with self.assertRaisesRegex(RuntimeError, "Translate"):
            self.make(FakeLLM())


class TestPipeline(Base):
    def args(self, **extra):
        values = dict(llm_model=None, tone="spoken", glossary=None, style_note="", label=None, channel=None)
        return argparse.Namespace(**{**values, **extra})

    def test_translating_also_fixes_the_captions_unless_told_not_to(self):
        def reply(system, user, *_):
            if "<english>" in user:
                return {"captions": GOOD}
            return {str(i): text for i, text in enumerate(EN, 1)}

        llm = FakeLLM()
        llm.complete = lambda system, user, **kw: Completion(json.dumps(reply(system, user), ensure_ascii=False))
        with (
            patch("transcriber.english.enrich.GeminiLLM", lambda model: llm),
            patch("transcriber.english.publish_common.DATA", self.run_dir),
            patch("transcriber.speech.align.Aligner", FakeAligner),
        ):
            outputs = run_stages(self.transcript, self.args(audio=self.audio), translate=True, summarize=False)
            self.assertIn(self.paths["captions_json"], outputs)
            self.assertEqual(self.captions()[1]["start"], 8.0)
            self.paths["captions_json"].unlink()
            run_stages(self.transcript, self.args(no_captions=True), translate=True, summarize=False)
            self.assertFalse(self.paths["captions_json"].exists())

    def test_the_recording_is_found_from_the_run(self):
        with patch("transcriber.english.enrich.RECORDINGS", self.run_dir):
            self.assertEqual(find_recording(self.transcript), self.audio)
            (self.run_dir / ".run.json").write_text('{"recording": "stored.m4a"}', encoding="utf-8")
            (self.run_dir / "stored.m4a").write_bytes(b"")
            self.assertEqual(find_recording(self.transcript), self.run_dir / "stored.m4a")
        with patch("transcriber.english.enrich.RECORDINGS", self.run_dir / "nowhere"):
            self.assertIsNone(find_recording(self.transcript))


if __name__ == "__main__":
    unittest.main()
