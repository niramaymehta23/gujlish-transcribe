#!/usr/bin/env python3
"""Write a YouTube title, description and chapters from the English: python scripts/youtube.py data/transcripts/<name>/<run>/"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from anywhere: the code lives one folder up

from transcriber.english.enrich import youtube_main  # noqa: E402

if __name__ == "__main__":
    youtube_main()
