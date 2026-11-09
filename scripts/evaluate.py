#!/usr/bin/env python3
"""Compute WER and CER for a reference/prediction text-file pair."""

from __future__ import annotations

import argparse
import re
import unicodedata
from pathlib import Path


def edit_distance(reference: list[str], prediction: list[str]) -> int:
    row = list(range(len(prediction) + 1))
    for i, expected in enumerate(reference, 1):
        next_row = [i]
        for j, actual in enumerate(prediction, 1):
            next_row.append(min(next_row[-1] + 1, row[j] + 1, row[j - 1] + (expected != actual)))
        row = next_row
    return row[-1]


def error_rate(reference: list[str], prediction: list[str]) -> float:
    if not reference:
        return 0.0 if not prediction else 1.0
    return edit_distance(reference, prediction) / len(reference)


def normalize(text: str) -> str:
    """NFC, casefold, punctuation removed (Gujarati combining marks are kept), whitespace collapsed.

    Without this, differences in danda/full-stop or capitalisation count as word errors even though they are not
    recognition mistakes. Use the same setting for every system you compare.
    """
    text = unicodedata.normalize("NFC", text).casefold()
    text = "".join(" " if unicodedata.category(c).startswith("P") else c for c in text)
    return re.sub(r"\s+", " ", text).strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare reference and predicted transcripts")
    parser.add_argument("reference", type=Path)
    parser.add_argument("prediction", type=Path)
    parser.add_argument(
        "--normalize", action="store_true", help="Ignore case, punctuation and Unicode composition differences"
    )
    args = parser.parse_args()
    reference = args.reference.read_text(encoding="utf-8").strip()
    prediction = args.prediction.read_text(encoding="utf-8").strip()
    if args.normalize:
        reference, prediction = normalize(reference), normalize(prediction)
    print(f"WER: {error_rate(reference.split(), prediction.split()):.4f}")
    print(f"CER: {error_rate(list(reference), list(prediction)):.4f}")


if __name__ == "__main__":
    main()
