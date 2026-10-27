#!/usr/bin/env python3
"""Summarize a translated recording: python summarize.py transcripts/<name>/<run>/"""

from asr.enrich import summarize_main

if __name__ == "__main__":
    summarize_main()
