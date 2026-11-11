"""Transcription on a Cloud Run GPU job, so the hosted website itself never needs a GPU.

The website runs on a small CPU-only Cloud Run service. For each recording it starts one execution of a GPU job
(`scripts/run_job.py` in the same image), which runs the same worker command the website would run locally and then
exits. The GPU is billed only while that execution runs: no open tab, visitor, link preview or bot can switch it on.

Both sides mount the same storage bucket at the same path. The job writes its progress to a small file in the run's
folder (`.job-<step>.json`); the website reads it every few seconds and passes the events on, exactly as it does with
a local worker's output. The execution's name is kept in the run's state file, so a website that restarted (Cloud
Run scales it to zero when nobody is using it) picks the running job up again instead of starting a second one.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://run.googleapis.com/v2"
TOKEN_URL = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token"
READ_EVERY_SECONDS = 2.0  # how often the progress file is read
STATUS_EVERY_SECONDS = 15.0  # how often Cloud Run is asked whether the execution is still running
FINISH_GRACE_SECONDS = 20.0  # after the execution ends, how long to wait for its last progress write to show up
STOP_CHECK_SECONDS = 15.0  # after a cancel, how long to wait before checking that the execution really ended
STOP_DEADLINE_SECONDS = 300.0  # a paused or stopped run's GPU job is cancelled again until it ends, for 5 minutes


def events_file(run_dir: Path, step: str) -> Path:
    return Path(run_dir) / f".job-{step}.json"


class CloudRunJobs:
    """Starts, follows and cancels executions of one Cloud Run job through the Cloud Run Admin API, as the service
    account the website runs as (it needs permission to run and cancel the job)."""

    def __init__(self, project: str, region: str, job: str, opener=None, clock=time):
        self.parent = f"projects/{project}/locations/{region}/jobs/{job}"
        self.open = opener or urllib.request.urlopen
        self.clock = clock
        self._token = ("", 0.0)

    def _auth(self) -> str:
        token, expires = self._token
        if not token or self.clock.time() > expires - 60:
            request = urllib.request.Request(TOKEN_URL, headers={"Metadata-Flavor": "Google"})
            with self.open(request, timeout=10) as response:
                data = json.loads(response.read())
            token, expires = data["access_token"], self.clock.time() + float(data.get("expires_in", 300))
            self._token = (token, expires)
        return token

    def _call(self, method: str, url: str, body: dict | None = None) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            method=method,
            headers={"Authorization": f"Bearer {self._auth()}", "Content-Type": "application/json"},
        )
        with self.open(request, timeout=30) as response:
            return json.loads(response.read() or b"{}")

    def start(self, env: dict[str, str], timeout_seconds: int) -> str:
        """Start one execution with these environment variables; return the execution's resource name."""
        body = {
            "overrides": {
                "containerOverrides": [{"env": [{"name": k, "value": v} for k, v in env.items()]}],
                "taskCount": 1,
                "timeout": f"{int(timeout_seconds)}s",
            }
        }
        operation = self._call("POST", f"{API}/{self.parent}:run", body)
        name = (operation.get("metadata") or {}).get("name")
        if not name:
            raise RuntimeError("Cloud Run did not say which execution it started.")
        return name

    def status(self, execution: str) -> dict:
        """{"done": bool, "ok": bool, "started": epoch or None, "ended": epoch or None, "reason": str}."""
        data = self._call("GET", f"{API}/{execution}")
        ended = _epoch(data.get("completionTime"))
        failed = [c.get("message", "") for c in data.get("conditions", []) if c.get("state") == "CONDITION_FAILED"]
        return {
            "done": ended is not None,
            "ok": bool(data.get("succeededCount")) and not data.get("failedCount") and not data.get("cancelledCount"),
            "started": _epoch(data.get("startTime")),
            "ended": ended,
            "reason": next((m for m in failed if m), ""),
        }

    def cancel(self, execution: str) -> None:
        try:
            self._call("POST", f"{API}/{execution}:cancel", {})
        except urllib.error.HTTPError as error:
            if error.code not in {400, 404, 409}:  # already finished or gone
                raise


def _epoch(stamp: str | None) -> float | None:
    """Seconds since 1970 from a Cloud Run time such as "2026-11-11T12:00:00.123456789Z". Its fraction can have nine
    digits, which datetime.fromisoformat rejects before Python 3.11, so it is cut to six."""
    if not stamp:
        return None
    main, _, fraction = stamp.rstrip("Z").partition(".")
    fraction = (fraction + "000000")[:6]
    return datetime.fromisoformat(f"{main}.{fraction}+00:00").timestamp()


class RemoteRun:
    """A running execution, standing in for a local worker process so the server can stop it the same way."""

    def __init__(self, jobs: CloudRunJobs, execution: str):
        self.jobs = jobs
        self.execution = execution
        self.stopped = False

    def stop(self, sleep=time.sleep) -> None:
        """Cancel the execution, and keep cancelling until Cloud Run says it has ended, for up to
        STOP_DEADLINE_SECONDS: a paused or stopped run must never leave a GPU running."""
        self.stopped = True
        deadline = self.jobs.clock.time() + STOP_DEADLINE_SECONDS
        while True:
            try:
                self.jobs.cancel(self.execution)
            except (OSError, ValueError):
                pass  # Cloud Run briefly unreachable: try again
            sleep(STOP_CHECK_SECONDS)
            try:
                if self.jobs.status(self.execution)["done"]:
                    return
            except (OSError, ValueError, KeyError):
                pass
            if self.jobs.clock.time() >= deadline:
                return

    def wait(self, timeout: float | None = None, sleep=time.sleep) -> None:
        """Wait (up to `timeout` seconds) for the execution to end, e.g. before its run folder is deleted."""
        deadline = self.jobs.clock.time() + (timeout or 0)
        while True:
            try:
                if self.jobs.status(self.execution)["done"]:
                    return
            except (OSError, ValueError, KeyError):
                pass
            if self.jobs.clock.time() >= deadline:
                return
            sleep(READ_EVERY_SECONDS)


def run_remote(
    jobs: CloudRunJobs,
    command: list[str],
    on_event,
    on_start=None,
    *,
    run_dir: Path,
    step: str,
    timeout_seconds: int,
    saved: str | None = None,
    on_execution=None,
    on_finish=None,
    sleep=time.sleep,
) -> tuple[int, list[str]]:
    """Run `command` on the job and follow it like a local worker: events go to `on_event`, and the result is the exit
    code and last log lines. `saved` is an execution started before a restart, which is followed again if it is
    still running. `on_execution(name or None)` records the execution in the run's state; `on_finish(status)` gets
    the final execution status (for the GPU usage log)."""
    progress = events_file(run_dir, step)
    execution = None
    if saved:
        try:
            if not jobs.status(saved)["done"]:
                execution = saved  # still running: follow it instead of starting another one
        except (OSError, ValueError, KeyError):
            pass
    if execution is None:
        progress.unlink(missing_ok=True)
        env = {"VAANI_JOB_COMMAND": json.dumps(command), "VAANI_JOB_EVENTS": str(progress)}
        execution = jobs.start(env, timeout_seconds)
    if on_execution:
        on_execution(execution)
    run = RemoteRun(jobs, execution)
    if on_start:
        on_start(run)

    seen, last_status, ended_at, status = 0, 0.0, None, None
    data: dict = {}
    while True:
        sleep(READ_EVERY_SECONDS)
        data = _read(progress) or data
        count = int(data.get("count", 0))
        if count > seen:
            fresh = data.get("events", [])[-(count - seen) :]
            for event in fresh:
                on_event(event)
            seen = count
        if data.get("done"):
            break
        now = jobs.clock.time()
        if ended_at is None and now - last_status >= STATUS_EVERY_SECONDS:
            last_status = now
            try:
                status = jobs.status(execution)
            except (OSError, ValueError, KeyError):
                status = None  # Cloud Run briefly unreachable: keep reading the file
            if status and status["done"]:
                ended_at = now
        if ended_at is not None and now - ended_at >= FINISH_GRACE_SECONDS:
            break  # the job ended without writing its result (killed, timed out, never started)

    try:
        status = jobs.status(execution)
    except (OSError, ValueError, KeyError):
        pass
    if on_finish and status:
        on_finish(execution, status)
    if on_execution:
        on_execution(None)
    if data.get("done"):
        return int(data.get("code", 1)), list(data.get("logs", []))
    if run.stopped:
        return 1, ["Stopped."]
    reason = (status or {}).get("reason") or "The transcription job stopped before it finished."
    return 1, [f"Error: {reason}"]


def _read(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
