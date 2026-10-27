"""Web app settings. Local use needs none; a hosted deployment sets GUJLISH_HOSTED=1 and the values below."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

MODELS = ("flex", "gujlish")


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _number(name: str, default: float) -> float:
    value = os.environ.get(name, "").strip()
    return float(value) if value else default


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

    @classmethod
    def from_env(cls) -> Settings:
        hosted = _flag("GUJLISH_HOSTED")
        models = tuple(m.strip() for m in os.environ.get("GUJLISH_MODELS", "").split(",") if m.strip()) or MODELS
        unknown = set(models) - set(MODELS)
        if unknown:
            raise ValueError(f"GUJLISH_MODELS lists unknown models: {', '.join(sorted(unknown))}")
        settings = cls(
            hosted=hosted,
            password=os.environ.get("GUJLISH_ACCESS_PASSWORD", ""),
            secret_key=os.environ.get("GUJLISH_SECRET_KEY", ""),
            max_upload_mb=_number("GUJLISH_MAX_UPLOAD_MB", 300 if hosted else 4096),
            max_minutes=_number("GUJLISH_MAX_MINUTES", 90 if hosted else 0),
            retention_hours=_number("GUJLISH_RETENTION_HOURS", 24 if hosted else 0),
            allow_gemini=_flag("GUJLISH_ALLOW_GEMINI") if hosted else True,
            models=models,
        )
        if hosted and len(settings.secret_key) < 32:
            raise ValueError("GUJLISH_HOSTED needs GUJLISH_SECRET_KEY set to a random value of at least 32 characters")
        return settings
