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
    "quote": {"required": {"text": 130}, "optional": {}},
    "compare": {"required": {"title": 50}, "optional": {}, "rows": (2, 5, 26)},
    "list": {"required": {"title": 50}, "optional": {}, "items": (2, 5, 52)},
    "agna": {"required": {"text": 90}, "optional": {"label": 20}},
    "story": {"required": {}, "optional": {"place": 40}, "lines": (1, 5, 72)},
    "speaker": {"required": {}, "optional": {"label": 40}},
    "end": {"required": {"text": 60}, "optional": {"sub": 60}},
}
MIN_SCENE_SECONDS = 2.0
LONG_GRAPHIC_SECONDS = 25.0  # past this a graphic goes stale; a warning, since a compare can earn the time


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


def caption_pages(
    words: list[dict], start: float, end: float, max_chars: int, span: tuple[float, float] | None = None
) -> list[dict]:
    """Caption pages for one clip, in clip time. A page ends at a sentence, or when full, and stays up across a short
    pause so captions don't flicker. `span` is where the clip's own sentences run: a cut moved onto a pause can reach
    past them, and the neighbouring words must not show up in its captions."""
    low, high = span or (start, end)
    inside = [word for word in words if low - 0.01 <= (word["start"] + word["end"]) / 2 <= high + 0.01]
    pages: list[list[dict]] = [[]]
    for word in inside:
        page = pages[-1]
        size = sum(len(w["text"]) + 1 for w in page)
        if page and size + len(word["text"]) > max_chars:
            cut = next(
                (
                    at
                    for at in range(len(page) - 1, 0, -1)
                    if page[at - 1]["text"].rstrip(QUOTES)[-1:] in ",;:—–"
                    and sum(len(w["text"]) + 1 for w in page[:at]) >= max_chars // 2
                ),
                len(page),
            )
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
    clip.update(first_line=first, last_line=last, start=lines[first - 1]["start"], end=lines[last - 1]["end"])
    clip.update(line_start=clip["start"], line_end=clip["end"])
    clip["text"] = " ".join(line["text"] for line in lines[first - 1 : last])
    clip["duration"] = round(clip["end"] - clip["start"], 3)
    clip["problem"] = limit_problem(clip)
    return clip


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
    start, start_ok = snap_cut(media, clip["start"], starting=True)
    end, end_ok = snap_cut(media, clip["end"], starting=False)
    moved = {**clip, "start": start, "end": end, "duration": round(end - start, 3)}
    if limit_problem(moved) is None and end > start:
        clip.update(start=start, end=end, duration=moved["duration"], snapped=start_ok and end_ok)
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


def check_storyboard(board: object, clip: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a storyboard against its clip. A storyboard with errors can't be rendered."""
    errors: list[str] = []
    warnings: list[str] = []
    scenes = board.get("scenes") if isinstance(board, dict) else None
    if not isinstance(scenes, list) or not scenes:
        return ["The storyboard has no scenes."], warnings
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
        if kind != "speaker" and end - begin > LONG_GRAPHIC_SECONDS:
            warnings.append(f"{name} stays up for {end - begin:.0f}s. Consider splitting it or cutting to the speaker.")
        if kind == previous and kind != "speaker":
            warnings.append(f"{name} repeats the scene type before it.")
        expected, previous = end, kind
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
        for field in ("rows", "items", "lines"):
            if field in spec:
                errors += _check_list(scene.get(field), field, spec[field], name, begin, end)
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
    if clip.get("has_video") and not any(isinstance(s, dict) and s.get("type") == "speaker" for s in scenes):
        warnings.append("A video is available but no scene shows the speaker.")
    errors += _check_post(board.get("post"), clip)
    return errors, warnings


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
        f"From {fmt(clip['source_start'])} to {fmt(clip['source_end'])} of the talk ({fmt(clip['duration'])}).",
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
    """The file to take sound from: the audio the subtitles were made from, else the video."""
    key = "audio" if "audio" in record else "video" if "video" in record else None
    return folder / record[key] if key else None


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
    words = timed_words(cues)
    for number in args.numbers:
        clip = clips.get(number)
        if not clip:
            raise StudioError(f"There is no clip {number} in clips.md.")
        start, end = clip["start"], clip["end"]
        target = folder / "clips" / f"{number:02d}-{safe_name(clip['title'], 60)}"
        public = target / "public"
        public.mkdir(parents=True, exist_ok=True)
        times = ["-ss", f"{start:.3f}", "-to", f"{end:.3f}"]
        cut(
            ["ffmpeg", "-y", "-hide_banner", *times, "-i", str(media), "-vn", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11"]
            + ["-ar", "48000", "-c:a", "aac", "-b:a", "192k", str(public / "audio.m4a")],
            "the audio",
        )
        if video:
            scale = "scale='if(gt(ih,1080),-2,iw)':'if(gt(ih,1080),1080,ih)',fps=30,format=yuv420p"
            cut(
                ["ffmpeg", "-y", "-hide_banner", *times, "-i", str(video), "-an", "-vf", scale, "-c:v", "libx264"]
                + ["-preset", "veryfast", "-crf", "20", "-g", "15", str(public / "video.mp4")],
                "the video",
            )
        duration = round(media_duration(public / "audio.m4a"), 3)
        info = {
            **{key: clip.get(key) for key in ("format", "type", "title", "hook", "caption", "text", "concerns")},
            "number": number,
            "source_start": start,
            "source_end": end,
            "duration": duration,
            "has_video": bool(video),
        }
        write_json(target / "clip.json", info)
        span = (clip["line_start"], clip["line_end"])
        pages = caption_pages(words, start, end, CAPTION_CHARS[clip["format"]], span)
        write_json(target / "captions.json", pages)
        print(f"Clip {number}: {target}")
        print(f"  {fmt(duration)} ({duration}s), {clip['format']}, {'with' if video else 'no'} speaker video")


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
        "scenes": board["scenes"],
        "captions": read_json(target / "captions.json"),
    }
    path = target / "props.json"
    write_json(path, props)
    return path


def remotion(command: list[str]) -> None:
    if not (REMOTION / "node_modules").exists():
        raise StudioError(f"Remotion isn't installed. Run: cd {REMOTION} && npm install")
    result = subprocess.run(["npx", "--no-install", "remotion", *command], cwd=REMOTION)
    if result.returncode:
        raise StudioError("Remotion stopped with an error (see above).")


def cmd_storyboard(args) -> None:
    """Check a storyboard and write the frames to look at."""
    folder = talk_folder(args.talk)
    for number in args.numbers:
        _, clip, board, warnings = load_board(folder, number)
        speaker = sum(s["to"] - s["from"] for s in board["scenes"] if s["type"] == "speaker")
        print(
            f"Clip {number}: storyboard is valid, {len(board['scenes'])} scenes, speaker on screen {speaker:.0f}s of {clip['duration']:.0f}s"
        )
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
        frame = min(last, max(0, round((scene["from"] + (scene["to"] - scene["from"]) * 0.8) * FPS)))
        remotion(
            ["still", "src/index.ts", "Clip", str(frames_dir / f"scene-{index:02d}-{scene['type']}.jpg")]
            + [f"--frame={frame}", f"--props={props}", f"--public-dir={target / 'public'}", "--log=error"]
        )
    print(f"Look at the frames in {frames_dir}")


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
