"""Command-line glue for the translation stage and the summary & YouTube stage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from transcriber.english.llm import DEFAULT_GEMINI_MODEL, GeminiLLM, LLMError
from transcriber.english.publish_common import read_text_file
from transcriber.english.summarize import run_summary
from transcriber.english.translate import STYLES, load_glossary, run_translation
from transcriber.english.youtube import run_youtube

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
    group.add_argument(
        "--channel",
        type=Path,
        help="Channel profile: how names and terms are spelled, the audience and title style (default: data/channel.md, "
        "if there). See examples/channel.md",
    )
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
    channel, channel_path = read_text_file(getattr(args, "channel", None), "channel.md")
    if channel_path:
        print(f"Channel profile: {channel_path}", flush=True)
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
            channel=channel,
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
        print("Writing the YouTube title and description", flush=True)
        footer, _ = read_text_file(None, "youtube_footer.txt")
        paths = run_youtube(transcript_json, llm, channel=channel, footer=footer, label=label)
        outputs += [paths["youtube_md"], paths["youtube_json"]]
        if progress:
            progress({"stage": "complete", "txt": str(paths["summary_md"]), "json": str(paths["summary_json"])})
    return outputs


def _progress_printer(as_json: bool):
    def show(event: dict) -> None:
        if as_json:
            print("ASR_EVENT " + json.dumps(event), flush=True)
        elif event["stage"] in {"translating", "summarizing", "describing"}:
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
        description="Summarize a translated recording (overview, main points by theme, stories, quotes) and write its "
        "YouTube title and description"
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
        help="Text file describing the channel, its audience and style (default: data/channel.md, if there). "
        "See examples/channel.md",
    )
    parser.add_argument("--style-note", default="", help="Extra context for this recording")
    parser.add_argument("--llm-model", help=f"Gemini model to use (default {DEFAULT_GEMINI_MODEL})")
    parser.add_argument("--label", help="The --label the translation was made with, if any; also tags the output")
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)


def youtube_main() -> None:
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
        help="Text added word for word to the end of every description, e.g. links (default: data/youtube_footer.txt, "
        "if there). See examples/youtube_footer.txt",
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
