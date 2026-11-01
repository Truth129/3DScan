"""Point-in-time backups of the case library that later runs update incrementally.

The SQLite snapshot comes first. Records are committed only after their files
are complete, so every file the snapshot references already exists when the
folders are copied afterwards; newer extra files are harmless. Files unchanged
since the previous backup in the same destination are hard-linked to it, never
to live data. Caches the service rebuilds on demand are skipped. Records store
absolute paths, so restore into the same SCAN3D_DATA_DIR.
"""

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import sys

from . import __version__

DATABASE = "scan3d.sqlite3"
REBUILT_ON_DEMAND = ("review-cache", "analysis-cache", "cinematic-cache", "import-uploads", "orthanc-staging")
_SNAPSHOT = re.compile(r"scan3d-\d{8}T\d{6}Z")


def _latest(destination):
    snapshots = [entry for entry in destination.iterdir() if _SNAPSHOT.fullmatch(entry.name) and (entry / "manifest.json").is_file()]
    return max(snapshots, key=lambda entry: entry.name, default=None)


def _copy(source, target, previous, counts):
    stat = source.stat()
    try:
        prior = previous.stat() if previous else None
    except FileNotFoundError:
        prior = None
    if prior and prior.st_size == stat.st_size and prior.st_mtime_ns == stat.st_mtime_ns:
        try:
            os.link(previous, target)
            counts["linked_files"] += 1
            counts["linked_bytes"] += stat.st_size
            return
        except OSError:
            pass  # Another filesystem or a link limit: copy instead.
    shutil.copy2(source, target)
    counts["copied_files"] += 1
    counts["copied_bytes"] += stat.st_size


def _copy_tree(source, target, previous, counts):
    for directory, folders, files in os.walk(source):
        relative = Path(directory).relative_to(source)
        (target / relative).mkdir(parents=True, exist_ok=True)
        folders[:] = [name for name in folders if not (Path(directory) / name).is_symlink()]
        for name in files:
            item = Path(directory) / name
            if item.is_symlink():
                counts["skipped_symlinks"] += 1
                continue
            _copy(item, target / relative / name, previous / relative / name if previous else None, counts)


def backup(data_dir, destination, log=lambda message: print(message, file=sys.stderr)):
    """Write DESTINATION/scan3d-<UTC time>/ and return its manifest."""
    data_dir, destination = Path(data_dir).resolve(), Path(destination).resolve()
    if destination == data_dir or data_dir in destination.parents:
        raise ValueError("Choose a backup destination outside the data directory")
    if not (data_dir / DATABASE).is_file():
        raise ValueError("The data directory has no 3DScan database")
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    previous, target = _latest(destination), destination / f"scan3d-{stamp}"
    partial = destination / f"scan3d-{stamp}.partial"
    try:
        partial.mkdir()
    except FileExistsError:
        raise ValueError("A backup started in this second already exists; retry") from None
    try:
        log("Snapshotting the database")
        # Read-only: never checkpoints or otherwise writes the live database.
        connection = sqlite3.connect(f"{(data_dir / DATABASE).as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            connection.execute("VACUUM INTO ?", (str(partial / DATABASE),))
        finally:
            connection.close()
        check = sqlite3.connect(partial / DATABASE)
        try:
            integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if integrity != "ok":
            raise RuntimeError(f"The database snapshot failed its integrity check: {integrity}")
        counts = dict(copied_files=0, copied_bytes=0, linked_files=0, linked_bytes=0, skipped_symlinks=0)
        included, skipped = [DATABASE], []
        for entry in sorted(data_dir.iterdir()):
            if entry.name.startswith(DATABASE):
                continue  # Snapshotted above, including any write-ahead log.
            if entry.name in REBUILT_ON_DEMAND or entry.name.endswith(".lock") or entry.is_symlink() or not (entry.is_dir() or entry.is_file()):
                skipped.append(entry.name)
                continue
            log(f"Copying {entry.name}")
            prior = previous / entry.name if previous else None
            if entry.is_dir():
                _copy_tree(entry, partial / entry.name, prior, counts)
            else:
                _copy(entry, partial / entry.name, prior, counts)
            included.append(entry.name)
        digest = hashlib.sha256((partial / DATABASE).read_bytes()).hexdigest()
        manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scan3d_version": __version__,
            "data_dir": str(data_dir),
            "snapshot": target.name,
            "previous": previous.name if previous else None,
            "database": {"sha256": digest, "integrity_check": integrity},
            "included": included,
            "skipped": skipped,
            "files": counts,
            "restore": "Stop the service, then copy this snapshot's contents into the same data_dir. Skipped caches rebuild on demand.",
        }
        (partial / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        partial.rename(target)
        return manifest
    except BaseException:
        shutil.rmtree(partial, ignore_errors=True)
        raise
