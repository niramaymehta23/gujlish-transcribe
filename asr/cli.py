#!/usr/bin/env python3
"""Local Gujarati ASR CLI."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .asr_backends import choose_device, load_backend
from .audio_stream import (
    CHUNK_SECONDS,
    GUJLISH_CHUNK_SECONDS,
    OVERLAP_SECONDS,
    audio_chunks,
    require_ffmpeg,
)
from .postprocess import confidence_plot, correct_segments, load_vocabulary, merge_segments
from .storage import TRANSCRIPTS, new_run, run_paths

CHECKPOINT_VERSION = 2


def _write_json(path: Path, value: dict) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _metadata(source: Path, args, device: str) -> dict:
    stat = source.stat()
    chunk_seconds = CHUNK_SECONDS if args.model == "flex" else GUJLISH_CHUNK_SECONDS
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "source_path": str(source),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "model": args.model,
        "mode": args.mode if args.model == "flex" else None,
        "vad": args.vad,
        "denoise": args.denoise,
        "device": device,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": OVERLAP_SECONDS,
    }


def run(args, progress=None) -> tuple[Path, Path, Path | None]:
    source = args.audio.expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(source)
    require_ffmpeg()
    if args.model != "flex" and args.mode != "mixed":
        raise ValueError("--mode applies only to the flex backend")
    if args.plot and args.model != "gujlish":
        raise ValueError("--plot is available only for the gujlish CTC backend")
    vocabulary = load_vocabulary(args.vocab.expanduser().resolve()) if args.vocab else None
    device = choose_device(args.device)
    metadata = _metadata(source, args, device)
    run_dir = getattr(args, "output_dir", None)
    if run_dir is None and not args.reset:
        label = args.model + (f"-{args.mode}" if args.model == "flex" else "")
        candidates = sorted((TRANSCRIPTS / source.stem / label).glob("*/.*.partial.json"), reverse=True)
        for candidate in candidates:
            try:
                if json.loads(candidate.read_text(encoding="utf-8")).get("metadata") == metadata:
                    run_dir = candidate.parent
                    break
            except (OSError, ValueError):
                continue
    run_dir = Path(run_dir).resolve() if run_dir else new_run(source, args.model, args.mode)
    run_dir.mkdir(parents=True, exist_ok=True)
    text_path, json_path, plot_path, checkpoint_path = run_paths(source, args.model, run_dir)

    def notify(stage, **details):
        if progress:
            progress({"stage": stage, **details})

    notify("loading", device=device)

    if args.reset and checkpoint_path.exists():
        checkpoint_path.unlink()
    segments: list[dict] = []
    if checkpoint_path.exists():
        saved = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if saved.get("metadata") != metadata:
            raise RuntimeError(f"Checkpoint settings/input differ; rerun with --reset: {checkpoint_path}")
        segments = saved.get("segments", [])
        print(f"Resuming after {len(segments)} completed chunks", flush=True)

    print(f"Backend: {args.model}; device: {device}; loading model on first unfinished chunk", flush=True)
    backend = None
    chunk_count = 0
    duration = 0.0
    chunk_seconds = CHUNK_SECONDS if args.model == "flex" else GUJLISH_CHUNK_SECONDS
    for index, chunk in enumerate(
        audio_chunks(source, chunk_seconds=chunk_seconds, vad=args.vad, denoise=args.denoise)
    ):
        chunk_count = index + 1
        duration = max(duration, chunk.end)
        if index < len(segments):
            notify("transcribing", processed=chunk.end, chunks=index + 1)
            continue
        if backend is None:
            backend = load_backend(args.model, device)
        print(f"[{index + 1}] {chunk.start:.1f}-{chunk.end:.1f}s", flush=True)
        segment = backend.transcribe(chunk.samples, chunk.start, chunk.end, args.mode)
        segments.append(segment)
        _write_json(checkpoint_path, {"metadata": metadata, "segments": segments})
        notify("transcribing", processed=chunk.end, chunks=index + 1)

    if not chunk_count:
        raise RuntimeError("No audio samples were decoded")
    if len(segments) != chunk_count:
        raise RuntimeError("Checkpoint contains more chunks than the decoded input; use --reset")
    if vocabulary:
        correct_segments(segments, vocabulary)
    transcript = merge_segments(segments)
    result = {
        "source": source.name,
        "model": args.model,
        "duration": duration,
        "mode": args.mode if args.model == "flex" else None,
        "device": device,
        "confidence": "unavailable" if args.model == "flex" else "model_probability",
        "segments": segments,
    }
    text_path.write_text(transcript + "\n", encoding="utf-8")
    _write_json(json_path, result)
    created_plot = None
    if args.plot:
        plot_path.parent.mkdir(parents=True, exist_ok=True)
        confidence_plot(segments, plot_path)
        created_plot = plot_path
    checkpoint_path.unlink(missing_ok=True)
    notify("complete", processed=duration, txt=str(text_path), json=str(json_path))
    return text_path, json_path, created_plot


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Stream long Gujarati/Gujarati-English audio through local ASR",
        epilog="Normal runs create only <recording>.<model>.txt and .json.",
    )
    parser.add_argument("audio", type=Path, help="Any file containing an FFmpeg-decodable audio stream")
    parser.add_argument("model", choices=["flex", "gujlish"])
    parser.add_argument(
        "--mode", choices=["mixed", "native"], default="mixed", help="Flex output mode (default: mixed)"
    )
    parser.add_argument(
        "--device",
        choices=["auto", "mps", "cuda", "cpu"],
        default="auto",
        help="Inference device (default: MPS on Apple Silicon when available)",
    )
    parser.add_argument("--vad", action="store_true", help="Use low-energy speech boundaries instead of fixed cuts")
    parser.add_argument(
        "--denoise", action="store_true", help="Apply FFmpeg's lightweight noise filter to the decoded stream"
    )
    parser.add_argument("--vocab", type=Path, help="JSON mapping preferred words to explicit ASR variants")
    parser.add_argument("--plot", action="store_true", help="After Gujlish transcription, write one confidence plot")
    parser.add_argument("--reset", action="store_true", help="Discard the matching partial checkpoint")
    parser.add_argument(
        "--output-dir", type=Path, help="Explicit transcript run folder; otherwise create an automatic dated run"
    )
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        progress = (lambda event: print("ASR_EVENT " + json.dumps(event), flush=True)) if args.progress_json else None
        outputs = run(args, progress=progress)
    except (ImportError, OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        parser.exit(1, f"Error: {error}\n")
    print("Done:")
    for output in outputs:
        if output:
            print(f"  {output}")


if __name__ == "__main__":
    main()
