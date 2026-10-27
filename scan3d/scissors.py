"""Exact orthographic through-depth cuts on one selected voxel mask."""

from __future__ import annotations

import numpy as np


SCISSORS_CHUNK_VOXELS = 256 * 1024
_BOUNDARY_EPSILON = 1e-10


def normalize_scissors(prompt):
    """Validate a captured RAS-to-clip frame and its screen-space polygon."""
    if prompt.get("include") is not False:
        raise ValueError("3D scissors only erase the selected segment")
    operation = prompt.get("operation")
    shape = prompt.get("shape")
    if operation not in ("erase_inside", "erase_outside"):
        raise ValueError("Choose erase inside or erase outside for 3D scissors")
    if shape not in ("freehand", "rectangle", "circle"):
        raise ValueError("Choose a freehand, rectangle or circle scissors shape")
    try:
        vertices = np.asarray(prompt.get("vertices"), dtype=np.float64)
        matrix = np.asarray(prompt.get("ras_to_clip"), dtype=np.float64)
    except (ValueError, TypeError) as exc:
        raise ValueError("3D scissors require finite screen vertices and an orthographic projection") from exc
    if (
        vertices.ndim != 2
        or vertices.shape[1:] != (2,)
        or not 3 <= len(vertices) <= 2048
        or not np.isfinite(vertices).all()
        or np.any(vertices < -1)
        or np.any(vertices > 1)
    ):
        raise ValueError("3D scissors require 3 to 2048 finite screen vertices between -1 and 1")
    if np.linalg.matrix_rank(vertices - vertices[0], tol=1e-10) < 2:
        raise ValueError("The scissors polygon must enclose a nonzero screen area")
    if matrix.shape != (4, 4) or not np.isfinite(matrix).all() or not np.array_equal(matrix[3], [0, 0, 0, 1]):
        raise ValueError("3D scissors require an orthographic RAS-to-clip matrix; perspective cuts are unsupported")
    try:
        condition = np.linalg.cond(matrix[:3, :3])
    except np.linalg.LinAlgError as exc:
        raise ValueError("The scissors projection must be invertible") from exc
    if not np.isfinite(condition) or condition > 1e12:
        raise ValueError("The scissors projection must be invertible and numerically stable")
    if shape == "rectangle":
        edges = np.roll(vertices, -1, axis=0) - vertices
        if len(vertices) != 4 or len(np.unique(vertices, axis=0)) != 4 or not np.all(np.count_nonzero(np.abs(edges) > _BOUNDARY_EPSILON, axis=1) == 1):
            raise ValueError("Rectangle scissors require four ordered, axis-aligned screen corners")
    return dict(operation=operation, shape=shape, vertices=vertices.tolist(), ras_to_clip=matrix.tolist())


def _polygon_contains(x, y, vertices, *, rectangle=False, cancelled=lambda: False):
    """Even-odd fill with boundary centres included; working arrays are bounded."""
    epsilon = _BOUNDARY_EPSILON
    low, high = vertices.min(axis=0), vertices.max(axis=0)
    within = (x >= low[0] - epsilon) & (x <= high[0] + epsilon) & (y >= low[1] - epsilon) & (y <= high[1] + epsilon)
    if rectangle or not within.any():
        return within
    selected = np.flatnonzero(within)
    px, py = x[selected], y[selected]
    inside = np.zeros(selected.size, dtype=bool)
    boundary = np.zeros(selected.size, dtype=bool)
    for index, (first, second) in enumerate(zip(vertices, np.roll(vertices, -1, axis=0))):
        if index % 32 == 0 and cancelled():
            raise InterruptedError("3D scissors cancelled")
        dx, dy = second - first
        crosses = (first[1] > py) != (second[1] > py)
        if dy and crosses.any():
            crossing = first[0] + (py[crosses] - first[1]) * dx / dy
            inside[crosses] ^= px[crosses] < crossing
        edge_box = (
            (px >= min(first[0], second[0]) - epsilon)
            & (px <= max(first[0], second[0]) + epsilon)
            & (py >= min(first[1], second[1]) - epsilon)
            & (py <= max(first[1], second[1]) + epsilon)
        )
        if edge_box.any():
            edge_indices = np.flatnonzero(edge_box)
            cross = (px[edge_indices] - first[0]) * dy - (py[edge_indices] - first[1]) * dx
            boundary[edge_indices] |= np.abs(cross) <= epsilon * max(1.0, float(np.hypot(dx, dy)))
    within[selected] = inside | boundary
    return within


def erase_projected(mask, affine, prompt, *, cancelled=lambda: False, progress=lambda _: None):
    """Erase projected foreground centres in bounded native C-order chunks.

    No full-volume coordinate grid or foreground-index list is constructed.
    Orthographic clip z is intentionally unused: cuts extend through the whole
    selected segment, including portions behind other surfaces or clip planes.
    """
    if mask.dtype != np.bool_ or mask.ndim != 3 or not mask.flags.c_contiguous:
        raise ValueError("3D scissors require a contiguous binary target mask")
    cut = normalize_scissors(prompt)
    transform = np.asarray(cut["ras_to_clip"]) @ np.asarray(affine, dtype=np.float64)
    if transform.shape != (4, 4) or not np.isfinite(transform).all():
        raise ValueError("The scissors projection does not match a finite reference grid")
    vertices = np.asarray(cut["vertices"])
    flat = mask.reshape(-1)
    stride_i, stride_j = mask.shape[1] * mask.shape[2], mask.shape[2]
    removed, next_progress = 0, 0
    for start in range(0, flat.size, SCISSORS_CHUNK_VOXELS):
        if cancelled():
            raise InterruptedError("3D scissors cancelled")
        stop = min(flat.size, start + SCISSORS_CHUNK_VOXELS)
        local = np.flatnonzero(flat[start:stop])
        if local.size:
            native = local + start
            i, j, k = native // stride_i, (native // stride_j) % mask.shape[1], native % stride_j
            x = transform[0, 0] * i + transform[0, 1] * j + transform[0, 2] * k + transform[0, 3]
            y = transform[1, 0] * i + transform[1, 1] * j + transform[1, 2] * k + transform[1, 3]
            if not np.isfinite(x).all() or not np.isfinite(y).all():
                raise ValueError("Projected voxel centres are not finite")
            inside = _polygon_contains(x, y, vertices, rectangle=cut["shape"] == "rectangle", cancelled=cancelled)
            remove = inside if cut["operation"] == "erase_inside" else ~inside
            flat[start + local[remove]] = False
            removed += int(np.count_nonzero(remove))
        if stop >= next_progress:
            progress(stop / flat.size)
            next_progress = stop + max(SCISSORS_CHUNK_VOXELS, flat.size // 32)
    return removed
