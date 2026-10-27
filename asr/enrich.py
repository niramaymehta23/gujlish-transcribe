"""Command-line glue for the translation and summary stages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .llm import DEFAULT_LOCAL_URL, LLMError, claude_available, make_llm
from .summarize import run_summary
from .translate import STYLES, load_glossary, run_translation

STAGE_ERRORS = (ImportError, OSError, RuntimeError, ValueError, LLMError, json.JSONDecodeError)


def add_llm_arguments(parser: argparse.ArgumentParser, *, translation: bool = True, summary: bool = True) -> None:
    group = parser.add_argument_group("Translation and summary")
    group.add_argument(
        "--llm",
        choices=["local", "claude", "gemini"],
        default="local",
        help="Where translation runs. local: an OpenAI-compatible server on this computer (Ollama, LM Studio, "
        "mlx_lm.server). claude: the Claude API (sends transcript text, not audio, to Anthropic; needs "
        "ANTHROPIC_API_KEY). gemini: the Gemini API (sends text to Google; needs GEMINI_API_KEY). Default: local",
    )
    group.add_argument("--llm-model", help="Model id for translation (a local model name, or a Claude/Gemini model id)")
    group.add_argument("--llm-url", help=f"Local server base URL (default {DEFAULT_LOCAL_URL}; env GUJLISH_LLM_URL)")
    if summary:
        group.add_argument(
            "--summary-llm",
            choices=["local", "claude", "gemini"],
            help="Where the summary runs. Default: claude when ANTHROPIC_API_KEY is set, otherwise the --llm value",
        )
        group.add_argument("--summary-model", help="Model id for the summary")
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
    provider = args.summary_llm or ("claude" if claude_available() else args.llm)
    model = args.summary_model or (args.llm_model if provider == args.llm else None)
    return make_llm(provider, model, args.llm_url)


def effective_label(args) -> str | None:
    return args.label or ("sample" if getattr(args, "limit", None) else None)


def run_stages(transcript_json: Path, args, *, translate: bool, summarize: bool, progress=None) -> list[Path]:
    outputs: list[Path] = []
    label = effective_label(args)
    translated_by = None
    if translate:
        llm = make_llm(args.llm, args.llm_model, args.llm_url)
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
        outputs += [paths["translation_txt"], paths["bilingual"], paths["translation_json"]]
        if progress:
            progress(
                {"stage": "complete", "txt": str(paths["translation_txt"]), "json": str(paths["translation_json"])}
            )
    if summarize:
        llm = summary_llm_for(args)
        print(f"Summarising with {llm.describe()}", flush=True)
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
        elif event["stage"] in {"translating", "summarizing"}:
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
    parser.set_defaults(summary_llm=None, summary_model=None)
    _run(parser, translate=True, summarize=False)


def summarize_main() -> None:
    parser = argparse.ArgumentParser(
        description="Summarise a translated recording: overview, main points by theme, stories, quotes"
    )
    parser.add_argument(
        "transcript", type=Path, help="The <name>.<model>.json file of a transcription run, or its folder"
    )
    add_llm_arguments(parser, translation=False)
    parser.add_argument("--reset", action="store_true", help="Discard interrupted summary progress and start again")
    parser.add_argument("--progress-json", action="store_true", help=argparse.SUPPRESS)
    parser.set_defaults(tone="spoken", style_note="", glossary=None, limit=None)
    _run(parser, translate=False, summarize=True)
