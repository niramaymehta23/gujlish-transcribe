"""Conservative overlap merging and optional vocabulary correction."""

from __future__ import annotations

import json
import os
import tempfile
import unicodedata
from pathlib import Path

EDGE_PUNCTUATION = ".,!?;:\"'()[]{}…।॥"


def _key(word: str) -> str:
    return unicodedata.normalize("NFC", word).casefold().strip(EDGE_PUNCTUATION)


def remove_overlap(previous: str, current: str, limit: int = 12) -> tuple[str, int]:
    left, right = previous.split(), current.split()
    for size in range(min(limit, len(left), len(right)), 0, -1):
        if [_key(word) for word in left[-size:]] == [_key(word) for word in right[:size]]:
            return " ".join(right[size:]), size
    return current.strip(), 0


def merge_segments(segments: list[dict]) -> str:
    merged = ""
    for segment in segments:
        contribution, removed = remove_overlap(merged, segment.get("text", ""))
        segment["text"] = contribution
        if removed and segment.get("words"):
            segment["words"] = segment["words"][removed:]
        if contribution:
            merged = f"{merged} {contribution}".strip()
    return merged


def load_vocabulary(path: Path) -> dict[str, str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("Vocabulary must be a JSON object of preferred words to variant lists")
    variants: dict[str, str] = {}
    for preferred, spellings in raw.items():
        if not isinstance(preferred, str) or not isinstance(spellings, list):
            raise ValueError("Each vocabulary value must be a list of variant spellings")
        for spelling in spellings:
            if not isinstance(spelling, str):
                raise ValueError("Vocabulary variants must be strings")
            variants[_key(spelling)] = preferred
    return variants


def correct_segments(segments: list[dict], variants: dict[str, str], threshold: float = 0.75) -> None:
    for segment in segments:
        words = segment.get("words")
        if words is not None:
            for word in words:
                replacement = variants.get(_key(word["word"]))
                if replacement and word.get("confidence", 1.0) < threshold:
                    word["word"] = replacement
            segment["text"] = " ".join(word["word"] for word in words)
        else:
            pieces = segment.get("text", "").split()
            segment["text"] = " ".join(variants.get(_key(piece), piece) for piece in pieces)


def confidence_plot(segments: list[dict], output: Path) -> None:
    points = [word for segment in segments for word in segment.get("words", [])]
    if not points:
        raise RuntimeError("No CTC word confidence data is available to plot")
    with tempfile.TemporaryDirectory(prefix="gujarati-asr-mpl-") as config_dir:
        os.environ["MPLCONFIGDIR"] = config_dir
        try:
            import matplotlib

            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
        except ImportError as error:
            raise RuntimeError("--plot requires matplotlib; install project requirements") from error
        x = [(word["start"] + word["end"]) / 2 for word in points]
        y = [word["confidence"] for word in points]
        fig, axis = plt.subplots(figsize=(12, 4))
        axis.plot(x, y, linewidth=0.8, color="#2864dc")
        axis.scatter(x, y, s=7, color="#2864dc")
        axis.axhline(0.75, color="#c43c35", linestyle="--", linewidth=0.8)
        axis.set(xlabel="Recording time (seconds)", ylabel="CTC probability", ylim=(0, 1.02))
        axis.grid(alpha=0.2)
        fig.tight_layout()
        fig.savefig(output, dpi=150)
        plt.close(fig)
