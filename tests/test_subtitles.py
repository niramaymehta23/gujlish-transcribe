"""SRT subtitle export."""

import re
import unittest

from asr.subtitles import MAX_CUE_CHARS, MAX_LINE_CHARS, segment_cues, srt_time, to_srt


class TestSubtitles(unittest.TestCase):
    def test_time_format(self):
        self.assertEqual(srt_time(0), "00:00:00,000")
        self.assertEqual(srt_time(3725.5), "01:02:05,500")
        self.assertEqual(srt_time(-1), "00:00:00,000")

    def test_numbered_blocks_and_skipped_segments(self):
        srt = to_srt(
            [
                {"start": 0.4, "end": 3.0, "text": "આજે machine learning"},
                {"start": 3.0, "end": 5.0, "text": ""},  # skipped chunk (no speech)
                {"start": 5.0, "end": 7.25, "text": "વિશે વાત"},
            ]
        )
        self.assertEqual(
            srt,
            "1\n00:00:00,400 --> 00:00:03,000\nઆજે machine learning\n\n2\n00:00:05,000 --> 00:00:07,250\nવિશે વાત\n",
        )

    def test_long_segments_split_into_readable_cues_in_order(self):
        text = " ".join(f"word{i}" for i in range(60))
        cues = segment_cues({"start": 10.0, "end": 40.0, "text": text})
        self.assertGreater(len(cues), 1)
        self.assertEqual(" ".join(c[2].replace("\n", " ") for c in cues), text)  # no words lost or reordered
        self.assertAlmostEqual(cues[0][0], 10.0)
        self.assertAlmostEqual(cues[-1][1], 40.0)
        for (start, end, body), following in zip(cues, cues[1:] + [None]):
            self.assertLess(start, end)
            self.assertLessEqual(len(body.replace("\n", " ")), MAX_CUE_CHARS)
            self.assertTrue(all(len(line) <= MAX_LINE_CHARS for line in body.split("\n")))
            if following:
                self.assertLessEqual(end, following[0] + 1e-9)

    def test_word_timings_are_used_when_they_match(self):
        words = [{"word": f"w{i}", "start": 1.0 + i, "end": 1.5 + i} for i in range(40)]
        text = " ".join(w["word"] for w in words)
        cues = segment_cues({"start": 0.0, "end": 50.0, "text": text, "words": words})
        self.assertAlmostEqual(cues[0][0], 1.0)  # first word's start, not the segment's
        self.assertAlmostEqual(cues[-1][1], 40.5)
        mismatched = segment_cues({"start": 0.0, "end": 50.0, "text": text + " extra", "words": words})
        self.assertAlmostEqual(mismatched[0][0], 0.0)  # falls back to proportional timing

    def test_output_parses_as_srt(self):
        srt = to_srt([{"start": i * 4.0, "end": i * 4.0 + 3.5, "text": f"line {i}"} for i in range(12)])
        blocks = srt.strip().split("\n\n")
        self.assertEqual(len(blocks), 12)
        for number, block in enumerate(blocks, 1):
            lines = block.split("\n")
            self.assertEqual(lines[0], str(number))
            self.assertRegex(lines[1], re.compile(r"^\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}$"))


if __name__ == "__main__":
    unittest.main()
