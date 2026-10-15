#!/usr/bin/env python3
"""Compute WER and CER for a reference/prediction text-file pair."""

from __future__ import annotations

import argparse
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


def main() -> None:
    parser = argparse.ArgumentParser(description="Compare reference and predicted transcripts")
    parser.add_argument("reference", type=Path)
    parser.add_argument("prediction", type=Path)
    args = parser.parse_args()
    reference = args.reference.read_text(encoding="utf-8").strip()
    prediction = args.prediction.read_text(encoding="utf-8").strip()
    print(f"WER: {error_rate(reference.split(), prediction.split()):.4f}")
    print(f"CER: {error_rate(list(reference), list(prediction)):.4f}")


if __name__ == "__main__":
    main()
