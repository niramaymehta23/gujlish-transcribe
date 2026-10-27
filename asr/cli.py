#!/usr/bin/env python3
"""Local Gujarati Transcriber CLI."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .asr_backends import choose_device, load_backend
from .audio_stream import (
    CHUNK_SECONDS,
    GUJLISH_CHUNK_SECONDS,
    OVERLAP_SECONDS,
    audio_chunks,
    load_detector,
    require_ffmpeg,
)
from .enrich import STAGE_ERRORS, add_llm_arguments, run_stages
from .guards import check_segment
from .postprocess import confidence_plot, correct_segments, load_vocabulary, merge_segments
from .segmenter import split_chunk
from .storage import TRANSCRIPTS, new_run, run_label, run_paths
from .textutil import write_json

CHECKPOINT_VERSION = 3
RETRY_FLAGS = {"repetition", "too_dense"}


def _metadata(source: Path, args, device: str, detector_name: str | None) -> dict:
    stat = source.stat()
    chunk_seconds = CHUNK_SECONDS if args.model == "flex" else GUJLISH_CHUNK_SECONDS
    return {
        "checkpoint_version": CHECKPOINT_VERSION,
        "source_path": str(source),
        "source_size": stat.st_size,
        "source_mtime_ns": stat.st_mtime_ns,
        "model": args.model,
        "mode": args.mode if args.model == "flex" else None,
        "chunking": args.chunking,
        "detector": detector_name,
        "denoise": args.denoise,
        "device": device,
        "chunk_seconds": chunk_seconds,
        "overlap_seconds": OVERLAP_SECONDS,
    }


def _merge_pieces(pieces: list[dict], start: float, end: float) -> dict:
    """Combine the results of decoding the two halves of a split chunk."""
    merged = {"start": start, "end": end, "text": " ".join(p["text"] for p in pieces if p["text"]).strip()}
    if any("words" in piece for piece in pieces):
        merged["words"] = [word for piece in pieces for word in piece.get("words", [])]
        scores = [piece["confidence"] for piece in pieces if piece.get("confidence") is not None]
        merged["confidence"] = sum(scores) / len(scores) if scores else None
    return merged


def _decode_chunk(backend, chunk, mode: str) -> dict:
    """Transcribe one chunk. Suspicious output is re-decoded in two halves; text is never edited or removed."""
    segment = backend.transcribe(chunk.samples, chunk.start, chunk.end, mode)
    flags = check_segment(segment.get("text", ""), chunk.duration)
    # Only loops and over-dense text are worth a second decode. Sparse output is flagged but kept: music and quiet
    # passages are legitimately sparse, and splitting the audio would not recover missing words.
    if set(flags) & RETRY_FLAGS:
        parts = split_chunk(chunk)
        if parts and len(parts) == 2:
            retried = _merge_pieces(
                [backend.transcribe(p.samples, p.start, p.end, mode) for p in parts], chunk.start, chunk.end
            )
            if not check_segment(retried["text"], chunk.duration):
                segment, flags = retried, ["redecoded_in_halves"]
    if flags:
        segment["flags"] = flags
    return segment


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
    detector = load_detector(args.detector) if args.chunking == "pause" else None
    metadata = _metadata(source, args, device, detector.name if detector else None)
    run_dir = getattr(args, "output_dir", None)
    if run_dir is None and not args.reset:
        candidates = sorted(
            (TRANSCRIPTS / source.stem / run_label(args.model, args.mode)).glob("*/.*.partial.json"), reverse=True
        )
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

    print(
        f"Backend: {args.model}; device: {device}; chunking: {args.chunking}"
        + (f" ({detector.name} speech detector)" if detector else "")
        + "; loading model on first unfinished chunk",
        flush=True,
    )
    backend = None
    chunk_count = 0
    duration = 0.0
    chunk_seconds = CHUNK_SECONDS if args.model == "flex" else GUJLISH_CHUNK_SECONDS
    for index, chunk in enumerate(
        audio_chunks(
            source, chunk_seconds=chunk_seconds, chunking=args.chunking, detector=detector, denoise=args.denoise
        )
    ):
        chunk_count = index + 1
        duration = max(duration, chunk.window_end or chunk.end)
        if index < len(segments):
            notify("transcribing", processed=chunk.end, chunks=index + 1)
            continue
        if not chunk.speech:
            segment = {"start": chunk.start, "end": chunk.end, "text": "", "skipped": "no_speech"}
            print(f"[{index + 1}] {chunk.start:.1f}-{chunk.end:.1f}s no speech detected; skipped", flush=True)
        else:
            if backend is None:
                backend = load_backend(args.model, device)
            print(f"[{index + 1}] {chunk.start:.1f}-{chunk.end:.1f}s", flush=True)
            segment = _decode_chunk(backend, chunk, args.mode)
        segment["overlap"] = chunk.overlap
        segments.append(segment)
        write_json(checkpoint_path, {"metadata": metadata, "segments": segments})
        notify("transcribing", processed=chunk.end, chunks=index + 1)

    if not chunk_count:
        raise RuntimeError("No audio samples were decoded")
    if len(segments) != chunk_count:
        raise RuntimeError("Checkpoint contains more chunks than the decoded input; use --reset")
    if not any(segment.get("text", "").strip() for segment in segments) and all(s.get("skipped") for s in segments):
        raise RuntimeError(
            "No speech was detected in this recording. If it is very quiet, retry with --chunking fixed, which "
            "does not rely on speech detection."
        )
    if vocabulary:
        correct_segments(segments, vocabulary)
    transcript = merge_segments(segments)
    flagged = [s for s in segments if s.get("flags")]
    if flagged:
        times = ", ".join(f"{s['start']:.0f}s" for s in flagged[:10])
        print(
            f"Note: {len(flagged)} chunk(s) look unusual (repeated, very dense or very sparse text) near {times}",
            flush=True,
        )
    result = {
        "source": source.name,
        "model": args.model,
        "duration": duration,
        "mode": args.mode if args.model == "flex" else None,
        "device": device,
        "chunking": args.chunking,
        "detector": detector.name if detector else None,
        "confidence": "unavailable" if args.model == "flex" else "model_probability",
        "segments": segments,
    }
    text_path.write_text(transcript + "\n", encoding="utf-8")
    write_json(json_path, result)
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
        epilog="Normal runs create only <recording>.<model>.txt and .json. Add --translate and --summarize for "
        "English output.",
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
    parser.add_argument(
        "--chunking",
        choices=["pause", "fixed"],
        default="pause",
        help="pause: cut at the speaker's pauses, trim silence and skip chunks without speech (default). "
        "fixed: the original fixed-length cuts with overlap, for comparison",
    )
    parser.add_argument(
        "--detector",
        choices=["auto", "silero", "energy"],
        default="auto",
        help="Speech detector for --chunking pause: Silero VAD when installed, else a simple energy gate",
    )
    parser.add_argument(
        "--denoise", action="store_true", help="Apply FFmpeg's lightweight noise filter to the decoded stream"
    )
    parser.add_argument("--vocab", type=Path, help="JSON mapping preferred words to explicit ASR variants")
    parser.add_argument("--plot", action="store_true", help="After Gujlish transcription, write one confidence plot")
    parser.add_argument("--reset", action="store_true", help="Discard the matching partial checkpoint")
    parser.add_argument(
        "--output-dir", type=Path, help="Explicit transcript run folder; otherwise create an automatic dated run"
    )
    parser.add_argument("--translate", action="store_true", help="After transcribing, translate to spoken-tone English")
    parser.add_argument("--summarize", action="store_true", help="After translating, write a structured summary")
    add_llm_arguments(parser)
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    try:
        progress = (lambda event: print("ASR_EVENT " + json.dumps(event), flush=True)) if args.progress_json else None
        outputs = list(run(args, progress=progress))
        if args.translate or args.summarize:
            outputs += run_stages(
                outputs[1], args, translate=args.translate, summarize=args.summarize, progress=progress
            )
    except STAGE_ERRORS as error:
        parser.exit(1, f"Error: {error}\n")
    print("Done:")
    for output in outputs:
        if output:
            print(f"  {output}")


if __name__ == "__main__":
    main()
