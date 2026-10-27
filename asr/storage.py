"""Automatic, recording-based run paths shared by the CLI and web app."""

from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RECORDINGS = ROOT / "recordings"
TRANSCRIPTS = ROOT / "transcripts"
PLOTS = ROOT / "plots"


def new_run(source: Path, model: str, mode: str) -> Path:
    label = model + (f"-{mode}" if model == "flex" else "")
    folder = TRANSCRIPTS / source.stem / label
    folder.mkdir(parents=True, exist_ok=True)
    run = folder / datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    run.mkdir()
    return run


def stage_paths(transcript_json: Path, label: str | None = None) -> dict[str, Path]:
    """Output paths for the translation and summary stages, next to a run's `<name>.<model>.json` transcript."""
    stem, folder = transcript_json.stem, transcript_json.parent
    tag = f"-{label}" if label else ""
    return {
        "translation_txt": folder / f"{stem}.en{tag}.txt",
        "translation_json": folder / f"{stem}.en{tag}.json",
        "bilingual": folder / f"{stem}.bilingual{tag}.md",
        "translation_partial": folder / f".{stem}.en{tag}.partial.jsonl",
        "summary_md": folder / f"{stem}.summary{tag}.md",
        "summary_json": folder / f"{stem}.summary{tag}.json",
    }


def run_paths(source: Path, model: str, run_dir: Path):
    stem = f"{source.stem}.{model}"
    try:
        relative = run_dir.relative_to(TRANSCRIPTS)
    except ValueError:
        relative = Path(source.stem) / run_dir.name
    return (
        run_dir / f"{stem}.txt",
        run_dir / f"{stem}.json",
        PLOTS / relative / f"{stem}.confidence.png",
        run_dir / f".{stem}.partial.json",
    )
