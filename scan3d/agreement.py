"""Agreement between saved model outputs and the masks reviewers accepted.

Reports are read-only: inference outputs, corrections, revisions and provenance
are inputs, never working state, and the only writes are content-addressed cache
records. Inference outputs are write-once files at
``cases/<case>/inference/<cache key>/run-<id>/<task>/<label>.nii.gz``, so that
path identifies a structure's model original even in revisions that predate
operation metadata. The nearest such mask in the lineage is the original, which
makes a later run that replaced a structure its new original.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import logging
from pathlib import Path
import re
import statistics

import numpy as np
from scipy import ndimage as ndi
from scipy.spatial import cKDTree

from . import imaging, review_volume
from .store import safe_id

log = logging.getLogger(__name__)

_IMPLEMENTATION = "mask-agreement-v1"
# The aggregate counts a case only once a reviewer has decided its active revision.
COUNTED_STATUSES = ("reviewed", "corrected")
_RUN = re.compile(r"run-[a-f0-9]{32}")
_NAME = re.compile(r"[A-Za-z0-9_]+")
_FACES = ndi.generate_binary_structure(3, 1)
_METRICS = ("dice", "hd95_mm", "original_voxels", "current_voxels", "added_voxels", "removed_voxels")
_SKIPPED = ("no_active_revision", "no_segmentation", "unreviewed", "unavailable")
_REASONS = {
    "reviewer_created": "Added by a reviewer; there is no model output to compare.",
    "no_model_output": "No saved model output was found in this revision's history.",
    "model_output_unavailable": "The saved model output is unavailable; nothing was recomputed.",
    "different_image": "The model output does not belong to this revision's source image.",
    "unreadable": "These masks could not be compared on the reference grid.",
}


def _surface(mask):
    """Foreground voxels with a face neighbour outside the mask or the grid."""
    return mask & ~ndi.binary_erosion(mask, structure=_FACES, border_value=0)


def _hd95(original, current, matrix):
    """Symmetric 95th-percentile surface distance in patient millimetres.

    Each surface voxel centre's nearest centre on the other surface is found
    exactly through the full affine, so oblique, reflected, anisotropic and
    sheared grids need no per-axis spacing (a sheared grid has none; compare
    review_volume._margin_crop). The larger directed 95th percentile is the
    result. Centres shared by both surfaces are zero without a search.
    """
    first, second = _surface(original), _surface(current)
    shared = first & second
    percentiles = []
    for source, target in ((first, second), (second, first)):
        pending = np.argwhere(source & ~shared)
        distances = np.zeros(int(np.count_nonzero(source)), dtype=np.float64)
        if len(pending):
            distances[:len(pending)] = cKDTree(np.argwhere(target) @ matrix.T).query(pending @ matrix.T)[0]
        percentiles.append(float(np.percentile(distances, 95)))
    return max(percentiles)


def _compare(original, current, matrix):
    before, after = int(np.count_nonzero(original)), int(np.count_nonzero(current))
    overlap = int(np.count_nonzero(original & current))
    added, removed = after - overlap, before - overlap
    # Equal nonempty masks have coincident surfaces; empty ones have none.
    hd95 = None if not before or not after else 0.0 if not added and not removed else _hd95(original, current, matrix)
    return {"dice": 2 * overlap / (before + after) if before + after else 1.0, "hd95_mm": hd95,
            "original_voxels": before, "current_voxels": after, "added_voxels": added, "removed_voxels": removed}


def _with_volumes(metrics, voxel_ml):
    """Volumes follow imaging.analyze_mask: voxels x |det(affine)| / 1000 mL."""
    before, after = metrics["original_voxels"], metrics["current_voxels"]
    return {**metrics, "unchanged": not metrics["added_voxels"] and not metrics["removed_voxels"],
            "original_volume_ml": before * voxel_ml, "current_volume_ml": after * voxel_ml,
            "volume_change_ml": (after - before) * voxel_ml}


def _voxel_ml(affine):
    return abs(float(np.linalg.det(np.asarray(affine, dtype=np.float64)[:3, :3]))) / 1000


def mask_agreement(original, current, affine):
    """Compare two binary masks sampled on one reference grid.

    Dice is 2|A∩B| / (|A| + |B|), or 1 for two empty masks. HD95 is None when
    either mask is empty. Added/removed voxels are current-only/original-only.
    """
    original, current = np.asarray(original, dtype=bool), np.asarray(current, dtype=bool)
    if original.ndim != 3 or original.shape != current.shape:
        raise ValueError("Agreement masks must share one three-dimensional grid")
    affine = np.asarray(affine, dtype=np.float64)
    return _with_volumes(_compare(original, current, affine[:3, :3]), _voxel_ml(affine))


def _crop(path, image, affine):
    """Validate every voxel on the reference grid; keep only the foreground box."""
    values, _, _, nonempty = imaging._binary_values(Path(path), image, affine)
    if not nonempty:
        return None
    low, high = imaging._foreground_bounds(values)
    return low, np.ascontiguousarray(values[tuple(slice(int(a), int(b) + 1) for a, b in zip(low, high))], dtype=bool)


def _dense(original_path, current_path, image, affine):
    # Decode one source at a time; only the two foreground boxes outlive it.
    crops = [_crop(original_path, image, affine), _crop(current_path, image, affine)]
    present = [crop for crop in crops if crop is not None]
    low = np.min([origin for origin, _ in present], axis=0) if present else np.zeros(3, dtype=int)
    high = (np.max([origin + values.shape for origin, values in present], axis=0) if present
            else np.ones(3, dtype=int))

    def placed(crop):
        # Outside the union box both masks are empty, so its surfaces match the full grid's.
        result = np.zeros(tuple(int(n) for n in high - low), dtype=bool)
        if crop is not None:
            origin, values = crop
            result[tuple(slice(int(a), int(a) + n) for a, n in zip(origin - low, values.shape))] = values
        return result

    return _compare(placed(crops[0]), placed(crops[1]), affine[:3, :3])


# Hashed once at import: cache keys describe the code that is running, even if
# the file is edited before the service restarts.
_CODE_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def _payload_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _cached(path, key):
    try:
        record = json.loads(path.read_text())
        metrics = record["agreement"]
        if (isinstance(metrics, dict) and record["key"] == key and set(_METRICS) <= metrics.keys()
                and record["sha256"] == _payload_digest(metrics)):
            return metrics
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def _inside(path, root):
    try:
        return Path(path).resolve().relative_to(root).parts
    except (TypeError, ValueError, OSError):
        return None


def _saved_output(entry, root):
    """The run and task of a structure whose mask is its own saved model output."""
    parts = _inside(entry.get("mask_file"), root)
    if not parts or len(parts) != 4 or not _RUN.fullmatch(parts[1]) or not parts[3].endswith(".nii.gz"):
        return None
    task, label = parts[2], parts[3][:-7]
    if (not _NAME.fullmatch(task) or not _NAME.fullmatch(label) or entry["id"] != f"{task}:{label}"
            or entry.get("task", task) != task):
        return None
    return {"run_id": parts[1], "task": task}


def _phantom(revision, entry, root):
    """A demo phantom label plays the model's role, only in its synthetic case."""
    parts = _inside(entry.get("mask_file"), root)
    return (revision.get("source") == "synthetic_demo" and bool(revision.get("synthetic"))
            and not revision.get("parent_id") and bool(parts) and len(parts) == 1
            and parts[0].endswith(".nii.gz") and entry["id"] == f"synthetic:{parts[0][:-7]}")


def _run_details(revision, run_id):
    for run in [*(revision.get("segmentation_runs") or []), revision.get("inference_manifest") or {}]:
        if isinstance(run, dict) and run.get("run_id") == run_id and run.get("engine_version"):
            return {"engine_version": str(run["engine_version"])}
    return {}


class _Lineage:
    """Ancestors loaded on demand; parents are immutable, so a cycle is corrupt."""

    def __init__(self, store, revision):
        self.store, self.revisions, self.complete = store, [revision], True
        self.indices = [{entry["id"]: entry for entry in revision["structures"]}]

    def __getitem__(self, index):
        while index >= len(self.revisions) and self.complete:
            parent = self.revisions[-1].get("parent_id")
            if not parent:
                return None
            try:
                revision = self.store.revision(self.revisions[0]["case_id"], parent)
            except (KeyError, ValueError):
                revision = None
            if revision is None or any(item["id"] == revision["id"] for item in self.revisions):
                self.complete = False
                return None
            self.revisions.append(revision)
            self.indices.append({entry["id"]: entry for entry in revision["structures"]})
        return (self.revisions[index], self.indices[index]) if index < len(self.revisions) else None

    def origin(self, structure_id, roots, synthetic):
        """Nearest saved model output, then the earliest revision still holding it."""
        index = 0
        while (item := self[index]) is not None and structure_id in item[1]:
            revision, entry = item[0], item[1][structure_id]
            saved = _saved_output(entry, roots[0])
            if saved or (synthetic and _phantom(revision, entry, roots[1])):
                while (previous := self[index + 1]) is not None and (
                        previous[1].get(structure_id, {}).get("mask_file") == entry["mask_file"]):
                    index += 1
                introduced = self[index][0]
                if not saved:
                    return introduced, entry, {"kind": "synthetic_phantom", "revision_id": introduced["id"]}
                return introduced, entry, {"kind": "model_output", "revision_id": introduced["id"], **saved,
                                           **_run_details(introduced, saved["run_id"])}
            index += 1
        return None


def _same_file(first, second):
    try:
        return Path(first).resolve() == Path(second).resolve()
    except (TypeError, OSError):
        return False


def _count(entry):
    value = entry.get("voxel_count")
    return value if type(value) is int and value >= 0 else None


def _unchanged(count):
    """An identical mask needs no decode; its saved measurement supplies the volume."""
    return {"dice": 1.0, "hd95_mm": 0.0 if count else None, "original_voxels": count, "current_voxels": count,
            "added_voxels": 0, "removed_voxels": 0}


def _agreement(service, case, revision):
    store = service.store
    case_dir = store.directory / "cases" / safe_id(case["id"])
    roots = ((case_dir / "inference").resolve(), (case_dir / "synthetic").resolve())
    synthetic = bool(case.get("synthetic") or revision.get("synthetic"))
    geometry = revision["geometry"]
    affine = np.asarray(geometry["affine_ras"], dtype=np.float64)
    voxel_ml = _voxel_ml(affine)
    matrix = affine[:3, :3]
    spacing = np.linalg.norm(matrix, axis=0)
    lineage = _Lineage(store, revision)
    # A revision bound to no verifiable image is invalid; an unverifiable
    # ancestor only disqualifies the originals it holds.
    images = {revision["id"]: store.resolve_reference(revision)["id"]} if revision["structures"] else {}

    def image_id(record):
        if record["id"] not in images:
            try:
                images[record["id"]] = store.resolve_reference(record)["id"]
            except (KeyError, ValueError, OSError):
                images[record["id"]] = None
        return images[record["id"]]

    implementation = {"implementation": _IMPLEMENTATION, "code_sha256": _CODE_SHA256,
                      "geometry": {"shape": geometry["shape"], "affine_ras": geometry["affine_ras"]}}
    compared, excluded, pending = {}, {}, {}
    hits = 0
    for structure in revision["structures"]:
        sid = structure["id"]
        found = lineage.origin(sid, roots, synthetic)
        if found is None:
            custom = structure.get("task") == "custom" or sid.startswith("custom:")
            excluded[sid] = "reviewer_created" if custom else "no_model_output"
            continue
        introduced, original, origin = found
        if image_id(introduced) != images[revision["id"]]:
            excluded[sid] = "different_image"
            continue
        count = _count(structure)
        if count is not None and _same_file(original["mask_file"], structure["mask_file"]):
            compared[sid] = (origin, "same_file", _unchanged(count))
            continue
        try:
            digests = [review_volume._file_digest(original["mask_file"]), review_volume._file_digest(structure["mask_file"])]
        except (OSError, ValueError):
            excluded[sid] = "model_output_unavailable" if not Path(original["mask_file"]).is_file() else "unreadable"
            continue
        if digests[0] == digests[1] and count is not None:
            compared[sid] = (origin, "same_digest", _unchanged(count))
            continue
        key = _payload_digest({**implementation, "original": digests[0], "current": digests[1]})
        path = service.settings.data_dir / "agreement-cache" / key[:2] / f"{key}.json"
        metrics = _cached(path, key)
        if metrics is not None:
            hits += 1
            compared[sid] = (origin, "voxels", metrics)
        else:
            pending[sid] = (origin, original["mask_file"], structure["mask_file"], key, path)
    if pending:
        compared.update(_compute(service, revision, pending, excluded))
    structures, exclusions = [], []
    for structure in revision["structures"]:
        sid = structure["id"]
        identity = {"id": sid, "name": structure.get("name", sid), "task": structure.get("task"),
                    "group": structure.get("group")}
        if sid in compared:
            origin, basis, metrics = compared[sid]
            structures.append({**identity, "origin": origin, "basis": basis,
                               **_with_volumes({key: metrics[key] for key in _METRICS}, voxel_ml)})
        else:
            exclusions.append({**identity, "code": excluded[sid], "reason": _REASONS[excluded[sid]]})
    active = case.get("active_revision_id") == revision["id"]
    counted = active and revision.get("review_status") in COUNTED_STATUSES and bool(structures)
    return {"schema_version": 1, "case_id": case["id"], "revision_id": revision["id"],
            "review_status": revision.get("review_status"), "synthetic": synthetic, "active": active,
            "aggregate_pool": ("synthetic" if synthetic else "real") if counted else None,
            "implementation": _IMPLEMENTATION, "hd95_exact": True,
            "grid": {"voxel_volume_ml": voxel_ml, "spacing_mm": spacing.tolist(),
                     "sheared": not np.allclose(matrix.T @ matrix, np.diag(spacing ** 2), rtol=1e-6, atol=1e-8)},
            "summary": {"structures": len(revision["structures"]), "compared": len(structures),
                        "unchanged": sum(entry["unchanged"] for entry in structures),
                        "changed": sum(not entry["unchanged"] for entry in structures),
                        "excluded": len(exclusions), **_statistics(structures, counts=False)},
            "structures": structures, "excluded": exclusions,
            "cache": {"hits": hits, "computed": len(pending)}}


def _compute(service, revision, pending, excluded):
    """Decode changed pairs once; concurrent requests share one bounded lane."""
    from .service import atomic_json

    results = {}
    image, affine = imaging._image(Path(revision["ct_file"]))
    with (service.settings.data_dir / "agreement.lock").open("a") as lock:
        # Pairs hold two full-volume decodes at a time; one request's bounded
        # pool runs at once across API threads and processes.
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)

        def measure(item):
            sid, (origin, original, current, key, path) = item
            metrics = _cached(path, key)
            if metrics is None:
                try:
                    metrics = _dense(original, current, image, affine)
                except Exception:
                    # One unreadable mask must not hide the rest of the report.
                    log.exception("Agreement for %s could not be measured", sid)
                    return sid, None
                try:
                    atomic_json(path, {"key": key, "agreement": metrics, "sha256": _payload_digest(metrics)})
                except OSError:
                    log.warning("Agreement cache for %s was not saved", sid, exc_info=True)
            return sid, (origin, "voxels", metrics)

        workers = min(len(pending), service._preparation_workers(revision["geometry"]))
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="scan3d-agreement") as executor:
            for sid, result in executor.map(measure, pending.items()):
                if result is None:
                    excluded[sid] = "unreadable"
                else:
                    results[sid] = result
    return results


def _statistics(rows, counts=True):
    dice = [row["dice"] for row in rows]
    summary = {"dice_mean": statistics.fmean(dice) if dice else None,
               "dice_median": statistics.median(dice) if dice else None,
               "abs_volume_change_ml_mean": statistics.fmean(abs(row["volume_change_ml"]) for row in rows) if rows else None}
    if counts:
        unchanged = sum(row["unchanged"] for row in rows)
        summary = {"reviewed": len(rows), "unchanged": unchanged,
                   "unchanged_percent": 100 * unchanged / len(rows) if rows else None, **summary}
    return summary


def revision_agreement(service, case_id, revision_id):
    """Per-structure agreement of one accepted revision with its model originals."""
    case = service.store.get("cases", case_id)
    return _agreement(service, case, service.store.revision(case_id, revision_id))


def aggregate_agreement(service):
    """Pool each case's decided active revision by task and structure.

    The active revision is what the reviewer currently accepts; one marked
    unreviewed has no decision yet. Synthetic demo cases form their own pool and
    never enter the real one.
    """
    skipped = Counter({reason: 0 for reason in _SKIPPED})
    pools = {name: {"cases": 0, "excluded": 0, "rows": defaultdict(list)} for name in ("real", "synthetic")}
    revisions = []
    cases = service.store.list("cases")
    for case in cases:
        revision_id = case.get("active_revision_id")
        if not revision_id:
            skipped["no_active_revision"] += 1
            continue
        try:
            revision = service.store.revision(case["id"], revision_id)
            if not revision["structures"]:
                skipped["no_segmentation"] += 1
                continue
            if revision.get("review_status") not in COUNTED_STATUSES:
                skipped["unreviewed"] += 1
                continue
            result = _agreement(service, case, revision)
        except (KeyError, ValueError, OSError):
            log.exception("Agreement for case %s is unavailable", case["id"])
            skipped["unavailable"] += 1
            continue
        pool = pools["synthetic" if result["synthetic"] else "real"]
        pool["cases"] += 1
        pool["excluded"] += len(result["excluded"])
        for row in result["structures"]:
            pool["rows"][(row["task"], row["id"])].append(row)
        revisions.append({"case_id": case["id"], "revision_id": revision["id"],
                          "review_status": revision["review_status"],
                          "pool": "synthetic" if result["synthetic"] else "real"})

    def summarize(pool):
        tasks = defaultdict(list)
        for (task, sid), rows in sorted(pool["rows"].items(), key=lambda item: (str(item[0][0]), item[0][1])):
            tasks[task].append({"id": sid, "name": rows[-1]["name"], "task": task, **_statistics(rows)})
        everything = [row for rows in pool["rows"].values() for row in rows]
        return {"cases": pool["cases"], "excluded": pool["excluded"],
                "summary": _statistics(everything) if everything else None,
                "tasks": [{"task": task, **_statistics([row for rows in pool["rows"].values()
                                                        for row in rows if row["task"] == task]),
                           "structures": entries} for task, entries in tasks.items()]}

    return {"schema_version": 1, "implementation": _IMPLEMENTATION,
            "selection": {"revision": "active", "review_statuses": list(COUNTED_STATUSES)},
            "cases": {"total": len(cases), "included": len(revisions), "skipped": dict(skipped)},
            "pools": {name: summarize(pool) for name, pool in pools.items()}, "revisions": revisions}
