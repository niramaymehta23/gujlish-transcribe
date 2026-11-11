"""Your correction dictionary: words the recognizer gets wrong, what they should be, and your Gujlish spellings.

Corrections run on the Gujarati as soon as it is recognized, before Gujlish, English and the summary are made from
it. A correction set to "always" replaces the words. One set to "ask" (the default) only marks them as a suggestion,
because a word that was misheard once may be the right word somewhere else; you accept or reject each suggestion.
Each correction keeps the words as heard and as corrected (in Gujarati and Gujlish), and, for every time you made or
accepted it and every time you rejected it, the sentence, its time in the recording and a short audio clip, so later
versions can learn from more than the spelling.

The file sits next to the recordings (storage.DATA), so a hosted site keeps it in its storage bucket.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from transcriber.files import storage
from transcriber.files.textutil import write_json
from transcriber.speech.postprocess import EDGE_PUNCTUATION, _key

MAX_EXAMPLES = 5  # recordings kept per correction as evidence (and as many rejections); the oldest go first
MODES = ("ask", "always")  # a correction without a mode asks: nothing is replaced silently unless you said so


def mode(rule: dict) -> str:
    return "always" if rule.get("mode") == "always" else "ask"


def dictionary_path() -> Path:
    return storage.DATA / "dictionary.json"


def clips_dir() -> Path:
    return storage.DATA / "dictionary-clips"


def load(path: Path | None = None) -> dict:
    path = path or dictionary_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raw = {}
    return {"corrections": list(raw.get("corrections", [])), "gujlish": dict(raw.get("gujlish", {}))}


def save(data: dict, path: Path | None = None) -> None:
    path = path or dictionary_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


def phrase_key(text: str) -> tuple[str, ...]:
    return tuple(key for key in (_key(word) for word in text.split()) if key)


def split_edges(token: str) -> tuple[str, str, str]:
    """("“", "word", ",”") for "“word,”": leading punctuation, the word, trailing punctuation."""
    core = token.strip(EDGE_PUNCTUATION)
    if not core:
        return "", token, ""
    start = token.index(core)
    return token[:start], core, token[start + len(core) :]


def with_edges(old: list[str], new: list[str]) -> list[str]:
    """Replacement words that keep the punctuation around the words they replace."""
    if not old or not new:
        return list(new)
    lead, trail = split_edges(old[0])[0], split_edges(old[-1])[2]
    new = list(new)
    new[0] = lead + new[0]
    new[-1] += trail  # after the line above: with one word, both edges go on it
    return new


def apply_corrections(segments: list[dict], corrections: list[dict]) -> tuple[int, int]:
    """Look for every remembered mistake, longest phrase first. An "always" correction replaces the words and the
    segment records it in "fixes"; an "ask" correction leaves them and records a suggestion in "suggestions". Both
    hold token ranges in the segment's final text. Returns (words replaced, suggestions made)."""
    rules = {}
    for rule in corrections:
        key = phrase_key(rule.get("heard", ""))
        if key and rule.get("correct", "").split() and phrase_key(rule["correct"]) != key:
            rules[key] = rule
    longest = max(map(len, rules), default=0)
    fixed = suggested = 0
    for segment in segments:
        tokens = segment.get("text", "").split()
        if not longest or not tokens:
            continue
        keys = [_key(token) for token in tokens]
        out, fixes, suggestions, index = [], [], [], 0
        while index < len(tokens):
            for size in range(min(longest, len(tokens) - index), 0, -1):
                rule = rules.get(tuple(keys[index : index + size]))
                if rule:
                    break
            else:
                out.append(tokens[index])
                index += 1
                continue
            heard = tokens[index : index + size]
            if mode(rule) == "always":
                new = with_edges(heard, rule["correct"].split())
                fixes.append(
                    {"start": len(out), "end": len(out) + len(new), "heard": " ".join(heard), "rule": rule.get("id")}
                )
                out += new
            else:
                suggestions.append(
                    {
                        "start": len(out),
                        "end": len(out) + size,
                        "correct": rule["correct"],
                        "correct_gujlish": rule.get("correct_gujlish", ""),
                        "rule": rule.get("id"),
                    }
                )
                out += heard
            index += size
        if fixes:
            segment["text"] = " ".join(out)
            segment["fixes"] = fixes
            fixed += len(fixes)
        if suggestions:
            segment["suggestions"] = suggestions
            suggested += len(suggestions)
    return fixed, suggested


def shift_fixes(segment: dict, start: int, end: int, added: int) -> None:
    """Keep a segment's fix and suggestion ranges correct after tokens [start, end) were replaced by `added` tokens.
    A fix or suggestion overlapping the replaced words is dropped (accepting a suggestion replaces its words)."""
    moved = added - (end - start)
    for field in ("fixes", "suggestions"):
        kept = []
        for item in segment.get(field, []):
            if item["end"] <= start:
                kept.append(item)
            elif item["start"] >= end:
                kept.append({**item, "start": item["start"] + moved, "end": item["end"] + moved})
        if kept:
            segment[field] = kept
        else:
            segment.pop(field, None)


def _keep_recent(items: list[dict], item: dict) -> list[dict]:
    """The newest MAX_EXAMPLES of items + [item]; the audio clips of the ones dropped are deleted."""
    items = [*items, item]
    for old in items[:-MAX_EXAMPLES]:
        if old.get("clip"):
            (clips_dir() / old["clip"]).unlink(missing_ok=True)
    return items[-MAX_EXAMPLES:]


def remember(
    data: dict, heard: str, correct: str, heard_gujlish: str, correct_gujlish: str, example: dict, always: bool = False
) -> dict:
    """Add or update a correction, and store the Gujlish spelling of each corrected word. A new correction asks
    before replacing unless `always`; `always` on an existing one switches it to always (it is never switched back
    here: that is done in the dictionary list). Returns the correction (an empty dict when only the Gujlish spelling
    changed)."""
    correct_words, latin_words = correct.split(), correct_gujlish.split()
    if len(correct_words) == len(latin_words):
        for word, latin in zip(correct_words, latin_words):
            gujarati, spelling = _key(word), latin.strip(EDGE_PUNCTUATION)
            if gujarati and spelling and gujarati != spelling.casefold():
                data["gujlish"][gujarati] = spelling
    key = phrase_key(heard)
    if not key or phrase_key(correct) == key:
        return {}
    rule = next((rule for rule in data["corrections"] if phrase_key(rule["heard"]) == key), None)
    if rule is None:
        rule = {"id": uuid4().hex[:12], "heard": heard, "heard_gujlish": heard_gujlish, "mode": "ask", "examples": []}
        rule["added"] = datetime.now().isoformat(timespec="seconds")
        data["corrections"].append(rule)
    rule.update(correct=correct, correct_gujlish=correct_gujlish)
    if always:
        rule["mode"] = "always"
    rule["examples"] = _keep_recent(rule.get("examples", []), example)
    return rule


def reject(data: dict, correction: str, example: dict) -> dict:
    """Record that a suggestion was wrong in this place: the speaker said the words as heard. Returns the correction,
    or an empty dict when it no longer exists."""
    rule = next((rule for rule in data["corrections"] if rule.get("id") == correction), None)
    if rule is None:
        return {}
    rule["rejected"] = _keep_recent(rule.get("rejected", []), example)
    return rule


def set_mode(data: dict, correction: str, new_mode: str) -> bool:
    """Switch a correction between "ask" and "always". True when it exists and the mode is valid."""
    rule = next((rule for rule in data["corrections"] if rule.get("id") == correction), None)
    if rule is None or new_mode not in MODES:
        return False
    rule["mode"] = new_mode
    return True


def forget(data: dict, correction: str | None = None, gujlish: str | None = None) -> bool:
    """Remove a correction (by id, with its audio clips) or a Gujlish spelling. True when something was removed."""
    if correction:
        rule = next((rule for rule in data["corrections"] if rule.get("id") == correction), None)
        if rule is None:
            return False
        data["corrections"].remove(rule)
        for clip in clips_dir().glob(f"{correction}-*"):
            clip.unlink(missing_ok=True)
        return True
    return data["gujlish"].pop(gujlish or "", None) is not None


def _cut(source: Path, start: float, end: float) -> list[str]:
    """FFmpeg arguments that read source[start:end] as 16 kHz mono; the caller adds the output."""
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(0.0, start):.2f}", "-t", f"{end - start:.2f}"]
    return command + ["-i", str(source), "-vn", "-ac", "1", "-ar", "16000"]


def save_clip(source: Path, start: float, end: float, target: Path) -> bool:
    """Cut the stretch of the recording a correction came from into a small Opus file. Best effort."""
    target.parent.mkdir(parents=True, exist_ok=True)
    command = _cut(source, start, end) + ["-c:a", "libopus", "-b:a", "24k", str(target)]
    try:
        return subprocess.run(command, capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def clip_audio(source: Path, start: float, end: float) -> bytes | None:
    """source[start:end] as a small WAV file (every browser plays it), or None when FFmpeg fails."""
    try:
        result = subprocess.run(_cut(source, start, end) + ["-f", "wav", "-"], capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 and result.stdout else None
