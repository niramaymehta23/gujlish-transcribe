"""Word timings: when each word of a finished transcript is spoken.

Flex gives the start and end of each chunk (10–27 s), not of its words. To time the English captions to the speech,
each chunk's text is lined up with its audio by forced alignment: the Gujlish wav2vec2 model (the comparison model,
whose characters cover Gujarati and Latin script) scores every 20 ms frame, and a Viterbi pass finds the most likely
place for each character of the known text. Nothing is re-recognized and the text is never changed; only times come
out. Silence, music and applause inside a chunk fall between words instead of being shared out over them.

The result is saved next to the transcript as `<name>.<model>.words.json`. After a word fix only the chunks whose
text changed are aligned again.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from transcriber.files.storage import stage_paths
from transcriber.files.textutil import write_json
from transcriber.speech.backends import GUJLISH_REPO

SAMPLE_RATE = 16_000
NEG = -1e30
SECONDS_PER_CHAR = 0.25  # a word lasting longer than this per character (plus WORD_SLACK) ran into a pause
WORD_SLACK = 0.3
VERSION = 1


def ctc_align(log_probs: np.ndarray, tokens: list[int], blank: int) -> list[tuple[int, int]] | None:
    """Viterbi CTC forced alignment. `log_probs` is frames × vocabulary; returns each token's [first, last) frame
    range, or None when there are too few frames to fit the tokens."""
    frames = log_probs.shape[0]
    if not tokens:
        return []
    ext = np.full(2 * len(tokens) + 1, blank)
    ext[1::2] = tokens
    states = len(ext)
    repeats = sum(1 for a, b in zip(tokens, tokens[1:]) if a == b)
    if frames < len(tokens) + repeats:
        return None
    skip = np.zeros(states, dtype=bool)
    skip[2:] = (ext[2:] != blank) & (ext[2:] != ext[:-2])
    score = np.full(states, NEG)
    score[0] = log_probs[0, ext[0]]
    score[1] = log_probs[0, ext[1]]
    back = np.zeros((frames, states), dtype=np.int8)
    columns = np.arange(states)
    for t in range(1, frames):
        stay = score
        step = np.concatenate(([NEG], score[:-1]))
        jump = np.where(skip, np.concatenate(([NEG, NEG], score[:-2])), NEG)
        options = np.stack([stay, step, jump])
        choice = options.argmax(axis=0)
        score = options[choice, columns] + log_probs[t, ext]
        back[t] = choice
    state = states - 1 if score[-1] >= score[-2] else states - 2
    if score[state] <= NEG / 2:
        return None
    spans: list[list[int]] = [[-1, -1] for _ in tokens]
    for t in range(frames - 1, -1, -1):
        if state % 2:
            span = spans[state // 2]
            span[0] = t
            if span[1] < 0:
                span[1] = t + 1
        state -= int(back[t, state])
    return [(a, b) for a, b in spans]


def fill_missing(times: list[dict | None], words: list[str], start: float, end: float) -> list[dict]:
    """Give words the alignment could not place (no known characters, e.g. a symbol) a time between their
    neighbours, shared out by length."""
    out: list[dict] = []
    index = 0
    while index < len(times):
        if times[index] is not None:
            out.append(times[index])
            index += 1
            continue
        stop = index
        while stop < len(times) and times[stop] is None:
            stop += 1
        low = out[-1]["end"] if out else start
        high = times[stop]["start"] if stop < len(times) else end
        high = max(high, low)
        sizes = [max(1, len(word)) for word in words[index:stop]]
        total, used = sum(sizes), 0
        for size in sizes:
            a = low + (high - low) * used / total
            used += size
            out.append({"start": round(a, 3), "end": round(low + (high - low) * used / total, 3)})
        index = stop
    return out


def estimate_times(text: str, start: float, end: float) -> list[dict]:
    """Rough word times for a chunk that could not be aligned: its time shared out by each word's length."""
    words = text.split()
    total, used, out = max(1, len(" ".join(words))), 0, []
    for word in words:
        a = start + (end - start) * used / total
        used += len(word)
        out.append({"start": round(a, 3), "end": round(start + (end - start) * used / total, 3)})
        used += 1  # the space
    return out


def word_times(
    words: list[str], log_probs: np.ndarray, vocab: dict[str, int], blank: int, start: float, end: float
) -> list[dict] | None:
    """Start and end (absolute seconds) of each word, from frame log-probabilities of the chunk [start, end)."""
    separator = vocab.get("|")
    tokens, owner = [], []
    for number, word in enumerate(words):
        known = [vocab[char] for char in word if char in vocab]
        if not known:
            continue  # timed from its neighbours below
        if tokens and separator is not None:
            tokens.append(separator)
            owner.append(-1)
        tokens += known
        owner += [number] * len(known)
    spans = ctc_align(log_probs, tokens, blank)
    if spans is None and separator is not None:  # too dense for the word gaps: try the characters alone
        keep = [i for i, who in enumerate(owner) if who >= 0]
        tokens, owner = [tokens[i] for i in keep], [owner[i] for i in keep]
        spans = ctc_align(log_probs, tokens, blank)
    if spans is None:
        return None
    seconds = (end - start) / max(1, log_probs.shape[0])
    found: list[list[int] | None] = [None] * len(words)
    for (a, b), who in zip(spans, owner):
        if who < 0 or a < 0:
            continue
        if found[who] is None:
            found[who] = [a, b]
        else:
            found[who][1] = b
    times: list[dict | None] = []
    for word, span in zip(words, found):
        if span is None:
            times.append(None)
            continue
        a, b = start + span[0] * seconds, start + span[1] * seconds
        # The last character can soak up a pause after the word; no word is spoken that slowly.
        b = min(b, a + WORD_SLACK + SECONDS_PER_CHAR * len(word))
        times.append({"start": round(a, 3), "end": round(b, 3)})
    return fill_missing(times, words, start, end)


def read_audio(source: Path, start: float, end: float) -> np.ndarray:
    """Mono 16 kHz float samples of source[start:end], decoded by FFmpeg."""
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0.0, start):.3f}", "-t", f"{max(0.0, end - start):.3f}",
        "-i", str(source), "-map", "0:a:0", "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-",
    ]  # fmt: skip
    result = subprocess.run(command, capture_output=True, timeout=600)
    if result.returncode:
        raise RuntimeError(f"FFmpeg could not read {source}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return np.frombuffer(result.stdout, dtype="<i2").astype(np.float32) / 32768.0


class Aligner:
    """Lines up text with audio using the Gujlish wav2vec2 model. Runs on the CPU, about half a second per chunk,
    so it never competes with a transcription for the GPU."""

    name = f"ctc:{GUJLISH_REPO}"

    def __init__(self, device: str = "cpu"):
        import torch
        from huggingface_hub import hf_hub_download
        from transformers import Wav2Vec2ForCTC

        self.torch = torch
        self.device = device
        self.model = Wav2Vec2ForCTC.from_pretrained(GUJLISH_REPO).to(device).eval()
        self.vocab = json.loads(Path(hf_hub_download(GUJLISH_REPO, "vocab.json")).read_text(encoding="utf-8"))
        self.blank = self.model.config.pad_token_id

    def log_probs(self, samples: np.ndarray) -> np.ndarray:
        torch = self.torch
        with torch.inference_mode():
            logits = self.model(torch.from_numpy(samples).unsqueeze(0).to(self.device)).logits[0]
            return logits.log_softmax(dim=-1).float().cpu().numpy()

    def align(self, samples: np.ndarray, start: float, end: float, text: str) -> list[dict] | None:
        words = text.split()
        if not words or len(samples) < SAMPLE_RATE // 10:
            return None
        return word_times(words, self.log_probs(samples), self.vocab, self.blank, start, end)


def _key(segment: dict) -> list:
    return [round(float(segment["start"]), 3), round(float(segment["end"]), 3), segment.get("text", "")]


def run_alignment(transcript_json: Path, audio: Path, aligner=None, progress=None) -> Path:
    """Write `<name>.<model>.words.json`: for every transcript segment, its words' start and end times. Segments
    aligned before with the same text are reused."""
    transcript_json = Path(transcript_json)
    segments = json.loads(transcript_json.read_text(encoding="utf-8")).get("segments", [])
    path = stage_paths(transcript_json)["words_json"]
    previous = {}
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("version") == VERSION:
            previous = {json.dumps(item["key"], ensure_ascii=False): item for item in saved.get("segments", [])}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    out = []
    todo = [i for i, s in enumerate(segments) if s.get("text", "").strip()]
    for done, index in enumerate(todo, 1):
        segment = segments[index]
        key = _key(segment)
        cached = previous.get(json.dumps(key, ensure_ascii=False))
        if cached is not None:
            out.append(cached)
        else:
            if aligner is None:
                aligner = Aligner()
            start, end = float(segment["start"]), float(segment["end"])
            words = aligner.align(read_audio(audio, start, end), start, end, segment["text"])
            out.append({"key": key, "words": words})
        if progress:
            progress({"stage": "aligning", "done": done, "total": len(todo)})
    write_json(path, {"kind": "words", "version": VERSION, "aligner": Aligner.name, "segments": out})
    return path


def load_word_times(transcript_json: Path, segments: list[dict]) -> list[list[dict] | None]:
    """Word times for each of `segments` (transcript segments), or None for a segment without them. Times come from
    the recognizer itself when it gives them (the Gujlish model), else from the saved alignment, used only while the
    segment's text is unchanged."""
    saved = {}
    try:
        data = json.loads(stage_paths(Path(transcript_json))["words_json"].read_text(encoding="utf-8"))
        if data.get("version") == VERSION:
            saved = {json.dumps(item["key"], ensure_ascii=False): item.get("words") for item in data["segments"]}
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        pass
    out: list[list[dict] | None] = []
    for segment in segments:
        words = segment.get("text", "").split()
        own = segment.get("words")
        if isinstance(own, list) and len(own) == len(words) and all("start" in w and "end" in w for w in own):
            out.append([{"start": float(w["start"]), "end": float(w["end"])} for w in own])
            continue
        found = saved.get(json.dumps(_key(segment), ensure_ascii=False))
        out.append(found if isinstance(found, list) and len(found) == len(words) else None)
    return out
