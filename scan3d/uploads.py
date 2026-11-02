"""Resumable browser imports that become a case only after every file is complete.

Starlette parses at most 1,000 files per multipart form, and one request for a
large study offers no progress or retry. A browser instead sends bounded,
ordered batches of whole files and byte ranges of larger ones. Bytes already
received must match exactly, so a retried batch cannot duplicate or alter a
file. Staged uploads stay outside the case library until finished; abandoned
uploads are discarded after a day without activity.
"""

from contextlib import contextmanager
import fcntl
import hashlib
import json
import re
import shutil
import time
import zipfile

from . import identity
from .errors import NotFound
from .fileio import atomic_json, extract_zip, safe_relative
from .store import identifier, now, safe_id

MAX_PARTS = 256
_STALE_SECONDS = 24 * 3600
_BLOCK = 1024 * 1024


class ImportConflict(ValueError):
    """The staged upload no longer matches this request; restart the import."""


def _store_range(stream, length, path, offset, received):
    """Write length bytes at offset; any bytes already received must match."""
    with path.open("r+b" if path.exists() else "w+b") as output:
        output.seek(offset)
        position = offset
        while block := stream.read(min(_BLOCK, offset + length - position)):
            overlap = max(0, min(len(block), received - position))
            if overlap and output.read(overlap) != block[:overlap]:
                raise ValueError("Duplicate upload filename")
            output.write(block[overlap:])
            position += len(block)
    if position != offset + length:
        raise ValueError("An upload part ended before its declared length")


def upload_owner(root, upload_id):
    """Return (found, owner_id, case_id) for the access gate. A malformed ID is simply not found."""
    try:
        state = json.loads((root / safe_id(upload_id) / "state.json").read_text())
    except (OSError, ValueError):
        return False, None, None
    return True, state.get("owner_id"), state.get("case_id")


def _progress(upload_id, state):
    return {"id": upload_id, **{key: state[key] for key in ("received_files", "total_files", "received_bytes", "total_bytes")}}


class Uploads:
    def __init__(self, service):
        self.service = service
        self.root = service.settings.data_dir / "import-uploads"
        # An abandoned upload holds scans, which should not wait for someone to start another.
        self._discard_stale()

    @contextmanager
    def _locked(self, upload_id):
        directory = self.root / safe_id(upload_id)
        try:
            lock = (directory / "lock").open("a")
        except FileNotFoundError:
            raise NotFound("Upload not found") from None
        with lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                # Re-read after waiting: a concurrent cancel may have removed it.
                state = json.loads((directory / "state.json").read_text())
            except FileNotFoundError:
                raise NotFound("Upload not found") from None
            yield directory, state

    def _discard_stale(self):
        """Remove idle uploads; one still in use holds its lock and is kept."""
        cutoff = time.time() - _STALE_SECONDS
        for directory in self.root.glob("*"):
            if not re.fullmatch(r"[a-f0-9]{32}", directory.name):
                continue
            try:
                if (directory / "state.json").stat().st_mtime > cutoff:
                    continue
                lock = (directory / "lock").open("a")
            except FileNotFoundError:
                continue
            with lock:
                try:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                shutil.rmtree(directory, ignore_errors=True)

    def start(self, label, total_files, total_bytes):
        if total_bytes > self.service.settings.upload_limit:
            raise ValueError("Upload exceeds import limit")
        self._discard_stale()
        upload_id = identifier()
        directory = self.root / upload_id
        (directory / "files").mkdir(parents=True)
        (directory / "pending").mkdir()
        state = dict(
            schema_version=1,
            label=label,
            total_files=total_files,
            total_bytes=total_bytes,
            received_files=0,
            received_bytes=0,
            expanded_bytes=0,
            case_id=None,
            owner_id=identity.actor_id(),
            created_at=now(),
            updated_at=now(),
        )
        atomic_json(directory / "state.json", state)
        return {**_progress(upload_id, state), "max_parts": MAX_PARTS}

    def receive(self, upload_id, manifest, parts):
        """Store one batch; parts are (stream, length) pairs in manifest order."""
        if len(parts) != len(manifest):
            raise ValueError("Each upload part needs exactly one manifest entry")
        if len(parts) > MAX_PARTS:
            raise ValueError(f"Send at most {MAX_PARTS} upload parts per request")
        limit = self.service.settings.upload_limit
        with self._locked(upload_id) as (directory, state):
            if state["case_id"]:
                raise ImportConflict("This upload has already finished")
            files, pending = directory / "files", directory / "pending"
            try:
                for entry, (stream, length) in zip(manifest, parts):
                    relative = safe_relative(entry["path"])
                    offset, size = entry["offset"], entry["size"]
                    if offset + length > size:
                        raise ValueError("An upload part extends beyond its file")
                    target = files / relative
                    if target.exists():
                        # A retried batch may resend finished files; any other reuse is a duplicate.
                        if not target.is_file() or target.stat().st_size != size:
                            raise ValueError("Duplicate upload filename")
                        _store_range(stream, length, target, offset, size)
                        continue
                    part = pending / hashlib.sha256(relative.as_posix().encode()).hexdigest()
                    received = part.stat().st_size if part.exists() else 0
                    if offset > received:
                        raise ImportConflict("Upload parts arrived out of order; restart the import")
                    added = max(0, offset + length - received)
                    if state["received_bytes"] + added > state["total_bytes"]:
                        raise ValueError("The upload is larger than its declared size")
                    if state["received_bytes"] + state["expanded_bytes"] + added > limit:
                        raise ValueError("Upload exceeds import limit")
                    try:
                        _store_range(stream, length, part, offset, received)
                    finally:
                        state["received_bytes"] += (part.stat().st_size - received) if part.exists() else 0
                    if part.stat().st_size < size:
                        continue
                    if state["received_files"] >= state["total_files"]:
                        raise ValueError("The upload contains more files than declared")
                    try:
                        target.parent.mkdir(parents=True, exist_ok=True)
                    except (FileExistsError, NotADirectoryError):
                        raise ValueError("A file and a folder in this upload share a name") from None
                    part.replace(target)
                    state["received_files"] += 1
                    if zipfile.is_zipfile(target):
                        extract_zip(target, files / (relative.stem + "_expanded"), limit - state["received_bytes"] - state["expanded_bytes"])
                        with zipfile.ZipFile(target) as archive:
                            state["expanded_bytes"] += sum(member.file_size for member in archive.infolist())
            finally:
                state["updated_at"] = now()
                atomic_json(directory / "state.json", state)
            return _progress(upload_id, state)

    def finish(self, upload_id):
        """Publish a complete upload as a case; repeating the request returns that case."""
        with self._locked(upload_id) as (directory, state):
            if state["case_id"]:
                case = self.service.store.get("cases", state["case_id"])
                if case["status"] == "import_failed":
                    raise ValueError(case.get("error") or "The import failed")
                if case["status"] != "importing":
                    return case
                # Indexing was interrupted, for example by a restart; repeat it.
            else:
                if state["received_files"] != state["total_files"] or state["received_bytes"] != state["total_bytes"] or any((directory / "pending").iterdir()):
                    raise ImportConflict(f"The upload is incomplete: {state['received_files']:,} of {state['total_files']:,} files were received")
                case = self.service.new_case(state["label"])
                shutil.move(directory / "files", self.service.store.case_dir(case["id"]) / "dicom")
                state.update(case_id=case["id"], updated_at=now())
                atomic_json(directory / "state.json", state)
                shutil.rmtree(directory / "pending", ignore_errors=True)
            try:
                return self.service.finish_import(case, self.service.store.case_dir(case["id"]) / "dicom")
            except Exception as exc:
                case.update(status="import_failed", error=str(exc))
                self.service.store.save("cases", case)
                raise

    def cancel(self, upload_id):
        """Discard an unfinished upload and its staged files."""
        with self._locked(upload_id) as (directory, state):
            if state["case_id"]:
                raise ImportConflict("This upload has already finished")
            shutil.rmtree(directory)
        return {"id": upload_id, "status": "cancelled"}
