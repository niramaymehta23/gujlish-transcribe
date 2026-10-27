"""Pause-aware chunking: cut long audio where the speaker pauses, not mid-word.

The ASR models accept about 30 s of audio. Cutting at fixed offsets splits words and forces text-level overlap
repair. Instead, the streaming decoder asks a speech detector for per-frame speech probabilities, cuts at the
latest usable pause before the maximum chunk length, trims leading/trailing silence, and skips chunks without
speech (a common trigger for generated text). A forced fixed cut with a small overlap is only the fallback when
no pause exists, e.g. continuous speech or music.

All cut logic is pure NumPy so it is unit-testable without model weights. Silero VAD is optional; a dependency-free
energy detector is the fallback.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field

import numpy as np

SAMPLE_RATE = 16_000
FRAME = 512  # samples per detector frame (32 ms at 16 kHz; also Silero's native window)
FRAME_SECONDS = FRAME / SAMPLE_RATE

SPEECH_THRESHOLD = 0.5
GOOD_PAUSE_SECONDS = 0.25  # a pause this long is a clean place to cut
SHORT_PAUSE_SECONDS = 0.10  # last-resort pause
MIN_CUT_SECONDS = 10.0  # prefer chunks at least this long (more context helps the recogniser)
MIN_FALLBACK_CUT_SECONDS = 6.0
EDGE_PAD_SECONDS = 0.40  # silence kept around speech when trimming; short word onsets sit just outside the VAD
MIN_SPEECH_SECONDS = 0.30  # less than this in a chunk counts as "no speech"

ENERGY_ABS_FLOOR = 150.0  # int16 RMS; about -47 dBFS


class SpeechDetector:
    """Maps int16 mono samples to one speech probability per FRAME samples (last frame zero-padded)."""

    name = "base"

    def frame_probs(self, samples: np.ndarray) -> np.ndarray:  # pragma: no cover - interface
        raise NotImplementedError


class EnergyDetector(SpeechDetector):
    """Adaptive RMS gate. Crude, but needs no dependencies and is deterministic."""

    name = "energy"

    def frame_probs(self, samples: np.ndarray) -> np.ndarray:
        count = -(-len(samples) // FRAME)
        if not count:
            return np.zeros(0, dtype=np.float32)
        padded = np.zeros(count * FRAME, dtype=np.float32)
        padded[: len(samples)] = samples
        rms = np.sqrt(np.mean(padded.reshape(count, FRAME) ** 2, axis=1))
        noise = float(np.percentile(rms, 10))
        loud = float(np.percentile(rms, 95))
        # min() keeps stationary signals (constant tone, steady hum) from being classified as silence.
        threshold = max(ENERGY_ABS_FLOOR, min(3.0 * noise, 0.35 * loud))
        return (rms >= threshold).astype(np.float32)


class SileroDetector(SpeechDetector):
    """Silero VAD (MIT licence, weights bundled in the pip wheel). Requires PyTorch."""

    name = "silero"

    def __init__(self) -> None:
        import torch

        # silero_vad calls torch.set_num_threads(1) on import, which would also throttle CPU ASR inference.
        threads = torch.get_num_threads()
        try:
            from silero_vad import load_silero_vad

            self.model = load_silero_vad()
        finally:
            torch.set_num_threads(threads)
        self.torch = torch

    def frame_probs(self, samples: np.ndarray) -> np.ndarray:
        torch = self.torch
        count = -(-len(samples) // FRAME)
        if not count:
            return np.zeros(0, dtype=np.float32)
        padded = np.zeros(count * FRAME, dtype=np.float32)
        padded[: len(samples)] = samples.astype(np.float32) / 32768.0
        audio = torch.from_numpy(padded)
        self.model.reset_states()
        probs = np.empty(count, dtype=np.float32)
        with torch.inference_mode():
            for index in range(count):
                probs[index] = float(self.model(audio[index * FRAME : (index + 1) * FRAME], SAMPLE_RATE).item())
        return probs


def load_detector(name: str = "auto") -> SpeechDetector:
    if name == "energy":
        return EnergyDetector()
    if name == "silero":
        return SileroDetector()
    if name != "auto":
        raise ValueError(f"Unknown speech detector {name!r}")
    try:
        return SileroDetector()
    except (ImportError, OSError) as error:
        print(
            f"Note: Silero VAD unavailable ({error.__class__.__name__}); using the energy detector. "
            "Install it with: python -m pip install silero-vad",
            file=sys.stderr,
            flush=True,
        )
        return EnergyDetector()


def runs(mask: np.ndarray, value: bool) -> list[tuple[int, int]]:
    """Half-open [start, end) frame ranges where mask == value."""
    if not len(mask):
        return []
    edges = np.flatnonzero(np.diff(mask.astype(np.int8))) + 1
    bounds = np.concatenate(([0], edges, [len(mask)]))
    return [(int(a), int(b)) for a, b in zip(bounds[:-1], bounds[1:]) if bool(mask[a]) == value]


def plan_cut(mask: np.ndarray, *, max_seconds: float, min_seconds: float = MIN_CUT_SECONDS) -> tuple[int, bool]:
    """Choose where to end a chunk. Returns (cut_frame, forced).

    1. Latest clean pause (>= 0.25 s) whose cut point falls in [min_seconds, max_seconds].
    2. Otherwise the longest short pause (>= 0.10 s) after MIN_FALLBACK_CUT_SECONDS.
    3. Otherwise a forced cut at max_seconds (the caller then keeps an overlap).
    """
    max_frame = min(len(mask), int(max_seconds / FRAME_SECONDS))
    pauses = runs(mask, False)

    def cut_point(start: int, end: int) -> int:
        return max(start, min((start + end) // 2, max_frame, end))

    good = int(GOOD_PAUSE_SECONDS / FRAME_SECONDS)
    low = int(min_seconds / FRAME_SECONDS)
    candidates = [
        cut_point(a, b) for a, b in pauses if b - a >= good and a <= max_frame and low <= cut_point(a, b) <= max_frame
    ]
    if candidates:
        return max(candidates), False

    short = int(SHORT_PAUSE_SECONDS / FRAME_SECONDS)
    floor = int(MIN_FALLBACK_CUT_SECONDS / FRAME_SECONDS)
    fallback = [
        (b - a, cut_point(a, b))
        for a, b in pauses
        if b - a >= short and a <= max_frame and floor <= cut_point(a, b) <= max_frame
    ]
    if fallback:
        return max(fallback)[1], False
    return max_frame, True


def speech_span(mask: np.ndarray) -> tuple[int, int] | None:
    """Padded [start, end) frame range containing speech, or None if there is too little speech."""
    speech = np.flatnonzero(mask)
    if len(speech) * FRAME_SECONDS < MIN_SPEECH_SECONDS:
        return None
    pad = int(EDGE_PAD_SECONDS / FRAME_SECONDS)
    return max(0, int(speech[0]) - pad), min(len(mask), int(speech[-1]) + 1 + pad)


@dataclass
class AudioChunk:
    start: float  # absolute seconds of the first sample in `samples`
    end: float
    samples: np.ndarray  # float32 mono; empty when the chunk has no speech
    speech: bool = True
    overlap: float = 0.0  # seconds at the start that may repeat the previous chunk's end (forced cuts only)
    mask: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=bool), repr=False)
    window_end: float = 0.0  # absolute end of the audio this chunk consumed, before trimming (0 = same as `end`)

    @property
    def duration(self) -> float:
        return self.end - self.start


def split_chunk(chunk: AudioChunk) -> list[AudioChunk] | None:
    """Split a chunk at its pause nearest the middle, for re-decoding a suspicious result. None if too short."""
    frames = len(chunk.mask)
    if chunk.duration < 4.0 or frames < 2:
        return None
    lo, hi = frames // 4, frames * 3 // 4
    pauses = [(a, b) for a, b in runs(chunk.mask, False) if lo <= (a + b) // 2 <= hi]
    if pauses:
        best = max(pauses, key=lambda p: (p[1] - p[0], -abs((p[0] + p[1]) // 2 - frames // 2)))
        cut_frame = (best[0] + best[1]) // 2
    else:
        cut_frame = frames // 2
    cut_sample = min(len(chunk.samples), cut_frame * FRAME)
    cut_time = chunk.start + cut_sample / SAMPLE_RATE
    left = AudioChunk(chunk.start, cut_time, chunk.samples[:cut_sample], True, chunk.overlap, chunk.mask[:cut_frame])
    right = AudioChunk(cut_time, chunk.end, chunk.samples[cut_sample:], True, 0.0, chunk.mask[cut_frame:])
    return [part for part in (left, right) if len(part.samples)]
