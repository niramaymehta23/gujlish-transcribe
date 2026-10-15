#!/usr/bin/env python3
"""Run the local web interface: python app.py."""

import argparse

from web.server import create_app

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Local Gujarati ASR web interface")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    create_app().run(host="127.0.0.1", port=args.port, threaded=True, debug=False)
