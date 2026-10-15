"""Bounded FFmpeg decoding for long recordings."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16_000
CHUNK_SECONDS = 27.5
OVERLAP_SECONDS = 0.4
GUJLISH_CHUNK_SECONDS = 15.5
READ_BYTES = 128 * 1024


@dataclass
class AudioChunk:
    start: float
    end: float
    samples: np.ndarray


def require_ffmpeg() -> None:
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required. Install it with: brew install ffmpeg")


def _quiet_cut(samples: np.ndarray, target_seconds: float) -> int:
    """Choose a low-energy boundary near 27.5 s for opt-in VAD chunking."""
    frame = SAMPLE_RATE // 50
    frames = samples[: len(samples) // frame * frame].reshape(-1, frame)
    if not len(frames):
        return min(len(samples), round(target_seconds * SAMPLE_RATE))
    rms = np.sqrt(np.mean(frames.astype(np.float32) ** 2, axis=1))
    lo, hi = int(max(1.0, target_seconds - 3.5) * 50), min(len(rms), int((target_seconds + 1.0) * 50))
    if hi <= lo:
        return min(len(samples), round(target_seconds * SAMPLE_RATE))
    threshold = max(120.0, float(np.percentile(rms[lo:hi], 25)))
    quiet = np.flatnonzero(rms[lo:hi] <= threshold) + lo
    target = int(target_seconds * 50)
    boundary = min(quiet, key=lambda value: abs(int(value) - target)) if len(quiet) else target
    return min(len(samples), int(boundary * frame))


def audio_chunks(
    source: Path,
    *,
    chunk_seconds: float = CHUNK_SECONDS,
    vad: bool = False,
    denoise: bool = False,
):
    """Yield overlapping float32 mono chunks without decoding the whole file."""
    require_ffmpeg()
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

    buffered_seconds = chunk_seconds + 1.5 if vad else chunk_seconds
    full_samples = round(buffered_seconds * SAMPLE_RATE)
    overlap = round(OVERLAP_SECONDS * SAMPLE_RATE)
    pending = bytearray()
    start_sample = 0
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=errors)
        assert process.stdout is not None
        try:
            while True:
                block = process.stdout.read(READ_BYTES)
                if not block:
                    break
                pending.extend(block)
                while len(pending) // 2 >= full_samples:
                    available = np.frombuffer(pending, dtype="<i2", count=full_samples)
                    cut = _quiet_cut(available, chunk_seconds) if vad else round(chunk_seconds * SAMPLE_RATE)
                    pcm = available[:cut].astype(np.float32) / 32768.0
                    del available  # release the bytearray view before resizing pending
                    yield AudioChunk(start_sample / SAMPLE_RATE, (start_sample + cut) / SAMPLE_RATE, pcm)
                    advance = max(1, cut - overlap)
                    del pending[: advance * 2]
                    start_sample += advance

            code = process.wait()
            if code:
                errors.seek(0)
                detail = errors.read().decode("utf-8", errors="replace").strip()
                raise RuntimeError(f"FFmpeg could not decode an audio stream from {source}:\n{detail}")
            remaining = len(pending) // 2
            if remaining > overlap or start_sample == 0:
                pcm = np.frombuffer(pending[: remaining * 2], dtype="<i2").astype(np.float32) / 32768.0
                if len(pcm):
                    yield AudioChunk(start_sample / SAMPLE_RATE, (start_sample + len(pcm)) / SAMPLE_RATE, pcm)
        finally:
            process.stdout.close()
            if process.poll() is None:
                process.kill()
            process.wait()
