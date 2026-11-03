"""Delete forever's file steps and Clear caches, under `maintenance.lock`.

Step 1 of a purge is one transaction in `Store.purge_case`. Every step here can run again, so a
crash, a busy lock or a refusal only means the next pass does it. Nothing here calls
`Store.case_dir`, which would make the folder again.
"""

from contextlib import ExitStack, contextmanager
import errno
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading

from .disk_usage import REBUILDABLE, REBUILT_ON_A_MISS, tree_bytes

log = logging.getLogger(__name__)

MAINTENANCE_LOCK = "maintenance.lock"
# Every review-volume writer, in any API process, holds one of these.
GENERATION_LOCKS = ("generation-full.lock", "generation-preview.lock", "generation-volume-mask-union.lock")
_KEY = re.compile(r"[0-9a-f]{64}")


class Unavailable(Exception):
    """Something still holds a file this step must remove, so the next pass tries again."""


@contextmanager
def maintenance_lock(data_dir, *, exclusive):
    """Shared while reading every file or writing shared caches with no job or request; exclusive while deleting.

    Shared waits, so a backup starts after a deletion. Exclusive never waits, so a deletion never stalls
    behind a long backup: it yields False instead.
    """
    with (Path(data_dir) / MAINTENANCE_LOCK).open("a+b") as stream:
        if os.geteuid() == 0:
            # As SQLite does for its -wal and -shm files: one a `sudo` run makes must still open for the service.
            owner = Path(data_dir).stat()
            os.fchown(stream.fileno(), owner.st_uid, owner.st_gid)
        try:
            fcntl.flock(stream, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


@contextmanager
def generation_locks(data_dir):
    """All three review-cache generation locks, or none: yields False if any is held."""
    directory = Path(data_dir) / "review-cache"
    directory.mkdir(parents=True, exist_ok=True)
    with ExitStack() as stack:
        for name in GENERATION_LOCKS:
            stream = stack.enter_context((directory / name).open("a+b"))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            stack.callback(fcntl.flock, stream, fcntl.LOCK_UN)
        yield True


def remove_tree(path):
    """Remove a file or folder, never following a link to it. Already gone is fine.

    A writer adding files makes rmtree fail with ENOTEMPTY, so try twice more.
    """
    path = Path(path)
    for attempt in range(3):
        try:
            if path.is_symlink() or not path.is_dir():
                path.unlink(missing_ok=True)
            else:
                shutil.rmtree(path)
            return
        except FileNotFoundError:
            return
        except OSError as exc:
            if exc.errno != errno.ENOTEMPTY or attempt == 2:
                raise


def _folders(path):
    try:
        return [child for child in Path(path).iterdir() if child.is_dir() and not child.is_symlink()]
    except FileNotFoundError:
        return []


def _owner(record):
    owner = record.get("case_id") if isinstance(record, dict) else None
    return owner if isinstance(owner, str) else None


def review_entries(data_dir):
    """Each review-volume folder with its record, or None for a leftover `.<key>.*` or one that cannot be read."""
    for shard in _folders(Path(data_dir) / "review-cache" / "volumes"):
        for entry in shard.iterdir():
            record = None
            if not entry.name.startswith("."):
                try:
                    record = json.loads((entry / "manifest.json").read_text())
                except (OSError, ValueError):
                    pass
            yield entry, record if isinstance(record, dict) else None


def remove_case_files(data_dir, case_ids):
    """Steps 2 and 3 for these cases: each folder, the staged uploads that became it, and its review volumes.

    Returns the cases with nothing left. A file this process cannot remove, such as one a `sudo` run wrote,
    keeps only its own case waiting.
    """
    data_dir, failed = Path(data_dir), set()

    def remove(case_id, path, remover=remove_tree):
        try:
            remover(path)
        except OSError as exc:
            log.warning("Deleted case %s: could not remove %s (%s)", case_id, path.name, exc.strerror)
            failed.add(case_id)

    for case_id in case_ids:
        remove(case_id, data_dir / "cases" / case_id)
    for folder in _folders(data_dir / "import-uploads"):
        try:
            state = json.loads((folder / "state.json").read_text())
        except (OSError, ValueError):
            continue
        if _owner(state) in case_ids:
            remove(_owner(state), folder, _remove_upload)
    for entry, record in review_entries(data_dir):
        if _owner(record) in case_ids:
            remove(_owner(record), entry)
    return set(case_ids) - failed


def _remove_upload(folder):
    """Under the upload's own lock, as `Uploads._discard_stale` takes it."""
    try:
        lock = (folder / "lock").open("a")
    except FileNotFoundError:
        return
    with lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Unavailable(folder.name) from None
        remove_tree(folder)


def _remove_shared(path):
    """Remove one shared-cache item, or log why not and return False, so the items and steps after it still run."""
    try:
        remove_tree(path)
    except OSError as exc:
        # Its name is a key or a cache folder's, never a label.
        log.warning("Could not remove %s from the shared caches (%s)", path.name, exc.strerror)
        return False
    return True


def sweep_after_purge(data_dir, live_cases, live_keys):
    """Steps 4 and 5, which can touch any case, so the caller holds every guard in `Service.idle`. True if everything went."""
    data_dir = Path(data_dir)
    swept = True
    # An entry that names no current case was written before entries named one, by an older build, or by a crash.
    for entry, record in review_entries(data_dir):
        if _owner(record) not in live_cases:
            swept &= _remove_shared(entry)
    for name in REBUILT_ON_A_MISS:
        swept &= _remove_shared(data_dir / name)
    return sweep_surface_assets(data_dir / "surface-cache" / "assets", live_keys) and swept


def sweep_surface_assets(directory, live_keys):
    """Remove every file of an asset no revision or draft names. A read answers 503 rather than rebuilding, so never more.

    Files are `<key>.glb` and its siblings, or `.<key>....writing` while written, so the key is the first 64
    characters once one leading dot goes. Lock files live in `locks/` and stay. True if every such file went.
    """
    swept = True
    for shard in _folders(directory):
        for item in shard.iterdir():
            key = item.name.removeprefix(".")[:64]
            if _KEY.fullmatch(key) and key not in live_keys:
                swept &= _remove_shared(item)
    return swept


def empty_rebuildable(data_dir):
    """Clear caches: every review volume and the caches rebuilt on a miss. Returns the bytes freed."""
    data_dir = Path(data_dir)
    freed = sum(tree_bytes(data_dir / name) for name in REBUILDABLE)
    review = data_dir / "review-cache"
    for child in review.iterdir() if review.is_dir() else ():
        # A holder of a generation lock must keep the same file.
        if not child.name.endswith(".lock"):
            remove_tree(child)
    for name in REBUILT_ON_A_MISS:
        remove_tree(data_dir / name)
    return freed


class Janitor:
    """Finishes each purge: files, shared caches, the write log, then `purged`. One thread, under `worker.lock`."""

    def __init__(self, service):
        self.service = service
        self.wake = threading.Event()
        self.returned_checked = False

    def run(self):
        try:
            while not self.service.stop_event.is_set():
                try:
                    waiting = self.run_pass()
                except Exception:
                    log.exception("A janitor pass failed")
                    waiting = True
                self.wake.wait(5 if waiting else 60)
                self.wake.clear()
        finally:
            self.service._lane_finished()

    def run_pass(self):
        """One attempt at every purge in progress. True while one waits on a lock or for idleness, so the next pass comes soon."""
        service = self.service
        store, data_dir = service.store, service.settings.data_dir
        purging = store.trash_case_ids("purging")
        # Once a run: a command-line write or a restored backup can bring a purged case's folder back.
        returned = [] if self.returned_checked else store.trash_case_ids("purged")
        if not purging and not returned:
            self.returned_checked = True
            return False
        with maintenance_lock(data_dir, exclusive=True) as held:
            if not held:
                return True
            try:
                removed = remove_case_files(data_dir, {*purging, *returned})
            except Unavailable:
                return True
            self.returned_checked = True
            # Step 1's rows leave the database files now, not only once nothing runs anywhere. It changes no row.
            store.checkpoint()
            ready = [case_id for case_id in purging if case_id in removed]
            if not ready:
                # Nothing to finish, or only cases whose files could not be removed: those wait a minute.
                return False
            with service.idle() as refusal:
                if refusal:
                    return True
                swept = sweep_after_purge(data_dir, store.existing_case_ids(), store.live_surface_keys())
            if not swept:
                # A shared file the service cannot remove, logged above: these cases wait a minute, as a stuck case does.
                return False
            if not store.checkpoint():
                return True
            store.finish_purge(ready)
        service.release_engine.set()
        return False
