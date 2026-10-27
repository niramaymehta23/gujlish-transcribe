#!/usr/bin/env python3
"""Translate a finished transcript into spoken-tone English: python translate.py transcripts/<name>/<run>/"""

from asr.enrich import translate_main

if __name__ == "__main__":
    translate_main()
