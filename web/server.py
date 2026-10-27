"""A local upload server that runs one worker subprocess at a time (transcribe, translate or summarize)."""

import hashlib
import json
import re
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

from asr.llm import LocalLLM, claude_available, gemini_available
from asr.storage import RECORDINGS, ROOT, TRANSCRIPTS, new_run, stage_paths

MODEL_ID = re.compile(r"[\w.:/@+-]{1,200}")
LOG_TAIL_LINES = 12  # recent subprocess output kept for error messages


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
                return {"status": "complete", "progress": 100, "message": "Ready", "llm": state.get("llm")}
        return state or {"status": "idle"}

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
            job = dict(jobs[job_id])
        hidden = {"source", "run_dir", "txt", "json", "translation", "summary"}
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

    @app.get("/api/capabilities")
    def capabilities():
        # What the translation/summary stages can use right now. API keys are read from the environment only.
        local = LocalLLM()
        return jsonify(
            claude=claude_available(),
            gemini=gemini_available(),
            local_url=local.base_url,
            local_models=local.models(),
        )

    def start_stage(job_id, key):
        body = request.get_json(silent=True) or {}
        provider, model = body.get("llm", "local"), body.get("model") or None
        if provider not in {"local", "claude", "gemini"}:
            return jsonify(error="Choose local, claude or gemini."), 400
        if model is not None and not (isinstance(model, str) and MODEL_ID.fullmatch(model)):
            return jsonify(error="That model name is not valid."), 400
        if provider == "claude" and not claude_available():
            return jsonify(error="The Claude API needs ANTHROPIC_API_KEY set where the server was started."), 400
        if provider == "gemini" and not gemini_available():
            return jsonify(error="The Gemini API needs GEMINI_API_KEY set where the server was started."), 400
        with lock:
            job = jobs.get(job_id)
            if not job:
                abort(404)
            if job.get("status") != "complete" or not job.get("json"):
                return jsonify(error="Finish the transcript first."), 409
            if key == "summary" and stage_view(job, "translation")["status"] != "complete":
                return jsonify(error="Translate the transcript before summarizing it."), 409
            if (job.get(key) or {}).get("status") in {"queued", "running"}:
                return jsonify(error="This step is already running."), 409
            job[key] = {
                "status": "queued",
                "progress": None,
                "message": "Waiting for the current run…",
                "llm": provider,
            }
            transcript_json = job["json"]
        script, provider_flag, model_flag = (
            ("translate.py", "--llm", "--llm-model")
            if key == "translation"
            else ("summarize.py", "--summary-llm", "--summary-model")
        )
        command = [sys.executable, str(ROOT / script), transcript_json, provider_flag, provider]
        if model:
            command.append(f"{model_flag}={model}")  # one argument, so a value can never be read as a flag
        command.append("--progress-json")
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
        completed = []
        paths = sorted(TRANSCRIPTS.glob("*/*/*/*.json"), key=lambda path: path.parent.name, reverse=True)
        for path in paths:
            if path.name.startswith(".") or not path.with_suffix(".txt").exists():
                continue
            try:
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
