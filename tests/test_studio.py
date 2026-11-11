"""The studio's checks: subtitles to sentences, clip limits and storyboards. No ffmpeg or Remotion needed."""

import importlib.util
import unittest
from pathlib import Path
from unittest import mock

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


class TestSpeechTiming(unittest.TestCase):
    def test_words_skip_the_pauses_inside_a_cue(self):
        cues = [{"start": 10.0, "end": 16.0, "text": "aaaa bbbb cccc dddd"}]
        # he starts a second late, speaks two seconds, pauses one, speaks two more
        words = studio.speech_words(cues, [(9.0, 11.0), (13.0, 14.0)])
        self.assertAlmostEqual(words[0]["start"], 11.0)
        self.assertAlmostEqual(words[1]["end"], 13.0)  # half the letters are said before the pause
        self.assertAlmostEqual(words[2]["start"], 14.0)  # and the rest after it
        self.assertAlmostEqual(words[-1]["end"], 16.0)

    def test_a_cue_with_no_pauses_is_shared_evenly(self):
        cues = [{"start": 0.0, "end": 4.0, "text": "aa bb"}]
        words = studio.speech_words(cues, [])
        self.assertEqual([(w["start"], w["end"]) for w in words], [(0.0, 2.0), (2.0, 4.0)])

    def test_pages_break_where_he_pauses(self):
        cues = [{"start": 0.0, "end": 10.0, "text": "one two three four five six seven eight nine ten eleven twelve"}]
        words = studio.speech_words(cues, [(5.5, 6.5)])
        pages = studio.caption_pages(words, 0.0, 10.0, max_chars=48)
        self.assertGreater(len(pages), 1)
        self.assertLessEqual(pages[0]["to"], 6.5)
        self.assertGreaterEqual(pages[1]["from"], 6.4)


class TestCaptionBreaks(unittest.TestCase):
    def test_a_page_breaks_after_punctuation_not_on_a_hanging_word(self):
        cues = [
            {
                "start": 0.0,
                "end": 12.0,
                "text": "That's why attending satsang is an investment: just an hour or two a week.",
            }
        ]
        words = studio.speech_words(cues, [(7.4, 8.4)])  # he pauses between "an" and "hour"
        pages = studio.caption_pages(words, 0.0, 12.0, max_chars=60)
        self.assertEqual(pages[0]["text"], "That's why attending satsang is an investment:")
        self.assertTrue(pages[1]["text"].startswith("just an hour"))

    def test_no_page_ends_on_a_small_word(self):
        cues = [
            {
                "start": 0.0,
                "end": 10.0,
                "text": "He took off the gold chain from his neck and gave it to the boy right away",
            }
        ]
        pages = studio.caption_pages(studio.speech_words(cues, []), 0.0, 10.0, max_chars=40)
        for page in pages[:-1]:
            self.assertNotIn(page["text"].split()[-1].lower(), studio.HANGING_WORDS)

    def test_a_clip_takes_the_words_of_its_own_lines(self):
        # the previous sentence's words are spoken late in their cue, but they belong to the line before the clip
        cues = [{"start": 0.0, "end": 10.0, "text": "It's just an hour. There's a sabha every Saturday."}]
        words = studio.speech_words(cues, [(0.0, 3.0)])
        lines = studio.sentence_lines(studio.timed_words(cues))
        span = (lines[1]["start"], lines[1]["end"])
        pages = studio.caption_pages(words, span[0], 10.0, max_chars=60, span=span)
        self.assertEqual(pages[0]["text"], "There's a sabha every Saturday.")


class TestLongPauses(unittest.TestCase):
    def test_a_long_silence_inside_a_clip_is_cut_out(self):
        segments = [{"start": 100.0, "end": 160.0, "line_start": 100.0, "line_end": 160.0}]
        pieces = studio.trim_long_pauses(segments, [(107.5, 115.4), (120.0, 121.5)])
        self.assertEqual([(p["start"], p["end"]) for p in pieces], [(100.0, 108.0), (114.9, 160.0)])
        self.assertAlmostEqual(studio.segments_length(pieces), 60.0 - 6.9)

    def test_short_pauses_and_pauses_at_the_edges_stay(self):
        segments = [{"start": 100.0, "end": 160.0, "line_start": 100.0, "line_end": 160.0}]
        self.assertEqual(studio.trim_long_pauses(segments, [(98.0, 104.0), (130.0, 133.0)]), segments)

    def test_captions_skip_the_cut_pause(self):
        cues = [
            {"start": 0.0, "end": 4.0, "text": "First he spoke."},
            {"start": 12.0, "end": 16.0, "text": "Then he went on."},
        ]
        words = studio.speech_words(cues, [(4.0, 12.0)])
        clip = {
            "segments": studio.trim_long_pauses(
                [{"start": 0.0, "end": 16.0, "line_start": 0.0, "line_end": 16.0}], [(4.0, 12.0)]
            )
        }
        pages = studio.clip_captions(words, clip, 60)
        self.assertEqual([p["text"] for p in pages], ["First he spoke.", "Then he went on."])
        self.assertLess(pages[1]["from"], 6.0)  # the second sentence comes straight after the shortened pause


class TestEmphasis(unittest.TestCase):
    def test_repeated_phrases_are_found_longest_first(self):
        pages = [
            {"text": "One yojan means twelve kilometers.", "from": 10.0, "to": 12.0},
            {"text": "Think about it.", "from": 12.0, "to": 14.0},
            {"text": "One yojan means twelve kilometers!", "from": 15.0, "to": 17.0},
        ]
        found = studio.repeated_phrases(pages)
        self.assertEqual(found[0], {"text": "one yojan means twelve kilometers", "times": [10.0, 15.0]})
        self.assertFalse(any(item["text"] == "twelve kilometers" for item in found))

    def test_nothing_repeated_finds_nothing(self):
        pages = [{"text": "A devotee brought a mango.", "from": 0.0, "to": 2.0}]
        self.assertEqual(studio.repeated_phrases(pages), [])


class TestAlignGraphics(unittest.TestCase):
    def words(self, text, start, step=0.4):
        return [{"text": word, "at": round(start + i * step, 2)} for i, word in enumerate(text.split())]

    def test_a_graphic_moves_to_when_its_words_are_said(self):
        words = self.words("and imagine a well brim-full of water.", 64.0)
        board = {
            "scenes": [
                {"type": "speaker", "from": 0.0, "to": 53.3},
                {
                    "type": "picture",
                    "from": 53.3,
                    "to": 62.0,
                    "setting": "water",
                    "title": "Imagine a well brim-full of water",
                },
                {"type": "speaker", "from": 62.0, "to": 80.0},
            ]
        }
        studio.align_graphics(board, words, 80.0)
        picture = board["scenes"][1]
        self.assertAlmostEqual(picture["from"], 64.4 - studio.GRAPHIC_LEAD_SECONDS, places=2)
        self.assertAlmostEqual(picture["to"] - picture["from"], 8.7, places=1)  # it keeps its length
        self.assertEqual(board["scenes"][0]["to"], picture["from"])

    def test_a_repeated_phrase_matches_the_saying_nearest_the_graphic(self):
        words = self.words("one yojan means twelve kilometers.", 108.8) + self.words(
            "one yojan means twelve kilometers!", 115.0
        )
        self.assertEqual(studio.spoken_at("Twelve kilometers!", words, 107, 125, near=114.9), 116.2)
        self.assertEqual(studio.spoken_at("Twelve kilometers!", words, 100, 125, near=108.0), 110.0)

    def test_a_paraphrase_is_left_where_it_is(self):
        words = self.words("he gave the chain without thinking twice", 10.0)
        self.assertIsNone(studio.spoken_at("A completely full well", words, 0, 30))

    def test_early_graphics_are_reported(self):
        words = self.words("a complete waste of time", 39.0)
        board = {
            "scenes": [
                {"type": "speaker", "from": 0, "to": 34.6},
                {"type": "punch", "from": 34.6, "to": 42, "text": "A complete waste"},
            ]
        }
        self.assertTrue(any("before he says" in w for w in studio.early_graphics(board, words)))


class TestRecording(unittest.TestCase):
    def test_sound_comes_from_the_video_when_it_has_sound(self):
        record = {"audio": "talk.mp3", "video": "talk.mp4"}
        folder = Path("/talks/x")
        with (
            mock.patch.object(studio, "has_sound", return_value=True),
            mock.patch.object(Path, "exists", return_value=True),
        ):
            self.assertEqual(studio.recording(folder, record), folder / "talk.mp4")
        with (
            mock.patch.object(studio, "has_sound", return_value=False),
            mock.patch.object(Path, "exists", return_value=True),
        ):
            self.assertEqual(studio.recording(folder, record), folder / "talk.mp3")
        self.assertEqual(studio.recording(folder, {"audio": "talk.mp3"}), folder / "talk.mp3")


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


class TestSkippedLines(unittest.TestCase):
    def setUp(self):
        self.lines = lines_of((0.0, 10.0), (10.0, 20.0), (20.0, 30.0), (30.0, 40.0), (40.0, 50.0))

    def candidate(self, skip, first=1, last=5):
        item = {"start_line": first, "end_line": last, "format": "short", "score": 8, "title": "C", "skip": skip}
        return studio.check_candidate(item, self.lines)

    def test_skipped_lines_leave_pieces_and_a_shorter_clip(self):
        clip = self.candidate([[2, 3]])
        self.assertIsNone(clip["problem"])
        self.assertEqual([(s["start"], s["end"]) for s in clip["segments"]], [(0.0, 10.0), (30.0, 50.0)])
        self.assertEqual(clip["duration"], 30.0)
        self.assertEqual((clip["start"], clip["end"]), (0.0, 50.0))
        self.assertNotIn("Line 2.", clip["text"])
        self.assertEqual(studio.joins_of(clip), [10.0])

    def test_bad_skips(self):
        self.assertIn("inside the clip", self.candidate([[1, 2]])["problem"])
        self.assertIn("inside the clip", self.candidate([[4, 5]])["problem"])
        self.assertIn("overlaps", self.candidate([[2, 3], [3, 4]])["problem"])
        self.assertIn("pairs", self.candidate([2, 3])["problem"])
        self.assertIn("too short", self.candidate([[2, 4]])["problem"])

    def test_no_skip_is_one_piece(self):
        clip = self.candidate(None)
        self.assertEqual(len(clip["segments"]), 1)
        self.assertEqual(studio.joins_of(clip), [])

    def test_captions_follow_on_across_a_join(self):
        words = [
            {"start": 1.0, "end": 2.0, "text": "Kept."},
            {"start": 12.0, "end": 13.0, "text": "Skipped."},
            {"start": 31.0, "end": 32.0, "text": "Later."},
        ]
        pages = studio.clip_captions(words, self.candidate([[2, 3]]), 60)
        self.assertEqual([page["text"] for page in pages], ["Kept.", "Later."])
        self.assertAlmostEqual(pages[1]["from"], 11.0)

    def test_joined_audio_fades_each_piece_and_concatenates(self):
        segments = self.candidate([[2, 3]])["segments"]
        command = studio.audio_command(Path("talk.mp3"), segments, Path("out.m4a"))
        self.assertEqual(command.count("-i"), 2)
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("concat=n=2:v=0:a=1", graph)
        self.assertIn("afade=t=out", graph)
        single = studio.audio_command(Path("talk.mp3"), segments[:1], Path("out.m4a"))
        self.assertNotIn("-filter_complex", single)

    def test_video_across_a_join_is_warned_about(self):
        board = {
            "style": "paper",
            "scenes": [
                {"type": "hook", "from": 0, "to": 4, "headline": "Why?"},
                {"type": "speaker", "from": 4, "to": 30},
                {"type": "punch", "from": 30, "to": 60, "text": "Now."},
            ],
            "post": good_post(),
        }
        warnings = studio.check_storyboard(board, clip_info(joins=[12.0]))[1]
        self.assertTrue(any("join at 12.0s" in w for w in warnings))
        warnings = studio.check_storyboard(board, clip_info(joins=[40.0]))[1]
        self.assertFalse(any("join" in w for w in warnings))


class TestStoryboards(unittest.TestCase):
    def board(self, *scenes):
        return {"style": "paper", "scenes": list(scenes), "post": good_post()}

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
        board = {**self.board(self.hook(0, 60)), "post": {**good_post(), "youtube_title": "x" * 101}}
        self.assertTrue(any("limit is 100" in e for e in studio.check_storyboard(board, clip_info())[0]))
        board = {**self.board(self.hook(0, 60)), "post": {**good_post(), "youtube_title": "a <b>"}}
        self.assertTrue(any("rejects" in e for e in studio.check_storyboard(board, clip_info())[0]))
        board = {"style": "paper", "scenes": [self.hook(0, 60)]}
        self.assertTrue(any("post section" in e for e in studio.check_storyboard(board, clip_info())[0]))

    def test_a_style_is_required(self):
        board = {**self.board(self.hook(0, 60)), "style": "pastel"}
        self.assertTrue(any("needs a style" in e for e in studio.check_storyboard(board, clip_info())[0]))

    def test_split_layout_needs_a_video_and_speakers_take_no_layout(self):
        split = {"type": "punch", "from": 4, "to": 60, "text": "Ego.", "layout": "split"}
        self.assertEqual(self.errors(self.hook(0, 4), split), [])
        no_video = self.errors(self.hook(0, 4), split, clip=clip_info(has_video=False))
        self.assertTrue(any("split scene" in e for e in no_video))
        self.assertTrue(any("layout must be" in e for e in self.errors(self.hook(0, 60, layout="half"))))
        speaker = {**self.speaker(4, 60), "layout": "split"}
        self.assertTrue(any("takes no layout" in e for e in self.errors(self.hook(0, 4), speaker)))

    def test_pacing_warnings(self):
        scenes = [self.hook(0, 6), {"type": "punch", "from": 6, "to": 20, "text": "Worry."}, self.speaker(20, 60)]
        warnings = studio.check_storyboard(self.board(*scenes), clip_info())[1]
        self.assertTrue(any("Scene 1 (hook)" in w and "under 4.5s" in w for w in warnings))
        self.assertTrue(any("Scene 2 (punch)" in w and "under 8s" in w for w in warnings))
        self.assertTrue(any("Scene 3 (speaker)" in w and "under 15s" in w for w in warnings))

    def test_icons_are_checked_by_name(self):
        known = {"heart", "hands-praying", "cube-3-d"}
        with mock.patch.object(studio, "icon_names", return_value=known):
            rows = [
                {"left": "Body", "right": "Doctor", "left_icon": "heart"},
                {"left": "Soul", "right": "Saint", "icon": "hands-praying"},
            ]
            compare = {"type": "compare", "from": 0, "to": 60, "title": "Healers", "rows": rows}
            self.assertEqual(self.errors(compare), [])
            rows[1]["icon"] = "halo"
            self.assertTrue(any("'halo'" in e for e in self.errors(compare)))
            rows[1]["icon"] = "HandsPraying"
            self.assertTrue(any("kebab-case" in e for e in self.errors(compare)))

    def test_scenes_take_no_lone_icon(self):
        with mock.patch.object(studio, "icon_names", return_value={"hands-praying"}):
            self.assertTrue(
                any("no longer takes an icon" in e for e in self.errors(self.hook(0, 60, icon="hands-praying")))
            )

    def test_art_is_one_to_three_known_icons(self):
        known = {"clock", "coins", "chart-line-up"}
        with mock.patch.object(studio, "icon_names", return_value=known):
            self.assertEqual(self.errors(self.hook(0, 60, art=["clock", "coins", "chart-line-up"])), [])
            self.assertTrue(any("one to three" in e for e in self.errors(self.hook(0, 60, art=[]))))
            self.assertTrue(any("one to three" in e for e in self.errors(self.hook(0, 60, art=["clock"] * 4))))
            self.assertTrue(any("'piggy'" in e for e in self.errors(self.hook(0, 60, art=["piggy"]))))
            speaker = {**self.speaker(4, 60), "art": ["clock"]}
            self.assertTrue(any("takes no art" in e for e in self.errors(self.hook(0, 4), speaker)))

    def test_picture_scenes(self):
        known = {"orange", "crown"}
        picture = {"type": "picture", "from": 4, "to": 10, "setting": "arch", "hero": "orange", "title": "A mango"}
        with mock.patch.object(studio, "icon_names", return_value=known):
            self.assertEqual(self.errors(self.hook(0, 4), {**picture, "to": 60}), [])
            bad = {**picture, "to": 60, "setting": "forest", "effect": "snow"}
            errors = self.errors(self.hook(0, 4), bad)
            self.assertTrue(any("needs a setting" in e for e in errors))
            self.assertTrue(any("effect must be" in e for e in errors))
            empty = {"type": "picture", "from": 4, "to": 60, "setting": "sky"}
            self.assertTrue(any("needs something on it" in e for e in self.errors(self.hook(0, 4), empty)))
            chips = {**picture, "to": 60, "chips": [{"text": "x" * 27}]}
            self.assertTrue(any("over 26" in e for e in self.errors(self.hook(0, 4), chips)))
            self.assertTrue(
                any("'nope'" in e for e in self.errors(self.hook(0, 4), {**picture, "to": 60, "hero": "nope"}))
            )

    def test_generated_pictures_stand_in_for_icons_in_art_and_hero(self):
        board = self.board(self.hook(0, 4, art=["mango", "clock"]), {**self.speaker(4, 60)})
        board["pictures"] = {"mango": "a ripe golden Alphonso mango with one green leaf"}
        with mock.patch.object(studio, "icon_names", return_value={"clock"}):
            self.assertEqual(studio.check_storyboard(board, clip_info())[0], [])
            board["pictures"] = {}
            self.assertTrue(any("'mango'" in e for e in studio.check_storyboard(board, clip_info())[0]))

    def test_generated_pictures_never_show_people(self):
        names, errors = studio._check_pictures({"court": "King Akbar on his throne in a grand court"})
        self.assertEqual(names, set())
        self.assertTrue(any("would show a person" in e for e in errors))
        names, errors = studio._check_pictures({"shawl": "a richly embroidered golden shawl, folded"})
        self.assertEqual((names, errors), ({"shawl"}, []))
        self.assertTrue(studio._check_pictures({"Gold Chain": "a heavy gold chain necklace"})[1])
        self.assertTrue(studio._check_pictures({"tv": "tv"})[1])

    def test_unused_pictures_are_flagged(self):
        board = self.board(self.hook(0, 4), self.speaker(4, 60))
        board["pictures"] = {"mango": "a ripe golden Alphonso mango with one green leaf"}
        warnings = studio.check_storyboard(board, clip_info())[1]
        self.assertTrue(any("'mango' isn't used" in w for w in warnings))

    @unittest.skipUnless(
        importlib.util.find_spec("PIL") and importlib.util.find_spec("numpy"),
        "the studio's image libraries (Pillow, numpy) aren't installed",
    )
    def test_key_out_clears_the_flat_background_but_keeps_green_inside(self):
        import io

        from PIL import Image, ImageDraw

        image = Image.new("RGB", (200, 200), (0, 255, 0))
        draw = ImageDraw.Draw(image)
        draw.ellipse((40, 40, 160, 160), fill="white")  # the sticker's white edge
        draw.ellipse((70, 70, 130, 130), fill=(60, 170, 60))  # a green leaf inside it
        draw.ellipse((95, 20, 105, 30), fill=(0, 255, 0))  # key green trapped inside: cleared
        draw.ellipse((90, 90, 110, 110), fill=(0, 250, 5))  # a hole of background inside: cleared
        draw.rectangle((2, 190, 6, 194), fill="white")  # a stray speck: dropped
        raw = io.BytesIO()
        image.save(raw, "PNG")
        out = studio.key_out(raw.getvalue())
        self.assertEqual(out.size, (1024, 1024))
        self.assertEqual(out.getpixel((5, 5))[3], 0)
        self.assertEqual(out.getpixel((512, 512))[3], 0)  # the hole
        self.assertEqual(out.getpixel((512, 380))[3], 255)  # the leaf around it
        self.assertGreater(out.getpixel((512, 380))[1], 150)

    def test_icon_names_come_from_the_installed_package(self):
        names = studio.icon_names()
        if names is None:
            self.skipTest("Remotion's packages aren't installed")
        self.assertIn("hands-praying", names)
        self.assertIn("first-aid-kit", names)


if __name__ == "__main__":
    unittest.main()
