"""What a talk contributes to search: ~40-second passages of its lines and the points of its summary.

Lines are grouped by *time* (every segment starting in the same 40-second window), never by count, so correcting a word
changes the text of one passage and moves no boundary: only that passage is re-indexed and re-embedded.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import re
from dataclasses import astuple, dataclass

WINDOW = 40.0
MEANING_LIMIT = 6000  # characters sent for one embedding; a 40-second passage is far shorter
TIME = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class Passage:
    kind: str  # "line" (transcript lines), "point" (a summary point) or "summary" (the summary's overview)
    n: int  # its number within the talk for that kind
    start: float | None  # seconds; None for a summary point without a time
    end: float | None
    seg: int | None  # the transcript segment the moment opens at
    label: str = ""
    gu: str = ""  # Gujarati
    gl: str = ""  # Gujlish
    en: str = ""  # English
    sm: str = ""  # summary text (English)
    # A "line" passage's lines, in order: (segment, start, then the length of its Gujarati, Gujlish and English text,
    # 0 where missing). With these a match found in the joined text is traced back to the line it is in.
    lines: tuple = ()

    @property
    def digest(self) -> str:
        return hashlib.sha1(json.dumps(astuple(self), ensure_ascii=False).encode("utf-8")).hexdigest()[:20]

    def meaning_text(self) -> str:
        return meaning_text(self.kind, self.label, self.gu, self.en, self.sm)


def meaning_text(kind: str, label: str, gu: str, en: str, sm: str) -> str:
    """What is embedded: a passage's Gujarati and English (Gujlish only repeats the Gujarati), or a summary point."""
    if kind == "line":
        text = "\n".join(part for part in (gu, en) if part)
    else:
        text = f"{label}: {sm}" if label and sm else sm
    return text[:MEANING_LIMIT]


def _text(value) -> str:
    return value.strip() if isinstance(value, str) else ""


def _seconds(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def line_passages(segments: list[dict], gujlish: list[list[str]] | None = None, english=None) -> list[Passage]:
    windows: dict[int, list[int]] = {}
    for at, segment in enumerate(segments):
        if not isinstance(segment, dict):
            continue
        start = _seconds(segment.get("start"))
        if start is not None and _text(segment.get("text")):
            windows.setdefault(int(start // WINDOW), []).append(at)
    passages = []
    for n, members in sorted(windows.items()):
        first, last = segments[members[0]], segments[members[-1]]
        gu = [_text(segments[at].get("text")) for at in members]
        gl = [" ".join(gujlish[at]).strip() if gujlish and at < len(gujlish) and gujlish[at] else "" for at in members]
        en = [_text(english[at]) if english and at < len(english) else "" for at in members]
        passages.append(
            Passage(
                kind="line",
                n=n,
                start=_seconds(first.get("start")),
                end=_seconds(last.get("end")) or _seconds(last.get("start")),
                seg=members[0],
                gu=" ".join(gu),
                gl=" ".join(filter(None, gl)),
                en=" ".join(filter(None, en)),
                lines=tuple(
                    (at, _seconds(segments[at].get("start")), len(a), len(b), len(c))
                    for at, a, b, c in zip(members, gu, gl, en)
                ),
            )
        )
    return passages


def line_at(lines, column: str, offset: int) -> tuple[int, float] | None:
    """(segment, start) of the line holding character `offset` of a line passage's joined `column` text."""
    width = {"gu": 2, "gl": 3, "en": 4}.get(column)
    if width is None or offset < 0:
        return None
    end = -1  # where the previous line's text ended; the next begins one space later
    for line in lines:
        if line[width]:
            end += 1 + line[width]
            if offset < end:
                return line[0], line[1]
    return None


def summary_passages(summary: dict, segments: list[dict]) -> list[Passage]:
    starts = [_seconds(segment.get("start")) or 0.0 if isinstance(segment, dict) else 0.0 for segment in segments]

    def moment(value) -> tuple[float | None, int | None]:
        match = TIME.match(_text(value))
        if not match:
            return None, None
        hours, minutes, seconds = (int(part or 0) for part in match.groups())
        at = hours * 3600 + minutes * 60 + seconds
        if not starts:
            return float(at), None
        seg = max(0, bisect.bisect_right(starts, at + 0.001) - 1)
        return starts[seg], seg

    points = []

    def add(label, text, time=None):
        if text:
            start, seg = moment(time)
            points.append(Passage("point", len(points), start, start, seg, label=label, sm=text))

    for theme in summary.get("themes") or []:
        if isinstance(theme, dict):
            for point in theme.get("points") or []:
                if isinstance(point, dict):
                    add(_text(theme.get("theme")) or "Theme", _text(point.get("point")), point.get("t"))
    for story in summary.get("stories") or []:
        if isinstance(story, dict):
            title = _text(story.get("title"))
            text = " ".join(filter(None, (_text(story.get("what_happened")), _text(story.get("point_of_story")))))
            add(f"Story: {title}" if title else "Story", text, story.get("start"))
    for quote in summary.get("quotes") or []:
        if isinstance(quote, dict):
            add("Quote", _text(quote.get("quote")), quote.get("t"))
    for ask in summary.get("asks") or []:
        if isinstance(ask, dict):
            add("Ask", _text(ask.get("item")), ask.get("t"))
    for term in summary.get("names_terms") or []:
        if isinstance(term, dict) and _text(term.get("term")):
            add("Term", ": ".join(filter(None, (_text(term.get("term")), _text(term.get("note"))))))
    overview = " ".join(filter(None, (_text(summary.get("one_line")), _text(summary.get("overview")))))
    head = [Passage("summary", 0, None, None, None, label="Summary", sm=overview)] if overview else []
    return head + points


def build_passages(segments, gujlish=None, english=None, summary=None) -> list[Passage]:
    """Every passage of one talk. `gujlish` and `english` line up with `segments` (None where a line is missing);
    `summary` is the summary JSON's "summary" object."""
    segments = segments if isinstance(segments, list) else []
    passages = line_passages(segments, gujlish, english)
    if isinstance(summary, dict):
        passages += summary_passages(summary, segments)
    return passages
