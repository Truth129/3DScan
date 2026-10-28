"""Bounded file hashing and atomic publication of derived artifacts."""

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import tempfile


def hash_file(path: Path) -> str:
    """Hash complete file bytes without loading an image or model into memory."""
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


@contextmanager
def atomic_destination(path):
    """Publish a complete sibling file, preserving the destination on failure."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".writing", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        yield temporary
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
