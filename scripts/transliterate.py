#!/usr/bin/env python3
"""Write a finished transcript in Gujlish (Gujarati in Latin letters): python scripts/transliterate.py data/transcripts/<name>/<run>/"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # run from anywhere: the code lives one folder up

from transcriber.speech.gujlish import main  # noqa: E402

if __name__ == "__main__":
    main()
