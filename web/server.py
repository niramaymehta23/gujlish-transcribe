"""The upload server. Transcription runs on a GPU queue, one recording at a time; Gujlish, English and summaries run
on a separate queue, so a translation never waits for a transcription or the reverse.

Each run keeps a small state file in its folder (RUN_FILE). A restarted server still lists every run, opens it by the
same id, and picks up work a restart cut short. Hosted on Cloud Run, the machine can be replaced at any time."""

import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Lock, Thread
from urllib.parse import urlparse
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for

import transcriber.files.storage as storage
from transcriber.english.captions import captions_current, captions_srt
from transcriber.english.llm import gemini_available
from transcriber.files.storage import ROOT, new_run, run_paths, stage_paths
from transcriber.files.subtitles import to_srt
from transcriber.files.textutil import fmt_time, write_json
from transcriber.speech import align
from transcriber.speech.dictionary import (
    clip_audio,
    clips_dir,
    dictionary_path,
    forget,
    mode,
    reject,
    remember,
    save_clip,
    set_mode,
)
from transcriber.speech.dictionary import load as load_dictionary
from transcriber.speech.dictionary import save as save_dictionary
from transcriber.speech.edits import load_gujlish, matches, replace_words, save_transcript
from transcriber.speech.gujlish import KnownWordsOnly, Transliterator, XlitEngine, write_gujlish, xlit_installed
from transcriber.speech.postprocess import drop_cut_stop
from web.documents import markdown_docx, transcript_docx
from web.recordings import FINGERPRINT, fingerprint, fingerprints
from web.recordings import find as find_recording
from web.settings import Settings

LOG_TAIL_LINES = 12  # recent subprocess output kept for error messages
UPLOAD_CHUNK_BYTES = 8 * 1024**2  # Cloud Run caps a request body at 32 MiB, so large files arrive in pieces
HEX_ID = re.compile(r"[0-9a-f]{32}")
ACTIVE = {"queued", "loading", "transcribing"}
SWEEP_EVERY_SECONDS = 600
OWNER_FILE = ".owner"
RUN_FILE = ".run.json"  # the recording, its settings, and the steps still to finish
MAX_RESUMES = 3  # restarts in a row after which cut-short work waits for you instead of resuming itself
STOP_GRACE_SECONDS = 10  # a stopped worker is killed if it is still running after this
WORD_PAD = 0.4  # seconds of audio played around words whose times are known (a little of the next word helps)
ROUGH_PAD = 1.0  # and around words whose times are estimated
LEFTOVER_SECONDS = 3600  # unused recordings and empty run folders younger than this may belong to an upload under way
EVENT_PREFIX = "ASR_EVENT "
STOPPED = "Stopped. Start it again to continue where it left off."


@dataclass(frozen=True)
class Stage:
    """One stage after transcription. Each goes by several names: job key ("translation"), URL (/translate), worker
    script, output file, and progress event ("translating")."""

    script: str
    output: str  # key in transcriber.files.storage.stage_paths
    verb: str


STAGES = {
    "gujlish": Stage("scripts/transliterate.py", "gujlish_json", "Writing Gujlish"),
    "translation": Stage("scripts/translate.py", "translation_txt", "Translating"),
    "captions": Stage("scripts/captions.py", "captions_json", "Timing the English captions"),
    "summary": Stage("scripts/summarize.py", "summary_md", "Writing the summary & YouTube details"),
}
PROGRESS_EVENTS = {"transliterating", "translating", "aligning", "captioning", "summarizing"}


def stage_output(transcript_json: str, key: str) -> Path:
    return stage_paths(Path(transcript_json))[STAGES[key].output]


def run_worker(command: list[str], on_event, on_start=None) -> tuple[int, list[str]]:
    """Run a worker script, passing each progress event to `on_event`; return its exit code and last log lines.
    `on_start` gets the process as soon as it exists, so it can be stopped."""
    logs: list[str] = []
    with subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        start_new_session=True,  # its own process group, so stopping it also stops the ffmpeg it starts
    ) as worker:
        if on_start:
            on_start(worker)
        for line in worker.stdout:
            logs = [*logs, line.strip()][-LOG_TAIL_LINES:]
            if line.startswith(EVENT_PREFIX):
                on_event(json.loads(line[len(EVENT_PREFIX) :]))
        return worker.wait(), logs


def stop_worker(worker) -> None:
    """Stop a worker and everything it started; kill them if they are still running after a grace period."""

    def send(sig):
        try:
            os.killpg(worker.pid, sig)
        except OSError:
            pass  # already finished

    send(signal.SIGTERM)

    def force():
        try:
            worker.wait(timeout=STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            send(signal.SIGKILL)

    Thread(target=force, daemon=True).start()


def last_error(logs: list[str], fallback: str) -> str:
    """The worker's final "Error: ..." line, or `fallback` when it printed none."""
    return next((line.removeprefix("Error: ") for line in reversed(logs) if line.startswith("Error:")), fallback)


def probe_audio(path: Path) -> float | None:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise ValueError("This file could not be decoded. Choose a supported audio or video file.")
    data = json.loads(result.stdout)
    if not data.get("streams"):
        raise ValueError("This file does not contain an audio track.")
    try:
        duration = float(data.get("format", {}).get("duration", 0))
        return duration if duration > 0 else None
    except (TypeError, ValueError):
        return None


def clean_filename(name: str) -> str:
    """Preserve Gujarati/English filenames, stripping paths and unsafe characters."""
    raw_name = unicodedata.normalize("NFC", Path(name.replace("\\", "/")).name)
    return "".join(
        char if char in " .()-_" or unicodedata.category(char)[0] in "LNM" else "_" for char in raw_name
    ).strip(" .")[:180]


def run_id(run_dir: Path) -> str:
    """A run's id comes from its folder, so it is the same after a restart and a page opened before one still works."""
    return hashlib.sha256("/".join(Path(run_dir).parts[-3:]).encode()).hexdigest()[:32]


def read_meta(run_dir: Path) -> dict:
    try:
        return json.loads((Path(run_dir) / RUN_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def started(run_dir: Path) -> str | None:
    """When the run began, with the server's time zone, so the browser can show it in yours."""
    try:
        return datetime.strptime(Path(run_dir).name, "%Y%m%d-%H%M%S-%f").astimezone().isoformat(timespec="seconds")
    except ValueError:
        return None


def find_transcript(run_dir: Path, meta: dict) -> Path | None:
    """The run's transcript JSON once transcription has finished, else None."""
    if meta.get("source") and meta.get("model"):
        txt, data = run_paths(Path(meta["source"]), meta["model"], run_dir)[:2]
        return data if data.exists() and txt.exists() else None
    # Runs from before RUN_FILE: the transcript has the shortest name, since stage files add to it.
    candidates = [p for p in run_dir.glob("*.json") if not p.name.startswith(".") and p.with_suffix(".txt").exists()]
    for path in sorted(candidates, key=lambda p: len(p.name)):
        try:
            if not json.loads(path.read_text(encoding="utf-8")).get("kind"):
                return path
        except (OSError, ValueError):
            continue
    return None


def stopped_at(job: dict) -> str:
    done, total = job.get("processed") or 0, job.get("duration")
    return f"{fmt_time(done)} of {fmt_time(total)}" if total else fmt_time(done)


def create_app(*, executor=None, settings: Settings | None = None) -> Flask:
    settings = settings or Settings.from_env()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = int(settings.max_upload_mb * 1024**2) + 1024**2
    app.config.update(
        SECRET_KEY=settings.secret_key or os.urandom(32).hex(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=settings.hosted,
        PERMANENT_SESSION_LIFETIME=timedelta(days=30),
    )
    jobs = {}
    lock = Lock()
    meta_lock = Lock()  # RUN_FILE is read, changed and written back from several threads
    edit_lock = Lock()  # one word fix at a time: each rewrites the transcript files and may save the dictionary
    align_lock = Lock()  # one line aligned at a time, on the CPU, with one copy of the model
    aligner: dict = {}  # "model": the Aligner (None when it can't load), loaded the first time a word is played
    aligned: dict = {}  # word times of lines aligned here, by transcript and line
    xlit = []  # the IndicXlit engine for editor suggestions, loaded on first use
    # Transcription needs the GPU, one recording at a time. Gujlish and the Gemini stages need neither.
    gpu = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr-gpu")
    text = executor or ThreadPoolExecutor(max_workers=2, thread_name_prefix="asr-text")
    workers = {}  # (job id, step) -> its running process; step is "transcribe" or a stage key
    tasks = {}  # (job id, step) -> the queued task allowed to run it; stopping or queueing again changes it
    stopping = {}  # (job id, step) -> why its process is being stopped: "pause", "stop" or "delete"
    deleted = set()  # run folders being removed in the background
    uploads_dir = Path(tempfile.gettempdir()) / "gujlish-uploads"  # local disk, not the mounted bucket
    last_sweep = [0.0]
    sequence = [0]
    recovered = [False]
    app.extensions["asr_jobs"] = jobs
    app.extensions["asr_settings"] = settings

    def owner_tag() -> str:
        """A per-browser id kept in the signed session cookie; hosted runs are visible only to their owner."""
        if "visitor" not in session:
            session.permanent = True
            session["visitor"] = uuid4().hex
        return hashlib.sha256(session["visitor"].encode()).hexdigest()[:32]

    def owns(job: dict) -> bool:
        return not settings.hosted or job.get("owner") == owner_tag()

    def run_dirs() -> list[Path]:
        """transcripts/<recording>/<model>/<run>/ folders. Listed level by level, so one deleted in the meantime is
        skipped (Python 3.10's glob fails on it instead)."""

        def folders(parent):
            try:
                return [path for path in parent.iterdir() if path.is_dir()]
            except OSError:
                return []

        runs = [run for name in folders(storage.TRANSCRIPTS) for label in folders(name) for run in folders(label)]
        return [run for run in runs if str(run) not in deleted]

    def load_run(run_dir: Path) -> dict | None:
        """A job record rebuilt from a run folder: finished, paused, or cut short by a restart."""
        meta = read_meta(run_dir)
        owner = run_dir / OWNER_FILE
        record = {
            "id": run_id(run_dir),
            "run_dir": str(run_dir),
            "owner": owner.read_text(encoding="utf-8") if settings.hosted and owner.exists() else None,
            "started": started(run_dir),
        }
        try:
            transcript = find_transcript(run_dir, meta)
            if transcript:
                data = json.loads(transcript.read_text(encoding="utf-8"))
                record.update(name=data["source"], model=data["model"], mode=data.get("mode"))
                record.update(duration=data["duration"], status="complete", progress=100, message="Transcript ready")
                record.update(txt=str(transcript.with_suffix(".txt")), json=str(transcript))
            elif meta.get("source"):
                record.update(name=meta["source"], model=meta["model"], mode=meta.get("mode"))
                checkpoint = run_paths(Path(meta["source"]), meta["model"], run_dir)[3]
                done = json.loads(checkpoint.read_text(encoding="utf-8"))["segments"] if checkpoint.exists() else []
                record.update(duration=meta.get("duration"), processed=done[-1]["end"] if done else 0)
                if record["duration"]:
                    record["progress"] = min(99, round(record["processed"] / record["duration"] * 100))
                if meta.get("paused"):
                    record.update(
                        status="paused", message=f"Paused at {stopped_at(record)}. Resume continues from there."
                    )
                else:
                    reason = meta.get("failed") or f"Stopped at {stopped_at(record)} when the server restarted."
                    record.update(status="error", message=reason)
            else:
                return None
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None
        # "name" is what you called the file; "recording" points at the stored copy when duplicates were merged.
        recording = storage.RECORDINGS / (meta.get("recording") or record["name"])
        record.update(name=meta.get("name") or record["name"], source=str(recording), sha256=meta.get("sha256"))
        record["size"] = meta.get("size") or (recording.stat().st_size if recording.is_file() else None)
        return record

    def restore(job_id: str) -> dict | None:
        """Load a run this server has not seen yet, e.g. one a page opened before a restart. The caller holds the lock."""
        for run_dir in run_dirs():
            if run_id(run_dir) == job_id:
                record = load_run(run_dir)
                if record:
                    jobs[job_id] = record
                return record
        return None

    def find_job(job_id: str) -> dict:
        """The job dict (the caller holds the lock), or 404 for an unknown run or someone else's."""
        job = jobs.get(job_id) or (restore(job_id) if HEX_ID.fullmatch(job_id) else None)
        if not job or not owns(job):
            abort(404)
        return job

    def all_runs() -> list[dict]:
        """Every run on disk, newest first, loading the ones this server has not seen yet."""
        found = []
        for run_dir in sorted(run_dirs(), key=lambda path: path.name, reverse=True):
            job_id = run_id(run_dir)
            with lock:
                job = jobs.get(job_id)
            if job is None:
                record = load_run(run_dir)
                if not record:
                    continue
                with lock:
                    job = jobs.setdefault(job_id, record)
            found.append(job)
        return found

    def update_meta(run_dir, change) -> None:
        """Read the run's RUN_FILE, let `change` edit it, write it back. Skipped once the run is being deleted."""
        with meta_lock:
            if str(run_dir) in deleted or not Path(run_dir).is_dir():
                return
            meta = read_meta(run_dir)
            change(meta)
            write_json(Path(run_dir) / RUN_FILE, meta)

    def drop_meta(run_dir, *keys):
        update_meta(run_dir, lambda meta: [meta.pop(key, None) for key in keys])

    def finish_pending(run_dir, keys, progressed=False):
        def change(meta):
            meta["pending"] = [key for key in meta.get("pending", []) if key not in keys]
            if not meta["pending"]:
                del meta["pending"]
            if progressed:
                meta.pop("resumes", None)

        update_meta(run_dir, change)

    def sweep():
        """Delete runs and recordings older than the retention period (hosted sites)."""
        if not settings.retention_hours or time.time() - last_sweep[0] < SWEEP_EVERY_SECONDS:
            return
        last_sweep[0] = time.time()
        cutoff = datetime.now() - timedelta(hours=settings.retention_hours)
        with lock:
            busy = {job.get("run_dir") for job in jobs.values() if job.get("status") in ACTIVE}
            busy |= {job.get("source") for job in jobs.values() if job.get("status") in ACTIVE}
        for run_dir in storage.TRANSCRIPTS.glob("*/*/*/"):
            try:
                started_at = datetime.strptime(run_dir.name, "%Y%m%d-%H%M%S-%f")
            except ValueError:
                continue
            if started_at < cutoff and str(run_dir) not in busy:
                shutil.rmtree(run_dir, ignore_errors=True)
                with lock:
                    for job_id in [k for k, job in jobs.items() if job.get("run_dir") == str(run_dir)]:
                        del jobs[job_id]
        for folder in [*storage.TRANSCRIPTS.glob("*/*/"), *storage.TRANSCRIPTS.glob("*/")]:
            try:
                folder.rmdir()  # only succeeds once empty
            except OSError:
                pass
        for path in [*storage.RECORDINGS.glob("*"), *uploads_dir.glob("*")]:
            try:
                old = datetime.fromtimestamp(path.stat().st_mtime) < cutoff
            except OSError:
                continue
            if old and path.is_file() and str(path) not in busy and not path.name.startswith("."):
                path.unlink(missing_ok=True)

    def started_worker(job_id, step, worker):
        """Keep a running process so it can be stopped, and stop it at once if that was asked before it started."""
        with lock:
            workers[(job_id, step)] = worker
            asked = (job_id, step) in stopping
        if asked:
            stop_worker(worker)

    def finished_worker(job_id, step, token):
        """Forget the step's process and task; return why it was stopped, if it was."""
        with lock:
            workers.pop((job_id, step), None)
            if tasks.get((job_id, step)) == token:
                del tasks[(job_id, step)]
            return stopping.pop((job_id, step), None)

    def queue_transcription(job_id, message):
        token = uuid4().hex
        with lock:
            sequence[0] += 1
            tasks[(job_id, "transcribe")] = token
            jobs[job_id].update(status="queued", message=message, seq=sequence[0])
        gpu.submit(process, job_id, token)

    def process(job_id, token):
        with lock:
            job = jobs.get(job_id)
            if not job or tasks.get((job_id, "transcribe")) != token:
                return  # deleted, paused or queued again since
            job.update(status="loading", message="Loading the model. The first run may download model weights.")
            run_dir = Path(job["run_dir"])
            command = [sys.executable, str(ROOT / "scripts" / "transcribe.py"), job["source"], job["model"]]
            command += ["--mode", job.get("mode") or "mixed", "--output-dir", str(run_dir)]
            command += ["--dictionary", str(dictionary_path()), "--progress-json"]

        def change(**fields):
            with lock:
                job.update(fields)

        def on_event(event):
            if event["stage"] == "transcribing":
                processed, total = event["processed"], job.get("duration")
                percent = min(99, round(processed / total * 100)) if total else None
                change(
                    status="transcribing",
                    processed=processed,
                    progress=percent,
                    message=f"Transcribing · {event['chunks']} chunks completed",
                )
            elif event["stage"] == "complete":
                change(txt=event["txt"], json=event["json"])
            elif event["stage"] == "loading":
                change(device=event["device"])

        code, logs = run_worker(command, on_event, lambda worker: started_worker(job_id, "transcribe", worker))
        reason = finished_worker(job_id, "transcribe", token)
        finished = not code and job.get("txt")
        if reason == "delete":
            shutil.rmtree(run_dir, ignore_errors=True)  # in case it wrote after the folder was removed
            return
        if reason == "pause" and not finished:
            change(status="paused", message=f"Paused at {stopped_at(job)}. Resume continues from there.")
            update_meta(run_dir, lambda meta: meta.update(paused=True))
            return
        if not finished:
            message = last_error(logs, "Transcription stopped. You can retry to resume completed chunks.")
            change(status="error", message=message)
            update_meta(run_dir, lambda meta: meta.update(failed=message))
            return
        drop_meta(run_dir, "resumes", "failed", "paused")
        # Gujlish follows straight away; English and the summary wait until you have fixed the words.
        queue_stages(job_id, ("gujlish",))
        change(status="complete", progress=100, message="Transcript ready")

    def stage_command(transcript_json, key, engine="auto", recording=None):
        command = [sys.executable, str(ROOT / STAGES[key].script), transcript_json, "--progress-json"]
        if key == "gujlish":
            command += ["--dictionary", str(dictionary_path()), "--engine", engine]
        if key == "translation":
            command.append("--no-captions")  # the captions run as their own step, with their own progress
        if key == "captions" and recording:
            command += ["--audio", recording]  # to time the captions to the speech
        return command

    def set_stage(job, key, **fields):
        with lock:
            job.setdefault(key, {}).update(fields)

    def queue_stages(job_id, keys, engine="auto"):
        token = uuid4().hex
        with lock:
            job = jobs[job_id]
            for key in keys:
                tasks[(job_id, key)] = token
                job[key] = {"status": "queued", "progress": None, "message": "Waiting to start…"}
            steps = [(key, stage_command(job["json"], key, engine, job.get("source"))) for key in keys]

        def change(meta):
            meta["pending"] = sorted({*meta.get("pending", []), *keys})
            meta["engine"] = engine

        update_meta(job["run_dir"], change)
        text.submit(run_stages, job_id, steps, token)

    def run_stage(job_id, key, command, token) -> bool:
        """Run a stage's worker script as a subprocess, mirroring its progress events into the job. True when done."""
        with lock:
            job = jobs.get(job_id)
            if not job or tasks.get((job_id, key)) != token:
                return False
        verb = STAGES[key].verb
        set_stage(job, key, status="running", progress=None, message=f"{verb}…")

        def on_event(event):
            if event["stage"] in PROGRESS_EVENTS and event["total"]:
                percent = min(99, round(event["done"] / event["total"] * 100))
                set_stage(job, key, progress=percent, message=f"{verb} · {event['done']} of {event['total']}")

        try:
            code, logs = run_worker(command, on_event, lambda worker: started_worker(job_id, key, worker))
        except OSError as error:
            code, logs = 1, [f"Error: {error}"]
        reason = finished_worker(job_id, key, token)
        if reason == "delete":
            shutil.rmtree(job["run_dir"], ignore_errors=True)
            return False
        if reason == "stop":
            set_stage(job, key, status="idle", progress=None, message=STOPPED)
        elif not code and stage_output(job["json"], key).exists():
            set_stage(job, key, status="complete", progress=100, message="Ready")
        else:
            set_stage(job, key, status="error", message=last_error(logs, f"{verb} stopped. Start it again to resume."))
        done = job[key]["status"] == "complete"
        finish_pending(job["run_dir"], {key}, progressed=done)
        return done

    def run_stages(job_id, steps, token):
        """Run stages in order (translation, then the summary), stopping at the first that does not finish."""
        for index, (key, command) in enumerate(steps):
            if run_stage(job_id, key, command, token):
                continue
            with lock:
                job = jobs.get(job_id)
                later = [key for key, _ in steps[index + 1 :] if tasks.get((job_id, key)) == token]
                for key in later:
                    del tasks[(job_id, key)]
            if job and later:
                for key in later:
                    set_stage(job, key, status="idle", progress=None, message="")
                finish_pending(job["run_dir"], set(later))
            return

    def merge_recordings():
        """Keep one stored copy of each recording. Runs that used a duplicate point at the kept copy instead. Run
        folders left by uploads whose transcription never started, and recordings no run uses, are removed once they
        are LEFTOVER_SECONDS old. Copies an unfinished run still reads from are left alone."""
        cutoff = time.time() - LEFTOVER_SECONDS
        for run_dir in run_dirs():
            began = started(run_dir)
            empty = not read_meta(run_dir) and not any(run_dir.glob(".*.partial.json"))
            if (
                empty
                and began
                and datetime.fromisoformat(began).timestamp() < cutoff
                and not find_transcript(run_dir, {})
            ):
                shutil.rmtree(run_dir, ignore_errors=True)
        users = {}
        for job in all_runs():
            users.setdefault(job.get("source"), []).append(job)
        copies = {}
        for name, value in fingerprints().items():
            copies.setdefault(value, []).append(storage.RECORDINGS / name)
        for paths in copies.values():
            if len(paths) < 2 or any(job["status"] != "complete" for p in paths for job in users.get(str(p), [])):
                continue
            keep = max(paths, key=lambda path: (len(users.get(str(path), [])), -path.stat().st_mtime))
            for path in paths:
                if path == keep:
                    continue
                for job in users.pop(str(path), []):
                    update_meta(job["run_dir"], lambda meta: meta.update(recording=keep.name))
                    with lock:
                        job["source"] = str(keep)
                    users.setdefault(str(keep), []).append(job)
                path.unlink(missing_ok=True)
        for name in fingerprints():
            path = storage.RECORDINGS / name
            if str(path) not in users and path.stat().st_mtime < cutoff:
                path.unlink(missing_ok=True)

    def recover():
        """Once per server start, resume the work a restart cut short. After MAX_RESUMES restarts in a row it waits for
        you instead, so a recording that brings the server down cannot keep doing so. Hosted sites also merge
        duplicate recordings."""
        with lock:
            if recovered[0]:
                return
            recovered[0] = True
        if settings.hosted:
            merge_recordings()
        for run_dir in run_dirs():
            meta = read_meta(run_dir)
            unfinished = meta.get("source") and not meta.get("paused") and not meta.get("failed")
            if not meta.get("pending") and not unfinished:
                continue
            with lock:
                job = jobs.get(run_id(run_dir)) or restore(run_id(run_dir))
            if not job:
                continue
            transcribe = job["status"] == "error"
            pending = [key for key in meta.get("pending", []) if key in STAGES] if job["status"] == "complete" else []
            if not transcribe and not pending:
                continue
            if meta.get("resumes", 0) >= MAX_RESUMES:
                update_meta(run_dir, lambda meta: meta.pop("pending", None))
                with lock:
                    if transcribe:
                        job["message"] = f"Stopped after {MAX_RESUMES} server restarts in a row. Resume to try again."
                    for key in pending:
                        job[key] = {
                            "status": "error",
                            "message": "Stopped after several server restarts. Start it again.",
                        }
                continue
            update_meta(run_dir, lambda meta: meta.update(resumes=meta.get("resumes", 0) + 1))
            if transcribe:
                queue_transcription(job["id"], "Continuing after a server restart")
            else:
                queue_stages(job["id"], pending, meta.get("engine", "auto"))

    def busy(job, *keys):
        return any((job.get(key) or {}).get("status") in {"queued", "running"} for key in keys)

    def transliterator(overrides):
        if not xlit and xlit_installed():
            xlit.append(XlitEngine())
        return Transliterator(xlit[0] if xlit else KnownWordsOnly(), overrides)

    def stage_view(job, key):
        """Stage state for the browser; files already on disk count as complete (e.g. after a server restart)."""
        state = dict(job.get(key) or {})
        if state.get("status") in {"queued", "running", "error"}:
            return state
        if job.get("json") and stage_output(job["json"], key).exists():
            return {"status": "complete", "progress": 100, "message": "Ready"}
        return state or {"status": "idle"}

    @app.before_request
    def guard_requests():
        recover()
        # Reject cross-site upload/POST requests from other pages.
        if request.method in {"POST", "PUT"} and request.headers.get("Origin"):
            if urlparse(request.headers["Origin"]).netloc != request.host:
                abort(403)
        if settings.password and not session.get("authed") and request.endpoint not in {"login", "static", "health"}:
            if request.path.startswith("/api/"):
                return jsonify(error="Sign in again: your session has ended."), 401
            return redirect(url_for("login"))

    @app.get("/health")  # Cloud Run reserves /healthz
    def health():
        return "ok"

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if not settings.password:
            return redirect(url_for("index"))
        error = None
        if request.method == "POST":
            if hmac.compare_digest(request.form.get("password", "").encode(), settings.password.encode()):
                session.permanent = True
                session["authed"] = True
                return redirect(url_for("index"))
            time.sleep(1)  # slows down password guessing
            error = "That password is not right."
        return render_template("login.html", error=error), 401 if error else 200

    @app.get("/")
    def index():
        owner_tag()
        return render_template(
            "index.html",
            hosted=settings.hosted,
            models=settings.models,
            max_upload_mb=round(settings.max_upload_mb),
            upload_limit=f"{settings.max_upload_mb / 1024:g} GB"
            if settings.max_upload_mb >= 1024
            else f"{settings.max_upload_mb:g} MB",
            max_minutes=round(settings.max_minutes),
            retention_hours=round(settings.retention_hours),
            chunk_bytes=UPLOAD_CHUNK_BYTES,
        )

    def upload_path(upload_id: str) -> Path:
        if not HEX_ID.fullmatch(upload_id):
            abort(404)
        return uploads_dir / f"{owner_tag()}-{upload_id}.part"

    @app.post("/api/uploads")
    def start_upload():
        uploads_dir.mkdir(parents=True, exist_ok=True)
        upload_id = uuid4().hex
        upload_path(upload_id).write_bytes(b"")
        return jsonify(id=upload_id), 201

    @app.put("/api/uploads/<upload_id>")
    def upload_chunk(upload_id):
        """Append one piece. `offset` must equal the bytes received so far, so a resent piece is never doubled."""
        path = upload_path(upload_id)
        if not path.exists():
            abort(404)
        size = path.stat().st_size
        offset = request.args.get("offset", type=int)
        if offset != size:
            return jsonify(error="Upload out of order.", received=size), 409
        data = request.get_data(cache=False)
        if len(data) > UPLOAD_CHUNK_BYTES or size + len(data) > settings.max_upload_mb * 1024**2:
            path.unlink(missing_ok=True)
            return jsonify(error=f"This file exceeds the {round(settings.max_upload_mb)} MB upload limit."), 413
        with path.open("ab") as handle:
            handle.write(data)
        return jsonify(received=size + len(data))

    def store_upload(upload, partial, filename) -> tuple[Path, bool, str]:
        """Save an uploaded recording, or reuse the stored copy of the same audio. Returns the file, whether it is
        new, and its fingerprint."""
        if partial is not None:
            sha = fingerprint(partial)
            same = find_recording(sha)
            if same:
                partial.unlink(missing_ok=True)
                return same, False, sha
        source = storage.RECORDINGS / filename
        base = source
        suffix = 2
        while True:
            try:
                with source.open("xb") as target:
                    if upload:
                        upload.save(target)
                    else:
                        with partial.open("rb") as handle:
                            shutil.copyfileobj(handle, target, 1024**2)
                break
            except FileExistsError:
                source = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
                suffix += 1
        if partial is not None:
            partial.unlink(missing_ok=True)
            return source, True, sha
        sha = fingerprint(source)
        same = find_recording(sha, exclude=source.name)
        if same:
            source.unlink()
            return same, False, sha
        return source, True, sha

    @app.get("/api/recordings/<sha>")
    def recording_status(sha):
        """Whether this audio is stored already (so it need not be uploaded), and your runs of it."""
        stored = find_recording(sha) if FINGERPRINT.fullmatch(sha) else None
        runs = []
        for job in all_runs():
            with lock:
                if owns(job) and stored and (job.get("sha256") == sha or job.get("source") == str(stored)):
                    runs.append({key: job.get(key) for key in ("id", "name", "started", "status")})
        return jsonify(stored=stored is not None, runs=runs)

    @app.post("/api/jobs")
    def create_job():
        sweep()
        model, mode = request.form.get("model", settings.models[0]), request.form.get("mode", "mixed")
        if model not in settings.models or mode not in {"mixed", "native"}:
            return jsonify(error="Choose a valid model and output mode."), 400
        upload, upload_id = request.files.get("audio"), request.form.get("upload_id")
        known = request.form.get("fingerprint", "")  # a recording already stored: nothing was uploaded
        name = upload.filename if upload else request.form.get("name", "")
        if not (upload or upload_id or known) or not name:
            return jsonify(error="Choose an audio file first."), 400
        filename = clean_filename(name)
        if not filename:
            return jsonify(error="Choose a file with a valid filename."), 400
        owner = owner_tag()
        if settings.hosted:
            with lock:
                if any(job.get("owner") == owner and job["status"] in ACTIVE for job in jobs.values()):
                    message = "A recording is already being transcribed. Pause or cancel it, or wait for it to finish."
                    return jsonify(error=message), 429
        partial = upload_path(upload_id) if upload_id and not upload else None
        if partial is not None and not partial.exists():
            return jsonify(error="The upload did not finish. Please try again."), 400
        storage.RECORDINGS.mkdir(parents=True, exist_ok=True)
        if upload or partial is not None:
            source, new, sha = store_upload(upload, partial, filename)
        else:
            source, new, sha = find_recording(known) if FINGERPRINT.fullmatch(known) else None, False, known
            if source is None:
                return jsonify(error="This recording is not stored here yet.", upload=True), 409
        try:
            duration = probe_audio(source)
            if settings.max_minutes and duration and duration > settings.max_minutes * 60:
                raise ValueError(f"Recordings up to {round(settings.max_minutes)} minutes are accepted here.")
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            if new:
                source.unlink(missing_ok=True)
            return jsonify(error=str(error)), 400
        run_dir = new_run(source, model, mode)
        if settings.hosted:
            (run_dir / OWNER_FILE).write_text(owner, encoding="utf-8")
        size = source.stat().st_size
        meta = {"source": source.name, "name": filename, "model": model, "mode": mode, "duration": duration}
        write_json(run_dir / RUN_FILE, {**meta, "size": size, "sha256": sha})
        job_id = run_id(run_dir)
        with lock:
            jobs[job_id] = {
                "id": job_id,
                "name": filename,
                "sha256": sha,
                "model": model,
                "mode": mode,
                "status": "queued",
                "progress": 0,
                "duration": duration,
                "processed": 0,
                "message": "Queued for transcription",
                "source": str(source),
                "run_dir": str(run_dir),
                "owner": owner,
                "size": size,
                "started": started(run_dir),
            }
        queue_transcription(job_id, "Queued for transcription")
        return jsonify(id=job_id), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        with lock:
            job = dict(find_job(job_id))
            if job["status"] == "queued" and job.get("seq"):
                job["ahead"] = sum(
                    1 for other in jobs.values() if other["status"] in ACTIVE and other.get("seq", 0) < job["seq"]
                )
        hidden = {"source", "run_dir", "txt", "json", "owner", "seq", *STAGES}
        result = {k: v for k, v in job.items() if k not in hidden}
        result["run"] = Path(job["run_dir"]).name
        for key in STAGES:
            result[key] = stage_view(job, key)
        if result["status"] == "complete":
            result["text"] = Path(job["txt"]).read_text(encoding="utf-8")
            if job.get("json"):
                result.update(transcript_view(Path(job["json"]), result))
        return jsonify(result)

    def transcript_view(transcript_json: Path, state: dict) -> dict:
        """Segments for the word editor, the Gujlish words lined up with them, the English and summary texts, and
        whether those were made before the latest word fixes."""
        segments = json.loads(transcript_json.read_text(encoding="utf-8")).get("segments", [])
        paths = stage_paths(transcript_json)
        gujlish, _ = load_gujlish(paths["gujlish_json"], segments)
        view = {
            "segments": [
                {
                    "start": s["start"],
                    "end": s["end"],
                    "text": s.get("text", ""),
                    "fixes": s.get("fixes", []),
                    "suggestions": s.get("suggestions", []),
                }
                for s in segments
            ],
            "gujlish_tokens": gujlish,
            "rev": "-".join(
                str(path.stat().st_mtime_ns) for path in (transcript_json, paths["gujlish_json"]) if path.exists()
            ),
        }
        if gujlish is not None:
            view["gujlish_text"] = paths["gujlish_txt"].read_text(encoding="utf-8")
        if state["translation"]["status"] == "complete":
            view["translation_text"] = paths["translation_txt"].read_text(encoding="utf-8")
            if paths["translation_json"].exists():
                english = json.loads(paths["translation_json"].read_text(encoding="utf-8")).get("segments", [])
                # The translator reads each chunk without its cut full stop (transcriber.speech.postprocess.drop_cut_stop).
                current = [drop_cut_stop(s["text"].strip()) for s in segments if s.get("text", "").strip()]
                view["english_stale"] = [s.get("source") for s in english] != current
            # A translation made before the caption step, or whose captions were not redone after it changed.
            view["captions_stale"] = state["captions"]["status"] != "running" and not captions_current(transcript_json)
        if state["summary"]["status"] == "complete":
            view["summary_markdown"] = paths["summary_md"].read_text(encoding="utf-8")
            made = paths["translation_json"]
            view["youtube_missing"] = not paths["youtube_json"].exists()  # a summary from before this step wrote them
            view["summary_stale"] = (
                view.get("english_stale", False)
                or view["youtube_missing"]
                or (made.exists() and paths["summary_md"].stat().st_mtime < made.stat().st_mtime)
            )
            if not view["youtube_missing"]:
                youtube = json.loads(paths["youtube_json"].read_text(encoding="utf-8"))
                view["youtube"] = {key: youtube.get(key, "") for key in ("title", "description")}
        return view

    def gemini_ready() -> bool:
        # Hosted sites use Gemini only when the owner allows it, because it runs on the owner's key.
        return gemini_available() and (not settings.hosted or settings.allow_gemini)

    @app.get("/api/capabilities")
    def capabilities():
        # Whether translation/summaries can run. The key itself stays in the server's environment.
        return jsonify(hosted=settings.hosted, gemini=gemini_ready())

    def start_stage(job_id, *keys, engine="auto"):
        with lock:
            find_job(job_id)
        if (set(keys) - {"gujlish"} or engine == "gemini") and not gemini_ready():
            if settings.hosted:
                return jsonify(error="Translation and summaries are not enabled on this site."), 400
            return jsonify(error="Set GEMINI_API_KEY in the terminal before starting the app (see the README)."), 400
        with lock:
            job = find_job(job_id)
            if job.get("status") != "complete" or not job.get("json"):
                return jsonify(error="Finish the transcript first."), 409
            if keys in {("summary",), ("captions",)} and stage_view(job, "translation")["status"] != "complete":
                return jsonify(error="Translate the transcript first."), 409
            if busy(job, *keys):
                return jsonify(error="This step is already running."), 409
        queue_stages(job_id, keys, engine)
        return jsonify(id=job_id), 202

    @app.post("/api/jobs/<job_id>/translate")
    def translate_job(job_id):
        return start_stage(job_id, "translation", "captions")

    @app.post("/api/jobs/<job_id>/captions")
    def captions_job(job_id):
        """Re-break and re-time the English subtitles, e.g. for a translation made before this step existed."""
        return start_stage(job_id, "captions")

    @app.post("/api/jobs/<job_id>/summarize")
    def summarize_job(job_id):
        return start_stage(job_id, "summary")

    @app.post("/api/jobs/<job_id>/english")
    def english_job(job_id):
        """Translate, then write the summary and YouTube details: the step you take once the words are right."""
        return start_stage(job_id, "translation", "captions", "summary")

    @app.post("/api/jobs/<job_id>/gujlish")
    def gujlish_job(job_id):
        engine = (request.get_json(silent=True) or {}).get("engine", "auto")
        if engine not in {"auto", "gemini"}:
            return jsonify(error="Choose a valid Gujlish engine."), 400
        return start_stage(job_id, "gujlish", engine=engine)

    @app.post("/api/jobs/<job_id>/stop")
    def stop_stage(job_id):
        """Stop the Gujlish, or the English and summary. Finished parts are kept, so starting again continues."""
        keys = {"gujlish": ("gujlish",), "english": ("translation", "captions", "summary")}.get(
            (request.get_json(silent=True) or {}).get("stage")
        )
        if not keys:
            return jsonify(error="Choose a step to stop."), 400
        running = []
        with lock:
            job = find_job(job_id)
            for key in keys:
                state = job.get(key) or {}
                if state.get("status") == "running":
                    stopping[(job_id, key)] = "stop"
                    state["message"] = "Stopping…"
                    if (job_id, key) in workers:
                        running.append(workers[(job_id, key)])
                elif state.get("status") == "queued":
                    tasks.pop((job_id, key), None)
                    job[key] = {"status": "idle", "progress": None, "message": STOPPED}
        for worker in running:
            stop_worker(worker)
        finish_pending(job["run_dir"], set(keys))
        return jsonify(stopped=True)

    @app.post("/api/jobs/<job_id>/pause")
    def pause_job(job_id):
        """Pause a transcription. Finished chunks are kept on disk; resuming continues after them."""
        worker = None
        with lock:
            job = find_job(job_id)
            if job["status"] not in ACTIVE:
                return jsonify(error="Only a recording being transcribed can be paused."), 409
            if job["status"] == "queued":
                tasks.pop((job_id, "transcribe"), None)
                job.update(status="paused", message=f"Paused at {stopped_at(job)}. Resume continues from there.")
            else:
                stopping[(job_id, "transcribe")] = "pause"
                job["message"] = "Pausing…"
                worker = workers.get((job_id, "transcribe"))  # not started yet: it is stopped as soon as it starts
        if worker:
            stop_worker(worker)
        elif job["status"] == "paused":
            update_meta(job["run_dir"], lambda meta: meta.update(paused=True))
        return jsonify(paused=True)

    @app.post("/api/jobs/<job_id>/resume")
    @app.post("/api/jobs/<job_id>/retry")
    def resume_job(job_id):
        with lock:
            job = find_job(job_id)
            if job["status"] not in {"error", "paused"}:
                return jsonify(error="Only a paused or stopped recording can be resumed."), 409
        drop_meta(job["run_dir"], "paused", "failed", "resumes")
        queue_transcription(job_id, "Queued to continue from where it stopped")
        return jsonify(id=job_id), 202

    def remove_run(job: dict, running: list) -> None:
        """Wait for the run's processes to stop, then delete its folder and, if no other run uses it, its recording."""
        for worker in running:
            try:
                worker.wait(timeout=STOP_GRACE_SECONDS + 5)
            except subprocess.TimeoutExpired:
                pass
        run_dir = Path(job["run_dir"])
        shutil.rmtree(run_dir, ignore_errors=True)
        for folder in (run_dir.parent, run_dir.parent.parent):
            try:
                folder.rmdir()  # only succeeds once empty
            except OSError:
                pass
        recording = Path(job.get("source") or "")
        if recording.is_file() and all(other.get("source") != str(recording) for other in all_runs()):
            recording.unlink(missing_ok=True)

    @app.post("/api/jobs/<job_id>/delete")
    def delete_job(job_id):
        """Cancel or delete a run: stop whatever it is doing, then remove its files in the background."""
        running = []
        with lock:
            job = find_job(job_id)
            del jobs[job_id]
            deleted.add(job["run_dir"])
            for step in ("transcribe", *STAGES):
                tasks.pop((job_id, step), None)
                started_step = job["status"] in {"loading", "transcribing"} if step == "transcribe" else busy(job, step)
                if started_step or (job_id, step) in workers:
                    stopping[(job_id, step)] = "delete"
                if (job_id, step) in workers:
                    running.append(workers[(job_id, step)])
        for worker in running:
            stop_worker(worker)
        Thread(target=remove_run, args=(job, running), daemon=True).start()
        return jsonify(deleted=True)

    @app.post("/api/jobs/<job_id>/edit")
    def edit_job(job_id):
        """Replace words in the transcript (and the same words elsewhere in it, if asked), keep the Gujlish in step,
        and remember the fix for future recordings unless told not to."""
        with lock:
            job = dict(find_job(job_id))
        body = request.get_json(silent=True) or {}
        try:
            number, start, end = int(body["segment"]), int(body["start"]), int(body["end"])
            heard = str(body["heard"])
            correct = " ".join(str(body["correct"]).split())
            correct_gujlish = " ".join(str(body.get("correct_gujlish") or "").split())
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Choose the words to replace."), 400
        if not correct:
            return jsonify(error="Type the correct word."), 400
        if job.get("status") != "complete" or not job.get("json"):
            return jsonify(error="Finish the transcript first."), 409
        if busy(job, "gujlish"):
            return jsonify(error="Gujlish is still being written. Try again in a moment."), 409
        with edit_lock:
            transcript_json = Path(job["json"])
            data = json.loads(transcript_json.read_text(encoding="utf-8"))
            segments = data["segments"]
            tokens = segments[number].get("text", "").split() if 0 <= number < len(segments) else []
            if not 0 <= start < end <= len(tokens) or " ".join(tokens[start:end]) != heard:
                return jsonify(error="This part of the transcript has changed. Reload the page and try again."), 409
            context, segment = segments[number]["text"], segments[number]
            paths = stage_paths(transcript_json)
            gujlish, engine = load_gujlish(paths["gujlish_json"], segments)
            heard_gujlish = " ".join(gujlish[number][start:end]) if gujlish else ""
            places = [(number, start)]
            if body.get("everywhere"):
                size = end - start
                places += [(n, s) for n, s in matches(segments, heard) if n != number or s + size <= start or s >= end]
            dictionary = load_dictionary()
            latin = replace_words(
                segments, gujlish, places, end - start, correct, correct_gujlish, transliterator(dictionary["gujlish"])
            )
            save_transcript(transcript_json, data)
            if gujlish is not None:
                write_gujlish(transcript_json, data, gujlish, engine)
            clip = None
            if body.get("remember", True):
                example = evidence(data, transcript_json, segment, context, start)
                always = bool(body.get("always"))
                rule = remember(dictionary, heard, correct, heard_gujlish, " ".join(latin), example, always)
                clip = keep_clip(job, rule, example)
                save_dictionary(dictionary)
        if clip:
            Thread(target=save_clip, args=clip, daemon=True).start()
        return jsonify(replaced=len(places), remembered=bool(body.get("remember", True)))

    def evidence(data: dict, transcript_json: Path, segment: dict, context: str, start: int) -> dict:
        """Where a fix (or a rejected suggestion) happened: the recording, the sentence as heard and roughly when the
        word was said (estimated from its place in the sentence: the recognizer gives no word times)."""
        tokens, seconds = context.split(), segment["end"] - segment["start"]
        return {
            "recording": data.get("source"),
            "run": transcript_json.parent.name,
            "start": segment["start"],
            "end": segment["end"],
            "at": round(segment["start"] + seconds * len(" ".join(tokens[:start])) / max(1, len(context)), 2),
            "context": context,
        }

    def keep_clip(job: dict, rule: dict, example: dict):
        """Name the audio clip for this example and return what save_clip needs to cut it, or None."""
        recording = Path(job.get("source") or "")
        if not rule or not recording.is_file():
            return None
        example["clip"] = f"{rule['id']}-{uuid4().hex[:6]}.ogg"
        return recording, example["start"], example["end"], clips_dir() / example["clip"]

    @app.post("/api/jobs/<job_id>/dismiss")
    def dismiss_suggestion(job_id):
        """Reject a dictionary suggestion in one place: the words stay as heard, and the dictionary keeps this place
        (sentence and audio clip) as a time the fix was wrong."""
        with lock:
            job = dict(find_job(job_id))
        body = request.get_json(silent=True) or {}
        try:
            number, start, end, rule_id = int(body["segment"]), int(body["start"]), int(body["end"]), str(body["rule"])
        except (KeyError, TypeError, ValueError):
            return jsonify(error="Choose the suggestion to dismiss."), 400
        if job.get("status") != "complete" or not job.get("json"):
            return jsonify(error="Finish the transcript first."), 409
        clip = None
        with edit_lock:
            transcript_json = Path(job["json"])
            data = json.loads(transcript_json.read_text(encoding="utf-8"))
            segments = data["segments"]
            segment = segments[number] if 0 <= number < len(segments) else {}
            suggestions = segment.get("suggestions", [])
            found = next(
                (s for s in suggestions if (s["start"], s["end"], s.get("rule")) == (start, end, rule_id)), None
            )
            if found is None:
                return jsonify(error="This part of the transcript has changed. Reload the page and try again."), 409
            suggestions.remove(found)
            if not suggestions:
                segment.pop("suggestions", None)
            save_transcript(transcript_json, data)
            dictionary = load_dictionary()
            example = evidence(data, transcript_json, segment, segment.get("text", ""), start)
            rule = reject(dictionary, rule_id, example)
            if rule:
                clip = keep_clip(job, rule, example)
                save_dictionary(dictionary)
        if clip:
            Thread(target=save_clip, args=clip, daemon=True).start()
        return jsonify(dismissed=True)

    def line_word_times(transcript_json: Path, segment: dict, recording: Path) -> list[dict] | None:
        """When each word of a transcript line is said: from the caption step's alignment, else by aligning the line
        now (about half a second on the CPU, plus loading the model the first time), else None."""
        saved = align.load_word_times(transcript_json, [segment])[0]
        if saved:
            return saved
        key = json.dumps([str(transcript_json), segment["start"], segment["end"], segment["text"]], ensure_ascii=False)
        with align_lock:
            if key not in aligned:
                if "model" not in aligner:
                    try:
                        aligner["model"] = align.Aligner()
                    except (ImportError, OSError, RuntimeError, ValueError):
                        aligner["model"] = None
                times = None
                if aligner["model"] is not None:
                    start, end = float(segment["start"]), float(segment["end"])
                    try:
                        samples = align.read_audio(recording, start, end)
                        times = aligner["model"].align(samples, start, end, segment["text"])
                    except (OSError, RuntimeError, ValueError):
                        times = None
                if len(aligned) >= 1000:
                    aligned.clear()
                aligned[key] = times
            return aligned[key]

    @app.get("/api/jobs/<job_id>/word-audio")
    def word_audio(job_id):
        """The moment words [start, end) of a line are said, as a short WAV, so a fix or a dictionary suggestion can
        be judged by ear. When the words can't be timed, their place in the line is estimated and more audio is
        kept around them (the X-Word-Timing header says which)."""
        with lock:
            job = dict(find_job(job_id))
        try:
            number, start, end = (int(request.args[name]) for name in ("segment", "start", "end"))
        except (KeyError, ValueError):
            return jsonify(error="Choose the words to play."), 400
        recording = Path(job.get("source") or "")
        if job.get("status") != "complete" or not job.get("json") or not recording.is_file():
            return jsonify(error="The recording isn't available."), 404
        transcript_json = Path(job["json"])
        segments = json.loads(transcript_json.read_text(encoding="utf-8")).get("segments", [])
        segment = segments[number] if 0 <= number < len(segments) else {}
        if not 0 <= start < end <= len(segment.get("text", "").split()):
            return jsonify(error="This part of the transcript has changed. Reload the page and try again."), 409
        times = line_word_times(transcript_json, segment, recording)
        exact = times is not None
        if not exact:
            times = align.estimate_times(segment["text"], float(segment["start"]), float(segment["end"]))
        pad = WORD_PAD if exact else ROUGH_PAD
        first, last = max(0.0, round(times[start]["start"] - pad, 3)), round(times[end - 1]["end"] + pad, 3)
        audio = clip_audio(recording, first, last)
        if audio is None:
            return jsonify(error="Could not cut this part of the recording."), 500
        response = send_file(BytesIO(audio), mimetype="audio/wav", download_name="words.wav")
        response.headers["X-Word-Timing"] = "aligned" if exact else "estimated"
        response.headers["Cache-Control"] = "private, max-age=300"
        return response

    @app.get("/api/transliterate")
    def transliterate_text():
        """Suggestions for the word editor: Gujarati for typed Gujlish (to=gu), or Gujlish for typed Gujarati."""
        text, to = request.args.get("text", "")[:200], request.args.get("to")
        if to not in {"gu", "latin"}:
            abort(400)
        try:
            words = transliterator(load_dictionary()["gujlish"])
            options = words.gujarati_options(text) if to == "gu" else [words.latin_text(text)] if text.strip() else []
        except (ImportError, OSError, RuntimeError, ValueError):
            options = []
        return jsonify(options=[option for option in options if option.strip()])

    @app.get("/api/dictionary")
    def dictionary_entries():
        data = load_dictionary()
        fields = ("id", "heard", "heard_gujlish", "correct", "correct_gujlish", "added")
        corrections = [
            {
                **{key: rule.get(key) for key in fields},
                "mode": mode(rule),
                "examples": len(rule.get("examples", [])),
                "rejected": len(rule.get("rejected", [])),
            }
            for rule in reversed(data["corrections"])
        ]
        return jsonify(corrections=corrections, gujlish=data["gujlish"])

    @app.post("/api/dictionary/forget")
    def dictionary_forget():
        body = request.get_json(silent=True) or {}
        with edit_lock:
            data = load_dictionary()
            if not forget(data, correction=body.get("correction"), gujlish=body.get("gujlish")):
                abort(404)
            save_dictionary(data)
        return jsonify(ok=True)

    @app.post("/api/dictionary/mode")
    def dictionary_mode():
        """Switch a correction between asking before each fix ("ask") and fixing without asking ("always")."""
        body = request.get_json(silent=True) or {}
        with edit_lock:
            data = load_dictionary()
            if not set_mode(data, str(body.get("correction", "")), str(body.get("mode", ""))):
                abort(404)
            save_dictionary(data)
        return jsonify(ok=True)

    @app.get("/api/jobs/<job_id>/download/<kind>")
    def download(job_id, kind):
        with lock:
            job = dict(find_job(job_id))
        if job.get("status") != "complete":
            abort(404)
        txt = Path(job["txt"])
        docx_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        if kind == "txt":
            return send_file(txt, as_attachment=True, download_name=txt.name, mimetype="text/plain; charset=utf-8")
        if kind == "docx":
            return send_file(
                transcript_docx(txt.read_text(encoding="utf-8"), txt.stem),
                as_attachment=True,
                download_name=txt.with_suffix(".docx").name,
                mimetype=docx_type,
            )
        if kind in {"srt", "en-srt"} and job.get("json"):
            # Built from the timed segments, so runs saved before SRT files existed can still be exported. The English
            # uses the caption step's breaks and times while they match the current translation.
            source = Path(job["json"]) if kind == "srt" else stage_paths(Path(job["json"]))["translation_json"]
            if not source.exists():
                abort(404)
            name = source.with_suffix(".srt").name
            if kind == "en-srt" and captions_current(Path(job["json"])):
                captions = stage_paths(Path(job["json"]))["captions_json"].read_text(encoding="utf-8")
                text = captions_srt(json.loads(captions).get("captions", []))
            else:
                text = to_srt(json.loads(source.read_text(encoding="utf-8")).get("segments", []))
            return send_file(
                BytesIO(text.encode("utf-8")),
                as_attachment=True,
                download_name=name,
                mimetype="application/x-subrip; charset=utf-8",
            )
        # Gujlish, English and summary files sit next to the transcript.
        paths = stage_paths(Path(job["json"])) if job.get("json") else {}
        wanted = {
            "gujlish-txt": ("gujlish_txt", False),
            "gujlish-docx": ("gujlish_txt", True),
            "gujlish-srt": ("gujlish_srt", False),
            "en-txt": ("translation_txt", False),
            "en-docx": ("translation_txt", True),
            "summary-md": ("summary_md", False),
            "summary-docx": ("summary_md", True),
        }.get(kind)
        if not wanted or not paths.get(wanted[0]) or not paths[wanted[0]].exists():
            abort(404)
        path, as_docx = paths[wanted[0]], wanted[1]
        if not as_docx:
            mimetype = {".md": "text/markdown", ".srt": "application/x-subrip"}.get(path.suffix, "text/plain")
            return send_file(path, as_attachment=True, download_name=path.name, mimetype=f"{mimetype}; charset=utf-8")
        text = path.read_text(encoding="utf-8")
        document = markdown_docx(text, path.stem) if path.suffix == ".md" else transcript_docx(text, path.stem)
        return send_file(document, as_attachment=True, download_name=path.with_suffix(".docx").name, mimetype=docx_type)

    @app.get("/api/history")
    def history():
        """Every run you can open, newest first, with what each one is doing right now."""
        sweep()
        fields = ("id", "name", "model", "mode", "status", "progress", "message", "duration", "size", "started")
        runs = []
        for job in all_runs():
            with lock:
                if not owns(job):
                    continue
                entry = {key: job.get(key) for key in fields}
                entry["run"] = Path(job["run_dir"]).name
                for key in STAGES:
                    state = job.get(key) or {}
                    if state.get("status") in {"queued", "running", "error"}:
                        entry[key] = {"status": state["status"], "progress": state.get("progress")}
            runs.append(entry)
            if len(runs) >= 100:
                break
        return jsonify(runs)

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error=f"This file exceeds the {round(settings.max_upload_mb)} MB upload limit."), 413

    return app
