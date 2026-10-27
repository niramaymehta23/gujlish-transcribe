"""Pause-aware chunking, speech detection and output guards."""

import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

from asr.audio_stream import audio_chunks
from asr.guards import check_segment, find_repetition
from asr.segmenter import (
    FRAME,
    FRAME_SECONDS,
    AudioChunk,
    EnergyDetector,
    load_detector,
    plan_cut,
    runs,
    speech_span,
    split_chunk,
)

RATE = 16_000


def mask_of(*parts):
    """parts: (seconds, is_speech) pairs -> per-frame boolean mask."""
    return np.concatenate([np.full(round(seconds / FRAME_SECONDS), flag, dtype=bool) for seconds, flag in parts])


def write_wav(path: Path, parts):
    """parts: (seconds, is_tone) pairs written as 16 kHz mono 16-bit audio."""
    pieces = []
    for seconds, tone in parts:
        t = np.arange(int(seconds * RATE)) / RATE
        pieces.append((8000 * np.sin(2 * np.pi * 220 * t)) if tone else np.zeros(len(t)))
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(RATE)
        handle.writeframes(np.concatenate(pieces).astype("<i2").tobytes())


class TestPlanCut(unittest.TestCase):
    def test_runs(self):
        mask = np.array([1, 1, 0, 0, 0, 1, 0], dtype=bool)
        self.assertEqual(runs(mask, True), [(0, 2), (5, 6)])
        self.assertEqual(runs(mask, False), [(2, 5), (6, 7)])
        self.assertEqual(runs(np.zeros(0, dtype=bool), False), [])

    def test_cuts_in_the_pause(self):
        mask = mask_of((12, True), (0.5, False), (20, True))
        cut, forced = plan_cut(mask, max_seconds=27.5)
        self.assertFalse(forced)
        start = round(12 / FRAME_SECONDS)
        self.assertTrue(start <= cut <= start + round(0.5 / FRAME_SECONDS), cut)

    def test_prefers_the_latest_clean_pause(self):
        mask = mask_of((11, True), (0.4, False), (10, True), (0.4, False), (20, True))
        cut, forced = plan_cut(mask, max_seconds=27.5)
        self.assertFalse(forced)
        self.assertGreater(cut * FRAME_SECONDS, 20)  # the second pause, not the first

    def test_ignores_a_pause_that_is_too_early(self):
        mask = mask_of((3, True), (0.5, False), (40, True))  # only pause is at 3 s: too short a chunk
        cut, forced = plan_cut(mask, max_seconds=27.5)
        self.assertTrue(forced)
        self.assertEqual(cut, int(27.5 / FRAME_SECONDS))

    def test_falls_back_to_a_short_pause(self):
        mask = mask_of((8, True), (0.15, False), (40, True))
        cut, forced = plan_cut(mask, max_seconds=27.5)
        self.assertFalse(forced)
        self.assertAlmostEqual(cut * FRAME_SECONDS, 8.07, delta=0.1)

    def test_continuous_speech_is_a_forced_cut(self):
        cut, forced = plan_cut(mask_of((60, True)), max_seconds=27.5)
        self.assertTrue(forced)
        self.assertEqual(cut, int(27.5 / FRAME_SECONDS))

    def test_speech_span_pads_and_rejects_noise(self):
        mask = mask_of((2, False), (3, True), (2, False))
        first, last = speech_span(mask)
        self.assertAlmostEqual(first * FRAME_SECONDS, 1.6, delta=0.05)
        self.assertAlmostEqual(last * FRAME_SECONDS, 5.4, delta=0.05)
        self.assertIsNone(speech_span(mask_of((5, False))))
        self.assertIsNone(speech_span(mask_of((2, False), (0.1, True), (2, False))))


class TestSplitChunk(unittest.TestCase):
    def chunk(self, parts, start=100.0):
        mask = mask_of(*parts)
        samples = np.zeros(len(mask) * FRAME, dtype=np.float32)
        return AudioChunk(start, start + len(samples) / RATE, samples, True, 0.0, mask)

    def test_splits_at_the_middle_pause_and_keeps_every_sample(self):
        chunk = self.chunk([(5, True), (0.5, False), (5, True)])
        left, right = split_chunk(chunk)
        self.assertEqual(len(left.samples) + len(right.samples), len(chunk.samples))
        self.assertAlmostEqual(left.end, right.start)
        self.assertAlmostEqual(left.end - chunk.start, 5.25, delta=0.1)

    def test_split_without_a_pause_and_with_an_all_silent_mask(self):
        for parts in ([(10, True)], [(10, False)]):
            left, right = split_chunk(self.chunk(parts))
            self.assertAlmostEqual(left.end - left.start, 5.0, delta=0.1)

    def test_too_short_to_split(self):
        self.assertIsNone(split_chunk(self.chunk([(3, True)])))


class TestDetectorAndChunks(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_energy_detector_finds_tone_bursts(self):
        t = np.arange(RATE) / RATE
        tone = (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
        samples = np.concatenate([np.zeros(RATE, np.int16), tone, np.zeros(RATE, np.int16)])
        probs = EnergyDetector().frame_probs(samples)
        speech_seconds = float((probs > 0.5).sum()) * FRAME_SECONDS
        self.assertAlmostEqual(speech_seconds, 1.0, delta=0.1)

    def test_energy_detector_handles_steady_signals_and_empty_input(self):
        t = np.arange(3 * RATE) / RATE
        steady = (8000 * np.sin(2 * np.pi * 220 * t)).astype(np.int16)
        self.assertTrue((EnergyDetector().frame_probs(steady) > 0.5).all())
        self.assertEqual(len(EnergyDetector().frame_probs(np.zeros(0, np.int16))), 0)

    def test_load_detector_names(self):
        self.assertEqual(load_detector("energy").name, "energy")
        with self.assertRaises(ValueError):
            load_detector("nope")

    def test_chunks_cut_in_pauses_and_cover_all_speech(self):
        path = self.root / "talk.wav"
        # 9 s speech, 0.6 s pause, repeated: 3 bursts fit in 27.5 s only if cut at a pause.
        write_wav(path, [(9, True), (0.6, False)] * 4)
        chunks = list(audio_chunks(path, chunk_seconds=27.5, chunking="pause", detector=EnergyDetector()))
        self.assertGreaterEqual(len(chunks), 2)
        for chunk in chunks:
            self.assertTrue(chunk.speech)
            self.assertEqual(chunk.overlap, 0.0)  # clean pauses: nothing repeated, so nothing to de-duplicate
            self.assertLessEqual(chunk.duration, 27.5 + 1e-6)
        for earlier, later in zip(chunks, chunks[1:]):
            self.assertLessEqual(earlier.end, later.start + 1e-6)
        speech_seconds = sum(len(c.samples) for c in chunks) / RATE
        self.assertGreater(speech_seconds, 36 - 0.2)  # all 36 s of tone survived trimming

    def test_silence_yields_a_skipped_chunk(self):
        path = self.root / "quiet.wav"
        write_wav(path, [(6, False)])
        chunks = list(audio_chunks(path, chunk_seconds=27.5, chunking="pause", detector=EnergyDetector()))
        self.assertEqual(len(chunks), 1)
        self.assertFalse(chunks[0].speech)

    def test_fixed_mode_is_the_original_behavior(self):
        path = self.root / "tone.wav"
        write_wav(path, [(35, True)])
        chunks = list(audio_chunks(path, chunk_seconds=27.5, chunking="fixed", detector=None))
        self.assertEqual(len(chunks), 2)
        self.assertAlmostEqual(chunks[1].start, 27.1, delta=0.01)  # 0.4 s overlap


class TestGuards(unittest.TestCase):
    def test_repetition(self):
        self.assertIsNotNone(find_repetition("હા " * 12))
        self.assertIsNotNone(find_repetition("so then " + "we go home " * 6))
        self.assertIsNone(find_repetition("હા હા હા ના ના હા હા"))
        self.assertIsNone(find_repetition("આજે machine learning વિશે વાત કરીશું"))

    def test_density_flags(self):
        self.assertEqual(check_segment("a" * 2000, 10), ["too_dense"])
        self.assertEqual(check_segment("ok", 10), ["too_sparse"])
        self.assertEqual(check_segment("ok", 1.5), [])  # too short to judge sparseness
        self.assertEqual(check_segment("આજે machine learning વિશે વાત કરીશું", 4), [])


if __name__ == "__main__":
    unittest.main()
