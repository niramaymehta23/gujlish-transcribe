"""Web app settings. Local use needs none; a hosted deployment sets VAANI_HOSTED=1 and the values below."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

MODELS = ("flex", "gujlish")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


def _gpu_job() -> str:
    """The Cloud Run GPU job to transcribe on, only when this site itself runs on Cloud Run (which sets K_SERVICE).
    Anywhere else, e.g. `python app.py` on a Mac, transcription always runs on this computer: a local test never
    starts a paid Google GPU, whatever the environment says."""
    job = os.environ.get("VAANI_GPU_JOB", "").strip()
    if job and not os.environ.get("K_SERVICE"):
        print("VAANI_GPU_JOB ignored: not running on Cloud Run, so transcription runs on this computer.", flush=True)
        return ""
    return job


@dataclass(frozen=True)
class Settings:
    hosted: bool = False
    # Shared password for the whole site; empty means anyone with the link can use it.
    password: str = ""
    # Signs the session cookie that remembers each visitor. Must stay the same across restarts when hosted.
    secret_key: str = ""
    max_upload_mb: float = 4096
    max_minutes: float = 0  # 0 = no length limit
    retention_hours: float = 0  # 0 = keep runs until you delete them
    # Gemini runs on the owner's API key, so a hosted site offers it only when explicitly allowed.
    allow_gemini: bool = True
    models: tuple[str, ...] = field(default=MODELS)
    # Hosted: transcription runs on this Cloud Run GPU job (web/cloudjobs.py), so the website needs no GPU.
    gpu_job: str = ""
    project: str = ""
    region: str = ""
    # Stop starting transcriptions once the GPU has run this many hours this month (UTC). 0 = no limit.
    gpu_hours_per_month: float = 5
    # For the page's cost estimate: what an hour of the GPU job costs (L4 + 4 vCPU + 16 GiB, Singapore, Nov 2026).
    gpu_pounds_per_hour: float = 0.77

    @classmethod
    def from_env(cls) -> Settings:
        hosted = _flag("VAANI_HOSTED")
        models = tuple(m.strip() for m in os.environ.get("VAANI_MODELS", "").split(",") if m.strip()) or MODELS
        unknown = set(models) - set(MODELS)
        if unknown:
            raise ValueError(f"VAANI_MODELS lists unknown models: {', '.join(sorted(unknown))}")
        settings = cls(
            hosted=hosted,
            password=os.environ.get("VAANI_ACCESS_PASSWORD", ""),
            secret_key=os.environ.get("VAANI_SECRET_KEY", ""),
            max_upload_mb=_number("VAANI_MAX_UPLOAD_MB", 300 if hosted else 4096),
            max_minutes=_number("VAANI_MAX_MINUTES", 90 if hosted else 0),
            retention_hours=_number("VAANI_RETENTION_HOURS", 24 if hosted else 0),
            allow_gemini=_flag("VAANI_ALLOW_GEMINI") if hosted else True,
            models=models,
            gpu_job=_gpu_job(),
            project=os.environ.get("VAANI_PROJECT", "").strip(),
            region=os.environ.get("VAANI_REGION", "").strip(),
            gpu_hours_per_month=_number("VAANI_GPU_HOURS_PER_MONTH", 5),
            gpu_pounds_per_hour=_number("VAANI_GPU_POUNDS_PER_HOUR", 0.77),
        )
        if settings.gpu_job and not (settings.project and settings.region):
            raise ValueError("VAANI_GPU_JOB needs VAANI_PROJECT and VAANI_REGION too")
        if hosted and len(settings.secret_key) < 32:
            raise ValueError("VAANI_HOSTED needs VAANI_SECRET_KEY set to a random value of at least 32 characters")
        return settings
