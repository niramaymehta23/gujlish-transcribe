"""Word fixes made in the browser: replace words in a finished transcript and keep its Gujlish in step."""

from __future__ import annotations

import json
from pathlib import Path

from .dictionary import shift_fixes, with_edges
from .gujlish import gujarati_words, latin_token
from .postprocess import _key
from .subtitles import write_srt
from .textutil import write_json


def transcript_text(segments: list[dict]) -> str:
    """The transcript as one line, as the CLI writes it (segment texts joined by spaces)."""
    return " ".join(text for text in (segment.get("text", "").strip() for segment in segments) if text)


def save_transcript(json_path: Path, data: dict) -> None:
    write_json(json_path, data)
    json_path.with_suffix(".txt").write_text(transcript_text(data["segments"]) + "\n", encoding="utf-8")
    write_srt(json_path.with_suffix(".srt"), data["segments"])


def load_gujlish(path: Path, segments: list[dict]) -> tuple[list[list[str]] | None, str]:
    """Gujlish words per segment and the engine that wrote them; None when there are none or they no longer line up
    with the Gujarati."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tokens = [segment["tokens"] for segment in data["segments"]]
    except (OSError, ValueError, KeyError, TypeError):
        return None, ""
    if len(tokens) != len(segments) or any(len(t) != len(s.get("text", "").split()) for t, s in zip(tokens, segments)):
        return None, ""
    return tokens, data.get("engine", "")


def matches(segments: list[dict], heard: str) -> list[tuple[int, int]]:
    """(segment, start) of every non-overlapping occurrence of the words `heard` (punctuation and case ignored)."""
    key = tuple(_key(word) for word in heard.split())
    found = []
    for number, segment in enumerate(segments):
        keys, index = [_key(token) for token in segment.get("text", "").split()], 0
        while key and index + len(key) <= len(keys):
            if tuple(keys[index : index + len(key)]) == key:
                found.append((number, index))
                index += len(key)
            else:
                index += 1
    return found


def replace_words(
    segments: list[dict],
    gujlish: list[list[str]] | None,
    places: list[tuple[int, int]],
    size: int,
    correct: str,
    correct_gujlish: str,
    transliterator=None,
) -> list[str]:
    """Replace `size` words at each (segment, start) with `correct`, editing the Gujlish words alongside. Returns the
    Gujlish used for the new words (yours when it has one word per Gujarati word, otherwise transliterated)."""
    new = correct.split()
    latin = correct_gujlish.split()
    if len(latin) != len(new):
        spellings = transliterator.spellings(gujarati_words(correct)) if transliterator else {}
        latin = [latin_token(word, spellings) for word in new]
    for number, start in sorted(places, reverse=True):  # right to left, so earlier positions stay valid
        segment, end = segments[number], start + size
        tokens = segment["text"].split()
        replacement = with_edges(tokens[start:end], new)
        if gujlish is not None:
            gujlish[number][start:end] = with_edges(gujlish[number][start:end], latin)
        tokens[start:end] = replacement
        segment["text"] = " ".join(tokens)
        shift_fixes(segment, start, end, len(replacement))
    return latin
