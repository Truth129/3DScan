"""Validated vector prompts and cropped native-grid nnInteractive inputs.

Drawing coordinates are native voxel centres on one source-image plane. The
remaining axes in increasing order are u/v; physical brushes use the complete
in-plane affine metric, including anisotropy, obliquity and shear.
"""

from __future__ import annotations

import re
import numpy as np

from .imaging import _voxel


DEFAULT_OPTIONS = {"autozoom": True, "start_empty": False}
_TOOLS = {"point": "Point", "bbox": "Box", "lasso": "Lasso", "scribble": "Hint brush", "scissors": "Scissors"}


def mark_name(prompt, number, direct=False):
    """A mark as the review panel names its tool, so a refusal says which one to redraw."""
    if direct and prompt["kind"] == "scribble":
        return f"{'Paint' if prompt['include'] else 'Erase'} stroke {number}"
    return f"{_TOOLS[prompt['kind']]} mark {number}"


def normalize_options(options=None):
    if options is None:
        return dict(DEFAULT_OPTIONS)
    if not isinstance(options, dict) or set(options) - DEFAULT_OPTIONS.keys():
        raise ValueError("Unknown interactive correction options")
    if any(not isinstance(value, bool) for value in options.values()):
        raise ValueError("Interactive correction options must be boolean")
    return {**DEFAULT_OPTIONS, **options}


def normalize_prompts(prompts, affine, shape):
    if not isinstance(prompts, list) or len(prompts) > 128:
        raise ValueError("A correction supports at most 128 prompts")
    result, seen, vertex_count = [], set(), 0
    for item in prompts:
        if not isinstance(item, dict):
            raise ValueError("Every interaction must be a prompt record")
        kind = item.get("kind", "point")
        common = {"id", "kind", "include"}
        allowed = common | (
            {"ras"} if kind == "point" else {"operation", "shape", "vertices", "ras_to_clip"} if kind == "scissors" else {"axis", "index", "vertices"}
        )
        if kind == "scribble":
            allowed.add("radius_mm")
        if kind not in {"point", "bbox", "scribble", "lasso", "scissors"} or set(item) - allowed:
            raise ValueError("Unknown interaction type or prompt fields")
        identity = item.get("id")
        if not isinstance(identity, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", identity) or identity in seen:
            raise ValueError("Prompt identifiers must be unique letters, digits, underscores or hyphens")
        if not isinstance(item.get("include"), bool):
            raise ValueError("Prompt polarity must be a boolean")
        record = dict(id=identity, kind=kind, include=item["include"])
        if kind == "scissors":
            from .scissors import normalize_scissors

            record.update(normalize_scissors(item))
            vertex_count += len(record["vertices"])
        elif kind == "point":
            try:
                ras = np.asarray(item.get("ras"), dtype=float)
            except (ValueError, TypeError) as exc:
                raise ValueError("A point requires three finite patient RAS coordinates") from exc
            if ras.shape != (3,) or not np.isfinite(ras).all():
                raise ValueError("A point requires three finite patient RAS coordinates")
            _voxel(np.asarray(affine), shape, ras)
            record["ras"] = ras.tolist()
        else:
            axis, index = item.get("axis"), item.get("index")
            if isinstance(axis, bool) or not isinstance(axis, int) or axis not in (0, 1, 2):
                raise ValueError("A drawing requires a native voxel axis")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < shape[axis]:
                raise ValueError("The native drawing plane lies outside the reference CT")
            try:
                vertices = np.asarray(item.get("vertices"), dtype=float)
            except (ValueError, TypeError) as exc:
                raise ValueError("Drawing vertices must contain finite u/v pairs") from exc
            minimum = 3 if kind == "lasso" else 2
            if vertices.ndim != 2 or vertices.shape[1:] != (2,) or not minimum <= len(vertices) <= 2048:
                raise ValueError(f"A {kind} requires {minimum} to 2048 native u/v vertices")
            if kind == "bbox" and len(vertices) != 2:
                raise ValueError("A bounding box requires exactly two opposite corners")
            if not np.isfinite(vertices).all():
                raise ValueError("Drawing vertices must be finite")
            axes = [a for a in range(3) if a != axis]
            if np.any(vertices < 0) or np.any(vertices > np.asarray(shape)[axes] - 1):
                raise ValueError("Drawing vertices lie outside the reference CT")
            if kind == "bbox" and np.any(np.ptp(vertices, axis=0) < 1e-6):
                raise ValueError("A bounding box must have positive extent in its plane")
            if kind == "lasso":
                area = abs(np.dot(vertices[:, 0], np.roll(vertices[:, 1], 1)) - np.dot(vertices[:, 1], np.roll(vertices[:, 0], 1)))
                if area < 1e-6:
                    raise ValueError("A lasso must enclose a nonzero area")
            record.update(axis=axis, index=index, vertices=vertices.tolist())
            if kind == "scribble":
                radius = item.get("radius_mm", 3.0)
                if isinstance(radius, bool) or not isinstance(radius, (int, float)) or not np.isfinite(radius) or not 0.5 <= radius <= 30:
                    raise ValueError("Scribble radius must be between 0.5 and 30 mm")
                record["radius_mm"] = float(radius)
            vertex_count += len(vertices)
        seen.add(identity)
        result.append(record)
    if vertex_count > 8192:
        raise ValueError("A correction supports at most 8192 drawing vertices")
    return result


def materialize_prompt(prompt, affine, shape, name="The drawing"):
    """Return small native arrays; callers normalize the complete list first.

    `name` is the mark's `mark_name`, for the refusal of a drawing that selects no voxel centre.
    """
    kind = prompt.get("kind", "point")
    if kind == "scissors":
        raise ValueError("3D scissors are deterministic direct edits, not AI prompts")
    result = dict(id=prompt["id"], kind=kind, include=prompt["include"])
    if kind == "point":
        voxel = _voxel(np.asarray(affine), shape, prompt["ras"])
        result.update(voxel=voxel.tolist(), support_bbox_ijk=[[max(0, int(v) - 4), min(int(s), int(v) + 5)] for v, s in zip(voxel, shape)])
        return result
    axis, index = prompt["axis"], prompt["index"]
    axes = [a for a in range(3) if a != axis]
    vertices = np.asarray(prompt["vertices"], dtype=float)
    basis = np.asarray(affine)[:3, axes]
    metric = basis.T @ basis
    radius = prompt.get("radius_mm", 0.0)
    reach = radius * np.sqrt(np.diag(np.linalg.inv(metric)))
    low = np.maximum(0, np.floor(vertices.min(axis=0) - reach).astype(int))
    high = np.minimum(np.asarray(shape)[axes], np.ceil(vertices.max(axis=0) + reach).astype(int) + 1)
    bounds = [[index, index + 1] if a == axis else [int(low[axes.index(a)]), int(high[axes.index(a)])] for a in range(3)]
    if kind == "bbox":
        result.update(bbox_ijk=bounds, support_bbox_ijk=bounds)
        return result
    plane = np.zeros(tuple(high - low), dtype=bool)
    if kind == "scribble":
        for first, second in zip(vertices[:-1], vertices[1:]):
            begin = np.maximum(low, np.floor(np.minimum(first, second) - reach).astype(int))
            end = np.minimum(high, np.ceil(np.maximum(first, second) + reach).astype(int) + 1)
            u, v = np.meshgrid(np.arange(begin[0], end[0]), np.arange(begin[1], end[1]), indexing="ij")
            delta = np.stack((u - first[0], v - first[1]), axis=-1)
            direction = second - first
            length2 = float(direction @ metric @ direction)
            fraction = np.clip(np.einsum("...i,i->...", delta, metric @ direction) / length2, 0, 1) if length2 > 1e-15 else 0
            residual = delta - np.asarray(fraction)[..., None] * direction
            inside = np.einsum("...i,ij,...j->...", residual, metric, residual) <= radius * radius + 1e-9
            slices = tuple(slice(int(a - o), int(b - o)) for a, b, o in zip(begin, end, low))
            plane[slices] |= inside
    else:
        u, v = np.meshgrid(np.arange(low[0], high[0]), np.arange(low[1], high[1]), indexing="ij")
        for first, second in zip(vertices, np.roll(vertices, -1, axis=0)):
            crosses = (first[1] > v) != (second[1] > v)
            if second[1] != first[1]:
                crossing = (second[0] - first[0]) * (v - first[1]) / (second[1] - first[1]) + first[0]
                plane ^= crosses & (u < crossing)
        # Include polygon boundary centres deterministically.
        for first, second in zip(vertices, np.roll(vertices, -1, axis=0)):
            direction = second - first
            delta = np.stack((u - first[0], v - first[1]), axis=-1)
            length2 = float(direction @ direction)
            fraction = np.clip(np.einsum("...i,i->...", delta, direction) / length2, 0, 1) if length2 > 1e-15 else 0
            residual = delta - np.asarray(fraction)[..., None] * direction
            plane |= np.einsum("...i,...i->...", residual, residual) < 1e-12
    if not plane.any():
        raise ValueError(f"{name} covers no voxel centre on its slice")
    image = np.expand_dims(plane.astype(np.uint8), axis=axis)
    result.update(interaction_bbox=bounds, image=np.ascontiguousarray(image), support_bbox_ijk=bounds)
    return result
