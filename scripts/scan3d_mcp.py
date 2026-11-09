# /// script
# requires-python = ">=3.12"
# dependencies = ["mcp>=2.3,<3", "httpx>=0.28,<1", "numpy>=2,<3"]
# ///
"""MCP server that lets an MCP client, such as Claude, work a running 3DScan through its HTTP API.

It is an ordinary API client (docs/API.md), so sign-in, access rules, the job queue and the
409 conflict checks apply to it exactly as to the browser. It imports nothing from scan3d and
runs in its own environment, because the MCP SDK needs a newer Starlette than the app pins:

    .tools/bin/uv run --script scripts/scan3d_mcp.py --url http://127.0.0.1:9010

Everything a tool returns, slice images included, goes to the MCP client's model. See
docs/MCP.md before pointing it at a library that holds patient scans.
"""

import argparse
import functools
import json
import logging
import math
import os
from pathlib import Path
import struct
import sys
import threading
import time
from typing import Any
import zlib

import httpx
import numpy as np

JOB_RUNNING = ("queued", "running")
# Slice images go to a model: big enough to read anatomy, small enough to keep the context.
PNG_MAX_SIDE = 1024
PNG_MIN_SIDE = 384
FILL_ALPHA = 0.35
MAX_OVERLAYS = 128
WINDOW_PRESETS = {"soft_tissue": (400, 40), "lung": (1500, -600), "bone": (1800, 400), "brain": (80, 40), "liver": (150, 90), "angio": (700, 200)}
ORIENTATION = {0: ("R", "L"), 1: ("A", "P"), 2: ("S", "I")}
PLANE_AXIS = {"axial": 2, "coronal": 1, "sagittal": 0}


class ApiError(Exception):
    """A refusal or failure from 3DScan, carrying the server's own message."""


class Scan3D:
    """A small synchronous client for the 3DScan HTTP API."""

    def __init__(self, base_url: str | None = None, *, username: str | None = None, password: str | None = None, http: httpx.Client | None = None):
        if http is None:
            if not base_url:
                raise ValueError("A 3DScan URL is required")
            http = httpx.Client(base_url=base_url.rstrip("/"), timeout=httpx.Timeout(120, connect=10))
        self.http = http
        url = http.base_url
        if url.scheme not in ("http", "https") or not url.host:
            raise ValueError("The 3DScan URL must be http:// or https:// with a host")
        # Mutations need an Origin that matches Host; see "Transport rules" in docs/API.md.
        self.origin = f"{url.scheme}://{url.netloc.decode('ascii')}"
        self.username = username
        self.password = password
        self.poll_seconds = 1.0
        # Tools run in parallel threads. One sign-in at a time: the server hashes at most two
        # passwords at once and answers 429 to a third that waits.
        self._sign_in_lock = threading.Lock()
        self._sign_ins = 0

    # HTTP

    def request(self, method: str, path: str, *, retry_sign_in: bool = True, **kwargs) -> httpx.Response:
        headers = {"Origin": self.origin, **kwargs.pop("headers", {})}
        signed_in = self._sign_ins
        response = self.http.request(method, path, headers=headers, **kwargs)
        if response.status_code == 401 and retry_sign_in and self.username and self.password and path != "/api/session":
            with self._sign_in_lock:
                if self._sign_ins == signed_in:
                    self.sign_in()
            return self.request(method, path, retry_sign_in=False, headers=headers, **kwargs)
        if response.status_code >= 400:
            raise ApiError(f"{method} {path} failed with {response.status_code}: {_detail(response)}")
        return response

    def get(self, path: str, **params) -> Any:
        return self.request("GET", path, params=_params(params)).json()

    def post(self, path: str, body: dict | None = None) -> Any:
        return self.request("POST", path, json=body if body is not None else {}).json()

    def patch(self, path: str, body: dict) -> Any:
        return self.request("PATCH", path, json=body).json()

    def sign_in(self) -> dict:
        view = self.request("POST", "/api/session", retry_sign_in=False, json={"username": self.username, "password": self.password}).json()
        self._sign_ins += 1
        return view

    # Jobs

    def wait(self, job_id: str, seconds: float) -> dict:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while True:
            job = self.get(f"/api/jobs/{job_id}")
            if job["status"] not in JOB_RUNNING or time.monotonic() >= deadline:
                return job
            time.sleep(self.poll_seconds)

    # Records

    def case(self, case_id: str) -> dict:
        return self.get(f"/api/cases/{case_id}")

    def revision_id(self, case_id: str, revision_id: str | None) -> str:
        if revision_id:
            return revision_id
        active = self.case(case_id).get("active_revision_id")
        if not active:
            raise ApiError("This case has no prepared image yet. Run prepare_series first.")
        return active

    def revision(self, case_id: str, revision_id: str | None = None) -> dict:
        return self.get(f"/api/cases/{case_id}/revisions/{self.revision_id(case_id, revision_id)}")

    def session(self, case_id: str, session_id: str) -> dict:
        return self.get(f"/api/cases/{case_id}/reviews/{session_id}")

    def reference_geometry(self, case_id: str, revision: dict) -> dict:
        asset = revision.get("image_asset") or self.get(f"/api/cases/{case_id}/images/{revision['reference_image_id']}")
        return asset["geometry"]

    # Slices

    def frame(self, case_id: str, revision_id: str, plane: str, position_mm: float, structure_ids=(), compare: bool = False) -> dict:
        params: dict[str, Any] = {"plane": plane, "position_mm": position_mm}
        if structure_ids:
            params["structure_ids"] = list(structure_ids)
            params["compare"] = "true" if compare else "false"
        response = self.request("GET", f"/api/cases/{case_id}/revisions/{revision_id}/review-frame", params=params)
        return decode_frame(response.headers.get("X-CT-Frame"), response.content)

    def slice_to_ras(self, case_id: str, revision_id: str, plane: str, position_mm: float, points) -> list[list[float]]:
        """Patient RAS millimetres of points given in a view_slice image's own pixels."""
        meta = self.frame(case_id, revision_id, plane, position_mm)["meta"]
        matrix = png_pixel_to_ras(meta)
        result = []
        for point in points:
            x, y = _pair(point, "A slice point")
            result.append([float(v) for v in (matrix @ np.array([x, y, 0.0, 1.0]))[:3]])
        return result


# Helpers shared by the tools and the tests.


def _detail(response: httpx.Response) -> str:
    try:
        detail = response.json().get("detail")
    except (ValueError, AttributeError):
        return response.text[:500] or response.reason_phrase
    return detail if isinstance(detail, str) else json.dumps(detail)[:1000]


def _params(params: dict) -> dict:
    return {key: value for key, value in params.items() if value is not None}


def _pair(value, what: str) -> tuple[float, float]:
    if not isinstance(value, (list, tuple)) or len(value) != 2 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in value):
        raise ApiError(f"{what} must be [x, y] numbers")
    return float(value[0]), float(value[1])


def _slice_spec(where) -> tuple[str, float]:
    if not isinstance(where, dict):
        raise ApiError("slice must be {plane, position_mm, ...}")
    plane, position = where.get("plane", "axial"), where.get("position_mm")
    if plane not in PLANE_AXIS or not isinstance(position, (int, float)) or not math.isfinite(position):
        raise ApiError("slice needs plane (axial, coronal or sagittal) and position_mm, as given to view_slice_image")
    return plane, float(position)


def _generation(session: dict, expected: int | None) -> int:
    """The generation a draft write expects: the caller's last-seen one when given, so a change by someone else is a 409."""
    return session["generation"] if expected is None else expected


def _round(value, digits=2):
    if isinstance(value, float):
        return round(value, digits)
    if isinstance(value, list):
        return [_round(v, digits) for v in value]
    return value


def decode_frame(header: str | None, body: bytes) -> dict:
    """Decode a review-frame response: float32 values, possibly byte-shuffled, then binary overlays."""
    if not header:
        raise ApiError("The slice response has no X-CT-Frame header")
    frame = json.loads(header)
    meta = frame["meta"]
    width, height = int(meta["width"]), int(meta["height"])
    pixels = width * height
    if frame.get("dtype") != "float32" or frame.get("pixels") != pixels or pixels < 1:
        raise ApiError("The slice response has an unexpected layout")
    layers = frame.get("overlays")
    if layers is None:
        layers = (
            []
            if frame.get("current_offset") is None
            else [{"id": None, "current_offset": frame["current_offset"], "parent_offset": frame.get("parent_offset")}]
        )
    expected = pixels * 4 + sum(pixels * (1 + (layer["parent_offset"] is not None)) for layer in layers)
    if len(body) != expected:
        raise ApiError("The slice response has an unexpected byte count")
    raw = np.frombuffer(body, np.uint8, count=pixels * 4)
    if frame.get("hu_encoding") == "byte-shuffle":
        # Four planes of byte 0, 1, 2 and 3 of every little-endian float32; interleave them back.
        raw = np.ascontiguousarray(raw.reshape(4, pixels).T)
    values = raw.view("<f4").reshape(height, width)

    def mask(offset):
        return None if offset is None else np.frombuffer(body, np.uint8, count=pixels, offset=offset).reshape(height, width).astype(bool)

    overlays = [{"id": layer["id"], "current": mask(layer["current_offset"]), "parent": mask(layer["parent_offset"])} for layer in layers]
    return {"meta": meta, "values": values, "overlays": overlays, "presentation": frame.get("presentation") or {}, "modality": frame.get("modality")}


def png_scale(width: int, height: int) -> float:
    """Frame pixels per PNG pixel. Deterministic, so marks given in PNG pixels map back exactly."""
    side = max(width, height)
    if side > PNG_MAX_SIDE:
        return float(math.ceil(side / PNG_MAX_SIDE))
    if side < PNG_MIN_SIDE:
        return 1.0 / (PNG_MIN_SIDE // side)
    return 1.0


def png_size(width: int, height: int) -> tuple[int, int]:
    scale = png_scale(width, height)
    return max(1, math.ceil(width / scale - 1e-9)), max(1, math.ceil(height / scale - 1e-9))


def png_pixel_to_ras(meta: dict) -> np.ndarray:
    """4x4 matrix from a PNG pixel centre (x right, y down, 0, 1) to patient RAS millimetres."""
    scale = png_scale(int(meta["width"]), int(meta["height"]))
    offset = (scale - 1) / 2
    to_frame = np.array([[scale, 0, 0, offset], [0, scale, 0, offset], [0, 0, 1, 0], [0, 0, 0, 1]], dtype=np.float64)
    return np.asarray(meta["pixel_to_ras"], dtype=np.float64) @ to_frame


def _resample(array: np.ndarray, scale: float) -> np.ndarray:
    height, width = array.shape[:2]
    out_width, out_height = png_size(width, height)
    offset = (scale - 1) / 2
    rows = np.clip(np.floor(np.arange(out_height) * scale + offset + 0.5).astype(int), 0, height - 1)
    cols = np.clip(np.floor(np.arange(out_width) * scale + offset + 0.5).astype(int), 0, width - 1)
    return array[np.ix_(rows, cols)]


def _edge(mask: np.ndarray) -> np.ndarray:
    padded = np.pad(mask, 1)
    interior = padded[:-2, 1:-1] & padded[2:, 1:-1] & padded[1:-1, :-2] & padded[1:-1, 2:]
    return mask & ~interior


def _rgb(color: str | None) -> np.ndarray:
    text = (color or "").lstrip("#")
    try:
        return np.array([int(text[i : i + 2], 16) for i in (0, 2, 4)], dtype=np.float64) if len(text) == 6 else np.array([56.0, 189.0, 248.0])
    except ValueError:
        return np.array([56.0, 189.0, 248.0])


def render_png(values: np.ndarray, window: float, level: float, layers, invert: bool = False) -> bytes:
    """Window the slice and draw each mask as a tinted fill with a solid edge; a compare mask as a white edge."""
    scale = png_scale(values.shape[1], values.shape[0])
    low = level - window / 2
    gray = np.clip((np.nan_to_num(_resample(values, scale).astype(np.float64), nan=low) - low) / max(window, 1e-6), 0, 1) * 255
    if invert:
        # MONOCHROME1 shows low values white, as the app and the scanner do.
        gray = 255 - gray
    rgb = np.repeat(gray[..., None], 3, axis=2)
    for color, current, parent in layers:
        if parent is not None:
            rgb[_edge(_resample(parent, scale))] = 255
        mask = _resample(current, scale)
        rgb[mask] = rgb[mask] * (1 - FILL_ALPHA) + color * FILL_ALPHA
        rgb[_edge(mask)] = color
    return encode_png(np.round(rgb).astype(np.uint8))


def encode_png(rgb: np.ndarray) -> bytes:
    height, width = rgb.shape[:2]
    rows = np.concatenate([np.zeros((height, 1), np.uint8), rgb.reshape(height, width * 3)], axis=1)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(rows.tobytes(), 6)) + chunk(b"IEND", b"")


def orientation(matrix: np.ndarray) -> dict:
    """Which patient direction each image edge faces, from the pixel axes' dominant RAS components."""

    def toward(vector):
        axis = int(np.argmax(np.abs(vector)))
        return ORIENTATION[axis][0 if vector[axis] > 0 else 1]

    def opposite(label):
        return next(pair[1 - pair.index(label)] for pair in ORIENTATION.values() if label in pair)

    right, bottom = toward(matrix[:3, 0]), toward(matrix[:3, 1])
    return {"left": opposite(right), "right": right, "top": opposite(bottom), "bottom": bottom}


def view_slice(
    api: Scan3D, case_id, plane="axial", position_mm=None, revision_id=None, structure_ids=None, compare=False, window=None, level=None, preset=None
):
    """Return (PNG bytes, description) for one patient-plane slice with mask overlays."""
    if plane not in PLANE_AXIS:
        raise ApiError("plane must be axial, coronal or sagittal")
    revision = api.revision(case_id, revision_id)
    rid = revision["id"]
    geometry = revision.get("geometry") or api.reference_geometry(case_id, revision)
    axis = PLANE_AXIS[plane]
    bounds = geometry["bounds_ras"]
    if position_mm is None:
        position_mm = float(geometry["center_ras"][axis])
    structures = {s["id"]: s for s in revision.get("structures") or []}
    if structure_ids is None:
        # Every mask whose bounds reach this plane, so the overlay request stays small. A correction
        # candidate reports no bounds for the masks it changed, so those come first, always.
        changed = [s["id"] for s in structures.values() if not s.get("bounds_ras") and (s.get("voxel_count") or compare)]
        reaching = [s["id"] for s in structures.values() if s.get("bounds_ras") and s["bounds_ras"][0][axis] <= position_mm <= s["bounds_ras"][1][axis]]
        selected = changed + reaching
    else:
        unknown = [sid for sid in structure_ids if sid not in structures]
        if unknown:
            raise ApiError(f"Not in this revision: {', '.join(unknown)}")
        selected = list(dict.fromkeys(structure_ids))
    skipped = selected[MAX_OVERLAYS:]
    selected = selected[:MAX_OVERLAYS]
    frame = api.frame(case_id, rid, plane, position_mm, selected, compare)
    meta = frame["meta"]
    presentation = frame["presentation"]
    if preset:
        if preset not in WINDOW_PRESETS:
            raise ApiError(f"preset must be one of {', '.join(WINDOW_PRESETS)}")
        window, level = WINDOW_PRESETS[preset]
    window = float(window if window is not None else presentation.get("default_window") or 400)
    level = float(level if level is not None else presentation.get("default_level") or 40)
    frame_matrix = np.asarray(meta["pixel_to_ras"], dtype=np.float64)
    pixel_mm2 = float(np.linalg.norm(frame_matrix[:3, 0]) * np.linalg.norm(frame_matrix[:3, 1]))
    layers, legend = [], []
    for layer in frame["overlays"]:
        structure = structures.get(layer["id"], {})
        area = int(layer["current"].sum())
        parent_area = None if layer["parent"] is None else int(layer["parent"].sum())
        if not area and not parent_area:
            continue
        layers.append((_rgb(structure.get("color")), layer["current"], layer["parent"]))
        row = {"id": layer["id"], "name": structure.get("name"), "color": structure.get("color"), "area_mm2": round(area * pixel_mm2, 1)}
        if parent_area is not None:
            row["parent_area_mm2"] = round(parent_area * pixel_mm2, 1)
        legend.append(row)
    png = render_png(frame["values"], window, level, layers, invert=presentation.get("photometric_interpretation") == "MONOCHROME1")
    width, height = png_size(int(meta["width"]), int(meta["height"]))
    matrix = png_pixel_to_ras(meta)
    description = {
        "case_id": case_id,
        "revision_id": rid,
        "plane": plane,
        "position_mm": round(float(position_mm), 2),
        "position_range_mm": [round(bounds[0][axis], 2), round(bounds[1][axis], 2)],
        "image_size": [width, height],
        "pixel_mm": round(float(np.linalg.norm(matrix[:3, 0])), 3),
        "window": window,
        "level": level,
        "edges": orientation(matrix),
        "pixel_to_ras": _round(matrix.tolist(), 3),
        "visible_structures": legend,
        "note": "Masks are tinted with a solid edge in their colour. With compare, a white edge is the parent revision's mask. "
        "Give marks on this image as slice points: x right, y down, in this image's pixels.",
    }
    if skipped:
        description["not_drawn"] = f"{len(skipped)} more structures; pass structure_ids to choose"
    return png, description


# Marks for correction drafts.

DRAWINGS = ("scribble", "lasso", "bbox")


def native_drawing(geometry: dict, ras_points, plane: str) -> tuple[int, int, list[list[float]]]:
    """Map the RAS points of one drawing made on a patient-plane view to a native slice.

    Returns (axis, index, [[u, v], ...]). The API takes drawings on a source-voxel slice, and a
    scribble paints its radius within that slice, so the slice is the voxel axis nearest the view's
    normal. A patient plane is a native slice only when the scan's grid is aligned with it, so a
    drawing on an oblique or tilted series is refused instead of being moved.
    """
    affine = np.asarray(geometry["affine_ras"], dtype=np.float64)
    inverse = np.linalg.inv(affine)
    voxels = np.array([(inverse @ np.array([*point, 1.0]))[:3] for point in ras_points])
    spread = voxels.max(axis=0) - voxels.min(axis=0)
    directions = affine[:3, :3] / np.linalg.norm(affine[:3, :3], axis=0)
    axis = int(np.argmax(np.abs(directions[PLANE_AXIS[plane]])))
    if spread[axis] > 0.5:
        raise ApiError("This drawing does not lie on one slice of the scan's own grid, as on an oblique or tilted series. Use point marks instead.")
    index = int(np.floor(voxels[:, axis].mean() + 0.5))
    shape = geometry["shape"]
    if not 0 <= index < shape[axis]:
        raise ApiError("This drawing lies outside the scan")
    u, v = [a for a in range(3) if a != axis]
    return axis, index, [[round(float(p[u]), 3), round(float(p[v]), 3)] for p in voxels]


def build_mark(api: Scan3D, case_id: str, revision: dict, mark: dict, mark_id: str) -> dict:
    if not isinstance(mark, dict):
        raise ApiError("Each mark must be an object")
    kind = mark.get("kind", "point")
    include = mark.get("include", True)
    if not isinstance(include, bool):
        raise ApiError("include must be true or false")
    prompt: dict[str, Any] = {"id": mark_id, "kind": kind, "include": include}
    if "native" in mark:
        if kind == "point":
            raise ApiError("A point takes ras or slice, not native")
        native = mark["native"]
        if not isinstance(native, dict):
            raise ApiError("native must be {axis, index, vertices}")
        prompt.update(axis=native.get("axis"), index=native.get("index"), vertices=native.get("vertices"))
    elif kind == "point":
        if "ras" in mark:
            prompt["ras"] = mark["ras"]
        elif "slice" in mark:
            plane, position = _slice_spec(mark["slice"])
            prompt["ras"] = api.slice_to_ras(case_id, revision["id"], plane, position, [[mark["slice"].get("x"), mark["slice"].get("y")]])[0]
        else:
            raise ApiError("A point needs ras [R, A, S] or slice {plane, position_mm, x, y}")
    elif kind in DRAWINGS:
        # No RAS form: a straight stroke in RAS lies on several native planes, and would paint on one at random.
        if "slice" not in mark:
            raise ApiError(f"A {kind} needs slice {{plane, position_mm, points}} or native {{axis, index, vertices}}")
        plane, position = _slice_spec(mark["slice"])
        corners = mark["slice"].get("points")
        if not isinstance(corners, list) or not corners:
            raise ApiError("slice.points must be a list of [x, y] image pixels")
        if kind == "bbox":
            if len(corners) != 2:
                raise ApiError("A bbox needs two opposite corners")
            (x0, y0), (x1, y1) = _pair(corners[0], "A corner"), _pair(corners[1], "A corner")
            corners = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
        points = api.slice_to_ras(case_id, revision["id"], plane, position, corners)
        axis, index, vertices = native_drawing(api.reference_geometry(case_id, revision), points, plane)
        if kind == "bbox":
            us, vs = [p[0] for p in vertices], [p[1] for p in vertices]
            box = [[min(us), min(vs)], [max(us), max(vs)]]
            # A rectangle on a view is a native box only when the scan is not rotated within the slice.
            if any(min(abs(u - box[0][0]), abs(u - box[1][0])) > 0.5 or min(abs(v - box[0][1]), abs(v - box[1][1])) > 0.5 for u, v in vertices):
                raise ApiError("This box is rotated against the scan's own grid. Use a lasso instead.")
            vertices = box
        prompt.update(axis=axis, index=index, vertices=vertices)
    else:
        raise ApiError("kind must be point, scribble, lasso or bbox")
    if kind == "scribble" and "radius_mm" in mark:
        prompt["radius_mm"] = mark["radius_mm"]
    return prompt


# Compact views of records, so tool results stay readable.


def case_row(case: dict) -> dict:
    keys = ("id", "label", "status", "phase", "active_revision_id", "selected_series_uid", "created_at", "opened_at", "synthetic", "error")
    row = {key: case[key] for key in keys if case.get(key) is not None}
    row["series"] = [
        {key: series.get(key) for key in ("uid", "description", "modality", "slice_count", "supported", "unsupported_reason") if series.get(key) is not None}
        for series in case.get("series") or []
    ]
    return row


def structure_row(structure: dict) -> dict:
    row = {key: structure.get(key) for key in ("id", "name", "group", "color", "volume_ml", "voxel_count")}
    row["volume_ml"] = _round(row["volume_ml"], 2)
    if structure.get("centroid_ras"):
        row["centroid_ras"] = _round(structure["centroid_ras"], 1)
        row["bounds_ras"] = _round(structure["bounds_ras"], 1)
    if structure.get("qc"):
        row["qc"] = structure["qc"]
    return row


def revision_row(revision: dict, structures: bool = False) -> dict:
    keys = ("id", "parent_id", "kind", "source", "review_status", "created_at", "reference_image_id", "qc")
    row = {key: revision.get(key) for key in keys if revision.get(key) not in (None, [])}
    row["structure_count"] = len(revision.get("structures") or [])
    if revision.get("acquisition"):
        row["acquisition"] = revision["acquisition"]
    if structures:
        row["structures"] = [structure_row(s) for s in revision.get("structures") or []]
    return row


def job_row(job: dict) -> dict:
    keys = ("id", "case_id", "kind", "status", "stage", "error", "revision_id", "review_session_id", "created_at", "completed_at", "progress")
    row = {key: job.get(key) for key in keys if job.get(key) is not None}
    if job.get("result") is not None:
        text = json.dumps(job["result"])
        row["result"] = job["result"] if len(text) <= 2000 else {"keys": sorted(job["result"])}
    if job["status"] in JOB_RUNNING:
        row["hint"] = "Still running: call wait_for_job."
    return row


def session_row(session: dict) -> dict:
    keys = ("id", "case_id", "revision_id", "structure_id", "method", "status", "generation", "pending_count", "applied_prompt_count", "dirty", "job_id")
    row = {key: session.get(key) for key in keys}
    for key in ("error", "accepted_revision_id", "target"):
        if session.get(key):
            row[key] = session[key]
    row["marks"] = [{key: p.get(key) for key in ("id", "kind", "include")} for p in session.get("prompts") or []]
    candidate = session.get("candidate")
    if candidate:
        row["candidate"] = {
            "revision_id": candidate.get("revision_id"),
            "summary": candidate.get("summary"),
            "warnings": candidate.get("warnings"),
            "affected_structures": [{key: s.get(key) for key in ("id", "name", "role", "summary")} for s in candidate.get("affected_structures") or []],
        }
    return row


# The tools. Plain methods, so tests can call them without the MCP SDK.

INSTRUCTIONS = """3DScan is a research workbench for CT and MR segmentation, not a diagnostic device.
A case holds imported series. Preparing a series makes an image-only revision; a segmentation run makes a
revision with structures (masks). Each saved change makes a new revision, so activate_revision is undo.
Masks are authoritative: volumes are voxel counts times voxel volume. Coordinates are patient RAS millimetres
(+R right, +A anterior, +S superior).
To look, use view_slice_image. To correct a mask: start_correction, add_marks, run_correction, view the candidate
with view_slice_image(revision_id=candidate.revision_id, compare=true), then save_correction or discard_correction.
Drafts and cases can be shared with people in the app: pass expected_generation, or expected_active_revision_id,
from the result you acted on, so a write stops if someone else has changed them since.
Model runs queue on one GPU lane; long jobs return while running, so call wait_for_job."""


class SliceImage:
    def __init__(self, png: bytes, description: dict):
        self.png, self.description = png, description


class Tools:
    """Every MCP tool, in the order a client lists them. READ_ONLY names those that change nothing."""

    READ_ONLY = {
        "status",
        "list_cases",
        "get_case",
        "segmentation_options",
        "list_jobs",
        "wait_for_job",
        "list_revisions",
        "get_revision",
        "agreement",
        "probe",
        "view_slice_image",
        "get_correction",
        "api_get",
    }
    # Tools that drop work: marks, a draft, a running job, or which revision is active.
    DESTRUCTIVE = {"cancel_job", "activate_revision", "remove_last_marks", "discard_correction"}

    def __init__(self, api: Scan3D):
        self.api = api

    def _started(self, job: dict, wait_seconds: float) -> dict:
        return job_row(self.api.wait(job["id"], wait_seconds) if wait_seconds > 0 else job)

    def status(self) -> dict:
        """Server health, the signed-in account, imaging modalities and whether AI assist is available."""
        # First a route that needs sign-in, which signs in; health and session never ask to.
        interactive = self.api.get("/api/interactive/status")
        health = self.api.get("/api/health")
        session = self.api.get("/api/session")
        return {
            "health": health,
            "sign_in": {"mode": session.get("mode"), "user": session.get("user")},
            "ai_assist": {key: interactive.get(key) for key in ("available", "reason", "version", "capabilities")},
        }

    def list_cases(self) -> list[dict]:
        """Every case in the library, most recently opened first."""
        return [case_row(case) for case in self.api.get("/api/cases")]

    def get_case(self, case_id: str) -> dict:
        """One case with its series, and the active revision's structures with volumes in mL."""
        case = self.api.case(case_id)
        row = case_row(case)
        if case.get("active_revision_id"):
            row["active_revision"] = revision_row(self.api.revision(case_id, case["active_revision_id"]), structures=True)
        return row

    def create_demo_case(self) -> dict:
        """Create the synthetic phantom case (a CT with seven masks). Safe for trying tools."""
        return case_row(self.api.post("/api/cases/demo"))

    def import_folder(self, path: str, label: str | None = None) -> dict:
        """Import a DICOM folder or ZIP that is on the 3DScan server, under its SCAN3D_IMPORT_ROOTS. Administrators only."""
        return case_row(self.api.post("/api/cases/import-local", _params({"path": path, "label": label})))

    def segmentation_options(self, modality: str = "CT") -> dict:
        """Segmentation tasks and GPU paths for a modality (CT or MR)."""
        options = self.api.get("/api/segmentation-options", modality=modality)
        keys = ("id", "label", "available", "license_required", "license_available", "class_count", "engine", "description")
        return {
            "defaults": options.get("defaults"),
            "tasks": [{key: task.get(key) for key in keys} for task in options.get("tasks") or []],
            "inference_paths": options.get("inference_paths"),
        }

    def prepare_series(
        self,
        case_id: str,
        phase: str,
        series_uid: str | None = None,
        profile: str = "chest_abdomen",
        cardiac_phase: str | None = None,
        rr_percent: float | None = None,
        wait_seconds: float = 120,
    ) -> dict:
        """Prepare a series for viewing, which makes an image-only revision. phase is free text for contrast timing
        (for example "portal venous", "non-contrast" or "unknown"). profile is chest, abdomen, chest_abdomen, corcta,
        cardiac, chest_abdomen_pelvis, abdomen_pelvis, pelvis, head_neck, breast, whole_body, other or unknown."""
        case = self.api.case(case_id)
        if series_uid is None:
            supported = [s["uid"] for s in case.get("series") or [] if s.get("supported")]
            if case.get("selected_series_uid"):
                series_uid = case["selected_series_uid"]
            elif len(supported) == 1:
                series_uid = supported[0]
            else:
                raise ApiError(f"Choose series_uid: this case has {len(supported)} supported series")
        body = {"series_uid": series_uid, "profile": profile, "phase": phase, "cardiac_phase": cardiac_phase, "rr_percent": rr_percent}
        body["expected_active_revision_id"] = case.get("active_revision_id")
        return self._started(self.api.post(f"/api/cases/{case_id}/process", _params(body)), wait_seconds)

    def run_segmentation(
        self,
        case_id: str,
        task: str | None = None,
        revision_id: str | None = None,
        robust_crop: bool | None = None,
        body_seg: bool | None = None,
        remove_small_blobs: bool | None = None,
        high_order_resampling: bool | None = None,
        inference_path: str | None = None,
        wait_seconds: float = 60,
    ) -> dict:
        """Run a segmentation task (see segmentation_options) on the active revision. Uses the GPU and can take minutes."""
        body = {
            "revision_id": self.api.revision_id(case_id, revision_id),
            "task": task,
            "robust_crop": robust_crop,
            "body_seg": body_seg,
            "remove_small_blobs": remove_small_blobs,
            "high_order_resampling": high_order_resampling,
            "inference_path": inference_path,
        }
        return self._started(self.api.post(f"/api/cases/{case_id}/segment", _params(body)), wait_seconds)

    def list_jobs(self, case_id: str) -> list[dict]:
        """A case's jobs, newest first."""
        return [job_row(job) for job in self.api.get(f"/api/cases/{case_id}/jobs")]

    def wait_for_job(self, job_id: str, wait_seconds: float = 300) -> dict:
        """Wait up to wait_seconds for a job to finish, and return it as it stands."""
        return job_row(self.api.wait(job_id, wait_seconds))

    def cancel_job(self, job_id: str) -> dict:
        """Cancel a queued or running job."""
        return job_row(self.api.post(f"/api/jobs/{job_id}/cancel"))

    def list_revisions(self, case_id: str) -> list[dict]:
        """A case's revision history, newest first. The active one is the case's active_revision_id."""
        return [revision_row(revision) for revision in self.api.get(f"/api/cases/{case_id}/revisions")]

    def get_revision(self, case_id: str, revision_id: str | None = None) -> dict:
        """One revision with its structures, volumes in mL, centroids and bounds. Defaults to the active revision."""
        return revision_row(self.api.revision(case_id, revision_id), structures=True)

    def activate_revision(self, case_id: str, revision_id: str, expected_active_revision_id: str | None = None) -> dict:
        """Make an earlier or later revision active again: this is undo and redo, and changes no mask.
        Pass expected_active_revision_id, the active revision you last saw, to refuse if someone has saved since."""
        expected = expected_active_revision_id or self.api.case(case_id).get("active_revision_id")
        return case_row(self.api.post(f"/api/cases/{case_id}/activate", _params({"revision_id": revision_id, "expected_active_revision_id": expected})))

    def set_review_status(self, case_id: str, revision_id: str, review_status: str) -> dict:
        """Mark a segmentation revision unreviewed, reviewed or corrected."""
        return revision_row(self.api.patch(f"/api/cases/{case_id}/revisions/{revision_id}", {"review_status": review_status}))

    def agreement(self, case_id: str, revision_id: str | None = None) -> dict:
        """How far each saved mask has moved from the model's original: Dice, HD95 and volume change."""
        rid = self.api.revision_id(case_id, revision_id)
        report = self.api.get(f"/api/cases/{case_id}/revisions/{rid}/agreement")
        keys = ("id", "name", "unchanged", "dice", "hd95_mm", "volume_change_ml", "added_voxels", "removed_voxels", "edit_kind")
        return {
            "revision_id": rid,
            "review_status": report.get("review_status"),
            "summary": report.get("summary"),
            "changed": [{key: _round(row.get(key), 3) for key in keys} for row in report.get("structures") or [] if not row.get("unchanged")],
            "excluded": report.get("excluded"),
        }

    def probe(self, case_id: str, ras: list[float], revision_id: str | None = None) -> dict:
        """The image value (HU on CT) at a patient RAS point in millimetres."""
        if len(ras) != 3:
            raise ApiError("ras must be [R, A, S]")
        rid = self.api.revision_id(case_id, revision_id)
        return self.api.get(f"/api/cases/{case_id}/revisions/{rid}/probe", x=ras[0], y=ras[1], z=ras[2])

    def view_slice_image(
        self,
        case_id: str,
        plane: str = "axial",
        position_mm: float | None = None,
        revision_id: str | None = None,
        structure_ids: list[str] | None = None,
        compare: bool = False,
        preset: str | None = None,
        window: float | None = None,
        level: float | None = None,
    ) -> SliceImage:
        """See one axial, coronal or sagittal slice as an image, with masks drawn on it.

        position_mm is the patient coordinate along the plane's axis (S for axial, A for coronal, R for
        sagittal), the scan's centre by default. Without structure_ids, every mask reaching the plane is drawn.
        compare draws the parent revision's masks as white edges, which shows a correction candidate's change.
        preset is soft_tissue, lung, bone, brain, liver or angio, or give window and level. The text lists
        the edges' patient directions, the drawn structures' areas and pixel_to_ras for this image."""
        return SliceImage(*view_slice(self.api, case_id, plane, position_mm, revision_id, structure_ids, compare, window, level, preset))

    def start_correction(self, case_id: str, structure_id: str, method: str = "nninteractive", revision_id: str | None = None) -> dict:
        """Open a correction draft for one structure. method is nninteractive (AI assist) or direct (paint and erase).
        An open draft for the same structure and method is returned as it stands."""
        body = {"revision_id": self.api.revision_id(case_id, revision_id), "structure_id": structure_id, "method": method}
        return session_row(self.api.post(f"/api/cases/{case_id}/reviews", body))

    def start_new_segment(
        self, case_id: str, name: str, method: str = "nninteractive", group: str | None = None, color: str | None = None, revision_id: str | None = None
    ) -> dict:
        """Open a draft for a new, empty segment, to be drawn with marks."""
        body = _params({"revision_id": self.api.revision_id(case_id, revision_id), "name": name, "method": method, "group": group, "color": color})
        return session_row(self.api.post(f"/api/cases/{case_id}/reviews/new-structure", body))

    def get_correction(self, case_id: str, session_id: str) -> dict:
        """A correction draft: its marks, status and current candidate."""
        return session_row(self.api.session(case_id, session_id))

    def add_marks(self, case_id: str, session_id: str, marks: list[dict], expected_generation: int | None = None) -> dict:
        """Add marks to a draft. Nothing runs until run_correction.

        Each mark is {kind, include, ...}. include true adds, false removes.
        - point (AI assist): {"ras": [R, A, S]} or {"slice": {"plane", "position_mm", "x", "y"}}, where x and y are
          pixels of the view_slice_image of that plane and position.
        - scribble, lasso or bbox: {"slice": {"plane", "position_mm", "points": [[x, y], ...]}}; a bbox takes two
          opposite corners. A scribble may set radius_mm (default 3). A direct draft takes scribbles only: include
          true paints, false erases. Drawings must lie on one slice of the scan's own grid.
        - Advanced: a drawing may give {"native": {"axis", "index", "vertices"}} in source voxels, as docs/API.md Prompts.
        """
        session = self.api.session(case_id, session_id)
        revision = self.api.revision(case_id, session["revision_id"])
        prompts = list(session.get("prompts") or [])
        taken = {p["id"] for p in prompts}
        number = len(prompts)
        for mark in marks:
            number += 1
            while f"mcp-{number}" in taken:
                number += 1
            prompts.append(build_mark(self.api, case_id, revision, mark, f"mcp-{number}"))
            taken.add(f"mcp-{number}")
        body = {"expected_generation": _generation(session, expected_generation), "prompts": prompts}
        return session_row(self.api.patch(f"/api/cases/{case_id}/reviews/{session_id}", body))

    def remove_last_marks(self, case_id: str, session_id: str, count: int = 1, expected_generation: int | None = None) -> dict:
        """Remove the last count marks from a draft, which is undo. Removing every mark resets it."""
        session = self.api.session(case_id, session_id)
        prompts = list(session.get("prompts") or [])
        keep = prompts[: max(0, len(prompts) - max(0, count))]
        body = {"expected_generation": _generation(session, expected_generation), "prompts": keep}
        return session_row(self.api.patch(f"/api/cases/{case_id}/reviews/{session_id}", body))

    def run_correction(
        self,
        case_id: str,
        session_id: str,
        full_refine: bool = False,
        include_only_adds: bool = False,
        inference_path: str | None = None,
        wait_seconds: float = 120,
        expected_generation: int | None = None,
    ) -> dict:
        """Apply the draft's pending marks and make a candidate, not a saved revision.
        full_refine (AI assist) refines the whole mask. include_only_adds guards this step: all-Include marks remove
        nothing and all-Exclude marks add nothing. View the result with view_slice_image(revision_id=candidate.revision_id,
        compare=true)."""
        session = self.api.session(case_id, session_id)
        body = {"generation": _generation(session, expected_generation), "full_refine": full_refine or None, "polarity_guard": include_only_adds or None}
        body["inference_path"] = inference_path
        job = self.api.post(f"/api/cases/{case_id}/reviews/{session_id}/predict", _params(body))
        job = self.api.wait(job["id"], wait_seconds) if wait_seconds > 0 else job
        return {"job": job_row(job), "draft": session_row(self.api.session(case_id, session_id))}

    def save_correction(self, case_id: str, session_id: str, wait_seconds: float = 120, expected_generation: int | None = None) -> dict:
        """Save the draft's current candidate as a new active revision marked corrected. Pass the expected_generation
        of the candidate you viewed, so a candidate someone else has since replaced is never saved unseen."""
        session = self.api.session(case_id, session_id)
        job = self.api.post(f"/api/cases/{case_id}/reviews/{session_id}/accept", {"generation": _generation(session, expected_generation)})
        job = self.api.wait(job["id"], wait_seconds) if wait_seconds > 0 else job
        return {"job": job_row(job), "draft": session_row(self.api.session(case_id, session_id))}

    def discard_correction(self, case_id: str, session_id: str, expected_generation: int | None = None) -> dict:
        """Close a draft without saving. Its marks and candidate are dropped; saved revisions are untouched.
        A draft is shared, so pass expected_generation to refuse if someone else has changed it since."""
        session = self.api.session(case_id, session_id)
        body = {"expected_generation": _generation(session, expected_generation)}
        return session_row(self.api.post(f"/api/cases/{case_id}/reviews/{session_id}/discard", body))

    def export_seg_file(
        self, case_id: str, output_path: str, revision_id: str | None = None, structure_ids: list[str] | None = None, wait_seconds: float = 120
    ) -> dict:
        """Write a revision's masks as one DICOM SEG file at output_path, on the machine running this MCP server.
        The file carries the source scan's patient and study identity. It never overwrites a file."""
        target = Path(output_path).expanduser()
        if target.exists():
            raise ApiError(f"{target} already exists")
        if not target.parent.is_dir():
            raise ApiError(f"{target.parent} is not a folder")
        rid = self.api.revision_id(case_id, revision_id)
        job = self.api.post(f"/api/cases/{case_id}/revisions/{rid}/dicom-seg", _params({"delivery": "file", "structure_ids": structure_ids}))
        job = self.api.wait(job["id"], wait_seconds)
        if job["status"] in JOB_RUNNING:
            # wait_for_job would finish the job but never write the file; a repeat call reuses the job.
            return {"job": {**job_row(job), "hint": "Still running: call export_seg_file again with the same arguments to write the file."}}
        if job["status"] != "completed":
            return {"job": job_row(job)}
        export_id = ((job.get("result") or {}).get("dicom_seg") or {}).get("export_id")
        if not export_id:
            raise ApiError("The export finished without a file")
        data = self.api.request("GET", f"/api/cases/{case_id}/revisions/{rid}/dicom-seg/{export_id}/segment.dcm").content
        with open(target, "xb") as handle:
            handle.write(data)
        return {"job": job_row(job), "path": str(target), "bytes": len(data)}

    def api_get(self, path: str, params: dict | None = None) -> Any:
        """Read any JSON GET route of the 3DScan API (docs/API.md), for what the other tools leave out."""
        if not path.startswith("/api/"):
            raise ApiError("path must start with /api/")
        response = self.api.request("GET", path, params=params or {})
        if "json" not in response.headers.get("content-type", ""):
            return {"content_type": response.headers.get("content-type"), "bytes": len(response.content), "note": "Not JSON, so not returned"}
        return response.json()


def tool_names() -> list[str]:
    return [name for name, value in vars(Tools).items() if callable(value) and not name.startswith("_")]


def build_server(api: Scan3D):
    from mcp.server.mcpserver import Image, MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp_types import ToolAnnotations

    server = MCPServer("3DScan", instructions=INSTRUCTIONS)
    tools = Tools(api)
    for name in tool_names():
        method = getattr(tools, name)
        read_only = name in Tools.READ_ONLY

        def wrap(method):
            @functools.wraps(method)
            def call(*args, **kwargs):
                try:
                    result = method(*args, **kwargs)
                except ApiError as exc:
                    raise ToolError(str(exc)) from None
                except httpx.HTTPError as exc:
                    raise ToolError(f"Cannot reach 3DScan: {exc}") from None
                except OSError as exc:
                    raise ToolError(f"File error: {exc}") from None
                except (TypeError, ValueError, KeyError, IndexError) as exc:
                    # Usually a malformed argument; the SDK would otherwise hide the reason.
                    raise ToolError(f"Invalid input ({type(exc).__name__}: {exc})") from None
                if isinstance(result, SliceImage):
                    return [Image(data=result.png, format="png"), json.dumps(result.description)]
                return result

            return call

        annotations = ToolAnnotations(read_only_hint=read_only, destructive_hint=name in Tools.DESTRUCTIVE, open_world_hint=False)
        # Plain text results: structured copies would double what reaches the model.
        server.tool(annotations=annotations, structured_output=False)(wrap(method))
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description="MCP server for a running 3DScan, over stdio")
    parser.add_argument("--url", default=os.environ.get("SCAN3D_MCP_URL"), help="3DScan address, such as http://127.0.0.1:9010 (or SCAN3D_MCP_URL)")
    args = parser.parse_args(argv)
    if not args.url:
        parser.error("--url or SCAN3D_MCP_URL is required")
    username, password = os.environ.get("SCAN3D_MCP_USERNAME"), os.environ.get("SCAN3D_MCP_PASSWORD")
    password_file = os.environ.get("SCAN3D_MCP_PASSWORD_FILE")
    if password_file and not password:
        password = Path(password_file).read_text().strip()
    api = Scan3D(args.url, username=username, password=password)
    # The SDK logs at INFO, which would add a stderr line for every API request.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    print(f"3DScan MCP server for {api.origin}", file=sys.stderr)
    build_server(api).run("stdio")


if __name__ == "__main__":
    main()
