"""A local upload server that runs one worker subprocess at a time (transcribe, translate or summarize)."""

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
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse
from uuid import uuid4

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, session, url_for

import asr.storage as storage
from asr.llm import gemini_available
from asr.storage import ROOT, new_run, stage_paths
from asr.subtitles import to_srt
from web.settings import Settings

LOG_TAIL_LINES = 12  # recent subprocess output kept for error messages
UPLOAD_CHUNK_BYTES = 8 * 1024**2  # Cloud Run caps a request body at 32 MiB, so large files arrive in pieces
UPLOAD_ID = re.compile(r"[0-9a-f]{32}")
ACTIVE = {"queued", "loading", "transcribing"}
SWEEP_EVERY_SECONDS = 600
OWNER_FILE = ".owner"


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


def _new_document(title: str):
    # Keep the Python-only server small; standard Word paragraphs preserve Unicode.
    from docx import Document
    from docx.oxml.ns import qn
    from docx.shared import Inches, Pt

    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Inches(8.27), Inches(11.69)
    section.top_margin = section.bottom_margin = Inches(0.8)
    normal = document.styles["Normal"]
    normal.font.name, normal.font.size = "Nirmala UI", Pt(11)
    normal.paragraph_format.line_spacing = 1.4
    fonts = normal.element.get_or_add_rPr().get_or_add_rFonts()
    fonts.set(qn("w:cs"), "Nirmala UI")
    document.core_properties.title = title
    return document


def _saved(document) -> BytesIO:
    buffer = BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


def transcript_docx(text: str, title: str) -> BytesIO:
    document = _new_document(title)
    for paragraph in text.splitlines() or [""]:
        document.add_paragraph(paragraph)
    return _saved(document)


def markdown_docx(markdown: str, title: str) -> BytesIO:
    """Render the summary's small Markdown subset (headings, bullets, quotes, bold/italic) as Word."""

    def plain(text: str) -> str:
        return re.sub(r"\*(.+?)\*", r"\1", re.sub(r"\*\*(.+?)\*\*", r"\1", text))

    document = _new_document(title)
    for line in markdown.splitlines():
        if line.startswith("### "):
            document.add_heading(plain(line[4:]), level=3)
        elif line.startswith("## "):
            document.add_heading(plain(line[3:]), level=2)
        elif line.startswith("# "):
            document.add_heading(plain(line[2:]), level=1)
        elif line.startswith("- "):
            document.add_paragraph(plain(line[2:]), style="List Bullet")
        elif line.startswith("> "):
            document.add_paragraph().add_run(plain(line[2:])).italic = True
        elif line.strip():
            document.add_paragraph(plain(line))
    return _saved(document)


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
        logs = []
        try:
            command = [
                sys.executable,
                str(ROOT / "transcribe.py"),
                str(source),
                model,
                "--mode",
                mode,
                "--output-dir",
                str(run_dir),
                "--progress-json",
            ]
            with subprocess.Popen(
                command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            ) as worker:
                for line in worker.stdout:
                    logs.append(line.strip())
                    logs = logs[-LOG_TAIL_LINES:]
                    if line.startswith("ASR_EVENT "):
                        event = json.loads(line[len("ASR_EVENT ") :])
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
                code = worker.wait()
            if code or not jobs[job_id].get("txt"):
                detail = next(
                    (line.removeprefix("Error: ") for line in reversed(logs) if line.startswith("Error:")),
                    "Transcription stopped. You can retry to resume completed chunks.",
                )
                raise RuntimeError(detail)
            update(job_id, status="complete", progress=100, message="Transcript ready")
        except Exception as error:
            update(job_id, status="error", message=str(error))

    # Each English stage goes by several names: job key "translation" / "summary", URL /translate / /summarize,
    # script translate.py / summarize.py, and progress event "translating" / "summarizing".
    STAGE_WORDS = {"translation": "Translating", "summary": "Summarizing"}

    def set_stage(job_id, key, **fields):
        with lock:
            jobs[job_id].setdefault(key, {}).update(fields)

    def run_stage(job_id, key, command):
        """Run translate.py / summarize.py as a subprocess, mirroring its progress events into the job."""
        set_stage(job_id, key, status="running", progress=None, message=f"{STAGE_WORDS[key]}…")
        logs = []
        try:
            with subprocess.Popen(
                command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1
            ) as worker:
                for line in worker.stdout:
                    logs.append(line.strip())
                    logs = logs[-LOG_TAIL_LINES:]
                    if line.startswith("ASR_EVENT "):
                        event = json.loads(line[len("ASR_EVENT ") :])
                        if event["stage"] in {"translating", "summarizing"} and event["total"]:
                            percent = min(99, round(event["done"] / event["total"] * 100))
                            set_stage(
                                job_id,
                                key,
                                progress=percent,
                                message=f"{STAGE_WORDS[key]} · {event['done']} of {event['total']}",
                            )
                code = worker.wait()
            paths = stage_paths(Path(jobs[job_id]["json"]))
            produced = paths["translation_txt"] if key == "translation" else paths["summary_md"]
            if code or not produced.exists():
                detail = next(
                    (line.removeprefix("Error: ") for line in reversed(logs) if line.startswith("Error:")),
                    f"{STAGE_WORDS[key]} stopped. Start it again to resume.",
                )
                raise RuntimeError(detail)
            set_stage(job_id, key, status="complete", progress=100, message="Ready")
        except Exception as error:
            set_stage(job_id, key, status="error", message=str(error))

    def stage_view(job, key):
        """Stage state for the browser; files already on disk count as complete (e.g. after a server restart)."""
        state = dict(job.get(key) or {})
        if state.get("status") in {"queued", "running", "error"}:
            return state
        if job.get("json"):
            paths = stage_paths(Path(job["json"]))
            produced = paths["translation_txt"] if key == "translation" else paths["summary_md"]
            if produced.exists():
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
        hidden = {"source", "run_dir", "txt", "json", "translation", "summary", "owner", "seq"}
        result = {k: v for k, v in job.items() if k not in hidden}
        result["translation"] = stage_view(job, "translation")
        result["summary"] = stage_view(job, "summary")
        if result["status"] == "complete":
            result["text"] = Path(job["txt"]).read_text(encoding="utf-8")
            if job.get("json"):
                paths = stage_paths(Path(job["json"]))
                if result["translation"]["status"] == "complete":
                    result["translation_text"] = paths["translation_txt"].read_text(encoding="utf-8")
                if result["summary"]["status"] == "complete":
                    result["summary_markdown"] = paths["summary_md"].read_text(encoding="utf-8")
        return jsonify(result)

    def gemini_ready() -> bool:
        # Hosted sites use Gemini only when the owner allows it, because it runs on the owner's key.
        return gemini_available() and (not settings.hosted or settings.allow_gemini)

    @app.get("/api/capabilities")
    def capabilities():
        # Whether translation/summaries can run. The key itself stays in the server's environment.
        return jsonify(hosted=settings.hosted, gemini=gemini_ready())

    def start_stage(job_id, key):
        with lock:
            find_job(job_id)
        if not gemini_ready():
            if settings.hosted:
                return jsonify(error="Translation and summaries are not enabled on this site."), 400
            return jsonify(error="Set GEMINI_API_KEY in the terminal before starting the app (see the README)."), 400
        with lock:
            job = find_job(job_id)
            if job.get("status") != "complete" or not job.get("json"):
                return jsonify(error="Finish the transcript first."), 409
            if key == "summary" and stage_view(job, "translation")["status"] != "complete":
                return jsonify(error="Translate the transcript before summarizing it."), 409
            if (job.get(key) or {}).get("status") in {"queued", "running"}:
                return jsonify(error="This step is already running."), 409
            job[key] = {"status": "queued", "progress": None, "message": "Waiting for the current run…"}
            transcript_json = job["json"]
        script = "translate.py" if key == "translation" else "summarize.py"
        command = [sys.executable, str(ROOT / script), transcript_json, "--progress-json"]
        pool.submit(run_stage, job_id, key, command)
        return jsonify(id=job_id), 202

    @app.post("/api/jobs/<job_id>/translate")
    def translate_job(job_id):
        return start_stage(job_id, "translation")

    @app.post("/api/jobs/<job_id>/summarize")
    def summarize_job(job_id):
        return start_stage(job_id, "summary")

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
        # English translation and summary files sit next to the transcript.
        paths = stage_paths(Path(job["json"])) if job.get("json") else {}
        wanted = {
            "en-txt": ("translation_txt", False),
            "en-docx": ("translation_txt", True),
            "summary-md": ("summary_md", False),
            "summary-docx": ("summary_md", True),
        }.get(kind)
        if not wanted or not paths.get(wanted[0]) or not paths[wanted[0]].exists():
            abort(404)
        path, as_docx = paths[wanted[0]], wanted[1]
        if not as_docx:
            mimetype = "text/markdown; charset=utf-8" if path.suffix == ".md" else "text/plain; charset=utf-8"
            return send_file(path, as_attachment=True, download_name=path.name, mimetype=mimetype)
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
