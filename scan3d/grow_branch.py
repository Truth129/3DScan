"""Grow branch: add a coronary branch CAS-Net missed, from CAS-Net's own stored probability.

The rule is the one scored on public ImageCAS scans (prob-grow-check, score_bounded.py): keep
voxels farther than 1 mm from the current mask, take the 26-connected piece of p >= t there
that holds the click, and add it. A click that misses that piece seeds from the allowed voxel
with the highest p within 1.5 mm. Distances are patient millimetres through the full affine,
so an oblique or sheared grid measures them correctly.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from scipy import ndimage as ndi

from . import imaging

THRESHOLDS = (0.10, 0.05)
EXCLUSION_MM = 1.0
SEED_SEARCH_MM = 1.5
# The scored test's largest addition was 0.09 mL; far more means the probability has spread.
MAX_ADDED_ML = 0.5
NOTHING = "CAS-Net saw nothing here; use nnInteractive or Direct edit."
NO_PROBABILITY = "This segment has no stored CAS-Net probability. Run CAS-Net on this scan again to use Grow branch."
_TOLERANCE = 1e-7


def _ball(affine, radius):
    """Voxel offsets within `radius` mm of the centre, in C order, as the scorer's ball."""
    matrix = np.asarray(affine, dtype=np.float64)[:3, :3]
    reach = np.ceil(radius * np.linalg.norm(np.linalg.inv(matrix), axis=1) + _TOLERANCE).astype(int)
    offsets = np.indices(reach * 2 + 1).reshape(3, -1).T - reach
    return offsets[np.linalg.norm(offsets @ matrix.T, axis=1) <= radius + _TOLERANCE], reach


def _near(mask, affine, radius):
    """Voxels within `radius` mm of the mask: the complement of 'farther than radius'."""
    offsets, reach = _ball(affine, radius)
    footprint = np.zeros(tuple(reach * 2 + 1), dtype=bool)
    footprint[tuple((offsets + reach).T)] = True
    return ndi.binary_dilation(mask, structure=footprint)


def grow(mask, probability, affine, voxel, threshold):
    """The voxels to add, as a boolean array, or None when nothing grows.

    `probability` is the stored uint8 p * 255. p >= t is compared on those integers, exactly.
    The label pass crops to the piece of p >= t holding the seed, padded by the exclusion reach,
    since the region lies inside that piece and only mask voxels that near can exclude any of it.
    """
    if threshold not in THRESHOLDS:
        raise ValueError("Grow branch threshold must be 0.10 or 0.05")
    level = int(np.ceil(threshold * 255 - _TOLERANCE))
    voxel = np.asarray(voxel, dtype=np.int64)
    shape = np.asarray(mask.shape)
    # The seed: the click, or the allowed voxel with the highest p within 1.5 mm of it.
    search, search_reach = _ball(affine, SEED_SEARCH_MM)
    _, near_reach = _ball(affine, EXCLUSION_MM)
    low = np.maximum(voxel - search_reach - near_reach, 0)
    high = np.minimum(voxel + search_reach + near_reach + 1, shape)
    box = tuple(slice(a, b) for a, b in zip(low, high))
    allowed_here = ~_near(mask[box], affine, EXCLUSION_MM)
    scores = np.where(allowed_here, probability[box].astype(np.int16), -1)
    around = search + (voxel - low)
    around = around[np.all((around >= 0) & (around < high - low), axis=1)]
    seed = voxel - low
    if scores[tuple(seed)] < level:
        seed = around[int(np.argmax(scores[tuple(around.T)]))]
    if scores[tuple(seed)] < level:
        return None
    seed = seed + low
    above = probability >= level
    pieces, _ = ndi.label(above, structure=np.ones((3, 3, 3), bool))
    bounds = imaging._foreground_bounds(pieces == pieces[tuple(seed)])
    del pieces
    low = np.maximum(bounds[0] - near_reach, 0)
    high = np.minimum(bounds[1] + near_reach + 1, shape)
    box = tuple(slice(a, b) for a, b in zip(low, high))
    candidates = above[box] & ~_near(mask[box], affine, EXCLUSION_MM)
    labels, _ = ndi.label(candidates, structure=np.ones((3, 3, 3), bool))
    region = np.zeros(mask.shape, dtype=bool)
    region[box] = labels == labels[tuple(seed - low)]
    return region


def grow_mask(ct_path, mask_path, probability_path, point_ras, threshold, output_path):
    """Write the current mask plus the grown region, refusing nothing, too much, or a mismatched probability."""
    if not probability_path or not Path(probability_path).is_file():
        raise ValueError(NO_PROBABILITY)
    ct, affine = imaging._image(Path(ct_path))
    mask, _, _ = imaging._binary(Path(mask_path), ct, affine)
    stored, stored_affine = imaging._image(Path(probability_path))
    if stored.shape != ct.shape or not np.allclose(stored_affine, affine, rtol=0, atol=1e-4):
        raise ValueError("The stored CAS-Net probability does not match this scan's grid. Run CAS-Net on this scan again.")
    probability = np.ascontiguousarray(np.asanyarray(stored.dataobj), dtype=np.uint8)
    voxel = imaging._voxel(affine, mask.shape, point_ras)
    region = grow(mask, probability, affine, voxel, threshold)
    if region is None or not region.any():
        raise ValueError(NOTHING)
    voxel_ml = abs(float(np.linalg.det(affine[:3, :3]))) / 1000
    added = int(np.count_nonzero(region))
    if added * voxel_ml > MAX_ADDED_ML:
        raise ValueError(
            f"Grow branch would add {added * voxel_ml:.2f} mL, over its {MAX_ADDED_ML} mL limit, so nothing was added. Use nnInteractive or Direct edit."
        )
    imaging._save_mask(mask | region, ct, output_path)
    return {
        "seed_voxel": voxel.tolist(),
        "added_voxels": added,
        "added_ml": added * voxel_ml,
        "before_volume_ml": float(np.count_nonzero(mask)) * voxel_ml,
    }
