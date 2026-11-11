#!/usr/bin/env python3
"""Entry point of the hosted site's GPU job (Cloud Run job, same image as the website). See web/cloudjobs.py.

Runs the worker command the website passed in VAANI_JOB_COMMAND, printing its output to the job's logs, and keeps a
small progress file (VAANI_JOB_EVENTS) in the run's folder on the shared storage bucket up to date for the website.
It also keeps this run's GPU usage file current, so a run that is killed or times out is still counted. When Cloud
Run cancels the execution (Pause, Stop or Delete on the site), the worker is stopped and its progress kept.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from transcriber.files.textutil import write_json  # noqa: E402
from web import usage  # noqa: E402

EVENT_PREFIX = "ASR_EVENT "
KEEP_EVENTS = 50  # the progress file holds the latest events; the website reads it every couple of seconds
KEEP_LOGS = 12
WRITE_EVERY_SECONDS = 2.0
USAGE_EVERY_SECONDS = 30.0


def main() -> int:
    command = json.loads(os.environ["VAANI_JOB_COMMAND"])
    progress = Path(os.environ["VAANI_JOB_EVENTS"])
    execution = os.environ.get("CLOUD_RUN_EXECUTION", f"local-{int(time.time())}")
    run = progress.parent.name
    step = progress.stem.removeprefix(".job-")
    began = time.time()
    state = {"count": 0, "events": [], "logs": [], "done": False}
    last_write = last_usage = 0.0

    def save(force=False):
        nonlocal last_write, last_usage
        now = time.time()
        if force or now - last_write >= WRITE_EVERY_SECONDS:
            write_json(progress, state)
            last_write = now
        if force or now - last_usage >= USAGE_EVERY_SECONDS:
            usage.record(execution, began, now, step=step, run=run)
            last_usage = now

    worker = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, start_new_session=True
    )

    def stop(signum, _frame):
        try:
            os.killpg(worker.pid, signal.SIGTERM)  # the worker saves its finished chunks as it goes
        except OSError:
            pass

    signal.signal(signal.SIGTERM, stop)
    save(force=True)
    for line in worker.stdout:
        print(line, end="", flush=True)
        state["logs"] = [*state["logs"], line.strip()][-KEEP_LOGS:]
        if line.startswith(EVENT_PREFIX):
            try:
                event = json.loads(line[len(EVENT_PREFIX) :])
            except ValueError:
                continue
            state["events"] = [*state["events"], event][-KEEP_EVENTS:]
            state["count"] += 1
            save(force=event.get("stage") != "transcribing")  # write at once except for routine chunk progress
        else:
            save()
    code = worker.wait()
    state.update(done=True, code=code)
    save(force=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
