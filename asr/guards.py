"""Cheap sanity checks on recognizer output.

Sequence-to-sequence recognizers can loop on a phrase or emit far more (or less) text than the audio holds. These
checks only *flag* a segment so it can be re-decoded and shown to the reader. They never edit or delete text:
chanting and deliberate repetition are legitimate speech.

The thresholds are conservative heuristics, not measured values for any particular model.
"""

from __future__ import annotations

from .postprocess import _key

MAX_CHARS_PER_SECOND = 35.0  # non-space characters per second of audio; above this is almost certainly a loop
MIN_CHARS_PER_SECOND = 1.0  # below this, with a few seconds of speech, words were probably dropped
MIN_SECONDS_FOR_SPARSE_CHECK = 3.0
MAX_NGRAM = 6


def _repeat_limit(size: int) -> int:
    return max(5, 9 - size)  # 1 word: 8 repeats, 2: 7, 3: 6, 4+: 5


def find_repetition(text: str) -> str | None:
    """Return the phrase if some 1-6 word group repeats back-to-back suspiciously often."""
    words = [_key(word) for word in text.split()]
    for size in range(1, MAX_NGRAM + 1):
        limit = _repeat_limit(size)
        if len(words) < size * limit:
            continue
        for start in range(len(words) - size * limit + 1):
            group = words[start : start + size]
            if not any(group):
                continue
            if all(words[start + step * size : start + (step + 1) * size] == group for step in range(1, limit)):
                return " ".join(text.split()[start : start + size])
    return None


def check_segment(text: str, seconds: float) -> list[str]:
    """Flags for one decoded chunk: any of "repetition", "too_dense", "too_sparse"."""
    flags: list[str] = []
    if find_repetition(text):
        flags.append("repetition")
    if seconds > 0:
        rate = len("".join(text.split())) / seconds
        if rate > MAX_CHARS_PER_SECOND:
            flags.append("too_dense")
        elif seconds >= MIN_SECONDS_FOR_SPARSE_CHECK and rate < MIN_CHARS_PER_SECOND:
            flags.append("too_sparse")
    return flags
