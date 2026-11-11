"""Download the models named in GUJLISH_MODELS, the Gujlish model (always: it times words), and the Gujlish
transliteration model, into the image's Hugging Face cache (run during docker build)."""

import os
from pathlib import Path

from huggingface_hub import snapshot_download

# Kept in step with transcriber/speech/backends.py; duplicated so code edits don't invalidate the cached model layer.
REPOS = {
    "flex": ("bodhan-ai/indic-transcribe-flex", ["nemo/*"]),
    "gujlish": ("WhissleAI/speech-tagger_gujlish_wav2vec2_meta", []),
}
# Kept in step with transcriber/speech/gujlish.py: IndicXlit in both directions (Gujarati → Gujlish, and Gujlish → Gujarati for the
# word editor's suggestions).
XLIT = ("Singla0009/all-indic-transliteration", "23279e441f195f6a45296fcee84ac89b541254ff")
XLIT_FOLDERS = ["indicxlit_indic_en_ct2_fp32/*", "indicxlit_ct2_fp32/*"]

secret = Path("/run/secrets/hf_token")
token = secret.read_text().strip() if secret.exists() else None
# The Gujlish model is always included: besides being a transcription choice, it times words for the English captions
# and the word editor's play button (transcriber/speech/align.py), even on a site that offers only Flex.
offered = [m.strip() for m in os.environ["GUJLISH_MODELS"].split(",") if m.strip()]
for model in dict.fromkeys([*offered, "gujlish"]):
    repo, ignore = REPOS[model]
    revision = os.environ.get("GUJLISH_FLEX_REVISION") if model == "flex" else None
    if model == "flex" and not token:
        raise SystemExit("Flex is gated: build with --secret id=hf_token,src=<file holding your Hugging Face token>")
    print(f"Fetching {repo}", flush=True)
    snapshot_download(repo, revision=revision, ignore_patterns=ignore or None, token=token)
print(f"Fetching {XLIT[0]}", flush=True)
snapshot_download(XLIT[0], revision=XLIT[1], allow_patterns=XLIT_FOLDERS)
