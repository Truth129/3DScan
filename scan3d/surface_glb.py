"""Strict glTF encoding for display surfaces and detailed mesh exports.

The codec operates on scene-metre arrays, independent of VTK and mask loading.
Quantized assets remain integer while repackaging, preserving their exact bytes.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import re
import struct
from typing import NamedTuple

import numpy as np

from .fileio import atomic_destination


# Display vertices snap to one absolute power-of-two grid in scene metres (about
# 0.031 mm, 2 m per uint16 range). Shared by all structures, coincident boundaries
# stay coincident, and requantizing decoded positions reproduces the same integers.
_POSITION_STEP_M = 2.0**-15
_QUANTIZATION = "KHR_mesh_quantization"
# uint16 indices, excluding the primitive restart value glTF reserves.
_PRIMITIVE_VERTICES = 65535


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


def write_model(structure_arrays, output_path: Path, detailed: bool = False, *, display_simplification) -> dict:
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

    def primitive(positions, normals, faces, *, quantized=False):
        if quantized:
            positions, normals = padded(positions, "<u2"), padded(normals, "i1")
        return (
            accessor(positions, "VEC3", 5123 if quantized else 5126, 34962, bounds=True),
            accessor(normals, "VEC3", 5120 if quantized else 5126, 34962, normalized=quantized),
            accessor(faces.astype("<u2" if quantized else "<u4").reshape(-1), "SCALAR", 5123 if quantized else 5125, 34963),
        )

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
            primitives.append(primitive(positions, normals, faces))
        else:
            # Cached quantized assets pass through bit-exactly into model.glb.
            quantized = _quantize(arrays)
            vertex = triangle = 0
            for vertices, triangles in quantized.parts:
                local = quantized.faces[triangle : triangle + triangles] - vertex
                primitives.append(
                    primitive(quantized.positions[vertex : vertex + vertices], quantized.normals[vertex : vertex + vertices], local, quantized=True)
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
    with atomic_destination(output_path) as temporary:
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
        "display_simplification": None if detailed else display_simplification,
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
