"""Reusable gzip siblings for immutable viewer assets, with bounded file IO."""

import fcntl
import gzip
import json
import os
from pathlib import Path
import shutil

from .fileio import atomic_destination, atomic_json


_VERSION = 1


def _signature(stat):
    return {"device": stat.st_dev, "inode": stat.st_ino, "bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns, "ctime_ns": stat.st_ctime_ns}


def _cached(path, compressed):
    try:
        # Metadata is small and local; a damaged entry must not trigger an
        # unbounded read or require hashing an entire GLB on every request.
        with compressed.with_name(compressed.name + ".json").open() as stream:
            metadata = json.loads(stream.read(4096))
        expected = {"version": _VERSION, "source": _signature(path.stat()), "gzip": _signature(compressed.stat())}
        return compressed if metadata == expected else None
    except (OSError, ValueError):
        return None


def cached_gzip(path: Path) -> Path | None:
    """Prepare or reuse a deterministic gzip sibling; IO failures leave raw serving available.

    Source and output file identities reject replaced, changed or interrupted
    cache entries. The lock serializes threads and processes without retaining
    per-file Python locks. Only the first cold request reads and compresses the
    source; warm requests inspect small metadata and filesystem attributes.
    """
    path = Path(path)
    compressed = path.with_name(path.name + ".gz")
    try:
        if _cached(path, compressed) is not None:
            return compressed
        with compressed.with_name(compressed.name + ".lock").open("a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                if _cached(path, compressed) is not None:
                    return compressed
                with path.open("rb") as source:
                    identity = _signature(os.fstat(source.fileno()))
                    with atomic_destination(compressed) as temporary:
                        with temporary.open("wb") as output:
                            with gzip.GzipFile(filename="", mode="wb", compresslevel=5, fileobj=output, mtime=0) as encoded:
                                shutil.copyfileobj(source, encoded, 1024 * 1024)
                        if _signature(os.fstat(source.fileno())) != identity or _signature(path.stat()) != identity:
                            raise OSError("Source changed during gzip preparation")
                if _signature(path.stat()) != identity:
                    return None
                atomic_json(compressed.with_name(compressed.name + ".json"), {"version": _VERSION, "source": identity, "gzip": _signature(compressed.stat())})
                return _cached(path, compressed)
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)
    except OSError:
        # Compression is optional: a full/read-only cache must not hide the GLB.
        return None
