"""On-demand, exact HU references for the cinematic heart display recipe.

This module never edits source data or revisions and never initializes inference.
It follows slicer_cinematic_heart.py's segment median selection. Native NIfTI
scaling and patient geometry remain authoritative. Computation uses bounded
slabs and a size-limited disk-backed sample spool, rather than a full CT copy.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from itertools import product
from pathlib import Path
import tempfile
import threading

import numpy as np

from . import imaging

_IMPLEMENTATION = "cinematic-reference-v1"
_SLAB_VOXELS = 262_144
_MAX_SAMPLES = 64 * 1024**2  # At most 512 MiB of temporary float64 samples.
_MAX_SEGMENTS = 128
_CACHE_ENTRIES = 64
_CACHE = OrderedDict()
_LOCK = threading.RLock()  # Also bounds all reference computations to one lane.


def _identity(path):
    path = Path(path).resolve()
    stat = path.stat()
    return (str(path), stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _select(structures):
    def name(structure):
        return " ".join(str(structure.get("name", "")).lower().replace("_", " ").replace("-", " ").split())

    myocardial, aorta, left_heart = [], [], []
    for structure in structures:
        value = name(structure)
        if "myocard" in value:
            myocardial.append(structure)
        if "aort" in value:
            aorta.append(structure)
        if any(word in value for word in ("left ventric", "ventricle left", "left atri", "atrium left")):
            left_heart.append(structure)
    selected = {structure["id"]: structure for group in (myocardial, aorta, left_heart) for structure in group}
    if len(selected) > _MAX_SEGMENTS:
        raise ValueError("Too many cardiac reference segments; enter the cinematic HU values manually")
    return myocardial, aorta, left_heart, list(selected.values())


def _median(ct, affine, structures, directory):
    """Return the exact pooled native-HU median and count, without a dense CT."""
    if not structures:
        return None, 0
    count = 0
    with tempfile.TemporaryFile(dir=directory, prefix="cinematic-hu-") as spool:
        # Proxies keep their file open for sequential slabs, including gzip.
        for structure in structures:
            mask, mask_affine = imaging._image(Path(structure["mask_file"]), keep_file_open=True)
            if mask.shape != ct.shape or not np.allclose(mask_affine, affine, rtol=0, atol=1e-4):
                raise ValueError("Cardiac reference mask geometry does not match the reference CT")
            for region in _regions(ct.shape):
                labels = np.asanyarray(mask.dataobj[region])
                if labels.dtype.kind not in "buif" or not np.all((labels == 0) | (labels == 1)):
                    raise ValueError("Cardiac reference masks must contain only binary values 0 and 1")
                if not np.any(labels):
                    continue
                # ArrayProxy applies the file's slope/intercept before conversion.
                hu = np.asarray(ct.dataobj[region], dtype=np.float64)
                selected = hu[labels == 1]
                selected = selected[np.isfinite(selected) & (selected > -900)]
                if count + selected.size > _MAX_SAMPLES:
                    raise ValueError("Cardiac reference region is too large; enter the cinematic HU values manually")
                selected.tofile(spool)
                count += int(selected.size)
        if not count:
            return None, 0
        spool.flush()
        values = np.memmap(spool, dtype=np.float64, mode="r+", shape=(count,))
        try:
            middle = count // 2
            ranks = [middle] if count % 2 else [middle - 1, middle]
            # In-place partition avoids an additional array of all selected HU.
            values.partition(ranks)
            median = float(values[middle]) if count % 2 else float(values[middle - 1] / 2 + values[middle] / 2)
        finally:
            del values
    return median, count


def _regions(shape):
    """Bound any image shape, preserving i-fastest sequential NIfTI traversal."""
    remaining, block = _SLAB_VOXELS, []
    for extent in shape:
        size = min(extent, remaining)
        block.append(size)
        remaining = max(1, remaining // size)
    for start_k, start_j, start_i in product(range(0, shape[2], block[2]), range(0, shape[1], block[1]), range(0, shape[0], block[0])):
        yield tuple(slice(start, min(extent, start + size)) for start, extent, size in zip((start_i, start_j, start_k), shape, block))


def reference_values(revision, temporary_dir=None):
    """Measure saved-revision cardiac references, caching only small results.

    Cache identities include revision, segment names/IDs and each source file's
    inode, size, mtime and ctime. Replacing a source with preserved mtime cannot
    reuse stale values. All authorized input identities are checked again after
    computation. No patient names, paths or scan metadata are returned.
    """
    myocardium, aorta, left_heart, selected = _select(revision["structures"])
    if not myocardium:
        raise ValueError("No myocardium segment was found; enter the cinematic HU values manually")
    if not (aorta or left_heart):
        raise ValueError("No aorta or left-heart blood-pool segment was found; enter the cinematic HU values manually")
    with _LOCK:
        identity = (
            _IMPLEMENTATION,
            revision["id"],
            _identity(revision["ct_file"]),
            tuple((item["id"], item["name"], _identity(item["mask_file"])) for item in selected),
        )
        if identity in _CACHE:
            _CACHE.move_to_end(identity)
            return deepcopy(_CACHE[identity])
        if temporary_dir is not None:
            temporary_dir = Path(temporary_dir)
            temporary_dir.mkdir(parents=True, exist_ok=True)
        ct, affine = imaging._image(Path(revision["ct_file"]), keep_file_open=True)
        myocardial_hu, myocardial_count = _median(ct, affine, myocardium, temporary_dir)
        blood_hu, blood_count = _median(ct, affine, aorta, temporary_dir)
        blood_structures, blood_source = aorta, "aorta"
        if blood_hu is None:
            blood_hu, blood_count = _median(ct, affine, left_heart, temporary_dir)
            blood_structures, blood_source = left_heart, "left_heart"
        if myocardial_hu is None:
            raise ValueError("The myocardium segments contain no finite tissue HU above -900; enter the cinematic HU values manually")
        if blood_hu is None:
            raise ValueError("The blood-pool segments contain no finite tissue HU above -900; enter the cinematic HU values manually")
        if blood_hu - myocardial_hu < 100:
            raise ValueError(
                "Blood-pool HU must be at least 100 HU above myocardium; measured %.1f / %.1f HU. Enter the cinematic HU values manually"
                % (myocardial_hu, blood_hu)
            )
        after = (
            _IMPLEMENTATION,
            revision["id"],
            _identity(revision["ct_file"]),
            tuple((item["id"], item["name"], _identity(item["mask_file"])) for item in selected),
        )
        if after != identity:
            raise ValueError("Cardiac reference input changed while reading; retry the request")
        result = {
            "schema_version": 1,
            "myocardium_hu": myocardial_hu,
            "blood_hu": blood_hu,
            "myocardium_structure_ids": [item["id"] for item in myocardium],
            "blood_structure_ids": [item["id"] for item in blood_structures],
            "myocardium_voxels": myocardial_count,
            "blood_voxels": blood_count,
            "blood_source": blood_source,
            "method": "median_hu_above_minus900",
        }
        _CACHE[identity] = result
        while len(_CACHE) > _CACHE_ENTRIES:
            _CACHE.popitem(last=False)
        return deepcopy(result)
