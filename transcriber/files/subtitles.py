"""SubRip (.srt) subtitles from timed transcript or translation segments."""

from __future__ import annotations

from pathlib import Path

MAX_LINE_CHARS = 42  # the usual subtitle line length
MAX_CUE_CHARS = 2 * MAX_LINE_CHARS  # at most two lines on screen at once
MIN_CUE_CHARS = 20  # a cue shorter than this is too brief to read; a short sentence shares the next one's cue
SENTENCE_END = ".?!…।॥"
PHRASE_END = ",;:—–"
QUOTES = "\"'”’)]"
# Verbs and auxiliaries that usually end a Gujarati clause, in Gujarati and in their Gujlish spellings.
CLAUSE_END = {
    "છે", "છું", "છો", "છીએ", "નથી", "હતો", "હતી", "હતું", "હતા", "હતાં", "હશે", "જોઈએ", "થાય", "થયું", "થયો", "થયા",
    "che", "chu", "cho", "chiye", "nathi", "hato", "hati", "hatu", "hata", "hashe", "joie", "thay", "thayu", "thayo", "thaya",
}  # fmt: skip


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


def _strength(word: str) -> int:
    """How good a place the end of this word is for a cue to end: 3 after a sentence, 2 after a phrase, 1 after a
    Gujarati clause-final verb (the recognizer rarely punctuates inside a chunk), else 0."""
    if word.rstrip(QUOTES)[-1:] in SENTENCE_END:
        return 3
    if word.rstrip(QUOTES)[-1:] in PHRASE_END:
        return 2
    return 1 if word.casefold() in CLAUSE_END else 0


def _groups(words: list[str]) -> list[list[int]]:
    """Indexes of consecutive words, grouped so each group fits in one cue. A cue ends after a sentence once it is
    long enough to read, and a cue that would overflow is cut at its best break: a sentence end, then a phrase end,
    then a clause end, then wherever it is full."""
    groups: list[list[int]] = [[]]

    def length(group: list[int]) -> int:
        return sum(len(words[i]) for i in group) + max(0, len(group) - 1)

    def cut(group: list[int]) -> int:
        """Where to end an overflowing cue. A sentence end may leave a short cue; a weaker break must fill half."""
        best = (0, len(group))
        for at, i in enumerate(group[:-1], 1):
            strength = _strength(words[i])
            if length(group[:at]) >= (MIN_CUE_CHARS if strength == 3 else MAX_CUE_CHARS // 2):
                best = max(best, (strength, at))
        return best[1] if best[0] else len(group)

    for index, word in enumerate(words):
        group = groups[-1]
        while group and length(group) + 1 + len(word) > MAX_CUE_CHARS:
            at = cut(group)
            groups[-1], group = group[:at], group[at:]
            groups.append(group)
        group.append(index)
        if _strength(word) == 3 and length(group) >= MIN_CUE_CHARS:
            groups.append([])
    groups = [group for group in groups if group]
    if len(groups) > 1 and length(groups[-1]) < MIN_CUE_CHARS:  # no word or two left dangling at the end
        if length(groups[-2] + groups[-1]) <= MAX_CUE_CHARS:
            groups[-2:] = [groups[-2] + groups[-1]]
        else:
            while length(groups[-1]) < MIN_CUE_CHARS and length(groups[-2][:-1]) >= MIN_CUE_CHARS:
                groups[-1].insert(0, groups[-2].pop())
    return groups


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
