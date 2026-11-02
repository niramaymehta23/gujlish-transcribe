"""Your correction dictionary: words the recognizer gets wrong, what they should be, and your Gujlish spellings.

Corrections run on the Gujarati as soon as it is recognized, before Gujlish, English and the summary are made from
it. Each correction keeps the words as heard and as corrected (in Gujarati and Gujlish), the sentence it came from,
its time in the recording and a short audio clip, so later versions can learn from more than the spelling.

The file sits next to the recordings (storage.DATA), so a hosted site keeps it in its storage bucket.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from . import storage
from .postprocess import EDGE_PUNCTUATION, _key
from .textutil import write_json

MAX_EXAMPLES = 5  # recordings kept per correction as evidence; the oldest go first


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


def apply_corrections(segments: list[dict], corrections: list[dict]) -> int:
    """Replace every remembered mistake, longest phrase first. Each segment records what changed in "fixes" (token
    ranges in the corrected text) so the transcript can show it. Returns how many replacements were made."""
    rules = {}
    for rule in corrections:
        key = phrase_key(rule.get("heard", ""))
        if key and rule.get("correct", "").split() and phrase_key(rule["correct"]) != key:
            rules[key] = rule
    longest = max(map(len, rules), default=0)
    total = 0
    for segment in segments:
        tokens = segment.get("text", "").split()
        if not longest or not tokens:
            continue
        keys = [_key(token) for token in tokens]
        out, fixes, index = [], [], 0
        while index < len(tokens):
            for size in range(min(longest, len(tokens) - index), 0, -1):
                rule = rules.get(tuple(keys[index : index + size]))
                if rule:
                    break
            else:
                out.append(tokens[index])
                index += 1
                continue
            new = with_edges(tokens[index : index + size], rule["correct"].split())
            heard = " ".join(tokens[index : index + size])
            fixes.append({"start": len(out), "end": len(out) + len(new), "heard": heard, "rule": rule.get("id")})
            out += new
            index += size
        if fixes:
            segment["text"] = " ".join(out)
            segment["fixes"] = fixes
            total += len(fixes)
    return total


def shift_fixes(segment: dict, start: int, end: int, added: int) -> None:
    """Keep a segment's fix ranges correct after tokens [start, end) were replaced by `added` tokens."""
    kept = []
    for fix in segment.get("fixes", []):
        if fix["end"] <= start:
            kept.append(fix)
        elif fix["start"] >= end:
            kept.append(
                {**fix, "start": fix["start"] + added - (end - start), "end": fix["end"] + added - (end - start)}
            )
    if kept:
        segment["fixes"] = kept
    else:
        segment.pop("fixes", None)


def remember(data: dict, heard: str, correct: str, heard_gujlish: str, correct_gujlish: str, example: dict) -> dict:
    """Add or update a correction, and store the Gujlish spelling of each corrected word. Returns the correction
    (an empty dict when only the Gujlish spelling changed)."""
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
        rule = {"id": uuid4().hex[:12], "heard": heard, "heard_gujlish": heard_gujlish, "examples": []}
        rule["added"] = datetime.now().isoformat(timespec="seconds")
        data["corrections"].append(rule)
    rule.update(correct=correct, correct_gujlish=correct_gujlish)
    rule["examples"] = [*rule.get("examples", []), example][-MAX_EXAMPLES:]
    return rule


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


def save_clip(source: Path, start: float, end: float, target: Path) -> bool:
    """Cut the stretch of the recording a correction came from into a small Opus file. Best effort."""
    target.parent.mkdir(parents=True, exist_ok=True)
    command = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(0.0, start):.2f}", "-t", f"{end - start:.2f}"]
    command += ["-i", str(source), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libopus", "-b:a", "24k", str(target)]
    try:
        return subprocess.run(command, capture_output=True, timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False
