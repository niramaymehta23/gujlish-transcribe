"""One stored copy of each recording. Every file's SHA-256 fingerprint is kept in recordings/.fingerprints.json, so
an upload identical to a stored recording reuses that file (the browser can even skip uploading it), and copies
stored before this existed can be merged."""

import hashlib
import json
import re
from pathlib import Path
from threading import Lock

import asr.storage as storage
from asr.textutil import write_json

INDEX = ".fingerprints.json"
FINGERPRINT = re.compile(r"[0-9a-f]{64}")
_lock = Lock()


def fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024**2), b""):
            digest.update(block)
    return digest.hexdigest()


def fingerprints() -> dict[str, str]:
    """File name -> fingerprint for every stored recording, hashing only files added or changed since last time."""
    with _lock:
        path = storage.RECORDINGS / INDEX
        try:
            index = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            index = {}
        current = {}
        files = storage.RECORDINGS.iterdir() if storage.RECORDINGS.is_dir() else []
        for file in files:
            if file.name.startswith(".") or not file.is_file():
                continue
            stat = file.stat()
            entry = index.get(file.name)
            if not entry or (entry.get("size"), entry.get("mtime_ns")) != (stat.st_size, stat.st_mtime_ns):
                entry = {"sha256": fingerprint(file), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
            current[file.name] = entry
        if current != index:
            write_json(path, current)
        return {name: entry["sha256"] for name, entry in current.items()}


def find(value: str, exclude: str | None = None) -> Path | None:
    """The stored recording with this fingerprint, if there is one (other than the file named `exclude`)."""
    names = sorted(name for name, stored in fingerprints().items() if stored == value and name != exclude)
    return storage.RECORDINGS / names[0] if names else None
