#!/usr/bin/env python3
"""The studio's exact work: folders and names, clip limits, cutting on real pauses, storyboard checks and rendering.

Claude Code makes the judgement calls (which clips, what story to tell) and writes them as JSON. This tool checks those
files instead of trusting them, so a clip can't run past its limit and a storyboard can't leave a gap. CLAUDE.md, next
to this folder, describes the whole workflow. Only the Python standard library and ffmpeg are needed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("STUDIO_HOME") or Path(__file__).resolve().parents[1])
REMOTION = Path(__file__).resolve().parents[1] / "remotion"
INBOX = HOME / "inbox"
TALKS = HOME / "talks"

AUDIO_TYPES = {".m4a", ".mp3", ".wav", ".aac", ".flac", ".ogg", ".opus"}
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".webm", ".m4v"}
SENTENCE_END = ".?!…"
QUOTES = "\"'”’)]"
LINE_GAP_SECONDS = 1.2  # a silence this long in the subtitles also ends a line

# Hard limits: Reels and Shorts allow at most 3:00, and shorter than 0:30 is not worth posting.
LIMITS = {"short": (30.0, 180.0), "long": (180.0, 600.0)}
SNAP_WINDOW_SECONDS = 2.5  # how far a cut may move to land on a real pause
SNAP_THRESHOLDS_DB = (-35, -30, -25, -20)  # a quiet room first, then a noisier hall
OVERLAP_SHARE = 0.5  # two clips of one format sharing more than this of the shorter one are the same clip
CAPTION_CHARS = {"short": 60, "long": 84}
FPS = 30
SIZES = {"short": (1080, 1920), "long": (1920, 1080)}

TITLE_LIMIT = 100  # YouTube
DESCRIPTION_LIMIT = 5000  # YouTube
INSTAGRAM_LIMIT = 2200

# Scene types: text fields with their length limits, and list fields with (fewest, most, limit per item).
SCENES = {
    "hook": {"required": {"headline": 70}, "optional": {"kicker": 30}},
    "punch": {"required": {"text": 32}, "optional": {"sub": 50}},
    "quote": {"required": {"text": 130}, "optional": {}},
    "compare": {"required": {"title": 50}, "optional": {}, "rows": (2, 5, 26)},
    "list": {"required": {"title": 50}, "optional": {}, "items": (2, 5, 52)},
    "agna": {"required": {"text": 90}, "optional": {"label": 20}},
    "story": {"required": {}, "optional": {"place": 40}, "lines": (1, 5, 72)},
    "speaker": {"required": {}, "optional": {"label": 40}},
    "end": {"required": {"text": 60}, "optional": {"sub": 60}},
    "picture": {"required": {}, "optional": {"kicker": 30, "title": 50, "stamp": 18}, "chips": (1, 6, 26)},
}
SETTINGS = ("city", "palace", "water", "hills", "arch", "sky")  # drawn in remotion/src/picture.tsx
EFFECTS = ("rays", "fall")
MIN_SCENE_SECONDS = 2.0
# Pacing, as warnings: a graphic alone on screen goes stale fast, one beside the speaker lasts a little longer, the hook
# must land at once, and a long stretch of the speaker alone wants a graphic to lift it.
HOLD_SECONDS = {"full": 8.0, "split": 15.0, "hook": 4.5, "speaker": 15.0, "picture": 12.0}
STYLES = ("paper", "engraving", "editorial")  # the art directions in remotion/src/looks.ts, all light
LAYOUTS = ("full", "split")
ICON_INDEX = REMOTION / "node_modules" / "@phosphor-icons" / "react" / "dist" / "index.d.ts"
# Generated illustrations (Gemini). The key comes from GEMINI_API_KEY, or the macOS Keychain item below; never a file.
GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
KEYCHAIN_ITEM = "harisena-gemini"
PICTURE_DESCRIPTION = (12, 300)
# Words that would put a person in a generated picture. The only images of people are from the talk's own video.
PEOPLE_WORDS = (
    "man",
    "men",
    "woman",
    "women",
    "person",
    "people",
    "boy",
    "girl",
    "child",
    "children",
    "king",
    "queen",
    "saint",
    "sadhu",
    "swami",
    "swamiji",
    "guru",
    "guruhari",
    "god",
    "krishna",
    "arjuna",
    "arjun",
    "akbar",
    "birbal",
    "bhagwan",
    "maharaj",
    "devotee",
    "face",
    "faces",
    "hand",
    "hands",
    "figure",
    "portrait",
    "student",
    "students",
    "crowd",
    "farmer",
    "merchant",
)
PICTURE_STYLES = {
    "paper": "a handmade cut-paper craft illustration: layered coloured card with visible paper grain and soft shadows "
    "between the layers, warm colours (terracotta, mustard, sage, cream)",
    "editorial": "a flat, bold, modern editorial vector illustration: clean geometric shapes, a few flat colours "
    "(warm orange, sunny yellow, cream, deep green), minimal shading",
    "engraving": "a classic engraved print illustration: fine cross-hatching and line work in dark sepia ink with a "
    "few washes of muted gold and red, like an old book plate",
}
LOOK_GROUNDS = {"paper": "#ece3d1", "engraving": "#efe7d6", "editorial": "#f4ecdc"}


class StudioError(Exception):
    pass


# ---- Text and timing ---------------------------------------------------------------------------------------------


def fmt(seconds: float) -> str:
    whole = int(seconds)
    return f"{whole // 60}:{whole % 60:02d}"


def stamp(seconds: float) -> str:
    tenths = round(seconds * 10)
    return f"{tenths // 600:02d}:{tenths % 600 / 10:04.1f}"


def parse_srt(text: str) -> list[dict]:
    """The cues of an SRT file, skipping any block that has no valid time line."""
    cues = []
    pattern = re.compile(r"(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)")
    for block in re.split(r"\n\s*\n", text.replace("﻿", "").replace("\r\n", "\n").strip()):
        lines = block.split("\n")
        for index, line in enumerate(lines):
            found = pattern.search(line)
            if found:
                h1, m1, s1, ms1, h2, m2, s2, ms2 = (int(value) for value in found.groups())
                words = " ".join(" ".join(lines[index + 1 :]).split())
                if words:
                    start = h1 * 3600 + m1 * 60 + s1 + ms1 / 1000
                    cues.append({"start": start, "end": h2 * 3600 + m2 * 60 + s2 + ms2 / 1000, "text": words})
                break
    return cues


def timed_words(cues: list[dict]) -> list[dict]:
    """Each word with an estimated time: a cue's time is shared out by word length. Subtitles carry no word timings."""
    words = []
    for cue in cues:
        parts = cue["text"].split()
        total = sum(len(part) for part in parts) or 1
        used = 0
        span = max(0.0, cue["end"] - cue["start"])
        for part in parts:
            start = cue["start"] + span * used / total
            used += len(part)
            words.append({"start": start, "end": cue["start"] + span * used / total, "text": part})
    return words


def speech_words(cues: list[dict], pauses: list[tuple[float, float]]) -> list[dict]:
    """Each word with a time that follows the real speech: a cue's words are shared out over the stretches where he is
    actually speaking (the cue minus the pauses inside it), not evenly over the whole cue. So a caption starts when he
    starts talking, and a cue split across a pause splits there."""
    words = []
    for cue in cues:
        parts = cue["text"].split()
        if not parts:
            continue
        runs, at = [], cue["start"]
        for low, high in sorted(pauses):
            if high <= at or low >= cue["end"]:
                continue
            if low - at >= 0.15:
                runs.append((at, low))
            at = max(at, high)
        if cue["end"] - at >= 0.15:
            runs.append((at, cue["end"]))
        if not runs:
            runs = [(cue["start"], cue["end"])]
        speech = sum(high - low for low, high in runs)

        def place(share: float, starting: bool) -> float:
            """The moment `share` of the cue's speech has been said. A word starting exactly at a pause starts
            when he speaks again; one ending there ends as he stops."""
            left = share * speech
            for low, high in runs:
                if left < high - low - 1e-9 or (not starting and left <= high - low + 1e-9):
                    return low + left
                left -= high - low
            return runs[-1][1]

        total = sum(len(part) for part in parts)
        span = max(0.0, cue["end"] - cue["start"])
        used = 0
        for part in parts:
            start = place(used / total, True)
            # where timed_words puts the word's middle: the sentence lines a clip is chosen by are cut on those
            # times, so a clip takes exactly the words of its lines, whatever their speech timing
            mid = cue["start"] + span * (used + len(part) / 2) / total
            used += len(part)
            # a word ends where its own run ends, never across a pause
            end = place(used / total, False)
            run_end = next((high for low, high in runs if low - 1e-9 <= start < high), end)
            words.append({"start": start, "end": min(end, run_end), "text": part, "mid": mid})
    return words


def speech_pauses(media: Path) -> list[tuple[float, float]]:
    """The pauses in the whole recording, at the quietest threshold that finds a natural share of them."""
    length = media_duration(media)
    for threshold in SNAP_THRESHOLDS_DB:
        pauses = silences(media, 0.0, length, threshold)
        if sum(high - low for low, high in pauses) >= 0.08 * length:
            return pauses
    return pauses


def ends_sentence(word: str) -> bool:
    return word.rstrip(QUOTES)[-1:] in SENTENCE_END


def sentence_lines(words: list[dict]) -> list[dict]:
    """Whole sentences with their times, numbered from 1, so a clip can begin where a thought begins. The subtitles
    themselves break mid-sentence."""
    lines: list[dict] = []
    current: list[dict] = []

    def close():
        if current:
            lines.append(
                {
                    "line": len(lines) + 1,
                    "start": round(current[0]["start"], 3),
                    "end": round(current[-1]["end"], 3),
                    "text": " ".join(word["text"] for word in current),
                }
            )
            current.clear()

    for word in words:
        if current and word["start"] - current[-1]["end"] > LINE_GAP_SECONDS:
            close()
        current.append(word)
        if ends_sentence(word["text"]):
            close()
    close()
    return lines


# Small words that lean on the word after them; a caption page never ends on one.
HANGING_WORDS = frozenset(
    "a an the of to in on at by for from with and but or nor so as than that this these those is are was were be "
    "been has have had my your his her its our their just very not no".split()
)


def piece_words(words: list[dict], start: float, end: float, span: tuple[float, float] | None = None) -> list[dict]:
    """The words of one piece of a clip: those of its own sentence lines (`span`) that are spoken inside the piece, so
    nothing cut away (past the clip's end, or across a trimmed pause) is shown."""
    low, high = span or (start, end)
    return [
        word
        for word in words
        if low - 0.01 <= word.get("mid", (word["start"] + word["end"]) / 2) <= high + 0.01
        and start - 0.3 <= (word["start"] + word["end"]) / 2 <= end + 0.3
    ]


def clip_words(words: list[dict], clip: dict) -> list[dict]:
    """Every word of a clip with when he says it, in clip time: what graphics are timed to."""
    timed, offset = [], 0.0
    for segment in segments_of(clip):
        span = (segment["line_start"], segment["line_end"])
        for word in piece_words(words, segment["start"], segment["end"], span):
            at = max(0.0, word["start"] - segment["start"]) + offset
            timed.append({"text": word["text"], "at": round(at, 3)})
        offset += segment["end"] - segment["start"]
    return timed


def caption_pages(
    words: list[dict], start: float, end: float, max_chars: int, span: tuple[float, float] | None = None
) -> list[dict]:
    """Caption pages for one clip, in clip time. A page ends at a sentence, or when full, and stays up across a short
    pause so captions don't flicker. `span` is where the clip's own sentences run: a cut moved onto a pause can reach
    past them, and the neighbouring words must not show up in its captions."""
    inside = piece_words(words, start, end, span)
    pages: list[list[dict]] = [[]]
    for word in inside:
        page = pages[-1]
        size = sum(len(w["text"]) + 1 for w in page)
        if page and size + len(word["text"]) > max_chars:
            # break where he pauses if there is such a place in the page's second half, else after a comma or dash
            def good(at: int, test) -> bool:
                return test(at) and sum(len(w["text"]) + 1 for w in page[:at]) >= max_chars // 2

            def pause(at: int) -> bool:
                return page[at]["start"] - page[at - 1]["end"] >= 0.2

            def comma(at: int) -> bool:
                return page[at - 1]["text"].rstrip(QUOTES)[-1:] in ",;:—–"

            def loose(at: int) -> bool:
                """A page may not end on a word that leans on the next one ("just an | hour")."""
                return page[at - 1]["text"].lower().strip("\"'“”‘’") in HANGING_WORDS

            cut = next((at for at in range(len(page) - 1, 0, -1) if good(at, comma)), None)
            if cut is None:
                cut = next((at for at in range(len(page) - 1, 0, -1) if good(at, pause) and not loose(at)), None)
            if cut is None:
                cut = len(page)
                while cut > 1 and loose(cut) and sum(len(w["text"]) + 1 for w in page[: cut - 1]) >= max_chars // 3:
                    cut -= 1
            pages[-1], page = page[:cut], page[cut:]
            pages.append(page)
        page.append(word)
        if ends_sentence(word["text"]) and sum(len(w["text"]) + 1 for w in page) >= max_chars // 3:
            pages.append([])
    pages = [page for page in pages if page]
    result = []
    for page in pages:
        result.append(
            {
                "text": " ".join(w["text"] for w in page),
                "from": round(max(0.0, page[0]["start"] - start), 3),
                "to": round(min(end - start, page[-1]["end"] - start), 3),
            }
        )
    for current, following in zip(result, result[1:]):
        if following["from"] - current["to"] < 0.6:
            current["to"] = following["from"]
    return result


def safe_name(title: str, limit: int = 90) -> str:
    """A title as a file or folder name: readable, with nothing a filesystem or a shell would trip over."""
    name = re.sub(r"[^\w '()&,.\-]", " ", title, flags=re.UNICODE)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:limit].rstrip(" .") or "Untitled"


def youtube_title(summary_text: str) -> str | None:
    """The suggested title in a summary downloaded from the web tool."""
    found = re.search(r"^## YouTube title\s*\n+(.+)$", summary_text, flags=re.MULTILINE)
    return found.group(1).strip() if found else None


def looks_english(cues: list[dict]) -> bool:
    letters = [char for cue in cues for char in cue["text"] if char.isalpha()]
    return bool(letters) and sum(char.isascii() for char in letters) / len(letters) > 0.8


# ---- Clips -------------------------------------------------------------------------------------------------------


def check_candidate(item: object, lines: list[dict]) -> dict:
    """One proposed clip with its times from the transcript; `problem` says why it can't be used (None when it can)."""
    item = item if isinstance(item, dict) else {}
    clip = {key: str(item.get(key) or "").strip() for key in ("type", "title", "hook", "why", "concerns", "caption")}
    clip["format"] = item.get("format")
    try:
        clip["score"] = max(0, min(10, int(item.get("score", 0))))
    except (TypeError, ValueError):
        clip["score"] = 0
    clip["problem"] = None
    if clip["format"] not in LIMITS:
        clip["problem"] = f"format must be short or long, not {clip['format']!r}"
        return clip
    first, last = item.get("start_line"), item.get("end_line")
    if not (isinstance(first, int) and isinstance(last, int) and 1 <= first <= last <= len(lines)):
        clip["problem"] = f"not a real line range ({first!r} to {last!r})"
        return clip
    kept, problem = kept_ranges(first, last, item.get("skip"))
    if problem:
        clip["problem"] = problem
        return clip
    clip.update(first_line=first, last_line=last, start=lines[first - 1]["start"], end=lines[last - 1]["end"])
    clip.update(line_start=clip["start"], line_end=clip["end"])
    clip["segments"] = [
        {"start": lines[a - 1]["start"], "end": lines[b - 1]["end"], "first_line": a, "last_line": b} for a, b in kept
    ]
    for segment in clip["segments"]:
        segment.update(line_start=segment["start"], line_end=segment["end"])
    clip["skipped"] = [[a, b] for a, b in skipped_ranges(kept)]
    clip["text"] = " … ".join(" ".join(line["text"] for line in lines[a - 1 : b]) for a, b in kept)
    clip["duration"] = segments_length(clip["segments"])
    clip["problem"] = limit_problem(clip)
    return clip


def kept_ranges(first: int, last: int, skip: object) -> tuple[list[tuple[int, int]], str | None]:
    """The line ranges a clip keeps once its `skip` ranges (lines to leave out from inside it) are taken away."""
    if skip in (None, []):
        return [(first, last)], None
    if not isinstance(skip, list) or not all(
        isinstance(pair, list) and len(pair) == 2 and all(isinstance(n, int) for n in pair) for pair in skip
    ):
        return [], "skip must be a list of [first_line, last_line] pairs"
    kept, at = [], first
    for low, high in sorted(skip):
        if not first < low <= high < last:
            return [], f"skip {low}-{high} must be inside the clip, leaving its first and last lines"
        if low < at:
            return [], f"skip {low}-{high} overlaps another skip"
        kept.append((at, low - 1))
        at = high + 1
    kept.append((at, last))
    return kept, None


def skipped_ranges(kept: list[tuple[int, int]]) -> list[tuple[int, int]]:
    return [(a[1] + 1, b[0] - 1) for a, b in zip(kept, kept[1:])]


def segments_length(segments: list[dict]) -> float:
    return round(sum(segment["end"] - segment["start"] for segment in segments), 3)


def segments_of(clip: dict) -> list[dict]:
    """A clip's pieces of the recording. Clips listed before skips existed are one piece."""
    return clip.get("segments") or [
        {"start": clip["start"], "end": clip["end"], "line_start": clip["line_start"], "line_end": clip["line_end"]}
    ]


EMPHASIS_SMALL_WORDS = frozenset(
    "a an the of to in on at by for from with and but or so as is are was were be it this that he she we you they i "
    "his her our your their my me him them us what how".split()
)


def repeated_phrases(pages: list[dict]) -> list[dict]:
    """Phrases he says more than once in a clip, with each time he says them: repetition is how he marks a big point,
    and the storyboard should make it a big moment on screen. Longest first; shorter phrases inside a reported one
    are left out."""
    words = []
    for index, page in enumerate(pages):
        for word in page["text"].split():
            clean = re.sub(r"[^\w'-]", "", word.lower())
            if clean:
                words.append((clean, index, page["from"]))
    found: dict[str, list[float]] = {}
    for size in range(6, 1, -1):
        for at in range(len(words) - size + 1):
            gram = words[at : at + size]
            if len({page for _, page, _ in gram}) > 1:
                continue  # a phrase across two pages isn't one phrase
            if sum(word not in EMPHASIS_SMALL_WORDS for word, _, _ in gram) < 2:
                continue
            times = found.setdefault(" ".join(word for word, _, _ in gram), [])
            if gram[0][2] not in times:
                times.append(gram[0][2])
    kept: dict[str, list[float]] = {}
    for text, times in sorted(found.items(), key=lambda item: -len(item[0].split())):
        if len(times) > 1 and not any(text in longer and len(times) <= len(more) for longer, more in kept.items()):
            kept[text] = times
    repeated = [{"text": text, "times": times} for text, times in kept.items()]
    return sorted(repeated, key=lambda item: (-len(item["text"].split()), item["times"][0]))


def trim_long_pauses(segments: list[dict], pauses: list[tuple[float, float]]) -> list[dict]:
    """The pieces of a clip with any long silence inside them cut out, such as a pause to drink water. Each side keeps
    a short breath of the pause, so the speech doesn't run together."""
    trimmed = []
    for segment in segments:
        at = segment["start"]
        for low, high in sorted(pauses):
            if high - low >= LONG_PAUSE_SECONDS and segment["start"] + 1 < low and high < segment["end"] - 1:
                trimmed.append({**segment, "start": at, "end": round(low + PAUSE_KEPT_SECONDS, 3)})
                at = round(high - PAUSE_KEPT_SECONDS, 3)
        trimmed.append({**segment, "start": at})
    return trimmed


def clip_captions(words: list[dict], clip: dict, max_chars: int) -> list[dict]:
    """Caption pages for a clip in clip time, each piece's captions following on from the last piece's."""
    pages, offset = [], 0.0
    for segment in segments_of(clip):
        span = (segment["line_start"], segment["line_end"])
        for page in caption_pages(words, segment["start"], segment["end"], max_chars, span):
            pages.append({**page, "from": round(page["from"] + offset, 3), "to": round(page["to"] + offset, 3)})
        offset += segment["end"] - segment["start"]
    return pages


def joins_of(clip: dict) -> list[float]:
    """Clip times where one piece of the recording ends and the next begins."""
    joins, at = [], 0.0
    for segment in segments_of(clip)[:-1]:
        at += segment["end"] - segment["start"]
        joins.append(round(at, 3))
    return joins


def limit_problem(clip: dict) -> str | None:
    low, high = LIMITS[clip["format"]]
    if clip["duration"] < low:
        return f"too short for {clip['format']} ({fmt(clip['duration'])}; the minimum is {fmt(low)})"
    if clip["duration"] > high:
        return f"too long for {clip['format']} ({fmt(clip['duration'])}; the maximum is {fmt(high)})"
    return None


def select(candidates: list[dict]) -> tuple[list[dict], list[dict]]:
    """Valid clips, best first, one of each passage per format; and the rejected ones with their reasons."""
    rejected = [clip for clip in candidates if clip["problem"]]
    kept: list[dict] = []
    for clip in sorted((c for c in candidates if not c["problem"]), key=lambda c: (-c["score"], c["start"])):
        twin = next(
            (
                other
                for other in kept
                if other["format"] == clip["format"]
                and max(0.0, min(clip["end"], other["end"]) - max(clip["start"], other["start"]))
                / min(clip["duration"], other["duration"])
                > OVERLAP_SHARE
            ),
            None,
        )
        if twin:
            rejected.append({**clip, "problem": f"same passage as the higher-scoring “{twin['title']}”"})
        else:
            kept.append(clip)
    return kept, rejected


def run(command: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, **kwargs)


def media_info(path: Path) -> dict:
    data = run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=codec_type,width,height", "-of", "json"]
        + [str(path)]
    )
    if data.returncode:
        raise StudioError(f"Could not read {path.name}: {data.stderr.strip()}")
    return json.loads(data.stdout)


def media_duration(path: Path) -> float:
    return float(media_info(path).get("format", {}).get("duration", 0))


def silences(media: Path, start: float, seconds: float, threshold: int) -> list[tuple[float, float]]:
    """(start, end) of the pauses in a stretch of the recording, in recording time."""
    begin = max(0.0, start)
    output = run(
        ["ffmpeg", "-hide_banner", "-nostats", "-ss", f"{begin:.3f}", "-t", f"{seconds:.3f}", "-i", str(media)]
        + ["-vn", "-af", f"silencedetect=noise={threshold}dB:d=0.2", "-f", "null", "-"]
    ).stderr
    pauses, opened = [], None
    for found in re.finditer(r"silence_(start|end): (-?[\d.]+)", output):
        at = float(found.group(2)) + begin
        if found.group(1) == "start":
            opened = at
        elif opened is not None:
            pauses.append((opened, at))
            opened = None
    if opened is not None:
        pauses.append((opened, begin + seconds))
    return pauses


def snap_cut(media: Path, at: float, *, starting: bool) -> tuple[float, bool]:
    """Move a cut to the nearest real pause: a start to where the pause ends, an end to where it begins."""
    for threshold in SNAP_THRESHOLDS_DB:
        pauses = silences(media, at - SNAP_WINDOW_SECONDS, SNAP_WINDOW_SECONDS * 2, threshold)
        edges = [pause[1] if starting else pause[0] for pause in pauses]
        edges = [edge for edge in edges if abs(edge - at) <= SNAP_WINDOW_SECONDS]
        if edges:
            best = min(edges, key=lambda edge: abs(edge - at))
            return round(max(0.0, best - 0.06 if starting else best + 0.12), 3), True
    return at, False


def snap_clip(clip: dict, media: Path) -> None:
    """Move every cut of a clip, including the ones around skipped lines, onto a real pause."""
    moved, all_ok = [], True
    for segment in segments_of(clip):
        start, start_ok = snap_cut(media, segment["start"], starting=True)
        end, end_ok = snap_cut(media, segment["end"], starting=False)
        moved.append({**segment, "start": start, "end": end})
        all_ok = all_ok and start_ok and end_ok
    ordered = all(a["end"] < b["start"] for a, b in zip(moved, moved[1:])) and all(m["end"] > m["start"] for m in moved)
    trial = {**clip, "duration": segments_length(moved)}
    if ordered and limit_problem(trial) is None:
        clip.update(
            segments=moved,
            start=moved[0]["start"],
            end=moved[-1]["end"],
            duration=trial["duration"],
            snapped=all_ok,
        )
    else:
        clip["snapped"] = False


def render_clip_list(kept: list[dict], rejected: list[dict], title: str) -> str:
    lines = [
        f"# Clips: {title}",
        "",
        "Tell Claude which numbers to make, for example “make 1, 3 and 7”.",
        "",
    ]
    for label, name in (("short", "Shorts and Reels (0:30 to 3:00)"), ("long", "Long format (over 3:00)")):
        clips = [clip for clip in kept if clip["format"] == label]
        lines += [f"## {name}", ""]
        if not clips:
            lines += ["None found.", ""]
        for clip in clips:
            lines += [
                f"### {clip['number']}. {clip['title'] or 'Untitled clip'}",
                "",
                f"**{fmt(clip['start'])} to {fmt(clip['end'])}** ({fmt(clip['duration'])}) · {clip['type']} · "
                f"score {clip['score']}/10"
                + "".join(
                    f" · leaves out {fmt(a['end'])} to {fmt(b['start'])}"
                    for a, b in zip(segments_of(clip), segments_of(clip)[1:])
                )
                + ("" if clip.get("snapped", True) else " · cut not on a clear pause, listen to the edges"),
                "",
            ]
            for name_, key in (("Hook", "hook"), ("Why it fits", "why"), ("Check", "concerns")):
                if clip[key]:
                    lines += [f"- **{name_}:** {clip[key]}"]
            lines += ["", f"> {clip['text']}", ""]
    if rejected:
        lines += ["## Not used", ""]
        for clip in rejected:
            where = f"{fmt(clip['start'])} to {fmt(clip['end'])} " if "start" in clip else ""
            lines += [f"- {where}{clip['title'] or clip['type'] or 'Untitled'}: {clip['problem']}"]
        lines += [""]
    return "\n".join(lines)


# ---- Storyboards -------------------------------------------------------------------------------------------------


def icon_names() -> set[str] | None:
    """The Phosphor icon names Remotion can draw, in kebab-case, or None when Remotion isn't installed."""
    if not ICON_INDEX.exists():
        return None
    names = re.findall(r"export \* from '\./csr/(\w+)'", ICON_INDEX.read_text(encoding="utf-8"))
    return {re.sub(r"(?<!^)(?=[A-Z0-9])", "-", name).lower() for name in names}


def _check_icons(scene: dict, name: str, known: set[str] | None, pictures: set[str] = frozenset()) -> list[str]:
    if scene.get("icon") is not None:
        return [
            f"{name}: a scene no longer takes an icon (a lone badge read as a stray mark). Show the object as art with "
            "one of the storyboard's pictures, or leave it out."
        ]
    wanted: list = []
    art = scene.get("art")
    if art is not None:
        if not isinstance(art, list) or not 1 <= len(art) <= 3:
            return [f"{name}: art must be a list of one to three picture or icon names."]
        if scene.get("type") == "speaker":
            return [f"{name}: a speaker scene takes no art."]
    # art and hero may name one of the storyboard's generated pictures instead of an icon
    wanted += [n for n in [scene.get("hero"), *(art or [])] if n not in pictures]
    for field in ("rows", "items", "lines"):
        for entry in scene.get(field) or []:
            if isinstance(entry, dict):
                wanted += [n for n in (entry.get("icon"), entry.get("left_icon")) if n not in pictures]
    errors = []
    for icon in wanted:
        if icon is None:
            continue
        if not isinstance(icon, str) or not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", icon):
            errors.append(f"{name}: icon {icon!r} must be a kebab-case Phosphor name, such as hands-praying.")
        elif known is not None and icon not in known:
            near = sorted(k for k in known if icon.split("-")[0] in k)[:6]
            hint = f" Close ones: {', '.join(near)}." if near else ""
            errors.append(f"{name}: there is no icon called {icon!r}.{hint}")
    return errors


def check_storyboard(board: object, clip: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a storyboard against its clip. A storyboard with errors can't be rendered."""
    errors: list[str] = []
    warnings: list[str] = []
    scenes = board.get("scenes") if isinstance(board, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return ["The storyboard has no scenes."], warnings
    if board.get("style") not in STYLES:
        errors.append(f"The storyboard needs a style: one of {', '.join(STYLES)}.")
    known = icon_names()
    pictures, picture_errors = _check_pictures(board.get("pictures"))
    errors += picture_errors
    used: set[str] = set()
    expected = 0.0
    previous = None
    for number, scene in enumerate(scenes, 1):
        name = f"Scene {number}"
        if not isinstance(scene, dict):
            errors.append(f"{name} is not an object.")
            continue
        kind = scene.get("type")
        spec = SCENES.get(kind)
        if not spec:
            errors.append(f"{name} has unknown type {kind!r}. Use one of: {', '.join(SCENES)}.")
            continue
        name = f"Scene {number} ({kind})"
        begin, end = scene.get("from"), scene.get("to")
        if not all(isinstance(value, (int, float)) for value in (begin, end)):
            errors.append(f"{name} needs numeric from and to.")
            continue
        if abs(begin - expected) > 0.01:
            errors.append(f"{name} starts at {begin}, but the previous scene ended at {expected}. No gaps or overlaps.")
        if end - begin < MIN_SCENE_SECONDS:
            errors.append(f"{name} lasts {end - begin:.1f}s; the minimum is {MIN_SCENE_SECONDS:.0f}s.")
        layout = scene.get("layout", "full")
        if kind == "speaker":
            if "layout" in scene:
                errors.append(f"{name}: a speaker scene is always full screen, so it takes no layout.")
        elif layout not in LAYOUTS:
            errors.append(f"{name}: layout must be one of {', '.join(LAYOUTS)}.")
        elif layout == "split" and not clip.get("has_video"):
            errors.append(f"{name}: this talk has no video, so a split scene can't be shown.")
        hold = HOLD_SECONDS.get("speaker" if kind == "speaker" else layout, HOLD_SECONDS["full"])
        if number == 1 and kind == "hook":
            hold = HOLD_SECONDS["hook"]
        elif kind == "picture":
            hold = HOLD_SECONDS["picture"]
        if end - begin > hold:
            advice = "Cut to a graphic or a split scene." if kind == "speaker" else "Split it, or cut to the speaker."
            warnings.append(f"{name} stays up for {end - begin:.0f}s; keep it under {hold:g}s. {advice}")
        if kind == previous and kind != "speaker":
            warnings.append(f"{name} repeats the scene type before it.")
        expected, previous = end, kind
        errors += _check_icons(scene, name, known, pictures)
        named = [scene.get("hero"), *(scene.get("art") or [])]
        for entry in scene.get("rows") or []:
            if isinstance(entry, dict):
                named += [entry.get("icon"), entry.get("left_icon")]
        used.update(n for n in named if isinstance(n, str))
        for field, limit in spec["required"].items():
            value = scene.get(field)
            if not isinstance(value, str) or not value.strip():
                errors.append(f"{name} needs {field}.")
            elif len(value) > limit:
                errors.append(f"{name}: {field} is {len(value)} characters; the limit is {limit}.")
        for field, limit in spec["optional"].items():
            value = scene.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                errors.append(f"{name}: {field} must be text of at most {limit} characters.")
        for field in ("rows", "items", "lines", "chips"):
            if field in spec and (field != "chips" or "chips" in scene):
                errors += _check_list(scene.get(field), field, spec[field], name, begin, end)
        if kind == "picture":
            if scene.get("setting") not in SETTINGS:
                errors.append(f"{name} needs a setting: one of {', '.join(SETTINGS)}.")
            if scene.get("effect") is not None and scene.get("effect") not in EFFECTS:
                errors.append(f"{name}: effect must be one of {', '.join(EFFECTS)}.")
            if not any(scene.get(field) for field in ("hero", "title", "chips", "stamp")):
                errors.append(f"{name} needs something on it: a hero, a title, chips or a stamp.")
        if kind == "speaker":
            if not clip.get("has_video"):
                errors.append(f"{name}: this talk has no video, so a speaker scene can't be shown.")
            focus = scene.get("focus", 0.5)
            if not isinstance(focus, (int, float)) or not 0 <= focus <= 1:
                errors.append(f"{name}: focus must be between 0 and 1.")
        emphasis = scene.get("emphasis", [])
        words = str(scene.get("headline") or scene.get("text") or "").casefold()
        if not isinstance(emphasis, list) or any(str(e).casefold() not in words for e in emphasis):
            errors.append(f"{name}: every emphasis word must appear in its text.")
    if scenes and isinstance(scenes[-1], dict) and abs(expected - clip["duration"]) > 0.05:
        errors.append(f"The last scene ends at {expected}, but the clip is {clip['duration']} seconds long.")
    for join in clip.get("joins", []):
        for scene in scenes:
            if not isinstance(scene, dict) or not isinstance(scene.get("from"), (int, float)):
                continue
            shows_video = scene.get("type") == "speaker" or scene.get("layout") == "split"
            if shows_video and scene["from"] + 0.3 < join < scene.get("to", 0) - 0.3:
                warnings.append(
                    f"The {scene['type']} scene at {scene['from']}s shows his video across the join at {join}s, so the "
                    "jump will show. Put a full-screen graphic over the join."
                )
    on_video = [s for s in scenes if isinstance(s, dict) and (s.get("type") == "speaker" or s.get("layout") == "split")]
    if clip.get("has_video") and not on_video:
        warnings.append("A video is available but no scene shows the speaker.")
    for number, scene in enumerate(scenes, 1):
        rows = scene.get("rows") if isinstance(scene, dict) else None
        marks = [r.get(f) for r in rows or [] if isinstance(r, dict) for f in ("left_icon", "icon") if r.get(f)]
        if marks and 0 < sum(m in pictures for m in marks) < len(marks):
            warnings.append(f"Scene {number} mixes pictures and icons in one table; use one kind so the rows match.")
    for unused in sorted(pictures - used):
        warnings.append(f"The picture {unused!r} isn't used by any scene.")
    errors += _check_post(board.get("post"), clip)
    return errors, warnings


def _check_pictures(pictures: object) -> tuple[set[str], list[str]]:
    """The storyboard's generated pictures: {name: description}. Returns the valid names and any errors."""
    if pictures is None:
        return set(), []
    if not isinstance(pictures, dict):
        return set(), ["pictures must be an object of name: description."]
    errors, names = [], set()
    low, high = PICTURE_DESCRIPTION
    for key, text in pictures.items():
        if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", key):
            errors.append(f"Picture name {key!r} must be kebab-case, such as gold-chain.")
            continue
        if not isinstance(text, str) or not low <= len(text.strip()) <= high:
            errors.append(f"Picture {key!r} needs a description of {low} to {high} characters.")
            continue
        people = sorted(set(re.findall(r"[a-z]+", text.casefold())) & set(PEOPLE_WORDS))
        if people:
            errors.append(
                f"Picture {key!r} would show a person ({', '.join(people)}). Generated pictures show objects and places "
                "only; people come from the talk's own video."
            )
            continue
        names.add(key)
    return names, errors


def _check_list(values, field, limits, name, begin, end) -> list[str]:
    fewest, most, each = limits
    if not isinstance(values, list) or not fewest <= len(values) <= most:
        return [f"{name} needs {fewest} to {most} {field}."]
    errors = []
    for entry in values:
        if not isinstance(entry, dict):
            errors.append(f"{name}: each of its {field} must be an object.")
            continue
        texts = [entry.get("left"), entry.get("right")] if field == "rows" else [entry.get("text")]
        if not all(isinstance(text, str) and text.strip() for text in texts):
            errors.append(f"{name}: one of its {field} is missing text.")
        elif any(len(text) > each for text in texts):
            errors.append(f"{name}: one of its {field} is over {each} characters.")
        at = entry.get("at")
        if at is not None and not (isinstance(at, (int, float)) and begin <= at < end):
            errors.append(f"{name}: `at` must be a clip time inside the scene, in seconds.")
    return errors


def _check_post(post: object, clip: dict) -> list[str]:
    if not isinstance(post, dict):
        return ["The storyboard needs a post section with youtube_title, youtube_description and instagram_caption."]
    errors = []
    limits = (
        ("youtube_title", TITLE_LIMIT),
        ("youtube_description", DESCRIPTION_LIMIT),
        ("instagram_caption", INSTAGRAM_LIMIT),
    )
    for field, limit in limits:
        if field == "instagram_caption" and clip["format"] == "long":
            continue
        value = post.get(field)
        if not isinstance(value, str) or not value.strip():
            errors.append(f"post needs {field}.")
        elif len(value) > limit:
            errors.append(f"post: {field} is {len(value)} characters; the limit is {limit}.")
        elif field.startswith("youtube") and re.search(r"[<>]", value):
            errors.append(f"post: YouTube rejects < and > in {field}.")
    return errors


def render_post(post: dict, clip: dict) -> str:
    lines = [
        f"# {post['youtube_title']}",
        "",
        f"From {fmt(clip['source_start'])} to {fmt(clip['source_end'])} of the talk ({fmt(clip['duration'])})"
        + "".join(
            f", leaving out {fmt(a[1])} to {fmt(b[0])}"
            for a, b in zip(clip.get("segments", []), clip.get("segments", [])[1:])
        )
        + ".",
        "",
        "## YouTube title",
        "",
        post["youtube_title"],
        "",
        "## YouTube description",
        "",
        post["youtube_description"],
        "",
    ]
    if clip["format"] == "short":
        lines += ["## Instagram caption", "", post["instagram_caption"], ""]
    return "\n".join(lines)


# ---- Commands ----------------------------------------------------------------------------------------------------


def talk_folder(name: str) -> Path:
    path = Path(name)
    if not path.is_absolute():
        path = TALKS / name
    if not (path / "talk.json").exists():
        known = sorted(p.name for p in TALKS.glob("*") if (p / "talk.json").exists())
        raise StudioError(f"No talk called {name!r}. Talks: {', '.join(known) or 'none yet'}")
    return path


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise StudioError(f"{path} doesn't exist yet.") from None
    except ValueError as error:
        raise StudioError(f"{path.name} isn't valid JSON: {error}") from None


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def cmd_new(args) -> None:
    """Move the files in the inbox into a new talk folder, named after the YouTube title."""
    files = [p for p in INBOX.iterdir() if p.is_file() and not p.name.startswith(".")] if INBOX.exists() else []
    subtitles = [p for p in files if p.suffix.lower() == ".srt"]
    if len(subtitles) != 1:
        found = ", ".join(p.name for p in subtitles) or "none"
        raise StudioError(f"The inbox needs exactly one English .srt file. Found: {found}")
    srt = subtitles[0]
    text = srt.read_text(encoding="utf-8", errors="replace")
    cues = parse_srt(text)
    if not cues:
        raise StudioError(f"{srt.name} has no subtitles in it.")
    if "gujlish" in srt.name.casefold() or not looks_english(cues):
        raise StudioError(f"{srt.name} doesn't look like the English SRT. Use the one from the English tab.")
    summaries = [p for p in files if p.suffix.lower() == ".md"]
    audios = [p for p in files if p.suffix.lower() in AUDIO_TYPES]
    videos = [p for p in files if p.suffix.lower() in VIDEO_TYPES]
    for label, group in (("summary .md", summaries), ("audio", audios), ("video", videos)):
        if len(group) > 1:
            raise StudioError(f"The inbox has more than one {label} file: {', '.join(p.name for p in group)}")
    title = args.title or (youtube_title(summaries[0].read_text(encoding="utf-8")) if summaries else None)
    if not title:
        raise StudioError("No YouTube title found. Write one from the SRT and pass it with --title.")
    name = safe_name(title)
    folder = TALKS / name
    suffix = 2
    while folder.exists():
        folder = TALKS / f"{name} ({suffix})"
        suffix += 1
    source = folder / "source"
    source.mkdir(parents=True)
    record = {"title": title, "duration": cues[-1]["end"], "warnings": []}
    moves = [("srt", srt, ".srt")]
    moves += [("summary", summaries[0], ".summary.md")] if summaries else []
    moves += [("audio", audios[0], audios[0].suffix.lower())] if audios else []
    moves += [("video", videos[0], videos[0].suffix.lower())] if videos else []
    for key, path, ending in moves:
        target = source / f"{name}{ending}"
        shutil.move(str(path), target)
        record[key] = str(target.relative_to(folder))
    lengths = {key: media_duration(folder / record[key]) for key in ("audio", "video") if key in record}
    if len(lengths) == 2 and abs(lengths["audio"] - lengths["video"]) > 2:
        record["warnings"].append(
            f"The audio ({fmt(lengths['audio'])}) and video ({fmt(lengths['video'])}) differ in length, so speaker "
            "scenes may be out of sync with the sound."
        )
    if lengths and cues[-1]["end"] > max(lengths.values()) + 5:
        record["warnings"].append("The subtitles run past the end of the media. Is this the right recording?")
    write_json(folder / "talk.json", record)
    build_lines(folder)
    print(f"Talk: {folder.name}")
    print(f"Audio: {'yes' if 'audio' in record else 'no'} · Video: {'yes' if 'video' in record else 'no'}")
    for warning in record["warnings"]:
        print(f"Warning: {warning}")
    print(f"Read {folder / 'lines.txt'}")


def build_lines(folder: Path) -> list[dict]:
    record = read_json(folder / "talk.json")
    cues = parse_srt((folder / record["srt"]).read_text(encoding="utf-8", errors="replace"))
    lines = sentence_lines(timed_words(cues))
    write_json(folder / "lines.json", lines)
    (folder / "lines.txt").write_text(
        "\n".join(f"#{ln['line']} [{stamp(ln['start'])}-{stamp(ln['end'])}] {ln['text']}" for ln in lines) + "\n",
        encoding="utf-8",
    )
    return lines


def recording(folder: Path, record: dict) -> Path | None:
    """The file to take sound from. With a video, its own soundtrack: a separate audio download can be offset from the
    picture (one talk's MP3 ran 36 ms ahead), which shows as lips out of sync. Else the audio file."""
    if "video" in record:
        video = folder / record["video"]
        if not video.exists() or has_sound(video):
            return video
    return folder / record["audio"] if "audio" in record else None


def has_sound(path: Path) -> bool:
    return any(stream.get("codec_type") == "audio" for stream in media_info(path).get("streams", []))


def cmd_check(args) -> None:
    """Check candidates.json against the limits, move the cuts onto real pauses, and write the numbered clip list."""
    folder = talk_folder(args.talk)
    record = read_json(folder / "talk.json")
    lines = read_json(folder / "lines.json")
    raw = read_json(folder / "candidates.json")
    items = raw.get("clips") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        raise StudioError('candidates.json must be {"clips": [...]}.')
    candidates = [check_candidate(item, lines) for item in items]
    media = recording(folder, record)
    if media:
        for clip in candidates:
            if not clip["problem"]:
                snap_clip(clip, media)
    kept, rejected = select(candidates)
    kept.sort(key=lambda clip: (clip["format"] != "short", -clip["score"], clip["start"]))
    for number, clip in enumerate(kept, 1):
        clip["number"] = number
    write_json(folder / "clips.json", {"clips": kept, "rejected": rejected})
    (folder / "clips.md").write_text(render_clip_list(kept, rejected, record["title"]), encoding="utf-8")
    shorts = sum(clip["format"] == "short" for clip in kept)
    print(f"{shorts} short and {len(kept) - shorts} long clips kept, {len(rejected)} not used.")
    for clip in kept:
        print(f"  {clip['number']}. [{clip['format']}] {fmt(clip['start'])}-{fmt(clip['end'])} ({fmt(clip['duration'])}) "
              f"{clip['score']}/10 {clip['title']}")  # fmt: skip
    for clip in rejected:
        print(f"  Not used: {clip['title'] or clip['type']}: {clip['problem']}")
    print(f"List: {folder / 'clips.md'}")


def clip_folder(folder: Path, number: int) -> Path:
    found = sorted((folder / "clips").glob(f"{number:02d}-*"))
    if not found:
        raise StudioError(f"Clip {number} hasn't been prepared. Run: prepare {folder.name!r} {number}")
    return found[0]


def cut(command: list[str], label: str) -> None:
    result = run(command)
    if result.returncode:
        raise StudioError(f"Cutting {label} failed: {result.stderr.strip()[-400:]}")


def cmd_prepare(args) -> None:
    """Make a clip's folder: its sound and video cut from the talk, and its captions in clip time."""
    folder = talk_folder(args.talk)
    record = read_json(folder / "talk.json")
    clips = {clip["number"]: clip for clip in read_json(folder / "clips.json")["clips"]}
    media = recording(folder, record)
    if not media:
        raise StudioError("This talk has no audio or video. Put the file in its source folder and add it to talk.json.")
    video = folder / record["video"] if "video" in record else None
    cues = parse_srt((folder / record["srt"]).read_text(encoding="utf-8", errors="replace"))
    pauses = speech_pauses(media)
    words = speech_words(cues, pauses)
    for number in args.numbers:
        clip = clips.get(number)
        if not clip:
            raise StudioError(f"There is no clip {number} in clips.md.")
        start, end = clip["start"], clip["end"]
        segments = trim_long_pauses(segments_of(clip), pauses)
        trims = len(segments) - len(segments_of(clip))
        clip = {**clip, "segments": segments}
        target = folder / "clips" / f"{number:02d}-{safe_name(clip['title'], 60)}"
        public = target / "public"
        public.mkdir(parents=True, exist_ok=True)
        cut(audio_command(media, segments, public / "audio.m4a"), "the audio")
        if video:
            cut(video_command(video, segments, public / "video.mp4"), "the video")
        duration = round(media_duration(public / "audio.m4a"), 3)
        info = {
            **{key: clip.get(key) for key in ("format", "type", "title", "hook", "caption", "text", "concerns")},
            "number": number,
            "source_start": start,
            "source_end": end,
            "segments": [[segment["start"], segment["end"]] for segment in segments],
            "joins": joins_of(clip),
            "duration": duration,
            "has_video": bool(video),
        }
        write_json(target / "clip.json", info)
        pages = clip_captions(words, clip, CAPTION_CHARS[clip["format"]])
        write_json(target / "captions.json", pages)
        write_json(target / "words.json", clip_words(words, clip))
        emphasis = repeated_phrases(pages)
        if emphasis:
            info["emphasis"] = emphasis
            write_json(target / "clip.json", info)
        print(f"Clip {number}: {target}")
        print(f"  {fmt(duration)} ({duration}s), {clip['format']}, {'with' if video else 'no'} speaker video")
        for item in emphasis[:5]:
            said = ", ".join(f"{t:.1f}s" for t in item["times"])
            print(f'  He repeats "{item["text"]}" ({said}): make it a big moment on screen.')
        if trims:
            print(
                f"  Cut {trims} long pause{'s' if trims > 1 else ''} (over {LONG_PAUSE_SECONDS:g}s of silence) out of it."
            )
        for join in info["joins"]:
            print(f"  Joins two pieces at {join}s: cover it with a full-screen graphic, or the cut shows on his video.")


SCALE = "scale='if(gt(ih,1080),-2,iw)':'if(gt(ih,1080),1080,ih)',fps=30,format=yuv420p"
LOUDNESS = "loudnorm=I=-14:TP=-1.5:LRA=11"
EDGE_FADE = 0.03  # seconds of fade at each join, so the sound doesn't click
LONG_PAUSE_SECONDS = 5.0  # a silence this long inside a clip (a drink of water, a break) is cut out
PAUSE_KEPT_SECONDS = 0.5  # of such a pause, this much stays on each side


def _inputs(path: Path, segments: list[dict]) -> list[str]:
    command = []
    for segment in segments:
        command += ["-ss", f"{segment['start']:.3f}", "-to", f"{segment['end']:.3f}", "-i", str(path)]
    return command


def audio_command(media: Path, segments: list[dict], output: Path) -> list[str]:
    """ffmpeg command for a clip's sound: its pieces joined with tiny fades, then evened out in loudness."""
    head = ["ffmpeg", "-y", "-hide_banner", *_inputs(media, segments), "-vn"]
    tail = ["-ar", "48000", "-c:a", "aac", "-b:a", "192k", str(output)]
    if len(segments) == 1:
        return head + ["-af", LOUDNESS] + tail
    parts = []
    for index, segment in enumerate(segments):
        length = segment["end"] - segment["start"]
        parts.append(
            f"[{index}:a]afade=t=in:d={EDGE_FADE},afade=t=out:st={length - EDGE_FADE:.3f}:d={EDGE_FADE}[a{index}]"
        )
    joined = "".join(f"[a{index}]" for index in range(len(segments)))
    graph = ";".join(parts) + f";{joined}concat=n={len(segments)}:v=0:a=1,{LOUDNESS}[out]"
    return head + ["-filter_complex", graph, "-map", "[out]"] + tail


def video_command(video: Path, segments: list[dict], output: Path) -> list[str]:
    """ffmpeg command for a clip's picture: its pieces joined back to back, with no sound."""
    head = ["ffmpeg", "-y", "-hide_banner", *_inputs(video, segments), "-an"]
    tail = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-g", "15", str(output)]
    if len(segments) == 1:
        return head + ["-vf", SCALE] + tail
    parts = [f"[{index}:v]{SCALE},setpts=PTS-STARTPTS[v{index}]" for index in range(len(segments))]
    joined = "".join(f"[v{index}]" for index in range(len(segments)))
    graph = ";".join(parts) + f";{joined}concat=n={len(segments)}:v=1:a=0[out]"
    return head + ["-filter_complex", graph, "-map", "[out]"] + tail


def load_board(folder: Path, number: int) -> tuple[Path, dict, dict, list[str]]:
    target = clip_folder(folder, number)
    clip = read_json(target / "clip.json")
    board = read_json(target / "storyboard.json")
    errors, warnings = check_storyboard(board, clip)
    if errors:
        raise StudioError("The storyboard isn't ready:\n  " + "\n  ".join(errors))
    return target, clip, board, warnings


def write_props(target: Path, clip: dict, board: dict) -> Path:
    props = {
        "format": clip["format"],
        "fps": FPS,
        "duration": clip["duration"],
        "hasAudio": True,
        "hasVideo": clip["has_video"],
        "style": board["style"],
        "scenes": board["scenes"],
        "captions": read_json(target / "captions.json"),
        "pictures": sorted(board.get("pictures") or {}),
    }
    missing = [n for n in props["pictures"] if not (target / "public" / "pictures" / f"{n}.png").exists()]
    if missing:
        raise StudioError(f"Pictures not made yet: {', '.join(missing)}. Run the pictures command first.")
    path = target / "props.json"
    write_json(path, props)
    return path


def remotion(command: list[str]) -> None:
    if not (REMOTION / "node_modules").exists():
        raise StudioError(f"Remotion isn't installed. Run: cd {REMOTION} && npm install")
    result = subprocess.run(["npx", "--no-install", "remotion", *command], cwd=REMOTION)
    if result.returncode:
        raise StudioError("Remotion stopped with an error (see above).")


GRAPHIC_LEAD_SECONDS = 0.15  # a graphic appears this much before its first word, as he starts to say it
EARLY_GRAPHIC_SECONDS = 1.0  # the check warns when a graphic is up this long before its words are said


def _tokens(text: str) -> list[str]:
    return [t for t in re.sub(r"[^\w\s'-]", " ", text.lower()).split() if t]


def scene_phrase(scene: dict) -> str:
    """The words a graphic shows that he says aloud: what it should appear with."""
    return str(scene.get("headline") or scene.get("text") or scene.get("title") or "")


def spoken_at(phrase: str, words: list[dict], low: float, high: float, near: float | None = None) -> float | None:
    """When he starts saying `phrase`, searching the words spoken between `low` and `high`: the start of the stretch
    that says most of its words in order. None when the phrase isn't really said there (a paraphrase)."""
    wanted = [t for t in _tokens(phrase) if t not in EMPHASIS_SMALL_WORDS] or _tokens(phrase)
    if not wanted:
        return None
    heard = [(t, w["at"]) for w in words if low <= w["at"] <= high for t in _tokens(w["text"])[:1]]
    found = []
    for at, (token, when) in enumerate(heard):
        if token not in wanted:
            continue
        window = {t for t, _ in heard[at : at + len(_tokens(phrase)) + 4]}
        found.append((sum(t in window for t in wanted) / len(wanted), when))
    if not found or max(score for score, _ in found) < 0.6:
        return None
    best = max(score for score, _ in found)
    # said more than once (he repeats a point): the saying nearest where the graphic was put is the one it marks
    return min((when for score, when in found if score >= best - 1e-9), key=lambda when: abs(when - (near or low)))


def align_graphics(board: dict, words: list[dict], duration: float) -> list[str]:
    """Move each graphic to start as he says its words, and each table row, list item or story line to appear as it
    is said. A graphic moved later leaves its old start to the speaker, so the graphic before it doesn't overstay.
    Returns what moved."""
    scenes, moved = board["scenes"], []
    index = 1
    while index < len(scenes):
        scene, before = scenes[index], scenes[index - 1]
        if scene["type"] != "speaker":
            said = spoken_at(scene_phrase(scene), words, scene["from"] - 8, scene["to"] + 4, scene["from"])
            if said is not None:
                target = max(round(said - GRAPHIC_LEAD_SECONDS, 2), before["from"] + MIN_SCENE_SECONDS)
                after = scenes[index + 1] if index + 1 < len(scenes) else None
                # moving later, the graphic keeps its length: the speaker after it gives up the time
                stretch = target > scene["from"] and after is not None and after["type"] == "speaker"
                end = scene["to"]
                if stretch:
                    end = min(scene["to"] + target - scene["from"], after["to"] - MIN_SCENE_SECONDS)
                target = round(min(target, end - MIN_SCENE_SECONDS), 2)
                if abs(target - scene["from"]) >= 0.3:
                    moved.append(f'{scene["type"]} "{scene_phrase(scene)[:40]}": {scene["from"]}s -> {target}s')
                    later = target - scene["from"]
                    if stretch:
                        scene["to"] = after["from"] = round(min(scene["to"] + later, end), 2)
                    if later >= MIN_SCENE_SECONDS and before["type"] != "speaker":
                        scenes.insert(index, {"type": "speaker", "from": scene["from"], "to": target})
                        index += 1
                    else:
                        before["to"] = target
                    scene["from"] = target
            for field in ("rows", "items", "lines", "chips"):
                for entry in scene.get(field) or []:
                    if not isinstance(entry, dict):
                        continue
                    text = entry.get("left") or entry.get("text") or ""
                    when = spoken_at(text, words, scene["from"], scene["to"], entry.get("at"))
                    at = when - GRAPHIC_LEAD_SECONDS if when is not None else entry.get("at")
                    if at is not None:
                        entry["at"] = round(min(max(at, scene["from"] + 0.2), scene["to"] - 0.5), 2)
        index += 1
    scenes[-1]["to"] = duration
    return moved


def early_graphics(board: dict, words: list[dict]) -> list[str]:
    """Graphics that come up well before he says their words."""
    found = []
    for index, scene in enumerate(board["scenes"][1:], 2):
        if scene["type"] == "speaker":
            continue
        said = spoken_at(scene_phrase(scene), words, scene["from"] - 8, scene["to"] + 4, scene["from"])
        if said is not None and said - scene["from"] > EARLY_GRAPHIC_SECONDS:
            found.append(
                f"Scene {index} ({scene['type']}) is up {said - scene['from']:.1f}s before he says its words. Run align."
            )
    return found


def cmd_align(args) -> None:
    """Time each graphic, and each row of a table or list, to when he says its words."""
    folder = talk_folder(args.talk)
    for number in args.numbers:
        target = clip_folder(folder, number)
        clip, board = read_json(target / "clip.json"), read_json(target / "storyboard.json")
        if not (target / "words.json").exists():
            raise StudioError(f"Clip {number} has no words.json yet. Run prepare again.")
        moved = align_graphics(board, read_json(target / "words.json"), clip["duration"])
        write_json(target / "storyboard.json", board)
        print(f"Clip {number}: {len(moved)} graphic{'s' if len(moved) != 1 else ''} moved to their words")
        for line in moved:
            print(f"  {line}")


def cmd_storyboard(args) -> None:
    """Check a storyboard and write the frames to look at."""
    folder = talk_folder(args.talk)
    for number in args.numbers:
        _, clip, board, warnings = load_board(folder, number)
        speaker = sum(s["to"] - s["from"] for s in board["scenes"] if s["type"] == "speaker")
        print(
            f"Clip {number}: storyboard is valid, {len(board['scenes'])} scenes, speaker on screen {speaker:.0f}s of {clip['duration']:.0f}s"
        )
        words_file = clip_folder(folder, number) / "words.json"
        if words_file.exists():
            warnings = warnings + early_graphics(board, read_json(words_file))
        for warning in warnings:
            print(f"  Warning: {warning}")


def cmd_preview(args) -> None:
    """Render one frame from near the end of each scene, for checking the look before the full render."""
    folder = talk_folder(args.talk)
    target, clip, board, warnings = load_board(folder, args.number)
    for warning in warnings:
        print(f"Warning: {warning}")
    props = write_props(target, clip, board)
    frames_dir = target / "preview"
    shutil.rmtree(frames_dir, ignore_errors=True)
    frames_dir.mkdir()
    last = round(clip["duration"] * FPS) - 1
    for index, scene in enumerate(board["scenes"], 1):
        # late in the scene, so everything has appeared; tables and lists build row by row, so at their very end
        share = 0.95 if any(scene.get(field) for field in ("rows", "items", "lines", "chips")) else 0.8
        frame = min(last, max(0, round((scene["from"] + (scene["to"] - scene["from"]) * share) * FPS)))
        remotion(
            ["still", "src/index.ts", "Clip", str(frames_dir / f"scene-{index:02d}-{scene['type']}.jpg")]
            + [f"--frame={frame}", f"--props={props}", f"--public-dir={target / 'public'}", "--log=error"]
        )
    print(f"Look at the frames in {frames_dir}")


def gemini_key() -> str:
    """The Gemini key from the environment, or from the macOS Keychain. Never printed or written anywhere."""
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if key:
        return key
    if shutil.which("security"):
        found = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_ITEM, "-w"], capture_output=True, text=True
        )
        if found.returncode == 0 and found.stdout.strip():
            return found.stdout.strip()
    raise StudioError(
        "No Gemini key. Set GEMINI_API_KEY, or store it once in the Keychain from your own terminal:\n"
        f'  security add-generic-password -s {KEYCHAIN_ITEM} -a "$USER" -w'
    )


def _gemini(path: str, key: str, body: dict | None = None) -> dict:
    import urllib.error
    import urllib.request

    request = urllib.request.Request(
        f"{GEMINI_API}/{path}",
        data=json.dumps(body).encode() if body is not None else None,
        headers={"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:400]
        raise StudioError(f"Gemini said {error.code}: {detail}") from None


def image_model(key: str) -> str:
    """STUDIO_IMAGE_MODEL, or the newest Gemini image model this key can use (a pro one first)."""
    if os.environ.get("STUDIO_IMAGE_MODEL"):
        return os.environ["STUDIO_IMAGE_MODEL"]
    models = _gemini("models?pageSize=1000", key).get("models", [])
    usable = [
        m["name"].removeprefix("models/")
        for m in models
        if "image" in m["name"]
        and "imagen" not in m["name"]
        and "generateContent" in m.get("supportedGenerationMethods", [])
    ]
    if not usable:
        raise StudioError("This Gemini key has no image model. Set STUDIO_IMAGE_MODEL to one that it can use.")

    def rank(name: str):
        version = [int(n) for n in re.findall(r"\d+", name.split("-image")[0])[:2]]
        return ("pro" in name, version, "preview" not in name)

    return max(usable, key=rank)


def picture_prompt(description: str, style: str) -> str:
    return (
        f"{description.strip().rstrip('.')}. Drawn as {PICTURE_STYLES[style]}. "
        "Make it a single die-cut sticker: the object alone, whole and centred, filling most of the square, with a thick "
        "clean white border that follows its outline. Put it on a perfectly flat, solid pure green (#00FF00) background "
        "with nothing else on it. No text, letters or numbers, no people, no hands, no faces, no frame, no shadow on the "
        "background."
    )


def key_out(raw: bytes):
    """A transparent PNG from a sticker on a flat green background: clear the background (what touches the edges, and
    any key green left inside, such as the hole of a coiled chain), drop stray specks, soften the cut and trim."""
    import io

    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    image = Image.open(io.BytesIO(raw)).convert("RGBA")
    width, height = image.size
    seeds = [(0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)]
    seeds += [(width // 2, 0), (width // 2, height - 1), (0, height // 2), (width - 1, height // 2)]
    ground = np.median([image.getpixel(seed)[:3] for seed in seeds], axis=0)
    for seed in seeds:
        if image.getpixel(seed)[3]:
            ImageDraw.floodfill(image, seed, (0, 0, 0, 0), thresh=90)
    rgba = np.array(image).astype(np.int16)
    r, g, b = rgba[..., 0], rgba[..., 1], rgba[..., 2]
    # only the background's own green: a leaf inside the sticker is a different, darker green
    near = np.sqrt(((rgba[..., :3] - ground) ** 2).sum(axis=-1)) < 70
    key = near & (g - np.maximum(r, b) > 80)
    rgba[..., 3][key] = 0
    solid = rgba[..., 3] > 0
    solid &= _main_parts(solid)
    alpha = Image.fromarray((solid * 255).astype(np.uint8))
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    rgba[..., 3] = np.array(alpha)
    # take the green cast off the soft edge
    edge = (rgba[..., 3] < 250) & (g > np.maximum(r, b))
    rgba[..., 1][edge] = np.maximum(r, b)[edge]
    image = Image.fromarray(rgba.clip(0, 255).astype(np.uint8), "RGBA")
    box = image.getbbox()
    if not box:
        raise StudioError("A picture came back empty after removing its background.")
    image = image.crop(box)
    side = max(image.size)
    square = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    square.paste(image, ((side - image.width) // 2, (side - image.height) // 2))
    return square.resize((1024, 1024), Image.LANCZOS)


def _main_parts(solid):
    """The sticker's own pieces: connected areas at least a twentieth the size of the biggest, without stray specks."""
    import numpy as np

    height, width = solid.shape
    step = max(1, max(height, width) // 256)
    small = solid[::step, ::step]
    labels = np.zeros(small.shape, dtype=np.int32)
    sizes = [0]
    for start in zip(*np.nonzero(small)):
        if labels[start]:
            continue
        label = len(sizes)
        labels[start] = label
        stack, size = [start], 0
        while stack:
            y, x = stack.pop()
            size += 1
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < small.shape[0] and 0 <= nx < small.shape[1] and small[ny, nx] and not labels[ny, nx]:
                    labels[ny, nx] = label
                    stack.append((ny, nx))
        sizes.append(size)
    if len(sizes) == 1:
        return solid
    keep = np.array([size >= max(sizes) / 20 for size in sizes])
    keep[0] = False
    big = np.repeat(np.repeat(keep[labels], step, axis=0), step, axis=1)[:height, :width]
    return big


def generate_picture(description: str, style: str, key: str, model: str) -> bytes:
    import base64

    reply = _gemini(
        f"models/{model}:generateContent",
        key,
        {
            "contents": [{"parts": [{"text": picture_prompt(description, style)}]}],
            "generationConfig": {"responseModalities": ["IMAGE"], "imageConfig": {"aspectRatio": "1:1"}},
        },
    )
    for candidate in reply.get("candidates", []):
        for part in candidate.get("content", {}).get("parts", []):
            data = (part.get("inlineData") or part.get("inline_data") or {}).get("data")
            if data:
                return base64.b64decode(data)
    reason = (reply.get("candidates") or [{}])[0].get("finishReason") or reply.get("promptFeedback")
    raise StudioError(f"Gemini returned no image ({reason}).")


def picture_uses(board: dict, captions: list[dict]) -> dict[str, list[str]]:
    """For each picture: where it appears ("0:12 punch, on screen: ...") and what is said there."""
    uses: dict[str, list[str]] = {name: [] for name in board.get("pictures") or {}}
    for scene in board["scenes"]:
        named = [scene.get("hero"), *(scene.get("art") or [])]
        for row in scene.get("rows") or []:
            named += [row.get("left_icon"), row.get("icon")]
        shown = scene.get("headline") or scene.get("text") or scene.get("title") or ""
        said = " ".join(c["text"] for c in captions if c["to"] > scene["from"] and c["from"] < scene["to"])
        for name in dict.fromkeys(n for n in named if n in uses):
            line = f"{fmt(scene['from'])} {scene['type']}"
            if shown:
                line += f", on screen: {shown}"
            uses[name].append(line)
            uses[name].append(f"    said: {said[:150]}{'...' if len(said) > 150 else ''}")
    return uses


def picture_sheet(board: dict, captions: list[dict], folder: Path, output: Path) -> None:
    """Every picture of a clip on its look's background, with its description, where it appears and what is said
    there, so the user can judge each one in context."""
    import textwrap

    from PIL import Image, ImageDraw, ImageFont

    style = board["style"]
    uses = picture_uses(board, captions)
    cell, width, pad = 320, 1500, 24
    ink, soft = "#2a2218", "#6b5f4e"
    title, body = ImageFont.load_default(size=34), ImageFont.load_default(size=23)
    rows = []
    for name, description in (board.get("pictures") or {}).items():
        lines = textwrap.wrap(description, 80) + [""]
        uses[name] = [
            u.replace("\u2014", " - ").replace("\u2013", " - ") for u in uses[name]
        ]  # the sheet's font has no dashes
        for use in uses[name] or ["(not used)"]:
            lines += textwrap.wrap(use, 92, subsequent_indent="      ")
        rows.append((name, lines, max(cell, 70 + 30 * len(lines))))
    sheet = Image.new("RGB", (width, sum(h for *_, h in rows) + pad), LOOK_GROUNDS[style])
    draw = ImageDraw.Draw(sheet)
    y = pad
    for name, lines, height in rows:
        picture = Image.open(folder / f"{name}.png").resize((cell - 2 * pad, cell - 2 * pad))
        sheet.paste(picture, (pad, y), picture)
        draw.text((cell + pad, y + 6), name, fill=ink, font=title)
        for i, line in enumerate(lines):
            draw.text(
                (cell + pad, y + 56 + 30 * i),
                line,
                fill=ink if line.startswith(("0", "1", "2", "3", "4", "5", "6", "7", "8", "9")) else soft,
                font=body,
            )
        y += height
        draw.line((pad, y - 6, width - pad, y - 6), fill=soft, width=1)
    sheet.save(output, quality=88)


def cmd_pictures(args) -> None:
    """Make each clip's generated pictures with Gemini (once per talk and look), and a sheet to approve them by."""
    folder = talk_folder(args.talk)
    key = model = None
    for number in args.numbers:
        target, _clip, board, _warnings = load_board(folder, number)
        wanted = board.get("pictures") or {}
        if not wanted:
            print(f"Clip {number}: the storyboard asks for no pictures.")
            continue
        style = board["style"]
        store = folder / "pictures" / style
        store.mkdir(parents=True, exist_ok=True)
        public = target / "public" / "pictures"
        public.mkdir(parents=True, exist_ok=True)
        for name, description in wanted.items():
            png, raw, note = store / f"{name}.png", store / f"{name}.raw.png", store / f"{name}.json"
            prompt = picture_prompt(description, style)
            known = read_json(note) if note.exists() else {}
            if raw.exists() and known.get("prompt") == prompt and name not in (args.redo or []):
                print(f"  {name}: already made")
            else:
                if key is None:
                    key = gemini_key()
                    model = image_model(key)
                    print(f"Making pictures with {model}")
                print(f"  {name}: making")
                raw.write_bytes(generate_picture(description, style, key, model))
                write_json(note, {"prompt": prompt, "model": model})
            key_out(raw.read_bytes()).save(png)  # cutting out is free, so redo it every time
            shutil.copyfile(png, public / f"{name}.png")
        sheet = target / "pictures.jpg"
        picture_sheet(board, read_json(target / "captions.json"), store, sheet)
        print(f"Clip {number}: look at {sheet}")


def cmd_render(args) -> None:
    """Render finished videos and check them: right size, right length, with sound."""
    folder = talk_folder(args.talk)
    for number in args.numbers:
        target, clip, board, warnings = load_board(folder, number)
        for warning in warnings:
            print(f"Warning: {warning}")
        props = write_props(target, clip, board)
        title = board["post"]["youtube_title"]
        output = target / f"{safe_name(re.sub(r'#[^ #]+', '', title))}.mp4"  # no #Shorts in the file name
        remotion(
            ["render", "src/index.ts", "Clip", str(output), f"--props={props}", f"--public-dir={target / 'public'}"]
            + ["--codec=h264", "--audio-codec=aac", "--crf=18", "--log=error"]
        )
        problems = verify_video(output, clip)
        if problems:
            raise StudioError(f"{output.name} came out wrong: " + "; ".join(problems))
        (target / "post.md").write_text(render_post(board["post"], clip), encoding="utf-8")
        size = output.stat().st_size / 1024**2
        print(f"Clip {number}: {output} ({fmt(clip['duration'])}, {size:.0f} MB)")


def verify_video(path: Path, clip: dict) -> list[str]:
    info = media_info(path)
    streams = info.get("streams", [])
    width, height = SIZES[clip["format"]]
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    problems = []
    if (video.get("width"), video.get("height")) != (width, height):
        problems.append(f"it is {video.get('width')}x{video.get('height')}, not {width}x{height}")
    if not any(s.get("codec_type") == "audio" for s in streams):
        problems.append("it has no sound")
    length = float(info.get("format", {}).get("duration", 0))
    if abs(length - clip["duration"]) > 0.25:
        problems.append(f"it is {length:.2f}s long, not {clip['duration']:.2f}s")
    low, high = LIMITS[clip["format"]]
    if not low <= length <= high + 0.05:
        problems.append(f"{fmt(length)} is outside the {clip['format']} limits")
    return problems


def cmd_status(_args) -> None:
    """Every talk and how far it has got."""
    talks = sorted(p for p in TALKS.glob("*") if (p / "talk.json").exists()) if TALKS.exists() else []
    inbox = [p.name for p in INBOX.glob("*") if p.is_file() and not p.name.startswith(".")] if INBOX.exists() else []
    print(f"Inbox: {', '.join(inbox) or 'empty'}")
    if not talks:
        print("No talks yet.")
    for folder in talks:
        print(f"\n{folder.name}")
        if not (folder / "clips.json").exists():
            print("  Clips not chosen yet.")
            continue
        clips = read_json(folder / "clips.json")["clips"]
        print(f"  {len(clips)} clips listed in clips.md")
        for target in sorted((folder / "clips").glob("*/")):
            done = [p.name for p in target.glob("*.mp4")]
            state = (
                "rendered"
                if done
                else "storyboard written"
                if (target / "storyboard.json").exists()
                else "waiting for a storyboard"
            )
            print(f"  {target.name}: {state}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)

    def add(name, handler, *, talk=True):
        command = commands.add_parser(name, help=handler.__doc__)
        command.set_defaults(handler=handler)
        if talk:
            command.add_argument("talk", help="The talk's folder name")
        return command

    add("new", cmd_new, talk=False).add_argument("--title", help="The title, if there is no summary to take it from")
    add("check", cmd_check)
    add("prepare", cmd_prepare).add_argument("numbers", type=int, nargs="+")
    add("storyboard", cmd_storyboard).add_argument("numbers", type=int, nargs="+")
    add("align", cmd_align).add_argument("numbers", type=int, nargs="+")
    pictures = add("pictures", cmd_pictures)
    pictures.add_argument("numbers", type=int, nargs="+")
    pictures.add_argument("--redo", nargs="+", metavar="NAME", help="Make these pictures again")
    add("preview", cmd_preview).add_argument("number", type=int)
    add("render", cmd_render).add_argument("numbers", type=int, nargs="+")
    add("status", cmd_status, talk=False)
    args = parser.parse_args(argv)
    try:
        args.handler(args)
    except StudioError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
