"""Shared pieces for the YouTube stage, which reads the English translation.

The model is shown the translation as numbered lines, one per recognized chunk, with each chunk's real start and end
time. It answers with line numbers, never with times, so every time in the output is computed from the recording's
own timings instead of trusting the model's arithmetic.
"""

from __future__ import annotations

import json
from pathlib import Path

from transcriber.english.llm import LLMError, parse_json_object
from transcriber.files.storage import DATA, stage_paths
from transcriber.files.textutil import escape_tags, fmt_time


def load_translation(transcript_json: Path, label: str | None = None) -> tuple[dict, list[dict]]:
    """The translation JSON next to `transcript_json` and its non-empty segments ({start, end, text})."""
    paths = stage_paths(Path(transcript_json), label)
    if not paths["translation_json"].exists():
        raise RuntimeError(
            f"No translation found at {paths['translation_json'].name}. Translate first (python scripts/translate.py RUN/), "
            "using the same --label if you gave one."
        )
    translation = json.loads(paths["translation_json"].read_text(encoding="utf-8"))
    segments = [
        {"start": float(s["start"]), "end": float(s["end"]), "text": s["text"].strip()}
        for s in translation.get("segments", [])
        if s.get("text", "").strip()
    ]
    if not segments:
        raise RuntimeError("The translation has no English text.")
    return translation, segments


def numbered_lines(segments: list[dict], first: int = 0, last: int | None = None) -> str:
    """Lines `#n [mm:ss-mm:ss] text` for segments[first..last] (inclusive), numbered from 1 across the recording."""
    last = len(segments) - 1 if last is None else last
    return "\n".join(
        f"#{i + 1} [{fmt_time(segments[i]['start'])}-{fmt_time(segments[i]['end'])}] {escape_tags(segments[i]['text'])}"
        for i in range(first, last + 1)
    )


def line_index(value: object, count: int) -> int | None:
    """A 1-based line number from the model ("#12", "12" or 12) as a 0-based index, or None if not a real line."""
    if isinstance(value, str):
        value = value.strip().lstrip("#")
        if not value.isdigit():
            return None
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value - 1 if 1 <= value <= count else None


def read_text_file(path: Path | None, default_name: str) -> tuple[str, Path | None]:
    """Text of `path`, or of DATA/<default_name> when no path is given and that file exists; ("", None) otherwise."""
    if path is None:
        candidate = DATA / default_name
        if not candidate.exists():
            return "", None
        path = candidate
    path = Path(path).expanduser()
    return path.read_text(encoding="utf-8").strip(), path


def call_json(llm, system: str, user: str, max_tokens: int) -> dict:
    """One JSON reply from the model, asking again once if it was not valid JSON."""
    last_error: LLMError | None = None
    for attempt in range(2):
        prompt = user if not attempt else user + "\n\nReturn ONLY one valid JSON object, with no other text."
        completion = llm.complete(system, prompt, max_tokens=max_tokens, temperature=0.4)
        try:
            return parse_json_object(completion.text)
        except LLMError as error:
            last_error = error
            if completion.truncated:
                max_tokens *= 2
    raise last_error or LLMError("The model did not return JSON")


def text_of(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def list_of(value: object) -> list:
    return value if isinstance(value, list) else []
