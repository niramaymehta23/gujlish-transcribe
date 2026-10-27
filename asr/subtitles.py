"""SubRip (.srt) subtitles from timed transcript or translation segments."""

from __future__ import annotations

from pathlib import Path

MAX_LINE_CHARS = 42  # the usual subtitle line length
MAX_CUE_CHARS = 2 * MAX_LINE_CHARS  # at most two lines on screen at once


def srt_time(seconds: float) -> str:
    milliseconds = max(0, round(seconds * 1000))
    hours, rest = divmod(milliseconds, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _wrap(text: str) -> str:
    """Break a cue into two lines at the space nearest the middle when it is too long for one."""
    if len(text) <= MAX_LINE_CHARS or " " not in text:
        return text
    middle = len(text) // 2
    spaces = [i for i, char in enumerate(text) if char == " "]
    cut = min(spaces, key=lambda i: abs(i - middle))
    return text[:cut] + "\n" + text[cut + 1 :]


def _groups(words: list[str]) -> list[list[int]]:
    """Indexes of consecutive words, grouped so each group's text fits in one cue."""
    groups: list[list[int]] = [[]]
    length = 0
    for index, word in enumerate(words):
        added = len(word) + (1 if groups[-1] else 0)
        if groups[-1] and length + added > MAX_CUE_CHARS:
            groups.append([])
            length, added = 0, len(word)
        groups[-1].append(index)
        length += added
    return [group for group in groups if group]


def segment_cues(segment: dict) -> list[tuple[float, float, str]]:
    """Split one segment into cues. Word timings are used when they match the text (Gujlish); otherwise time is
    shared out in proportion to each cue's length (Flex and translations have segment times only)."""
    words = str(segment.get("text") or "").split()
    start, end = float(segment["start"]), float(segment["end"])
    if not words or end <= start:
        return []
    timed = segment.get("words")
    use_words = isinstance(timed, list) and len(timed) == len(words) and all("start" in w and "end" in w for w in timed)
    total_chars = sum(len(word) for word in words) or 1
    cues, consumed = [], 0
    for group in _groups(words):
        text = " ".join(words[i] for i in group)
        if use_words:
            cue_start, cue_end = float(timed[group[0]]["start"]), float(timed[group[-1]]["end"])
        else:
            chars = sum(len(words[i]) for i in group)
            cue_start = start + (end - start) * consumed / total_chars
            consumed += chars
            cue_end = start + (end - start) * consumed / total_chars
        cues.append((max(start, cue_start), min(end, max(cue_end, cue_start + 0.01)), _wrap(text)))
    return cues


def to_srt(segments: list[dict]) -> str:
    blocks = []
    for segment in segments:
        for start, end, text in segment_cues(segment):
            blocks.append(f"{len(blocks) + 1}\n{srt_time(start)} --> {srt_time(end)}\n{text}\n")
    return "\n".join(blocks)


def write_srt(path: Path, segments: list[dict]) -> Path:
    path.write_text(to_srt(segments), encoding="utf-8")
    return path
