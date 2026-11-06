"""Command-line glue for the translation and summary stages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .llm import DEFAULT_GEMINI_MODEL, GeminiLLM, LLMError
from .summarize import run_summary
from .translate import STYLES, load_glossary, run_translation

STAGE_ERRORS = (ImportError, OSError, RuntimeError, ValueError, LLMError, json.JSONDecodeError)


def add_llm_arguments(parser: argparse.ArgumentParser, *, translation: bool = True, summary: bool = True) -> None:
    group = parser.add_argument_group("Translation and summary (Gemini; needs GEMINI_API_KEY)")
    group.add_argument(
        "--llm-model",
        help=f"Gemini model for translation and, by default, the summary (default {DEFAULT_GEMINI_MODEL})",
    )
    if summary:
        group.add_argument("--summary-model", help="Gemini model for the summary, if different")
    if translation:
        group.add_argument(
            "--tone",
            choices=sorted(STYLES),
            default="spoken",
            help="spoken: natural spoken English that carries the speaker's tone (default). faithful: closer to the "
            "Gujarati structure, useful for checking",
        )
        group.add_argument("--style-note", default="", help='Extra context, e.g. "a talk to a large live audience"')
        group.add_argument("--glossary", type=Path, help='JSON {"term": "English rendering"} for names and terms')
        group.add_argument("--limit", type=int, help="Translate only the first N segments (to compare models quickly)")
    group.add_argument("--label", help="Suffix for output files, so runs with different models do not overwrite")


def resolve_transcript(path: Path) -> Path:
    """Accept a transcript JSON or a run folder containing exactly one."""
    path = Path(path).expanduser().resolve()
    if path.is_file():
        return path
    if path.is_dir():
        found = []
        for candidate in sorted(path.glob("*.json")):
            if candidate.name.startswith("."):
                continue
            try:
                data = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and "segments" in data and "kind" not in data:
                found.append(candidate)
        if len(found) == 1:
            return found[0]
        raise RuntimeError(f"Expected one transcript JSON in {path}, found {len(found)}; pass the file itself")
    raise FileNotFoundError(path)


def summary_llm_for(args):
    return GeminiLLM(args.summary_model or args.llm_model)


def effective_label(args) -> str | None:
    return args.label or ("sample" if getattr(args, "limit", None) else None)


def run_stages(transcript_json: Path, args, *, translate: bool, summarize: bool, progress=None) -> list[Path]:
    outputs: list[Path] = []
    label = effective_label(args)
    translated_by = None
    if translate:
        llm = GeminiLLM(args.llm_model)
        print(f"Translating with {llm.describe()}", flush=True)
        translated_by = llm.label
        paths = run_translation(
            transcript_json,
            llm,
            tone=args.tone,
            glossary=load_glossary(args.glossary),
            style_note=args.style_note,
            limit=getattr(args, "limit", None),
            label=label,
            reset=getattr(args, "reset", False),
            progress=progress,
        )
        outputs += [paths["translation_txt"], paths["translation_srt"], paths["bilingual"], paths["translation_json"]]
        if progress:
            progress(
                {"stage": "complete", "txt": str(paths["translation_txt"]), "json": str(paths["translation_json"])}
            )
    if summarize:
        llm = summary_llm_for(args)
        print(f"Summarizing with {llm.describe()}", flush=True)
        paths = run_summary(
            transcript_json,
            llm,
            translation_llm_label=translated_by,
            label=label,
            reset=getattr(args, "reset", False),
            progress=progress,
        )
        outputs += [paths["summary_md"], paths["summary_json"]]
        if progress:
            progress({"stage": "complete", "txt": str(paths["summary_md"]), "json": str(paths["summary_json"])})
    return outputs


def _progress_printer(as_json: bool):
    def show(event: dict) -> None:
        if as_json:
            print("ASR_EVENT " + json.dumps(event), flush=True)
        elif event["stage"] in {"translating", "summarizing", "describing", "clipping"}:
            print(f"  {event['stage']} {event['done']}/{event['total']}", flush=True)

    return show


def _run(parser: argparse.ArgumentParser, *, translate: bool, summarize: bool) -> None:
    args = parser.parse_args()
    try:
        transcript = resolve_transcript(args.transcript)
        outputs = run_stages(
            transcript, args, translate=translate, summarize=summarize, progress=_progress_printer(args.progress_json)
        )
    except STAGE_ERRORS as error:
        parser.exit(1, f"Error: {error}\n")
    print("Done:")
    for output in outputs:
        print(f"  {output}")


def translate_main() -> None:
    parser = argparse.ArgumentParser(
        description="Translate a finished Gujarati transcript into English that keeps the speaker's tone"
    )
    parser.add_argument(
        "transcript", type=Path, help="The <name>.<model>.json file of a transcription run, or its folder"
    )
    add_llm_arguments(parser, summary=False)
    parser.add_argument("--reset", action="store_true", help="Discard an interrupted translation and start again")
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(summary_model=None)
    _run(parser, translate=True, summarize=False)


def summarize_main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarize a translated recording: overview, main points by theme, stories, quotes"
    )
    parser.add_argument(
        "transcript", type=Path, help="The <name>.<model>.json file of a transcription run, or its folder"
    )
    add_llm_arguments(parser, translation=False)
    parser.add_argument("--reset", action="store_true", help="Discard interrupted summary progress and start again")
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(tone="spoken", style_note="", glossary=None, limit=None)
    _run(parser, translate=False, summarize=True)


def _channel_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--channel",
        type=Path,
        help="Text file describing the channel, its audience and style (default: channel.md in the project folder, "
        "if there). See channel.example.md",
    )
    parser.add_argument("--style-note", default="", help="Extra context for this recording")
    parser.add_argument("--llm-model", help=f"Gemini model to use (default {DEFAULT_GEMINI_MODEL})")
    parser.add_argument("--label", help="The --label the translation was made with, if any; also tags the output")
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)


def youtube_main() -> None:
    from .publish_common import read_text_file
    from .youtube import run_youtube

    parser = argparse.ArgumentParser(
        description="Write a YouTube title, description, chapters and tags from a recording's English translation"
    )
    parser.add_argument(
        "transcript", type=Path, help="The <name>.<model>.json file of a transcription run, or its folder"
    )
    _channel_arguments(parser)
    parser.add_argument(
        "--footer",
        type=Path,
        help="Text added word for word to the end of every description, e.g. links (default: youtube_footer.txt "
        "in the project folder, if there)",
    )
    args = parser.parse_args()
    try:
        transcript = resolve_transcript(args.transcript)
        channel, channel_path = read_text_file(args.channel, "channel.md")
        footer, _ = read_text_file(args.footer, "youtube_footer.txt")
        llm = GeminiLLM(args.llm_model)
        print(f"Writing YouTube metadata with {llm.describe()}", flush=True)
        if channel_path:
            print(f"Channel profile: {channel_path}", flush=True)
        paths = run_youtube(
            transcript,
            llm,
            channel=channel,
            footer=footer,
            style_note=args.style_note,
            label=args.label,
            progress=_progress_printer(args.progress_json),
        )
    except STAGE_ERRORS as error:
        parser.exit(1, f"Error: {error}\n")
    print("Done:")
    for output in paths.values():
        print(f"  {output}")


def clips_main() -> None:
    from .clips import DEFAULT_MIN_SCORE, run_clips
    from .publish_common import read_text_file

    parser = argparse.ArgumentParser(
        description="Find 30-second to 3-minute clips for YouTube Shorts and Instagram Reels in a recording's English "
        "translation, judged by your channel's clip rules"
    )
    parser.add_argument(
        "transcript", type=Path, help="The <name>.<model>.json file of a transcription run, or its folder"
    )
    parser.add_argument(
        "--rules",
        type=Path,
        help="Text file with the channel's clip rules and clip types (default: clip_rules.md in the project folder, "
        "if there). See clip_rules.example.md",
    )
    _channel_arguments(parser)
    parser.add_argument(
        "--min-score",
        type=int,
        default=DEFAULT_MIN_SCORE,
        help=f"Leave out clips the model scores below this, 1-10 (default {DEFAULT_MIN_SCORE})",
    )
    parser.add_argument("--max-clips", type=int, help="Keep at most this many clips, best first")
    parser.add_argument("--reset", action="store_true", help="Discard interrupted progress and start again")
    args = parser.parse_args()
    try:
        transcript = resolve_transcript(args.transcript)
        rules, rules_path = read_text_file(args.rules, "clip_rules.md")
        channel, _ = read_text_file(args.channel, "channel.md")
        llm = GeminiLLM(args.llm_model)
        print(f"Finding clips with {llm.describe()}", flush=True)
        print(f"Clip rules: {rules_path or 'none given, using general rules'}", flush=True)
        paths = run_clips(
            transcript,
            llm,
            rules=rules,
            rules_from=rules_path.name if rules_path else "",
            channel=channel,
            style_note=args.style_note,
            min_score=args.min_score,
            max_clips=args.max_clips,
            label=args.label,
            reset=args.reset,
            progress=_progress_printer(args.progress_json),
        )
    except STAGE_ERRORS as error:
        parser.exit(1, f"Error: {error}\n")
    print("Done:")
    for output in paths.values():
        print(f"  {output}")
