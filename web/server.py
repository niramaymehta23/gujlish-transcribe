"""A local upload server with one ASR subprocess at a time."""

import json
import subprocess
import sys
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from pathlib import Path
from threading import Lock
from urllib.parse import urlparse
from uuid import uuid4

from flask import Flask, abort, jsonify, render_template, request, send_file

from asr.storage import RECORDINGS, ROOT, TRANSCRIPTS, new_run


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


def transcript_docx(text: str, title: str) -> BytesIO:
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
    for paragraph in text.splitlines() or [""]:
        document.add_paragraph(paragraph)
    buffer = BytesIO()
    document.save(buffer)
    buffer.seek(0)
    return buffer


def create_app(*, executor=None) -> Flask:
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 4 * 1024**3
    jobs = {}
    lock = Lock()
    pool = executor or ThreadPoolExecutor(max_workers=1, thread_name_prefix="asr")
    app.extensions["asr_jobs"] = jobs

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
                    logs = logs[-12:]
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

    @app.before_request
    def local_requests():
        # Local use only. Reject cross-site upload/POST requests from other pages.
        if request.method == "POST" and request.headers.get("Origin"):
            if urlparse(request.headers["Origin"]).netloc != request.host:
                abort(403)

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.post("/api/jobs")
    def create_job():
        model, mode = request.form.get("model", "flex"), request.form.get("mode", "mixed")
        if model not in {"flex", "gujlish"} or mode not in {"mixed", "native"}:
            return jsonify(error="Choose a valid model and output mode."), 400
        upload = request.files.get("audio")
        if not upload or not upload.filename:
            return jsonify(error="Choose an audio file first."), 400
        # Preserve Gujarati/English filenames, stripping paths and unsafe characters.
        raw_name = unicodedata.normalize("NFC", Path(upload.filename.replace("\\", "/")).name)
        filename = "".join(
            char if char in " .()-_" or unicodedata.category(char)[0] in "LNM" else "_" for char in raw_name
        ).strip(" .")[:180]
        if not filename:
            return jsonify(error="Choose a file with a valid filename."), 400
        RECORDINGS.mkdir(parents=True, exist_ok=True)
        source = RECORDINGS / filename
        base = source
        suffix = 2
        while True:
            try:
                with source.open("xb") as target:
                    upload.save(target)
                break
            except FileExistsError:
                source = base.with_name(f"{base.stem}-{suffix}{base.suffix}")
                suffix += 1
        try:
            duration = probe_audio(source)
        except (ValueError, OSError, subprocess.TimeoutExpired) as error:
            source.unlink(missing_ok=True)
            return jsonify(error=str(error)), 400
        run_dir = new_run(source, model, mode)
        job_id = uuid4().hex
        with lock:
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
            }
        pool.submit(process, job_id, source, run_dir, model, mode)
        return jsonify(id=job_id), 202

    @app.get("/api/jobs/<job_id>")
    def job_status(job_id):
        with lock:
            if job_id not in jobs:
                abort(404)
            result = {k: v for k, v in jobs[job_id].items() if k not in {"source", "run_dir", "txt", "json"}}
            txt = jobs[job_id].get("txt")
        if result["status"] == "complete":
            result["text"] = Path(txt).read_text(encoding="utf-8")
        return jsonify(result)

    @app.post("/api/jobs/<job_id>/retry")
    def retry_job(job_id):
        with lock:
            job = jobs.get(job_id)
            if not job:
                abort(404)
            if job["status"] != "error":
                return jsonify(error="Only a failed run can be retried."), 409
            job.update(status="queued", message="Queued to resume completed chunks")
            values = (Path(job["source"]), Path(job["run_dir"]), job["model"], job["mode"])
        pool.submit(process, job_id, *values)
        return jsonify(id=job_id), 202

    @app.get("/api/jobs/<job_id>/download/<kind>")
    def download(job_id, kind):
        with lock:
            job = dict(jobs.get(job_id, {}))
        if job.get("status") != "complete":
            abort(404)
        txt = Path(job["txt"])
        if kind == "txt":
            return send_file(txt, as_attachment=True, download_name=txt.name, mimetype="text/plain; charset=utf-8")
        if kind == "docx":
            return send_file(
                transcript_docx(txt.read_text(encoding="utf-8"), txt.stem),
                as_attachment=True,
                download_name=txt.with_suffix(".docx").name,
                mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        abort(404)

    @app.get("/api/history")
    def history():
        # Re-open saved runs after restarting the server without a database.
        completed = []
        paths = sorted(TRANSCRIPTS.glob("*/*/*/*.json"), key=lambda path: path.parent.name, reverse=True)
        for path in paths:
            if path.name.startswith(".") or not path.with_suffix(".txt").exists():
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                import hashlib

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
        return jsonify(error="This file exceeds the 4 GB upload limit."), 413

    return app
