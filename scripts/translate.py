#!/usr/bin/env python3
"""Translate a finished transcript into spoken-tone English: python scripts/translate.py data/transcripts/<name>/<run>/"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from anywhere: the code lives one folder up

from transcriber.english.enrich import translate_main  # noqa: E402

if __name__ == "__main__":
    translate_main()
