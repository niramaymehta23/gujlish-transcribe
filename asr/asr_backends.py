"""Small wrappers around the two supported ASR models.

Environment variables starting with GUJLISH_ belong to this project (its working name), not to the Gujlish model.
"""

from __future__ import annotations

import importlib.util
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FLEX_REPO = "bodhan-ai/indic-transcribe-flex"
GUJLISH_REPO = "WhissleAI/speech-tagger_gujlish_wav2vec2_meta"
META_TOKEN = re.compile(r"^(?:AGE|GENDER|EMOTION|INTENT|ENTITY)_.+|^END$")


def choose_device(requested: str) -> str:
    import torch

    if requested != "auto":
        available = {"cpu": True, "mps": torch.backends.mps.is_available(), "cuda": torch.cuda.is_available()}
        if not available[requested]:
            raise RuntimeError(f"Requested device {requested!r} is not available")
        return requested
    if torch.backends.mps.is_available():
        return "mps"
    if torch.cuda.is_available():
        return "cuda"
    return "cpu"


class FlexBackend:
    """Default model (Bodhan Indic-Transcribe Flex): mixed or native script output, no confidence scores."""

    def __init__(self, device: str):
        from huggingface_hub import snapshot_download

        # The Flex wrapper is Python code from the model repository and is executed below. Set
        # GUJLISH_FLEX_REVISION to a commit hash you have reviewed to stop upstream changes from altering behavior.
        revision = os.environ.get("GUJLISH_FLEX_REVISION") or None
        model_dir = Path(snapshot_download(FLEX_REPO, ignore_patterns=["nemo/*"], revision=revision))
        sys.path.insert(0, str(model_dir))
        try:
            module_path = model_dir / "indic_transcribe.py"
            spec = importlib.util.spec_from_file_location("bodhan_indic_transcribe", module_path)
            if spec is None or spec.loader is None:
                raise RuntimeError(f"Cannot import Flex wrapper from {module_path}")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            self.model = module.IndicTranscribe.from_pretrained(str(model_dir), device=device)
        finally:
            sys.path.remove(str(model_dir))

    def transcribe(self, samples: np.ndarray, start: float, end: float, mode: str) -> dict:
        text = self.model(samples, lang="gu", mode=mode).strip()
        return {"start": start, "end": end, "text": text}


@dataclass
class _Token:
    text: str
    start: float
    end: float
    confidence: float


class GujlishBackend:
    """Comparison model (WhissleAI wav2vec2 CTC): Gujlish output with per-word timings and probabilities."""

    def __init__(self, device: str):
        import json

        import torch
        from huggingface_hub import hf_hub_download
        from transformers import Wav2Vec2ForCTC

        self.torch = torch
        self.device = device
        self.model = Wav2Vec2ForCTC.from_pretrained(GUJLISH_REPO).to(device).eval()
        vocab_path = hf_hub_download(GUJLISH_REPO, "vocab.json")
        vocab = json.loads(Path(vocab_path).read_text(encoding="utf-8"))
        self.id_to_token = {token_id: token for token, token_id in vocab.items()}
        self.blank_id = self.model.config.pad_token_id

    def _token_text(self, token_id: int) -> str:
        token = self.id_to_token.get(token_id)
        if token in {None, "<pad>", "<s>", "</s>", "<unk>"}:
            return ""
        return " " if token == "|" else str(token)

    def transcribe(self, samples: np.ndarray, start: float, end: float, mode: str) -> dict:
        # `mode` is accepted for interface parity with FlexBackend; this model has a single output script.
        torch = self.torch
        input_values = torch.from_numpy(samples).unsqueeze(0).to(self.device)
        with torch.inference_mode():
            logits = self.model(input_values=input_values).logits[0]
            probabilities = logits.softmax(dim=-1)
            frame_probs, frame_ids = probabilities.max(dim=-1)
        ids = frame_ids.detach().cpu().tolist()
        probs = frame_probs.detach().cpu().tolist()
        frame_seconds = (end - start) / max(1, len(ids))

        tokens: list[_Token] = []
        index = 0
        while index < len(ids):
            stop = index + 1
            while stop < len(ids) and ids[stop] == ids[index]:
                stop += 1
            token_id = ids[index]
            text = self._token_text(token_id)
            if token_id != self.blank_id and text and not META_TOKEN.match(text):
                confidence = math.exp(sum(math.log(max(probs[i], 1e-12)) for i in range(index, stop)) / (stop - index))
                tokens.append(_Token(text, start + index * frame_seconds, start + stop * frame_seconds, confidence))
            index = stop

        words = []
        chars: list[_Token] = []

        def finish_word() -> None:
            if not chars:
                return
            word = "".join(token.text for token in chars).strip()
            if word:
                conf = math.exp(sum(math.log(max(token.confidence, 1e-12)) for token in chars) / len(chars))
                words.append({"word": word, "start": chars[0].start, "end": chars[-1].end, "confidence": conf})
            chars.clear()

        for token in tokens:
            if token.text.isspace():
                finish_word()
            else:
                chars.append(token)
        finish_word()
        text = " ".join(word["word"] for word in words)
        confidence = None
        if tokens:
            confidence = math.exp(sum(math.log(max(token.confidence, 1e-12)) for token in tokens) / len(tokens))
        return {"start": start, "end": end, "text": text, "confidence": confidence, "words": words}


def load_backend(name: str, device: str):
    return FlexBackend(device) if name == "flex" else GujlishBackend(device)
