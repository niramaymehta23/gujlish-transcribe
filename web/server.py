"""A local upload server that runs one worker subprocess at a time (transcribe, Gujlish, translate or summarize)."""

import hashlib
import hmac
import json
import os
import re
import shutil
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

import asr.storage as storage
from asr.dictionary import clips_dir, dictionary_path, forget, remember, save_clip
from asr.dictionary import load as load_dictionary
from asr.dictionary import save as save_dictionary
from asr.edits import load_gujlish, matches, replace_words, save_transcript
from asr.gujlish import KnownWordsOnly, Transliterator, XlitEngine, write_gujlish, xlit_installed
from asr.llm import gemini_available
from asr.storage import ROOT, new_run, stage_paths
from asr.subtitles import to_srt
from web.documents import markdown_docx, transcript_docx
from web.settings import Settings

LOG_TAIL_LINES = 12  # recent subprocess output kept for error messages
UPLOAD_CHUNK_BYTES = 8 * 1024**2  # Cloud Run caps a request body at 32 MiB, so large files arrive in pieces
UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
ACTIVE = {"queued", "loading", "transcribing"}
SWEEP_EVERY_SECONDS = 600
OWNER_FILE = ".owner"
EVENT_PREFIX = "ASR_EVENT "


@dataclass(frozen=True)
class Stage:
    """One stage after transcription. Each goes by several names: job key ("translation"), URL (/translate), worker
    script, output file, and progress event ("translating")."""

    script: str
    output: str  # key in asr.storage.stage_paths
    verb: str


STAGES = {
    "gujlish": Stage("transliterate.py", "gujlish_json", "Writing Gujlish"),
    "translation": Stage("translate.py", "translation_txt", "Translating"),
    "summary": Stage("summarize.py", "summary_md", "Summarizing"),
}
PROGRESS_EVENTS = {"transliterating", "translating", "summarizing"}


def stage_output(transcript_json: str, key: str) -> Path:
    return stage_paths(Path(transcript_json))[STAGES[key].output]


def run_worker(command: list[str], on_event) -> tuple[int, list[str]]:
    """Run a worker script, passing each progress event to `on_event`; return its exit code and last log lines."""
    logs: list[str] = []
    with subprocess.Popen(
        command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
    ) as worker:
        for line in worker.stdout:
            logs = [*logs, line.strip()][-LOG_TAIL_LINES:]
            if line.startswith(EVENT_PREFIX):
                on_event(json.loads(line[len(EVENT_PREFIX) :]))
        return worker.wait(), logs


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
    edit_lock = Lock()  # one word fix at a time: each rewrites the transcript files and may save the dictionary
    xlit = []  # the IndicXlit engine for editor suggestions, loaded on first use
    pool = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
    uploads_dir = Path(tempfile.gettempdir()) / "gujlish-uploads"  # local disk, not the mounted bucket
    last_sweep = [0.0]
    sequence = [0]
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

    def find_job(job_id: str) -> dict:
        """The job dict (under the lock's protection by the caller), or 404 for unknown or someone else's job."""
        job = jobs.get(job_id)
        if not job or not owns(job):
            abort(404)
        return job

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
                started = datetime.strptime(run_dir.name, "%Y%m%d-%H%M%S-%f")
            except ValueError:
                continue
            if started < cutoff and str(run_dir) not in busy:
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

    def update(job_id, **fields):
        with lock:
            jobs[job_id].update(fields)

    def process(job_id, source, run_dir, model, mode):
        update(job_id, status="loading", message="Loading the model. The first run may download model weights.")

        def on_event(event):
            if event["stage"] == "transcribing":
                processed = event["processed"]
                total = jobs[job_id]["duration"]
                percent = min(99, round(processed / total * 100)) if total else None
                update(
                    job_id,
                    status="transcribing",
                    processed=processed,
                    progress=percent,
                    message=f"Transcribing · {event['chunks']} chunks completed",
                )
            elif event["stage"] == "complete":
                update(job_id, txt=event["txt"], json=event["json"])
            elif event["stage"] == "loading":
                update(job_id, device=event["device"])

        command = [sys.executable, str(ROOT / "transcribe.py"), str(source), model, "--mode", mode]
        command += ["--output-dir", str(run_dir), "--dictionary", str(dictionary_path()), "--progress-json"]
        try:
            code, logs = run_worker(command, on_event)
            if code or not jobs[job_id].get("txt"):
                raise RuntimeError(last_error(logs, "Transcription stopped. You can retry to resume completed chunks."))
            set_stage(job_id, "gujlish", status="queued", progress=None, message="Writing Gujlish…")
            update(job_id, status="complete", progress=100, message="Transcript ready")
        except Exception as error:
            update(job_id, status="error", message=str(error))
            return
        # Gujlish follows straight away; English and the summary wait until you have fixed the words.
        run_stage(job_id, "gujlish", stage_command(jobs[job_id]["json"], "gujlish"))

    def stage_command(transcript_json, key, engine="auto"):
        command = [sys.executable, str(ROOT / STAGES[key].script), transcript_json, "--progress-json"]
        if key == "gujlish":
            command += ["--dictionary", str(dictionary_path()), "--engine", engine]
        return command

    def set_stage(job_id, key, **fields):
        with lock:
            jobs[job_id].setdefault(key, {}).update(fields)

    def run_stage(job_id, key, command):
        """Run a stage's worker script as a subprocess, mirroring its progress events into the job."""
        verb = STAGES[key].verb
        set_stage(job_id, key, status="running", progress=None, message=f"{verb}…")

        def on_event(event):
            if event["stage"] in PROGRESS_EVENTS and event["total"]:
                percent = min(99, round(event["done"] / event["total"] * 100))
                set_stage(job_id, key, progress=percent, message=f"{verb} · {event['done']} of {event['total']}")

        try:
            code, logs = run_worker(command, on_event)
            if code or not stage_output(jobs[job_id]["json"], key).exists():
                raise RuntimeError(last_error(logs, f"{verb} stopped. Start it again to resume."))
            set_stage(job_id, key, status="complete", progress=100, message="Ready")
        except Exception as error:
            set_stage(job_id, key, status="error", message=str(error))

    def run_stages(job_id, steps):
        """Run stages in order (translation, then the summary), stopping at the first that fails."""
        for index, (key, command) in enumerate(steps):
            run_stage(job_id, key, command)
            with lock:
                failed = jobs[job_id][key]["status"] != "complete"
            if failed:
                for later, _ in steps[index + 1 :]:
                    set_stage(job_id, later, status="idle", progress=None, message="")
                return

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
        if not UPLOAD_ID.fullmatch(upload_id):
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

    @app.post("/api/jobs")
    def create_job():
        sweep()
        model, mode = request.form.get("model", settings.models[0]), request.form.get("mode", "mixed")
        if model not in settings.models or mode not in {"mixed", "native"}:
            return jsonify(error="Choose a valid model and output mode."), 400
        upload, upload_id = request.files.get("audio"), request.form.get("upload_id")
        name = upload.filename if upload else request.form.get("name", "")
        if not (upload or upload_id) or not name:
            return jsonify(error="Choose an audio file first."), 400
        filename = clean_filename(name)
        if not filename:
            return jsonify(error="Choose a file with a valid filename."), 400
        owner = owner_tag()
        if settings.hosted:
            with lock:
                if any(job.get("owner") == owner and job["status"] in ACTIVE for job in jobs.values()):
                    return jsonify(error="You already have a recording in progress. Wait for it to finish."), 429
        partial = upload_path(upload_id) if upload_id and not upload else None
        if partial is not None and not partial.exists():
            return jsonify(error="The upload did not finish. Please try again."), 400
        storage.RECORDINGS.mkdir(parents=True, exist_ok=True)
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
        try:
            duration = probe_audio(source)
            if settings.max_minutes and duration and duration > settings.max_minutes * 60:
                raise ValueError(f"Recordings up to {round(settings.max_minutes)} minutes are accepted here.")
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            source.unlink(missing_ok=True)
            return jsonify(error=str(error)), 400
        run_dir = new_run(source, model, mode)
        if settings.hosted:
            (run_dir / OWNER_FILE).write_text(owner, encoding="utf-8")
        job_id = uuid4().hex
        with lock:
            sequence[0] += 1
            jobs[job_id] = {
                "id": job_id,
                "name": source.name,
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
                "seq": sequence[0],
            }
        pool.submit(process, job_id, source, run_dir, model, mode)
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
                {"start": s["start"], "end": s["end"], "text": s.get("text", ""), "fixes": s.get("fixes", [])}
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
                current = [s["text"].strip() for s in segments if s.get("text", "").strip()]
                view["english_stale"] = [s.get("source") for s in english] != current
        if state["summary"]["status"] == "complete":
            view["summary_markdown"] = paths["summary_md"].read_text(encoding="utf-8")
            made = paths["translation_json"]
            view["summary_stale"] = view.get("english_stale", False) or (
                made.exists() and paths["summary_md"].stat().st_mtime < made.stat().st_mtime
            )
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
            if keys == ("summary",) and stage_view(job, "translation")["status"] != "complete":
                return jsonify(error="Translate the transcript before summarizing it."), 409
            if busy(job, *keys):
                return jsonify(error="This step is already running."), 409
            for key in keys:
                job[key] = {"status": "queued", "progress": None, "message": "Waiting for the current run…"}
            steps = [(key, stage_command(job["json"], key, engine)) for key in keys]
        pool.submit(run_stages, job_id, steps)
        return jsonify(id=job_id), 202

    @app.post("/api/jobs/<job_id>/translate")
    def translate_job(job_id):
        return start_stage(job_id, "translation")

    @app.post("/api/jobs/<job_id>/summarize")
    def summarize_job(job_id):
        return start_stage(job_id, "summary")

    @app.post("/api/jobs/<job_id>/english")
    def english_job(job_id):
        """Translate, then summarize: the step you take once the words are right."""
        return start_stage(job_id, "translation", "summary")

    @app.post("/api/jobs/<job_id>/gujlish")
    def gujlish_job(job_id):
        engine = (request.get_json(silent=True) or {}).get("engine", "auto")
        if engine not in {"auto", "gemini"}:
            return jsonify(error="Choose a valid Gujlish engine."), 400
        return start_stage(job_id, "gujlish", engine=engine)

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
                seconds = segment["end"] - segment["start"]
                example = {
                    "recording": data.get("source"),
                    "run": transcript_json.parent.name,
                    "start": segment["start"],
                    "end": segment["end"],
                    "at": round(segment["start"] + seconds * len(" ".join(tokens[:start])) / max(1, len(context)), 2),
                    "context": context,
                }
                rule = remember(dictionary, heard, correct, heard_gujlish, " ".join(latin), example)
                recording = storage.RECORDINGS / str(data.get("source") or "")
                if rule and data.get("source") and recording.is_file():
                    example["clip"] = f"{rule['id']}-{uuid4().hex[:6]}.ogg"
                    clip = (recording, segment["start"], segment["end"], clips_dir() / example["clip"])
                save_dictionary(dictionary)
        if clip:
            Thread(target=save_clip, args=clip, daemon=True).start()
        return jsonify(replaced=len(places), remembered=bool(body.get("remember", True)))

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
            {**{key: rule.get(key) for key in fields}, "examples": len(rule.get("examples", []))}
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

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id):
        with lock:
            job = find_job(job_id)
            if job["status"] != "error":
                return jsonify(error="Only a failed run can be retried."), 409
            job.update(status="queued", message="Queued to resume completed chunks")
            values = (Path(job["source"]), Path(job["run_dir"]), job["model"], job["mode"])
        pool.submit(process, job_id, *values)
        return jsonify(id=job_id), 202

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
            # Built from the timed segments, so runs saved before SRT files existed can still be exported.
            source = Path(job["json"]) if kind == "srt" else stage_paths(Path(job["json"]))["translation_json"]
            if not source.exists():
                abort(404)
            segments = json.loads(source.read_text(encoding="utf-8")).get("segments", [])
            name = source.with_suffix(".srt").name
            return send_file(
                BytesIO(to_srt(segments).encode("utf-8")),
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
        # Re-open saved runs after restarting the server without a database.
        sweep()
        owner = owner_tag()
        completed = []
        paths = sorted(storage.TRANSCRIPTS.glob("*/*/*/*.json"), key=lambda path: path.parent.name, reverse=True)
        for path in paths:
            if path.name.startswith(".") or not path.with_suffix(".txt").exists():
                continue
            try:
                run_owner = (path.parent / OWNER_FILE).read_text(encoding="utf-8") if settings.hosted else None
                if settings.hosted and run_owner != owner:
                    continue
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("kind"):  # translation/summary files share the folder but are not transcripts
                    continue
                job_id = hashlib.sha256(str(path).encode()).hexdigest()[:32]
                record = {
                    "id": job_id,
                    "name": data["source"],
                    "model": data["model"],
                    "mode": data.get("mode"),
                    "duration": data["duration"],
                    "status": "complete",
                    "progress": 100,
                    "message": "Transcript ready",
                    "txt": str(path.with_suffix(".txt")),
                    "json": str(path),
                    "run": path.parent.name,
                    "owner": run_owner,
                    "run_dir": str(path.parent),
                }
                with lock:
                    jobs.setdefault(job_id, record)
                completed.append({k: record[k] for k in ("id", "name", "model", "mode", "run")})
            except (OSError, ValueError, KeyError):
                continue
            if len(completed) >= 50:
                break
        return jsonify(completed)

    @app.errorhandler(413)
    def too_large(_error):
        return jsonify(error=f"This file exceeds the {round(settings.max_upload_mb)} MB upload limit."), 413

    return app
