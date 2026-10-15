# Contributing

Keep this project focused on local Gujarati/Gujarati-English transcription. Prefer small changes that improve correctness, usability, or measured performance.

## Development setup

For application development, follow the [README](README.md) installation steps, then install `requirements-test.txt` in the same virtual environment. For a lightweight test-only environment:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-test.txt
```

Install FFmpeg separately (`brew install ffmpeg` on macOS or `sudo apt install ffmpeg` on Ubuntu). Tests require both `ffmpeg` and `ffprobe`.

## Checks

```bash
python -m ruff check .
python -m ruff format --check .
python -m unittest discover -s tests -v
node --check web/static/app.js
```

To apply Python formatting: `python -m ruff format .`. Node is needed only for the JavaScript syntax check, not to run the application.

Automated tests use synthetic audio and stubbed ASR inference. They require no Hugging Face account or model downloads. Backend changes also need a separately documented real inference check; never describe a passing synthetic test as evidence of recognition accuracy or GPU performance.

## Pull requests

Explain the problem and resulting behavior, include verification, and update relevant documentation. Maintain mixed-script output as the Flex default, bounded streaming, truthful probability reporting, opt-in processing extras, and a plot-free web workflow.

Keep recordings, transcripts, exported documents, model weights, virtual environments, and secrets out of commits. Use public or synthetic examples for bug reports. Application code is MIT licensed; model weights and upstream code retain their own terms.
