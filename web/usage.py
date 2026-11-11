"""GPU usage log for the hosted site: one small file per GPU job run, in the storage bucket under usage/<YYYY-MM>/.

The job writes its file while it runs (so a run that is killed still counts), and the website rewrites it with Cloud
Run's own start and end times once the run is over, since those are what Google bills. The site adds the month up to
show what the GPU has cost and to stop starting transcriptions once the monthly limit is reached.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import transcriber.files.storage as storage
from transcriber.files.textutil import write_json


def usage_dir() -> Path:
    return storage.DATA / "usage"


def month_of(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m")


def record(execution: str, started: float, ended: float, *, step: str = "transcribe", run: str = "") -> Path:
    """Write (or overwrite) the usage file for one job execution. `execution` may be a full resource name."""
    name = execution.rsplit("/", 1)[-1]
    path = usage_dir() / month_of(started) / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "execution": name,
        "step": step,
        "run": run,
        "started": round(started, 1),
        "ended": round(ended, 1),
        "seconds": round(max(0.0, ended - started), 1),
    }
    write_json(path, data)
    return path


def month_total(month: str | None = None) -> dict:
    """{"month", "seconds", "runs"} for the month (UTC), default this one."""
    month = month or month_of(datetime.now(timezone.utc).timestamp())
    seconds, runs = 0.0, 0
    for path in sorted((usage_dir() / month).glob("*.json")):
        try:
            seconds += float(json.loads(path.read_text(encoding="utf-8")).get("seconds", 0))
            runs += 1
        except (OSError, ValueError, TypeError, AttributeError):
            continue
    return {"month": month, "seconds": round(seconds), "runs": runs}


def summary(cap_hours: float, pounds_per_hour: float) -> dict:
    """What the page shows: this month's GPU hours, runs, estimated cost, and the limit."""
    total = month_total()
    hours = total["seconds"] / 3600
    return {
        **total,
        "hours": round(hours, 2),
        "cost": round(hours * pounds_per_hour, 2),
        "cap_hours": cap_hours,
        "left_hours": round(max(0.0, cap_hours - hours), 2) if cap_hours else None,
        "over_cap": bool(cap_hours) and hours >= cap_hours,
    }
