"""Physical mask surfaces and dependency-free glTF 2.0 binary packaging."""

from __future__ import annotations

from pathlib import Path
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import struct
import tempfile
import threading
import time
import zipfile

import numpy as np

from .imaging import _binary, _foreground_bounds


_DISPLAY_ORGAN_TRIANGLES = 12000
_DISPLAY_TREE_TRIANGLES = 50000
_DISPLAY_LOCAL_ERROR_MM = 0.25
_MESH_IMPLEMENTATION = "physical-flying-edges-v2"
_ASSET_IMPLEMENTATION = "physical-glb-v2"
_SMP_CONFIGURATION = None
_SMP_LOCK = threading.Lock()


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
            _SMP_CONFIGURATION = {"backend": vtkSMPTools.GetBackend(),
                                  "threads": vtkSMPTools.GetEstimatedNumberOfThreads()}
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
    parameters = None if detailed else {"target": _DISPLAY_TREE_TRIANGLES if _delicate(structure) else _DISPLAY_ORGAN_TRIANGLES,
                                       "local_error_mm": _DISPLAY_LOCAL_ERROR_MM, "preserve_topology": True}
    asset_key = _key({"base": base_key, "implementation": _ASSET_IMPLEMENTATION, "detailed": bool(detailed),
                      "parameters": parameters, "id": stable_id, "name": structure.get("name", stable_id), "color": color})
    return base_key, asset_key


def _display_simplification():
    return {"algorithm": "vtkDecimatePro", "preserve_topology": True,
            "local_error_mm": _DISPLAY_LOCAL_ERROR_MM, "organ_target_triangles": _DISPLAY_ORGAN_TRIANGLES,
            "vascular_airway_target_triangles": _DISPLAY_TREE_TRIANGLES, "targets_are_soft": True}


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
        cells.SetData(numpy_to_vtk(np.arange(0, faces.size + 1, 3, dtype=np.int64), deep=True),
                      numpy_to_vtk(faces.reshape(-1), deep=True))
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
    cells.SetData(numpy_to_vtk(np.arange(0, faces.size + 1, 3, dtype=np.int64), deep=True),
                  numpy_to_vtk(np.ascontiguousarray(faces, dtype=np.int64).reshape(-1), deep=True))
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
    result = {"id": stable_id, "key": asset_key, "path": str(asset_path.resolve()), "triangles": 0,
              "cache": cache, "timings_seconds": timings, "mesh_processing": _configure_smp()}
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


def _write_model(structure_arrays, output_path: Path, detailed: bool = False) -> dict:
    document = {"asset": {"version": "2.0", "generator": "3DScan physical mask surfaces"},
                "scene": 0, "scenes": [{"nodes": []}], "nodes": [], "meshes": [],
                "materials": [], "accessors": [], "bufferViews": [],
                "extras": {"units": "metres", "coordinates": "scene=(-R,S,A)/1000",
                           "detailed": detailed, "source": "segmentation masks"}}
    binary = bytearray()
    triangle_count = 0
    counts = {}

    def accessor(array, kind, component, target, bounds=False):
        while len(binary) % 4:
            binary.append(0)
        view = len(document["bufferViews"])
        document["bufferViews"].append({"buffer": 0, "byteOffset": len(binary),
                                        "byteLength": array.nbytes, "target": target})
        binary.extend(array.tobytes(order="C"))
        item = {"bufferView": view, "componentType": component, "count": len(array), "type": kind}
        if bounds:
            item["min"] = array.min(axis=0).astype(float).tolist()
            item["max"] = array.max(axis=0).astype(float).tolist()
        index = len(document["accessors"])
        document["accessors"].append(item)
        return index

    seen = set()
    for structure, arrays in structure_arrays:
        stable_id = str(structure["id"])
        if stable_id in seen:
            raise ValueError("Model contains duplicate structure identities")
        seen.add(stable_id)
        if arrays is None:
            continue
        positions, normals, faces = arrays
        if not len(faces):
            continue
        color = str(structure.get("color", "#b7c4d0"))
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            raise ValueError("Structure colors must use #RRGGBB")
        srgb = [int(color[i:i + 2], 16) / 255 for i in (1, 3, 5)]
        # glTF material factors use linear light; app/legend hex colors use sRGB.
        rgb = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
               for channel in srgb]
        material = len(document["materials"])
        document["materials"].append({"name": stable_id, "pbrMetallicRoughness": {
            "baseColorFactor": rgb + [1.0], "metallicFactor": 0.0, "roughnessFactor": 0.7},
            "doubleSided": False})
        position_accessor = accessor(positions, "VEC3", 5126, 34962, bounds=True)
        normal_accessor = accessor(normals, "VEC3", 5126, 34962)
        index_accessor = accessor(faces.astype("<u4").reshape(-1), "SCALAR", 5125, 34963)
        mesh_index = len(document["meshes"])
        document["meshes"].append({"name": stable_id, "primitives": [{"attributes": {
            "POSITION": position_accessor, "NORMAL": normal_accessor}, "indices": index_accessor,
            "material": material, "mode": 4}]})
        node_index = len(document["nodes"])
        document["nodes"].append({"name": stable_id, "mesh": mesh_index, "extras": {
            "structure_id": stable_id, "display_name": structure.get("name", stable_id)}})
        document["scenes"][0]["nodes"].append(node_index)
        triangle_count += len(faces)
        counts[stable_id] = len(faces)
    if not document["nodes"]:
        raise ValueError("Cannot build a model without nonempty segmentation surfaces")
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
    return {"triangles": triangle_count, "structures": len(counts), "triangle_counts": counts,
            "display_simplification": None if detailed else _display_simplification()}


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
    document = json.loads(data[20:20 + json_length])
    bin_length, bin_kind = struct.unpack_from("<II", data, 20 + json_length)
    binary = memoryview(data)[28 + json_length:]
    if bin_kind != 0x004E4942 or len(binary) != bin_length:
        raise ValueError("Invalid surface buffer")
    if document.get("buffers") != [{"byteLength": len(binary)}]:
        raise ValueError("Surface must have one embedded buffer")
    return document, binary


def _primitive_arrays(document, binary, primitive):
    if primitive.get("mode") != 4 or set(primitive.get("attributes", {})) != {"POSITION", "NORMAL"}:
        raise ValueError("Surface must have position/normal indexed triangles")

    def array(index, dtype, columns, component, kind):
        if not isinstance(index, int) or index < 0 or index >= len(document["accessors"]):
            raise ValueError("Invalid surface accessor")
        accessor = document["accessors"][index]
        if accessor.get("componentType") != component or accessor.get("type") != kind or "sparse" in accessor:
            raise ValueError("Invalid surface array format")
        view_index = accessor.get("bufferView")
        if not isinstance(view_index, int) or view_index < 0 or view_index >= len(document["bufferViews"]):
            raise ValueError("Invalid surface buffer view")
        view = document["bufferViews"][accessor["bufferView"]]
        count = accessor.get("count")
        offset = view.get("byteOffset", 0)
        length = view.get("byteLength")
        if (view.get("buffer") != 0 or "byteStride" in view or accessor.get("byteOffset", 0) != 0
                or not isinstance(count, int) or count <= 0 or not isinstance(offset, int) or offset < 0
                or not isinstance(length, int) or length < 0 or offset % 4
                or count * columns * np.dtype(dtype).itemsize != length or offset + length > len(binary)):
            raise ValueError("Invalid surface array bounds")
        return np.frombuffer(binary, dtype=dtype, count=accessor["count"] * columns,
                             offset=offset).reshape(-1, columns)

    positions = array(primitive["attributes"]["POSITION"], "<f4", 3, 5126, "VEC3")
    normals = array(primitive["attributes"]["NORMAL"], "<f4", 3, 5126, "VEC3")
    faces = array(primitive["indices"], "<u4", 1, 5125, "SCALAR")
    if (len(positions) != len(normals) or len(faces) % 3 or not np.isfinite(positions).all()
            or not np.isfinite(normals).all() or faces.max() >= len(positions)):
        raise ValueError("Invalid surface geometry")
    return positions, normals, faces.reshape(-1, 3)


def _asset_arrays(path):
    """Read one of our GLBs; aggregate packaging retains only one extra asset."""
    document, binary = _read_glb(path)
    return _primitive_arrays(document, binary, document["meshes"][0]["primitives"][0])


def import_display_assets(structures, model_path: Path, cache_dir: Path,
                          expected_simplification: dict, progress=None) -> dict:
    """Split a trusted existing revision's app GLB without remeshing its masks.

    The caller must supply that revision's original structures and recorded
    simplification settings. Only our untransformed display-model schema is
    accepted. Every stable identity and numerical buffer is checked before any
    cache write. Existing verified assets are retained. Contours are not made:
    detailed exports remain a separate on-demand operation.
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
        if (document.get("asset") != {"version": "2.0", "generator": "3DScan physical mask surfaces"}
                or document.get("extras") != {"units": "metres", "coordinates": "scene=(-R,S,A)/1000",
                                              "detailed": False, "source": "segmentation masks"}
                or document.get("scene") != 0
                or document.get("scenes") != [{"nodes": list(range(len(structures)))}]
                or len(document.get("nodes", [])) != len(structures)
                or len(document.get("meshes", [])) != len(structures)
                or any(key in document for key in ("extensions", "extensionsRequired", "extensionsUsed", "animations", "skins"))):
            raise ValueError("Existing model is not an untransformed 3DScan display model")
        validated = {}
        used_meshes = set()
        for node in document["nodes"]:
            stable_id = node.get("name")
            mesh_index = node.get("mesh")
            if (set(node) != {"name", "mesh", "extras"} or stable_id not in by_id or stable_id in validated
                    or node.get("extras", {}).get("structure_id") != stable_id
                    or not isinstance(mesh_index, int) or mesh_index < 0 or mesh_index >= len(structures)
                    or mesh_index in used_meshes):
                raise ValueError("Existing model structure identities do not match revision")
            used_meshes.add(mesh_index)
            mesh = document["meshes"][mesh_index]
            if mesh.get("name") != stable_id or len(mesh.get("primitives", [])) != 1:
                raise ValueError("Existing model requires one named mesh per structure")
            primitive = mesh["primitives"][0]
            arrays = _primitive_arrays(document, binary, primitive)
            material_index = primitive.get("material")
            if (not isinstance(material_index, int) or material_index < 0 or material_index >= len(document["materials"])
                    or document["materials"][material_index].get("name") != stable_id
                    or document["materials"][material_index].get("doubleSided") is not False):
                raise ValueError("Existing surface material identity is invalid")
            validated[stable_id] = arrays
    except (KeyError, IndexError, TypeError, struct.error) as error:
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
        assets.append({"id": str(structure["id"]), "key": asset_key,
                       "path": str(asset_path.resolve()), "triangles": triangles})
    return {"surface_assets": assets, "cache": cache, "timings_seconds": timings}


def build_model(structures, output_path: Path, detailed: bool = False, progress=None,
                cache_dir: Path | None = None) -> dict:
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
                    progress(f"Generating {'detailed' if detailed else 'display'} meshes: {index + 1}/{len(structures)} ({structure.get('name', structure['id'])})")
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
