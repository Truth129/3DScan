"""Physical mask surfaces and dependency-free glTF 2.0 binary packaging."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import re
import struct
import tempfile
import threading
import time
from typing import NamedTuple
import zipfile

import numpy as np

from .imaging import _binary, _foreground_bounds


_DISPLAY_ORGAN_TRIANGLES = 12000
_DISPLAY_TREE_TRIANGLES = 50000
_DISPLAY_LOCAL_ERROR_MM = 0.25
_MESH_IMPLEMENTATION = "physical-flying-edges-v2"
# Detailed exports keep interoperable float32 GLBs; display assets are quantized.
_ASSET_IMPLEMENTATION = "physical-glb-v2"
_DISPLAY_ASSET_IMPLEMENTATION = "quantized-glb-v3"
# Display vertices snap to one absolute power-of-two grid in scene metres (about
# 0.031 mm, 2 m per uint16 range). Shared by all structures, coincident boundaries
# stay coincident, and requantizing decoded positions reproduces the same integers.
_POSITION_STEP_M = 2.0**-15
_QUANTIZATION = "KHR_mesh_quantization"
# uint16 indices, excluding the primitive restart value glTF reserves.
_PRIMITIVE_VERTICES = 65535
_SMP_CONFIGURATION = None
_SMP_LOCK = threading.Lock()


class _Quantized(NamedTuple):
    """Display arrays as stored: scene metres = translation + scale * positions.

    Meshes beyond uint16 indices are consecutive primitives, ``parts`` holding
    (vertices, triangles) for each; only their seam vertices are repeated.
    """

    positions: np.ndarray  # (n, 3) uint16 grid offsets, parts concatenated
    normals: np.ndarray  # (n, 3) int8, normalized by 127
    faces: np.ndarray  # (m, 3) indices into the concatenated vertices
    translation: tuple
    scale: float
    parts: tuple


def _configure_smp():
    """Initialize VTK's available native parallel contour backend once."""
    from vtkmodules.vtkCommonCore import vtkSMPTools

    global _SMP_CONFIGURATION
    with _SMP_LOCK:
        if _SMP_CONFIGURATION is None:
            configured = os.environ.get("SCAN3D_MESH_THREADS", "16")
            try:
                threads = int(configured)
            except ValueError as error:
                raise ValueError("SCAN3D_MESH_THREADS must be a positive integer") from error
            if threads <= 0:
                raise ValueError("SCAN3D_MESH_THREADS must be a positive integer")
            threads = min(threads, os.cpu_count() or 1)
            # Wheels may lack a parallel backend; retain the supported fallback
            # and report it explicitly rather than claiming GPU acceleration.
            vtkSMPTools.SetBackend("STDThread")
            vtkSMPTools.Initialize(threads)
            _SMP_CONFIGURATION = {"backend": vtkSMPTools.GetBackend(), "threads": vtkSMPTools.GetEstimatedNumberOfThreads()}
        return dict(_SMP_CONFIGURATION)


def hash_file(path: Path) -> str:
    """Hash the complete immutable mask file, including its physical geometry."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _key(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def surface_asset_path(cache_dir: Path, key: str) -> Path:
    """Resolve a content-addressed asset without allowing path traversal."""
    if not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError("Invalid surface asset key")
    return Path(cache_dir) / "assets" / key[:2] / f"{key}.glb"


@contextmanager
def _cache_lock(cache_dir, key):
    directory = Path(cache_dir) / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"{key}.lock").open("a+b") as handle:
        # flock also serializes independent threads using independently opened FDs.
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _atomic_destination(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".writing", dir=path.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        yield temporary
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _metadata_path(path):
    return path.with_name(path.name + ".json")


def _valid_cache(path):
    try:
        metadata = json.loads(_metadata_path(path).read_text())
        if path.stat().st_size != metadata["bytes"] or hash_file(path) != metadata["sha256"]:
            return None
        return metadata
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _record_cache(path, **metadata):
    metadata.update(bytes=path.stat().st_size, sha256=hash_file(path))
    with _atomic_destination(_metadata_path(path)) as temporary:
        temporary.write_text(json.dumps(metadata, sort_keys=True, separators=(",", ":")))


def _delicate(structure):
    identity = " ".join(str(structure.get(key, "")) for key in ("id", "name", "group")).lower()
    return any(token in identity for token in ("arter", "vein", "vena", "vessel", "aorta", "airway", "trachea", "bronch"))


def _surface_identity(structure, detailed):
    from vtkmodules.vtkCommonCore import vtkVersion

    stable_id = str(structure["id"])
    color = str(structure.get("color", "#b7c4d0"))
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
        raise ValueError("Structure colors must use #RRGGBB")
    digest = hash_file(Path(structure["mask_file"]))
    base_key = _key({"mask": digest, "implementation": _MESH_IMPLEMENTATION, "vtk": vtkVersion.GetVTKVersion()})
    parameters = (
        None
        if detailed
        else {
            "target": _DISPLAY_TREE_TRIANGLES if _delicate(structure) else _DISPLAY_ORGAN_TRIANGLES,
            "local_error_mm": _DISPLAY_LOCAL_ERROR_MM,
            "preserve_topology": True,
        }
    )
    implementation = _ASSET_IMPLEMENTATION if detailed else _DISPLAY_ASSET_IMPLEMENTATION
    asset_key = _key(
        {
            "base": base_key,
            "implementation": implementation,
            "detailed": bool(detailed),
            "parameters": parameters,
            "id": stable_id,
            "name": structure.get("name", stable_id),
            "color": color,
        }
    )
    return base_key, asset_key


def _display_simplification():
    return {
        "algorithm": "vtkDecimatePro",
        "preserve_topology": True,
        "local_error_mm": _DISPLAY_LOCAL_ERROR_MM,
        "organ_target_triangles": _DISPLAY_ORGAN_TRIANGLES,
        "vascular_airway_target_triangles": _DISPLAY_TREE_TRIANGLES,
        "targets_are_soft": True,
    }


def _contour(mask_path, decoded_mask=None):
    # VTK is deliberately imported in the numerical worker, not API startup.
    from vtkmodules.util.numpy_support import numpy_to_vtk, vtk_to_numpy
    from vtkmodules.vtkCommonDataModel import vtkImageData, vtkPolyData
    from vtkmodules.vtkFiltersGeneral import vtkDiscreteFlyingEdges3D

    _configure_smp()

    # A bounded per-structure worker can share the exact decode already used
    # for voxel measurements/QC instead of inflating the same NIfTI twice.
    mask, _, affine = _binary(mask_path) if decoded_mask is None else decoded_mask
    box = _foreground_bounds(mask)
    if box is None:
        return None
    low, high = box
    cropped = mask[tuple(slice(int(a), int(b) + 1) for a, b in zip(low, high))]
    padded = np.pad(cropped.astype(np.uint8), 1)
    image = vtkImageData()
    image.SetDimensions(*padded.shape)
    image.SetOrigin(*(low.astype(float) - 1))
    image.SetSpacing(1, 1, 1)
    image.GetPointData().SetScalars(numpy_to_vtk(padded.ravel(order="F"), deep=True))
    contour = vtkDiscreteFlyingEdges3D()
    contour.SetInputData(image)
    contour.SetValue(0, 1)
    contour.ComputeNormalsOff()
    contour.Update()
    mesh = vtkPolyData()
    mesh.DeepCopy(contour.GetOutput())
    if not mesh.GetNumberOfPolys():
        return None
    points = vtk_to_numpy(mesh.GetPoints().GetData()).astype(np.float64)
    points = points @ affine[:3, :3].T + affine[:3, 3]
    mesh.GetPoints().SetData(numpy_to_vtk(np.ascontiguousarray(points), deep=True))
    if np.linalg.det(affine[:3, :3]) < 0:
        from vtkmodules.vtkCommonDataModel import vtkCellArray

        faces = vtk_to_numpy(mesh.GetPolys().GetConnectivityArray()).reshape(-1, 3).copy()
        faces[:, [1, 2]] = faces[:, [2, 1]]
        cells = vtkCellArray()
        cells.SetData(numpy_to_vtk(np.arange(0, faces.size + 1, 3, dtype=np.int64), deep=True), numpy_to_vtk(faces.reshape(-1), deep=True))
        mesh.SetPolys(cells)
    return mesh


def _mesh_arrays(mesh, detailed, delicate=False):
    from vtkmodules.util.numpy_support import vtk_to_numpy
    from vtkmodules.vtkCommonDataModel import vtkPolyData
    from vtkmodules.vtkFiltersCore import vtkDecimatePro, vtkPolyDataNormals

    _configure_smp()

    if mesh is None:
        return None
    # Triangle targets are soft: preserve topology and small branches even when
    # this prevents meeting the target. Error is evaluated in patient mm, before
    # conversion to glTF metres. Detailed geometry is never simplified.
    target = _DISPLAY_TREE_TRIANGLES if delicate else _DISPLAY_ORGAN_TRIANGLES
    if not detailed and mesh.GetNumberOfPolys() > target:
        decimator = vtkDecimatePro()
        decimator.SetInputData(mesh)
        decimator.SetTargetReduction(1 - target / mesh.GetNumberOfPolys())
        decimator.PreserveTopologyOn()
        decimator.BoundaryVertexDeletionOff()
        decimator.SplittingOff()
        decimator.SetErrorIsAbsolute(1)
        decimator.SetAbsoluteError(_DISPLAY_LOCAL_ERROR_MM)
        decimator.AccumulateErrorOn()
        decimator.Update()
        if decimator.GetOutput().GetNumberOfPolys():
            reduced = vtkPolyData()
            reduced.DeepCopy(decimator.GetOutput())
            mesh = reduced
    normals = vtkPolyDataNormals()
    normals.SetInputData(mesh)
    normals.SplittingOff()
    normals.ConsistencyOn()
    # Contours already point from foreground into background. Autorientation
    # would invert cavity faces, changing the meaning of an enclosed void.
    normals.AutoOrientNormalsOff()
    normals.ComputePointNormalsOn()
    normals.Update()
    output = normals.GetOutput()
    positions = vtk_to_numpy(output.GetPoints().GetData()).astype(np.float64)
    normal_values = vtk_to_numpy(output.GetPointData().GetNormals()).astype(np.float64)
    faces = vtk_to_numpy(output.GetPolys().GetConnectivityArray()).reshape(-1, 3).astype(np.uint32)
    # This rotation is right handed: the reflection of R and swap of A/S cancel.
    scene_rotation = np.array([[-1, 0, 0], [0, 0, 1], [0, 1, 0]], dtype=float)
    positions = (positions @ scene_rotation.T / 1000).astype("<f4")
    normal_values = (normal_values @ scene_rotation.T).astype("<f4")
    a, b, c = (positions[faces[:, i]] for i in range(3))
    valid = np.linalg.norm(np.cross(b - a, c - a), axis=1) > 1e-16
    faces = faces[valid]
    return positions, normal_values, faces


def _mesh(mask_path, detailed, delicate=False):
    """Legacy direct path, also useful for numerical equivalence tests."""
    return _mesh_arrays(_contour(mask_path), detailed, delicate)


def _save_base(mesh, path):
    from vtkmodules.util.numpy_support import vtk_to_numpy

    if mesh is None:
        points = np.empty((0, 3), dtype=np.float64)
        faces = np.empty((0, 3), dtype=np.int64)
    else:
        points = vtk_to_numpy(mesh.GetPoints().GetData()).astype(np.float64)
        faces = vtk_to_numpy(mesh.GetPolys().GetConnectivityArray()).reshape(-1, 3).astype(np.int64)
    with _atomic_destination(path) as temporary:
        with temporary.open("wb") as handle:
            # No compression: CPU time is more valuable than compressing these
            # reusable arrays. The full CT/masks remain their original format.
            np.savez(handle, points=points, faces=faces)
    _record_cache(path)


def _load_base(path):
    from vtkmodules.util.numpy_support import numpy_to_vtk
    from vtkmodules.vtkCommonCore import vtkPoints
    from vtkmodules.vtkCommonDataModel import vtkCellArray, vtkPolyData

    with np.load(path, allow_pickle=False) as saved:
        points = saved["points"]
        faces = saved["faces"]
    if points.ndim != 2 or points.shape[1] != 3 or faces.ndim != 2 or faces.shape[1] != 3:
        raise ValueError("Malformed cached contour")
    if not len(faces):
        return None
    if not np.isfinite(points).all() or faces.min() < 0 or faces.max() >= len(points):
        raise ValueError("Invalid cached contour")
    mesh = vtkPolyData()
    vtk_points = vtkPoints()
    vtk_points.SetData(numpy_to_vtk(np.ascontiguousarray(points, dtype=np.float64), deep=True))
    mesh.SetPoints(vtk_points)
    cells = vtkCellArray()
    cells.SetData(
        numpy_to_vtk(np.arange(0, faces.size + 1, 3, dtype=np.int64), deep=True),
        numpy_to_vtk(np.ascontiguousarray(faces, dtype=np.int64).reshape(-1), deep=True),
    )
    mesh.SetPolys(cells)
    return mesh


def prepare_surface(structure, cache_dir: Path, detailed=False, progress=None, decoded_mask=None) -> dict:
    """Prepare one immutable GLB, reusing both contour and presentation caches.

    Contours are in patient mm and independent of color/display settings. Cache
    identity includes full mask bytes, physical meshing implementation, VTK,
    decimation parameters, and the exported structure metadata. Sidecar digests
    detect interrupted writes or damaged entries before reuse.
    """
    stable_id = str(structure["id"])
    cache = {"base_hits": 0, "base_misses": 0, "surface_hits": 0, "surface_misses": 0}
    timings = {key: 0.0 for key in ("hash", "cache_io", "contour", "simplify_normals", "package")}
    if progress:
        progress(f"Preparing {'detailed' if detailed else 'display'} surface: {structure.get('name', stable_id)}")
    started = time.perf_counter()
    base_key, asset_key = _surface_identity(structure, detailed)
    timings["hash"] += time.perf_counter() - started
    delicate = _delicate(structure)
    asset_path = surface_asset_path(cache_dir, asset_key)
    result = {
        "id": stable_id,
        "key": asset_key,
        "path": str(asset_path.resolve()),
        "triangles": 0,
        "cache": cache,
        "timings_seconds": timings,
        "mesh_processing": _configure_smp(),
    }
    with _cache_lock(cache_dir, asset_key):
        started = time.perf_counter()
        metadata = _valid_cache(asset_path)
        if metadata is not None and (not isinstance(metadata.get("triangles"), int) or metadata["triangles"] < 0):
            metadata = None
        timings["cache_io"] += time.perf_counter() - started
        if metadata is not None:
            cache["surface_hits"] += 1
            result["triangles"] = metadata["triangles"]
            if not result["triangles"]:
                result["path"] = None
            return result
        cache["surface_misses"] += 1
        base_path = Path(cache_dir) / "contours" / base_key[:2] / f"{base_key}.npz"
        with _cache_lock(cache_dir, base_key):
            started = time.perf_counter()
            mesh = None
            valid = _valid_cache(base_path)
            if valid is not None:
                try:
                    mesh = _load_base(base_path)
                except (OSError, ValueError, KeyError, EOFError, zipfile.BadZipFile):
                    valid = None
            timings["cache_io"] += time.perf_counter() - started
            if valid is None:
                cache["base_misses"] += 1
                if progress:
                    progress(f"Extracting physical surface: {structure.get('name', stable_id)}")
                started = time.perf_counter()
                if decoded_mask is None:
                    mesh = _contour(Path(structure["mask_file"]))
                else:
                    mesh = _contour(Path(structure["mask_file"]), decoded_mask=decoded_mask)
                timings["contour"] += time.perf_counter() - started
                started = time.perf_counter()
                _save_base(mesh, base_path)
                timings["cache_io"] += time.perf_counter() - started
            else:
                cache["base_hits"] += 1
        if progress:
            progress(f"Finishing {'detailed' if detailed else 'display'} surface: {structure.get('name', stable_id)}")
        started = time.perf_counter()
        arrays = _mesh_arrays(mesh, detailed, delicate)
        timings["simplify_normals"] += time.perf_counter() - started
        started = time.perf_counter()
        if arrays is None or not len(arrays[2]):
            # Empty entries are also cached, with a zero-byte sentinel that is
            # never advertised as a GLB to the browser.
            with _atomic_destination(asset_path) as temporary:
                temporary.write_bytes(b"")
            result["path"] = None
        else:
            _write_model([(structure, arrays)], asset_path, detailed)
            result["triangles"] = len(arrays[2])
        _record_cache(asset_path, triangles=result["triangles"])
        timings["package"] += time.perf_counter() - started
    return result


def _split(faces, limit):
    """Greedy consecutive face ranges, each referencing at most ``limit`` vertices.

    VTK's spatially coherent face order keeps repeated seam vertices rare (under
    3% on real display meshes), and each range keeps its vertices in VTK order.
    """
    references = faces.reshape(-1)
    order = np.argsort(references, kind="stable")
    previous = np.full(len(references), -1)
    repeated = references[order[1:]] == references[order[:-1]]
    previous[order[1:][repeated]] = order[:-1][repeated]
    chunks, start, window = [], 0, 4 * limit
    while start < len(faces):
        # Vertices first referenced since `start`, counted at each triangle end.
        stop = min(len(faces), start + window)
        fresh = np.cumsum(previous[3 * start : 3 * stop] < 3 * start)[2::3]
        end = start + int(np.searchsorted(fresh, limit, side="right"))
        if end == stop < len(faces):
            window *= 2
            continue
        used = np.unique(faces[start:end])
        chunks.append((used, np.searchsorted(used, faces[start:end])))
        start = end
    return chunks


def _quantize(arrays, limit=_PRIMITIVE_VERTICES):
    """Encode float display arrays for KHR_mesh_quantization, idempotently.

    Positions round to the shared grid, at most half a step per axis (0.015 mm,
    or 0.026 mm in 3D). The node's uniform scale keeps stored normals valid under
    Three's normal matrix. Vertex order and triangles are unchanged unless a mesh
    needs splitting for uint16 indices.
    """
    if isinstance(arrays, _Quantized):
        return arrays
    positions, normals, faces = arrays
    points = np.asarray(positions, np.float64)
    scale = _POSITION_STEP_M
    while True:
        cells = np.rint(points / scale)
        low = cells.min(axis=0)
        if (cells.max(axis=0) - low).max() <= 65535:
            break
        scale *= 2  # Only a structure longer than 2 m needs a coarser grid.
    grid = (cells - low).astype("<u2")
    encoded = np.clip(np.rint(np.asarray(normals, np.float64) * 127), -127, 127).astype("i1")
    translation = tuple((low * scale).tolist())
    if len(grid) <= limit:
        return _Quantized(grid, encoded, faces, translation, scale, ((len(grid), len(faces)),))
    chunks = _split(faces, limit)
    offsets = np.cumsum([0] + [len(used) for used, _ in chunks[:-1]])
    return _Quantized(
        np.concatenate([grid[used] for used, _ in chunks]),
        np.concatenate([encoded[used] for used, _ in chunks]),
        np.concatenate([local + offset for (_, local), offset in zip(chunks, offsets)]).astype("<u4"),
        translation,
        scale,
        tuple((len(used), len(local)) for used, local in chunks),
    )


def _float_arrays(arrays):
    """Scene-metre positions, unit normals and faces from either encoding."""
    if not isinstance(arrays, _Quantized):
        return arrays
    positions = np.asarray(arrays.translation) + arrays.scale * arrays.positions.astype(np.float64)
    return positions, np.maximum(arrays.normals / 127, -1), arrays.faces


def _display_quantization(step):
    step_mm = step * 1000
    return {"encoding": _QUANTIZATION, "position_step_mm": step_mm, "position_error_bound_mm": step_mm * math.sqrt(3) / 2, "normals": "int8"}


def _write_model(structure_arrays, output_path: Path, detailed: bool = False) -> dict:
    """Write float32 detailed or quantized display GLBs; display meshes never measure."""
    document = {
        "asset": {"version": "2.0", "generator": "3DScan physical mask surfaces"},
        "scene": 0,
        "scenes": [{"nodes": []}],
        "nodes": [],
        "meshes": [],
        "materials": [],
        "accessors": [],
        "bufferViews": [],
        "extras": {"units": "metres", "coordinates": "scene=(-R,S,A)/1000", "detailed": detailed, "source": "segmentation masks"},
    }
    binary = bytearray()
    triangle_count = 0
    counts = {}
    steps = []

    def accessor(array, kind, component, target, bounds=False, normalized=False):
        while len(binary) % 4:
            binary.append(0)
        view = len(document["bufferViews"])
        document["bufferViews"].append({"buffer": 0, "byteOffset": len(binary), "byteLength": array.nbytes, "target": target})
        values = array
        if kind == "VEC3" and array.shape[1] == 4:
            # glTF aligns each vertex element to 4 bytes; the fourth column pads.
            document["bufferViews"][view]["byteStride"] = 4 * array.itemsize
            values = array[:, :3]
        binary.extend(array.tobytes(order="C"))
        item = {"bufferView": view, "componentType": component, "count": len(array), "type": kind}
        if normalized:
            item["normalized"] = True
        if bounds:
            item["min"] = values.min(axis=0).tolist()
            item["max"] = values.max(axis=0).tolist()
        index = len(document["accessors"])
        document["accessors"].append(item)
        return index

    def padded(values, dtype):
        rows = np.zeros((len(values), 4), dtype)
        rows[:, :3] = values
        return rows

    seen = set()
    for structure, arrays in structure_arrays:
        stable_id = str(structure["id"])
        if stable_id in seen:
            raise ValueError("Model contains duplicate structure identities")
        seen.add(stable_id)
        if arrays is None:
            continue
        faces = arrays[2]
        if not len(faces):
            continue
        color = str(structure.get("color", "#b7c4d0"))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Structure colors must use #RRGGBB")
        srgb = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        # glTF material factors use linear light; app/legend hex colors use sRGB.
        rgb = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4 for channel in srgb]
        material = len(document["materials"])
        document["materials"].append(
            {"name": stable_id, "pbrMetallicRoughness": {"baseColorFactor": rgb + [1.0], "metallicFactor": 0.0, "roughnessFactor": 0.7}, "doubleSided": False}
        )
        transform = {}
        primitives = []
        if detailed:
            if isinstance(arrays, _Quantized):
                raise ValueError("Detailed models require float geometry")
            positions, normals, _ = arrays
            primitives.append(
                (
                    accessor(positions, "VEC3", 5126, 34962, bounds=True),
                    accessor(normals, "VEC3", 5126, 34962),
                    accessor(faces.astype("<u4").reshape(-1), "SCALAR", 5125, 34963),
                )
            )
        else:
            # Cached quantized assets pass through bit-exactly into model.glb.
            quantized = _quantize(arrays)
            vertex = triangle = 0
            for vertices, triangles in quantized.parts:
                local = quantized.faces[triangle : triangle + triangles] - vertex
                primitives.append(
                    (
                        accessor(padded(quantized.positions[vertex : vertex + vertices], "<u2"), "VEC3", 5123, 34962, bounds=True),
                        accessor(padded(quantized.normals[vertex : vertex + vertices], "i1"), "VEC3", 5120, 34962, normalized=True),
                        accessor(local.astype("<u2").reshape(-1), "SCALAR", 5123, 34963),
                    )
                )
                vertex += vertices
                triangle += triangles
            transform = {"translation": list(quantized.translation), "scale": [quantized.scale] * 3}
            steps.append(quantized.scale)
        mesh_index = len(document["meshes"])
        document["meshes"].append(
            {
                "name": stable_id,
                "primitives": [
                    {"attributes": {"POSITION": position_accessor, "NORMAL": normal_accessor}, "indices": index_accessor, "material": material, "mode": 4}
                    for position_accessor, normal_accessor, index_accessor in primitives
                ],
            }
        )
        node_index = len(document["nodes"])
        document["nodes"].append(
            {"name": stable_id, "mesh": mesh_index, **transform, "extras": {"structure_id": stable_id, "display_name": structure.get("name", stable_id)}}
        )
        document["scenes"][0]["nodes"].append(node_index)
        triangle_count += len(faces)
        counts[stable_id] = len(faces)
    if not document["nodes"]:
        raise ValueError("Cannot build a model without nonempty segmentation surfaces")
    if not detailed:
        document["extensionsUsed"] = [_QUANTIZATION]
        document["extensionsRequired"] = [_QUANTIZATION]
    while len(binary) % 4:
        binary.append(0)
    document["buffers"] = [{"byteLength": len(binary)}]
    encoded = json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    encoded += b" " * ((-len(encoded)) % 4)
    total = 12 + 8 + len(encoded) + 8 + len(binary)
    with _atomic_destination(output_path) as temporary:
        with temporary.open("wb") as handle:
            handle.write(struct.pack("<III", 0x46546C67, 2, total))
            handle.write(struct.pack("<II", len(encoded), 0x4E4F534A))
            handle.write(encoded)
            handle.write(struct.pack("<II", len(binary), 0x004E4942))
            handle.write(binary)
    return {
        "triangles": triangle_count,
        "structures": len(counts),
        "triangle_counts": counts,
        "display_simplification": None if detailed else _display_simplification(),
        "display_quantization": None if detailed else _display_quantization(max(steps)),
    }


def _read_glb(path):
    data = Path(path).read_bytes()
    if len(data) < 28:
        raise ValueError("Invalid surface GLB")
    magic, version, total = struct.unpack_from("<III", data)
    json_length, json_kind = struct.unpack_from("<II", data, 12)
    if magic != 0x46546C67 or version != 2 or total != len(data) or json_kind != 0x4E4F534A:
        raise ValueError("Invalid surface GLB")
    if json_length % 4 or json_length > len(data) - 28:
        raise ValueError("Invalid surface JSON chunk")
    document = json.loads(data[20 : 20 + json_length])
    bin_length, bin_kind = struct.unpack_from("<II", data, 20 + json_length)
    binary = memoryview(data)[28 + json_length :]
    if bin_kind != 0x004E4942 or len(binary) != bin_length:
        raise ValueError("Invalid surface buffer")
    if document.get("buffers") != [{"byteLength": len(binary)}]:
        raise ValueError("Surface must have one embedded buffer")
    return document, binary


def _primitive_arrays(document, binary, primitive, node):
    """Decode our float32 (legacy display, detailed) or quantized primitive.

    Quantized data stays integer so repackaging never requantizes; the node must
    hold its uniform dequantization transform, and float surfaces none at all.
    """
    if primitive.get("mode") != 4 or set(primitive.get("attributes", {})) != {"POSITION", "NORMAL"}:
        raise ValueError("Surface must have position/normal indexed triangles")

    def accessor(index):
        if not isinstance(index, int) or index < 0 or index >= len(document["accessors"]):
            raise ValueError("Invalid surface accessor")
        return document["accessors"][index]

    def array(index, dtype, columns, component, kind, stride=None, normalized=False):
        item = accessor(index)
        if item.get("componentType") != component or item.get("type") != kind or "sparse" in item or item.get("normalized", False) is not normalized:
            raise ValueError("Invalid surface array format")
        view_index = item.get("bufferView")
        if not isinstance(view_index, int) or view_index < 0 or view_index >= len(document["bufferViews"]):
            raise ValueError("Invalid surface buffer view")
        view = document["bufferViews"][view_index]
        count = item.get("count")
        offset = view.get("byteOffset", 0)
        length = view.get("byteLength")
        itemsize = np.dtype(dtype).itemsize
        width = (stride or columns * itemsize) // itemsize
        if (
            view.get("buffer") != 0
            or view.get("byteStride") != stride
            or item.get("byteOffset", 0) != 0
            or not isinstance(count, int)
            or count <= 0
            or not isinstance(offset, int)
            or offset < 0
            or not isinstance(length, int)
            or length < 0
            or offset % 4
            or count * width * itemsize != length
            or offset + length > len(binary)
        ):
            raise ValueError("Invalid surface array bounds")
        return np.frombuffer(binary, dtype=dtype, count=count * width, offset=offset).reshape(count, width)[:, :columns]

    attributes = primitive["attributes"]
    if accessor(attributes["POSITION"]).get("componentType") == 5126:
        if any(key in node for key in ("matrix", "translation", "rotation", "scale")):
            raise ValueError("Float surfaces must be untransformed")
        positions = array(attributes["POSITION"], "<f4", 3, 5126, "VEC3")
        normals = array(attributes["NORMAL"], "<f4", 3, 5126, "VEC3")
        faces = array(primitive["indices"], "<u4", 1, 5125, "SCALAR")
        if (
            len(positions) != len(normals)
            or len(faces) % 3
            or not np.isfinite(positions).all()
            or not np.isfinite(normals).all()
            or faces.max() >= len(positions)
        ):
            raise ValueError("Invalid surface geometry")
        return positions, normals, faces.reshape(-1, 3)

    def vector(values):
        return (
            isinstance(values, list)
            and len(values) == 3
            and all(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) for value in values)
        )

    translation, scale = node.get("translation"), node.get("scale")
    if (
        _QUANTIZATION not in document.get("extensionsRequired", [])
        or "matrix" in node
        or "rotation" in node
        or not vector(translation)
        or not vector(scale)
        or len(set(scale)) != 1
        or scale[0] <= 0
    ):
        raise ValueError("Quantized surfaces require a uniform dequantization transform")
    positions = array(attributes["POSITION"], "<u2", 3, 5123, "VEC3", stride=8)
    normals = array(attributes["NORMAL"], "i1", 3, 5120, "VEC3", stride=4, normalized=True)
    faces = array(primitive["indices"], "<u2", 1, 5123, "SCALAR")
    if len(positions) != len(normals) or len(positions) > _PRIMITIVE_VERTICES or len(faces) % 3 or faces.max() >= len(positions):
        raise ValueError("Invalid surface geometry")
    return _Quantized(positions, normals, faces.reshape(-1, 3), tuple(map(float, translation)), float(scale[0]), ((len(positions), len(faces) // 3),))


def _surface_arrays(document, binary, mesh, node):
    """One structure's arrays; quantized primitives of one mesh concatenate as parts."""
    decoded = [_primitive_arrays(document, binary, primitive, node) for primitive in mesh.get("primitives", [])]
    if len(decoded) == 1:
        return decoded[0]
    if not decoded or not all(isinstance(part, _Quantized) for part in decoded):
        raise ValueError("Surface must have one float primitive or quantized parts")
    offsets = np.cumsum([0] + [len(part.positions) for part in decoded[:-1]])
    return _Quantized(
        np.concatenate([part.positions for part in decoded]),
        np.concatenate([part.normals for part in decoded]),
        np.concatenate([part.faces.astype("<u4") + offset for part, offset in zip(decoded, offsets)]),
        decoded[0].translation,
        decoded[0].scale,
        tuple(part.parts[0] for part in decoded),
    )


def _asset_arrays(path):
    """Read one of our GLBs; aggregate packaging retains only one extra asset."""
    document, binary = _read_glb(path)
    return _surface_arrays(document, binary, document["meshes"][0], document["nodes"][0])


def import_display_assets(structures, model_path: Path, cache_dir: Path, expected_simplification: dict, progress=None) -> dict:
    """Split a trusted existing revision's app GLB without remeshing its masks.

    The caller must supply that revision's original structures and recorded
    simplification settings. Only our own display-model schemas are accepted:
    legacy float32 surfaces are quantized into current assets, and quantized ones
    are copied exactly. Every stable identity and numerical buffer is checked
    before any cache write. Existing verified assets are retained. Contours are
    not made: detailed exports remain a separate on-demand operation.
    """
    if expected_simplification != _display_simplification():
        raise ValueError("Existing display simplification differs from current parameters")
    structures = list(structures)
    by_id = {str(item["id"]): item for item in structures}
    if not structures or len(by_id) != len(structures):
        raise ValueError("Display import requires unique structure identities")
    timings = {key: 0.0 for key in ("hash", "cache_io", "contour", "simplify_normals", "package")}
    cache = {"base_hits": 0, "base_misses": 0, "surface_hits": 0, "surface_misses": 0}
    started = time.perf_counter()
    try:
        document, binary = _read_glb(model_path)
        # Legacy display models declare no extensions; current ones only quantization.
        extensions = document.get("extensionsUsed")
        quantized = extensions == [_QUANTIZATION]
        if (
            document.get("asset") != {"version": "2.0", "generator": "3DScan physical mask surfaces"}
            or document.get("extras") != {"units": "metres", "coordinates": "scene=(-R,S,A)/1000", "detailed": False, "source": "segmentation masks"}
            or document.get("scene") != 0
            or document.get("scenes") != [{"nodes": list(range(len(structures)))}]
            or len(document.get("nodes", [])) != len(structures)
            or len(document.get("meshes", [])) != len(structures)
            or (extensions is not None and not quantized)
            or document.get("extensionsRequired") != extensions
            or any(key in document for key in ("extensions", "animations", "skins"))
        ):
            raise ValueError("Existing model is not an unmodified 3DScan display model")
        validated = {}
        used_meshes = set()
        for node in document["nodes"]:
            stable_id = node.get("name")
            mesh_index = node.get("mesh")
            if (
                set(node) - {"translation", "scale"} != {"name", "mesh", "extras"}
                or stable_id not in by_id
                or stable_id in validated
                or node.get("extras", {}).get("structure_id") != stable_id
                or not isinstance(mesh_index, int)
                or mesh_index < 0
                or mesh_index >= len(structures)
                or mesh_index in used_meshes
            ):
                raise ValueError("Existing model structure identities do not match revision")
            used_meshes.add(mesh_index)
            mesh = document["meshes"][mesh_index]
            if mesh.get("name") != stable_id or not mesh.get("primitives"):
                raise ValueError("Existing model requires one named mesh per structure")
            arrays = _surface_arrays(document, binary, mesh, node)
            if isinstance(arrays, _Quantized) != quantized:
                raise ValueError("Existing model mixes surface encodings")
            for primitive in mesh["primitives"]:
                material_index = primitive.get("material")
                if (
                    not isinstance(material_index, int)
                    or material_index < 0
                    or material_index >= len(document["materials"])
                    or document["materials"][material_index].get("name") != stable_id
                    or document["materials"][material_index].get("doubleSided") is not False
                ):
                    raise ValueError("Existing surface material identity is invalid")
            validated[stable_id] = arrays
    except (KeyError, IndexError, TypeError, AttributeError, struct.error) as error:
        raise ValueError("Malformed existing display model") from error
    timings["cache_io"] += time.perf_counter() - started
    assets = []
    for index, structure in enumerate(structures):
        if progress:
            progress(f"Preparing existing display meshes: {index + 1}/{len(structures)} ({structure.get('name', structure['id'])})")
        started = time.perf_counter()
        _, asset_key = _surface_identity(structure, detailed=False)
        timings["hash"] += time.perf_counter() - started
        asset_path = surface_asset_path(cache_dir, asset_key)
        arrays = validated[str(structure["id"])]
        triangles = len(arrays[2])
        with _cache_lock(cache_dir, asset_key):
            started = time.perf_counter()
            metadata = _valid_cache(asset_path)
            timings["cache_io"] += time.perf_counter() - started
            if metadata is not None and isinstance(metadata.get("triangles"), int) and metadata["triangles"] > 0:
                cache["surface_hits"] += 1
                triangles = metadata["triangles"]
            else:
                cache["surface_misses"] += 1
                started = time.perf_counter()
                _write_model([(structure, arrays)], asset_path, detailed=False)
                _record_cache(asset_path, triangles=triangles)
                timings["package"] += time.perf_counter() - started
        assets.append({"id": str(structure["id"]), "key": asset_key, "path": str(asset_path.resolve()), "triangles": triangles})
    return {"surface_assets": assets, "cache": cache, "timings_seconds": timings}


def build_model(structures, output_path: Path, detailed: bool = False, progress=None, cache_dir: Path | None = None) -> dict:
    """Build the portable combined model and immutable per-structure assets.

    Supplying cache_dir lets different revisions reuse unchanged structures and
    detailed exports reuse the contour extracted for the browser display mesh.
    The no-cache path preserves the original numerical behavior.
    """
    structures = list(structures)
    seen = set()
    for structure in structures:
        stable_id = str(structure["id"])
        if stable_id in seen:
            raise ValueError("Model contains duplicate structure identities")
        seen.add(stable_id)
    assets = []
    cache = {"base_hits": 0, "base_misses": 0, "surface_hits": 0, "surface_misses": 0}
    timings = {key: 0.0 for key in ("hash", "cache_io", "contour", "simplify_normals", "package")}
    if cache_dir is not None:
        for index, structure in enumerate(structures):
            if progress:
                progress(f"Generating {'detailed' if detailed else 'display'} meshes: {index + 1}/{len(structures)} ({structure.get('name', structure['id'])})")
            result = prepare_surface(structure, cache_dir, detailed=detailed, progress=progress)
            for key, value in result["cache"].items():
                cache[key] += value
            for key, value in result["timings_seconds"].items():
                timings[key] += value
            if result["path"]:
                assets.append({key: result[key] for key in ("id", "key", "path", "triangles")})
        by_id = {item["id"]: item for item in assets}

        def prepared():
            for structure in structures:
                asset = by_id.get(str(structure["id"]))
                if progress:
                    progress(f"Packaging surface: {structure.get('name', structure['id'])}")
                yield structure, _asset_arrays(asset["path"]) if asset else None

        started = time.perf_counter()
        report = _write_model(prepared(), output_path, detailed)
        timings["package"] += time.perf_counter() - started
    else:

        def generated():
            for index, structure in enumerate(structures):
                if progress:
                    progress(
                        f"Generating {'detailed' if detailed else 'display'} meshes: {index + 1}/{len(structures)} ({structure.get('name', structure['id'])})"
                    )
                started = time.perf_counter()
                mesh = _contour(Path(structure["mask_file"]))
                timings["contour"] += time.perf_counter() - started
                started = time.perf_counter()
                arrays = _mesh_arrays(mesh, detailed, _delicate(structure))
                timings["simplify_normals"] += time.perf_counter() - started
                yield structure, arrays

        started = time.perf_counter()
        report = _write_model(generated(), output_path, detailed)
        timings["package"] += time.perf_counter() - started - timings["contour"] - timings["simplify_normals"]
    report.update(surface_assets=assets, cache=cache, timings_seconds=timings, mesh_processing=_configure_smp())
    return report
