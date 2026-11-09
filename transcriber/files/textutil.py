"""Small helpers shared by the translation and summary stages."""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

GUJARATI_CHARS = re.compile(r"[઀-૿]")
LETTERS = re.compile(r"[^\W\d_]", re.UNICODE)


def fmt_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"


def parse_time(value: object) -> float | None:
    """Parse "mm:ss" or "h:mm:ss" (as written by fmt_time), or a plain number of seconds; None if neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) if value >= 0 else None
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d{2})\s*", value)
    if not match:
        return None
    hours, minutes, secs = (int(part or 0) for part in match.groups())
    if secs >= 60 or (match.group(1) and minutes >= 60):
        return None
    return float(hours * 3600 + minutes * 60 + secs)


def gujarati_ratio(text: str) -> float:
    """Share of letters in `text` that are Gujarati script (0 when there are no letters)."""
    letters = len(LETTERS.findall(text))
    return len(GUJARATI_CHARS.findall(text)) / letters if letters else 0.0


def escape_tags(text: str) -> str:
    """Keep transcript text from forging the prompt's section tags."""
    return text.replace("<", "‹").replace(">", "›")


def sha(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def write_json(path: Path, value: object) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
