"""Delete forever's file steps and Clear caches, under `maintenance.lock`.

Step 1 of a purge is one transaction in `Store.purge_case`. Every step here can run again, so a
crash, a busy lock or a refusal only means the next pass does it. Nothing here calls
`Store.case_dir`, which would make the folder again.
"""

from contextlib import contextmanager
import fcntl
from pathlib import Path

MAINTENANCE_LOCK = "maintenance.lock"


@contextmanager
def maintenance_lock(data_dir, *, exclusive):
    """Shared while reading every file or writing shared caches with no job or request; exclusive while deleting.

    Shared waits, so a backup starts after a deletion. Exclusive never waits, so a deletion never stalls
    behind a long backup: it yields False instead.
    """
    with (Path(data_dir) / MAINTENANCE_LOCK).open("a+b") as stream:
        try:
            fcntl.flock(stream, (fcntl.LOCK_EX | fcntl.LOCK_NB) if exclusive else fcntl.LOCK_SH)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)
