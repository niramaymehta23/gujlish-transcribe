"""The studio's checks: subtitles to sentences, clip limits and storyboards. No ffmpeg or Remotion needed."""

import importlib.util
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("studio_tool", ROOT / "studio" / "tools" / "studio.py")
studio = importlib.util.module_from_spec(spec)
spec.loader.exec_module(studio)

SRT = """1
00:00:01,000 --> 00:00:05,000
Why do we need satsang? When
the body is ill

2
00:00:05,000 --> 00:00:09,000
you go to a doctor.

3
00:00:20,000 --> 00:00:24,000
But who cures the soul
"""


def lines_of(*spans):
    return [{"line": i + 1, "start": s, "end": e, "text": f"Line {i + 1}."} for i, (s, e) in enumerate(spans)]


def good_post():
    return {"youtube_title": "Title", "youtube_description": "About it", "instagram_caption": "Caption"}


def clip_info(**extra):
    return {"format": "short", "duration": 60.0, "has_video": True, **extra}


class TestSubtitles(unittest.TestCase):
    def test_parses_cues_and_joins_wrapped_lines(self):
        cues = studio.parse_srt(SRT)
        self.assertEqual(len(cues), 3)
        self.assertEqual(cues[0]["start"], 1.0)
        self.assertEqual(cues[0]["text"], "Why do we need satsang? When the body is ill")

    def test_lines_are_whole_sentences_across_cues(self):
        lines = studio.sentence_lines(studio.timed_words(studio.parse_srt(SRT)))
        self.assertEqual(
            [line["text"] for line in lines],
            ["Why do we need satsang?", "When the body is ill you go to a doctor.", "But who cures the soul"],
        )
        self.assertEqual([line["line"] for line in lines], [1, 2, 3])
        self.assertLess(lines[0]["end"], lines[1]["start"] + 0.01)
        self.assertEqual(lines[1]["end"], 9.0)
        self.assertEqual(lines[2]["start"], 20.0)  # a long silence starts a new line even without a full stop

    def test_gujarati_subtitles_are_not_taken_for_english(self):
        self.assertTrue(studio.looks_english(studio.parse_srt(SRT)))
        self.assertFalse(studio.looks_english([{"text": "સત્સંગ શા માટે જોઈએ"}]))

    def test_caption_pages_stay_short_and_inside_the_clip(self):
        words = studio.timed_words(studio.parse_srt(SRT))
        pages = studio.caption_pages(words, 1.0, 9.0, 30)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page["text"]) <= 30 for page in pages))
        self.assertEqual(pages[0]["from"], 0.0)
        self.assertLessEqual(pages[-1]["to"], 8.0)
        self.assertNotIn("soul", " ".join(page["text"] for page in pages))


class TestCaptionEdges(unittest.TestCase):
    def test_a_cut_moved_past_the_sentences_does_not_pull_in_neighbouring_words(self):
        words = [
            {"start": 0.0, "end": 1.0, "text": "before."},
            {"start": 1.0, "end": 2.0, "text": "Inside"},
            {"start": 2.0, "end": 3.0, "text": "words."},
            {"start": 3.0, "end": 4.0, "text": "after"},
        ]
        pages = studio.caption_pages(words, 0.4, 3.6, 60, span=(1.0, 3.0))
        self.assertEqual([page["text"] for page in pages], ["Inside words."])
        self.assertEqual((pages[0]["from"], pages[0]["to"]), (0.6, 2.6))

    def test_times_round_without_showing_sixty_seconds(self):
        self.assertEqual(studio.stamp(839.97), "14:00.0")
        self.assertEqual(studio.stamp(83.25), "01:23.2")


class TestNames(unittest.TestCase):
    def test_title_from_summary(self):
        self.assertEqual(
            studio.youtube_title("# T\n\n## YouTube title\n\nWho cures the soul?\n"), "Who cures the soul?"
        )
        self.assertIsNone(studio.youtube_title("# T\n\n## Overview\n"))

    def test_safe_names(self):
        self.assertEqual(studio.safe_name('Who cures the soul? <a/b>: "now"'), "Who cures the soul a b now")
        self.assertEqual(studio.safe_name("???"), "Untitled")
        self.assertLessEqual(len(studio.safe_name("word " * 50)), 90)


class TestClipLimits(unittest.TestCase):
    def setUp(self):
        self.lines = lines_of((0.0, 10.0), (10.0, 30.0), (30.0, 180.0), (180.0, 181.0), (181.0, 700.0))

    def candidate(self, first, last, form="short", score=8, title="Clip"):
        item = {"start_line": first, "end_line": last, "format": form, "score": score, "title": title}
        return studio.check_candidate(item, self.lines)

    def test_times_come_from_the_lines(self):
        clip = self.candidate(2, 3)
        self.assertIsNone(clip["problem"])
        self.assertEqual((clip["start"], clip["end"], clip["duration"]), (10.0, 180.0, 170.0))
        self.assertEqual(clip["text"], "Line 2. Line 3.")

    def test_short_limits_are_hard(self):
        self.assertIn("too short", self.candidate(1, 1)["problem"])
        self.assertIsNone(self.candidate(1, 2)["problem"])  # exactly 0:30
        self.assertIsNone(self.candidate(1, 3)["problem"])  # exactly 3:00
        self.assertIn("too long", self.candidate(1, 4)["problem"])  # 3:01

    def test_long_format_starts_above_three_minutes_and_has_a_ceiling(self):
        self.assertIsNone(self.candidate(1, 4, "long")["problem"])
        self.assertIn("too short", self.candidate(1, 2, "long")["problem"])
        self.assertIn("too long", self.candidate(1, 5, "long")["problem"])

    def test_bad_ranges_and_formats(self):
        self.assertIn("not a real line", self.candidate(9, 12)["problem"])
        self.assertIn("not a real line", self.candidate(3, 2)["problem"])
        self.assertIn("format", self.candidate(1, 2, "reel")["problem"])
        self.assertIn("format", studio.check_candidate("junk", self.lines)["problem"])

    def test_select_keeps_the_best_of_each_passage_per_format(self):
        lines = lines_of((0.0, 60.0), (60.0, 120.0), (120.0, 330.0))
        self.lines = lines
        best = self.candidate(1, 2, score=9, title="Best")
        twin = self.candidate(1, 1, score=7, title="Twin")
        other = self.candidate(2, 2, score=8, title="Other")
        broad = self.candidate(1, 3, "long", score=6, title="Long one")
        kept, rejected = studio.select([twin, best, broad, other])
        self.assertEqual([clip["title"] for clip in kept], ["Best", "Long one"])
        self.assertIn("same passage", rejected[0]["problem"])
        self.assertEqual({clip["title"] for clip in rejected}, {"Twin", "Other"})


class TestStoryboards(unittest.TestCase):
    def board(self, *scenes):
        return {"scenes": list(scenes), "post": good_post()}

    def hook(self, begin, end, **extra):
        return {"type": "hook", "from": begin, "to": end, "headline": "Who cures the soul?", **extra}

    def speaker(self, begin, end):
        return {"type": "speaker", "from": begin, "to": end}

    def errors(self, *scenes, clip=None):
        return studio.check_storyboard(self.board(*scenes), clip or clip_info())[0]

    def test_a_good_storyboard_passes(self):
        scenes = [self.hook(0, 20), self.speaker(20, 30), {"type": "end", "from": 30, "to": 60, "text": "Satsang"}]
        errors, warnings = studio.check_storyboard(self.board(*scenes), clip_info())
        self.assertEqual(errors, [])
        self.assertTrue(any("stays up" in warning for warning in warnings))

    def test_gaps_overlaps_and_wrong_length(self):
        self.assertTrue(any("No gaps" in e for e in self.errors(self.hook(0, 20), self.speaker(21, 60))))
        self.assertTrue(any("No gaps" in e for e in self.errors(self.hook(0, 20), self.speaker(19, 60))))
        self.assertTrue(any("clip is 60" in e for e in self.errors(self.hook(0, 20), self.speaker(20, 50))))

    def test_speaker_scenes_need_a_video(self):
        scenes = (self.hook(0, 20), self.speaker(20, 60))
        self.assertTrue(any("no video" in e for e in self.errors(*scenes, clip=clip_info(has_video=False))))

    def test_text_limits_and_required_fields(self):
        self.assertTrue(any("characters" in e for e in self.errors({**self.hook(0, 60), "headline": "x" * 71})))
        self.assertTrue(any("needs headline" in e for e in self.errors({"type": "hook", "from": 0, "to": 60})))
        self.assertTrue(any("unknown type" in e for e in self.errors({"type": "x", "from": 0, "to": 60})))

    def test_compare_rows_are_checked(self):
        scene = {"type": "compare", "from": 0, "to": 60, "title": "Who heals what?"}
        row = {"left": "Body", "right": "Doctor"}
        self.assertTrue(any("2 to 5 rows" in e for e in self.errors({**scene, "rows": [row]})))
        self.assertEqual(self.errors({**scene, "rows": [row, {**row, "at": 30}]}), [])
        self.assertTrue(any("`at`" in e for e in self.errors({**scene, "rows": [row, {**row, "at": 90}]})))

    def test_emphasis_must_be_in_the_text(self):
        self.assertEqual(self.errors(self.hook(0, 60, emphasis=["SOUL"])), [])
        self.assertTrue(any("emphasis" in e for e in self.errors(self.hook(0, 60, emphasis=["body"]))))

    def test_post_limits(self):
        board = {"scenes": [self.hook(0, 60)], "post": {**good_post(), "youtube_title": "x" * 101}}
        self.assertTrue(any("limit is 100" in e for e in studio.check_storyboard(board, clip_info())[0]))
        board = {"scenes": [self.hook(0, 60)], "post": {**good_post(), "youtube_title": "a <b>"}}
        self.assertTrue(any("rejects" in e for e in studio.check_storyboard(board, clip_info())[0]))
        board = {"scenes": [self.hook(0, 60)]}
        self.assertTrue(any("post section" in e for e in studio.check_storyboard(board, clip_info())[0]))


if __name__ == "__main__":
    unittest.main()
