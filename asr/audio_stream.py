"""Bounded FFmpeg decoding for long recordings."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

import numpy as np

from .segmenter import (
    FRAME,
    SAMPLE_RATE,
    SPEECH_THRESHOLD,
    AudioChunk,
    SpeechDetector,
    load_detector,
    plan_cut,
    speech_span,
)

__all__ = [
    "AudioChunk",
    "CHUNK_SECONDS",
    "GUJLISH_CHUNK_SECONDS",
    "OVERLAP_SECONDS",
    "SAMPLE_RATE",
    "audio_chunks",
    "load_detector",
    "require_ffmpeg",
]

CHUNK_SECONDS = 27.5  # Flex decodes up to ~30 s windows; keep headroom
OVERLAP_SECONDS = 0.4  # shared audio on forced cuts so a word split at the edge is heard in full by one chunk
GUJLISH_CHUNK_SECONDS = 15.5  # Gujlish was trained on clips of at most 16 s
READ_BYTES = 128 * 1024


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required. Install it with: brew install ffmpeg")


def _command(source: Path, denoise: bool) -> list[str]:
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        str(source),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
    ]
    if denoise:
        command.extend(["-af", "afftdn"])
    command.extend(["-acodec", "pcm_s16le", "-f", "s16le", "pipe:1"])
    return command


def _float(pcm: np.ndarray) -> np.ndarray:
    return pcm.astype(np.float32) / 32768.0


def _pause_chunk(
    window: np.ndarray,
    start_sample: int,
    *,
    detector: SpeechDetector,
    max_seconds: float | None,
    carried_overlap: float,
) -> tuple[AudioChunk, int, bool]:
    """Cut `window` (int16) at a pause, trim silence, and describe the result.

    Returns (chunk, samples consumed from the window, whether the cut was forced). With max_seconds=None the whole
    window is one chunk (end of file).
    """
    mask = detector.frame_probs(window) >= SPEECH_THRESHOLD
    if max_seconds is None:
        cut_frame, forced = len(mask), False
        cut = len(window)
    else:
        cut_frame, forced = plan_cut(mask, max_seconds=max_seconds)
        cut = min(len(window), cut_frame * FRAME)
    mask = mask[:cut_frame]
    base = start_sample / SAMPLE_RATE
    window_end = base + cut / SAMPLE_RATE
    span = speech_span(mask)
    if span is None:
        silent = AudioChunk(
            base, window_end, np.zeros(0, dtype=np.float32), False, carried_overlap, mask, window_end=window_end
        )
        return silent, cut, forced
    first, last = span
    s0, s1 = first * FRAME, min(cut, last * FRAME)
    chunk = AudioChunk(
        (start_sample + s0) / SAMPLE_RATE,
        (start_sample + s1) / SAMPLE_RATE,
        _float(window[s0:s1]),
        True,
        # Trimmed-away leading audio cannot contain duplicated speech, so no overlap repair is needed then.
        carried_overlap if s0 == 0 else 0.0,
        mask[first:last],
        window_end=window_end,
    )
    return chunk, cut, forced


def audio_chunks(
    source: Path,
    *,
    chunk_seconds: float = CHUNK_SECONDS,
    chunking: str = "pause",
    detector: SpeechDetector | None = None,
    denoise: bool = False,
):
    """Yield float32 mono chunks without decoding the whole file.

    chunking="pause": cut at the latest usable pause, trim edge silence, and yield silent chunks with
    speech=False (so chunk indexes stay stable for checkpoint resume). Overlap is kept only for forced cuts.
    chunking="fixed": the original behavior: fixed cuts with a 0.4 s overlap and no trimming.
    """
    if chunking not in {"pause", "fixed"}:
        raise ValueError(f"Unknown chunking mode {chunking!r}")
    if chunking == "pause" and detector is None:
        detector = load_detector("auto")
    require_ffmpeg()
    max_samples = round(chunk_seconds * SAMPLE_RATE)
    overlap = round(OVERLAP_SECONDS * SAMPLE_RATE)
    pending = bytearray()
    start_sample = 0
    carried = 0.0  # overlap seconds the next chunk will repeat from this one
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(_command(source, denoise), stdout=subprocess.PIPE, stderr=errors)
        assert process.stdout is not None
        try:
            while True:
                block = process.stdout.read(READ_BYTES)
                if not block:
                    break
                pending.extend(block)
                while len(pending) // 2 >= max_samples:
                    window = np.frombuffer(pending, dtype="<i2", count=max_samples).copy()
                    if chunking == "fixed":
                        chunk = AudioChunk(
                            start_sample / SAMPLE_RATE,
                            (start_sample + max_samples) / SAMPLE_RATE,
                            _float(window),
                            True,
                            carried,
                        )
                        cut, forced = max_samples, True
                    else:
                        chunk, cut, forced = _pause_chunk(
                            window, start_sample, detector=detector, max_seconds=chunk_seconds, carried_overlap=carried
                        )
                    yield chunk
                    advance = max(1, cut - overlap) if forced else max(1, cut)
                    del pending[: advance * 2]
                    start_sample += advance
                    carried = OVERLAP_SECONDS if forced else 0.0

            code = process.wait()
            if code:
                errors.seek(0)
                detail = errors.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"FFmpeg could not decode an audio stream from {source}:\n{detail}")
            remaining = len(pending) // 2
            # A tail no longer than the overlap is already covered by the previous chunk.
            if remaining and (remaining > (overlap if carried else 0) or start_sample == 0):
                tail = np.frombuffer(pending, dtype="<i2", count=remaining).copy()
                if chunking == "fixed":
                    yield AudioChunk(
                        start_sample / SAMPLE_RATE,
                        (start_sample + remaining) / SAMPLE_RATE,
                        _float(tail),
                        True,
                        carried,
                    )
                else:
                    chunk, _, _ = _pause_chunk(
                        tail, start_sample, detector=detector, max_seconds=None, carried_overlap=carried
                    )
                    yield chunk
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
