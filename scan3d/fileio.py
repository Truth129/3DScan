"""Bounded file hashing, safe archive extraction and atomic publication of derived artifacts."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import zipfile


def hash_file(path: Path) -> str:
    """Hash complete file bytes without loading an image or model into memory."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def code_fingerprint():
    """Identify the exact package source that produced a revision."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


@contextmanager
def atomic_destination(path, suffix=".writing"):
    """Publish a complete sibling file, preserving the destination on failure.

    Writers that infer a format from the name (nibabel) pass a matching suffix.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=suffix, dir=path.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        yield temporary
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_text(path, text):
    with atomic_destination(path) as temporary:
        temporary.write_text(text)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, allow_nan=False))


def safe_relative(name):
    path = Path(name.replace("\\", "/"))
    if path.is_absolute() or any(p in ("..", "") for p in path.parts) or not path.parts:
        raise ValueError("Unsafe archive or upload filename")
    return path


def extract_zip(archive, target, limit):
    with zipfile.ZipFile(archive) as z:
        entries = z.infolist()
        if len(entries) > 100_000 or sum(e.file_size for e in entries) > limit:
            raise ValueError("Archive exceeds expanded import limit")
        for entry in entries:
            path = target / safe_relative(entry.filename)
            if (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Archive symlinks are not supported")
            if entry.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                if path.exists():
                    raise ValueError("Duplicate archive filename")
                with z.open(entry) as src, path.open("wb") as dst:
                    shutil.copyfileobj(src, dst, 1024 * 1024)
