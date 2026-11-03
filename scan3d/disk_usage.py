"""What the library takes on disk, measured on each request.

Nothing is cached, because several writes reach a case folder with no job to signal them. A folder
sum is honest: masks and CT files are shared by path only within one case, and imports copy or move.
Never call `Store.case_dir` here, because it creates the folder.
"""

import os
import shutil
import stat

SHARED_CACHES = ("review-cache", "surface-cache", "analysis-cache", "agreement-cache", "cinematic-cache")
# Rebuilt when next needed, so Clear caches may empty them. surface-cache/assets is not: a read answers 503.
REBUILT_ON_A_MISS = ("analysis-cache", "agreement-cache", "surface-cache/contours")


def tree_bytes(path):
    """Allocated bytes, as `du` counts them. Each file once, no link followed, and anything that vanishes is skipped."""
    total, linked, pending = 0, set(), [path]
    while pending:
        current = pending.pop()
        try:
            info = os.lstat(current)
            if stat.S_ISDIR(info.st_mode):
                pending.extend(os.path.join(current, name) for name in os.listdir(current))
            elif info.st_nlink > 1:
                if (info.st_dev, info.st_ino) in linked:
                    continue
                linked.add((info.st_dev, info.st_ino))
        except OSError:
            continue
        total += info.st_blocks * 512
    return total


def case_sizes(root, case_ids):
    return {case_id: tree_bytes(root / "cases" / case_id) for case_id in case_ids}


def storage_report(store, root, scope):
    rows = store.storage_rows(scope)
    sizes = case_sizes(root, [row["id"] for row in rows])
    for row in rows:
        row["size"] = sizes[row["id"]]
    others = [path for path in root.iterdir() if path.name not in (*SHARED_CACHES, "cases")]
    others += [path for path in (root / "cases").glob("*") if path.name not in sizes]
    usage = shutil.disk_usage(root)
    totals = {
        "cases": sum(row["size"] for row in rows if not row["trashed_at"]),
        "trash": sum(row["size"] for row in rows if row["trashed_at"]),
        "shared_caches": sum(tree_bytes(root / name) for name in SHARED_CACHES),
        "other": sum(tree_bytes(path) for path in others),
    }
    # Another owner's case gives its owner, date and size. Its ID and Trash state stay theirs.
    shown = [row if row["label"] is not None else {key: row[key] for key in ("label", "owner", "created_at", "size")} for row in rows]
    return {"cases": shown, "totals": totals, "disk": {"total": usage.total, "free": usage.free}}
