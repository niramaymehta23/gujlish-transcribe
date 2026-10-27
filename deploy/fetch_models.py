"""Download the models named in GUJLISH_MODELS into the image's Hugging Face cache (run during docker build)."""

import os
from pathlib import Path

from huggingface_hub import snapshot_download

# Kept in step with asr/asr_backends.py; duplicated so code edits don't invalidate the cached model layer.
REPOS = {
    "flex": ("bodhan-ai/indic-transcribe-flex", ["nemo/*"]),
    "gujlish": ("WhissleAI/speech-tagger_gujlish_wav2vec2_meta", []),
}

secret = Path("/run/secrets/hf_token")
token = secret.read_text().strip() if secret.exists() else None
for model in filter(None, (m.strip() for m in os.environ["GUJLISH_MODELS"].split(","))):
    repo, ignore = REPOS[model]
    revision = os.environ.get("GUJLISH_FLEX_REVISION") if model == "flex" else None
    if model == "flex" and not token:
        raise SystemExit("Flex is gated: build with --secret id=hf_token,src=<file holding your Hugging Face token>")
    print(f"Fetching {repo}", flush=True)
    snapshot_download(repo, revision=revision, ignore_patterns=ignore or None, token=token)
