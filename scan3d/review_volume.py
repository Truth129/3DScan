"""Native CT slabs and compact previews for browser-local anatomical review.

The cache is a presentation artifact: it never rewrites a CT or mask. Arrays
use native CT voxel axes, little-endian samples and i-fastest (Fortran) order;
the reference affine remains the authority for patient-space reslicing.
"""

from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from io import BytesIO
import fcntl
import gzip
import hashlib
import json
import math
from pathlib import Path
import re
import shutil
import tempfile
import threading

from nibabel.volumeutils import apply_read_scaling
import numpy as np
from scipy import ndimage as ndi

from . import imaging


_IMPLEMENTATION = "native-review-slabs-v2"
_CHUNK_DEPTH = 16
_PREVIEW_MAX_DIMENSION = 192
_KEY = re.compile(r"[0-9a-f]{64}")
_DIGESTS = OrderedDict()
_DIGEST_LOCK = threading.RLock()
_DIGEST_STRIPES = tuple(threading.Lock() for _ in range(32))


def _stat(path):
    value = Path(path).stat()
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _file_digest(path):
    """Hash complete inputs and artifacts, invalidating even same-mtime swaps."""
    path = Path(path).resolve()
    with _DIGEST_STRIPES[hash(str(path)) % len(_DIGEST_STRIPES)]:
        before = _stat(path)
        signature = (str(path), *before)
        with _DIGEST_LOCK:
            if signature in _DIGESTS:
                _DIGESTS.move_to_end(signature)
                return _DIGESTS[signature]
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if _stat(path) != before:
            raise ValueError("Review volume input changed while reading; retry the request")
        result = digest.hexdigest()
        with _DIGEST_LOCK:
            _DIGESTS[signature] = result
            while len(_DIGESTS) > 512:
                _DIGESTS.popitem(last=False)
        return result


def _identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _entry(cache_dir, key):
    if not _KEY.fullmatch(key):
        raise ValueError("Invalid review volume key")
    return Path(cache_dir) / "volumes" / key[:2] / key


@contextmanager
def _generation_lock(cache_dir, quality):
    """Bound each quality lane to one generation across API processes.

    Bulk native-volume preparation cannot delay a newly selected preview mask.
    The two lanes allow at most two concurrent decodes/compressions overall.
    """
    if quality not in ("full", "preview"):
        raise ValueError("Review quality must be full or preview")
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / f"generation-{quality}.lock").open("a+b") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _geometry(image, affine):
    return {"shape": list(image.shape), "affine_ras": affine.tolist(),
            "spacing": np.linalg.norm(affine[:3, :3], axis=0).tolist(),
            "bounds_ras": imaging._bounds(image.shape, affine).tolist(),
            "center_ras": imaging._world(affine, (np.asarray(image.shape) - 1) / 2).tolist()}


def _review_grid(image, affine, quality):
    if quality not in ("full", "preview"):
        raise ValueError("Review quality must be full or preview")
    reference = _geometry(image, affine)
    stride = max(1.0, max(image.shape) / _PREVIEW_MAX_DIMENSION) if quality == "preview" else 1.0
    shape = np.ceil(np.asarray(image.shape) / stride).astype(int)
    step = np.asarray(image.shape) / shape
    origin = (step - 1) / 2
    target = affine.copy()
    target[:3, :3] = affine[:3, :3] @ np.diag(step)
    target[:3, 3] = imaging._world(affine, origin)
    grid = {"shape": shape.tolist(), "affine_ras": target.tolist(),
            "spacing": np.linalg.norm(target[:3, :3], axis=0).tolist(),
            "bounds_ras": reference["bounds_ras"], "center_ras": reference["center_ras"]}
    return grid, reference, step, origin


def _read_cached(cache_dir, key):
    directory = _entry(cache_dir, key)
    try:
        record = json.loads((directory / "manifest.json").read_text())
        descriptor = record["descriptor"]
        if (record["implementation"] != _IMPLEMENTATION or descriptor["key"] != key
                or descriptor["schema_version"] != 1):
            return None
        # Digests are memoized by inode, length, mtime AND ctime. A corrupted or
        # replaced compressed artifact is checked again before it can be served.
        for chunk, saved in zip(descriptor["chunks"], record["files"], strict=True):
            path = directory / f"{chunk['index']}.bin.gz"
            if path.stat().st_size != saved["byte_length"] or _file_digest(path) != saved["sha256"]:
                return None
        return descriptor
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _write(cache_dir, descriptor, values, *, source_slope=1.0, source_intercept=0.0,
           source=None):
    """Publish only a complete, verified generation; interrupted writes are inert."""
    destination = _entry(cache_dir, descriptor["key"])
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{descriptor['key']}.", dir=destination.parent))
    files = []
    chunks = []
    byte_offset = 0
    dtype = np.dtype(descriptor["dtype"]).newbyteorder("<")
    try:
        for index, start in enumerate(range(0, values.shape[2], _CHUNK_DEPTH)):
            depth = min(_CHUNK_DEPTH, values.shape[2] - start)
            slab = values[:, :, start:start + depth]
            if descriptor["kind"] == "ct":
                # This mirrors Nibabel's ArrayProxy float32 conversion, which
                # performs scaling with its float64 proxy slope/intercept and
                # rounds once to float32. It also validates raw integer streams.
                scaled = np.asarray(apply_read_scaling(slab, source_slope, source_intercept),
                                    dtype=np.float32)
                if not np.isfinite(scaled).all():
                    raise ValueError("Reference CT contains nonfinite intensities")
                if descriptor["dtype"] == "float32":
                    slab = scaled
            raw = np.asarray(slab, dtype=dtype).tobytes(order="F")
            compressed = gzip.compress(raw, compresslevel=1, mtime=0)
            path = temporary / f"{index}.bin.gz"
            path.write_bytes(compressed)
            chunks.append({"index": index, "start_k": start, "depth": depth,
                           "byte_offset": byte_offset, "byte_length": len(raw)})
            files.append({"byte_length": len(compressed), "sha256": hashlib.sha256(compressed).hexdigest()})
            byte_offset += len(raw)
        descriptor = {**descriptor, "byte_length": byte_offset, "chunk_depth": _CHUNK_DEPTH, "chunks": chunks}
        if source is not None and _file_digest(source[0]) != source[1]:
            raise ValueError("Review volume input changed while reading; retry the request")
        record = {"implementation": _IMPLEMENTATION, "descriptor": descriptor, "files": files}
        (temporary / "manifest.json").write_text(json.dumps(record, sort_keys=True, separators=(",", ":"),
                                                           allow_nan=False))
        # A previously corrupted generation is disposable; original case files
        # are outside this content-addressed cache and are never touched.
        if destination.exists():
            shutil.rmtree(destination)
        temporary.replace(destination)
        return descriptor
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def ct_volume(ct_path, cache_dir, quality="full"):
    """Return native lossless HU or an explicitly approximate, bounded preview."""
    ct_path = Path(ct_path)
    source_digest = _file_digest(ct_path)
    image, affine = imaging._image(ct_path)
    geometry, reference, step, origin = _review_grid(image, affine, quality)
    key = _identity({"implementation": _IMPLEMENTATION, "kind": "ct",
                     "source": source_digest, "geometry": geometry, "quality": quality,
                     **({"preview_quantization": "nearest-int16-hu-v1"} if quality == "preview" else {})})
    cached = _read_cached(cache_dir, key)
    if cached is not None:
        return cached
    with _generation_lock(cache_dir, quality):
        cached = _read_cached(cache_dir, key)
        if cached is not None:
            return cached
        proxy = image.dataobj
        values = np.asanyarray(proxy.get_unscaled())
        if values.dtype.kind not in "biuf":
            raise ValueError("Reference CT must contain real scalar intensities")
        slope, intercept = float(proxy.slope), float(proxy.inter)
        if not np.isfinite([slope, intercept]).all():
            raise ValueError("Reference CT contains invalid intensity scaling")
        # Typical CTs retain their exact 16-bit stored samples. Other scalar
        # types are sent as the same scaled float32 HU used by local review.
        raw_dtype = values.dtype.newbyteorder("=")
        preserve_raw = quality == "full" and raw_dtype in (np.dtype("int16"), np.dtype("uint16"))
        if quality == "preview":
            # A bounded image of the entire scan makes every patient plane
            # available after a small initial transfer. Its voxel faces exactly
            # match the reference FOV, including oblique/reflected directions.
            values = ndi.affine_transform(values, np.diag(step), origin,
                                          output_shape=tuple(geometry["shape"]), output=np.float64,
                                          order=1, mode="nearest", prefilter=False)
            preview_hu = np.asarray(apply_read_scaling(values, slope, intercept), dtype=np.float32)
            if not np.isfinite(preview_hu).all():
                raise ValueError("Reference CT contains nonfinite intensities")
            rounded = np.rint(preview_hu)
            if rounded.min() >= -32768 and rounded.max() <= 32767:
                values = rounded.astype(np.int16)
                preserve_raw = True
            else:
                values = preview_hu
            slope, intercept = 1.0, 0.0
        dtype = values.dtype.newbyteorder("=").name if preserve_raw else "float32"
        descriptor = {"schema_version": 1, "key": key, "kind": "ct", "dtype": dtype,
                      "quality": quality, "approximate": quality == "preview",
                      "preview_quantization_hu": .5 if quality == "preview" and dtype == "int16" else 0,
                      "reference_geometry": reference,
                      "shape": geometry["shape"], "affine_ras": geometry["affine_ras"], "geometry": geometry,
                      "slope": slope if preserve_raw else 1.0,
                      "intercept": intercept if preserve_raw else 0.0}
        return _write(cache_dir, descriptor, values, source_slope=slope, source_intercept=intercept,
                      source=(ct_path, source_digest))


def display_margin(value):
    """Canonical display-only margin; zero retains the exact measurement mask."""
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= 5:
        raise ValueError("Display mask margin must be between 0 and 5 mm")
    return math.floor(value * 1000 + .5) / 1000


def volume_mask_alias(structure_id, margin_mm):
    margin_mm = display_margin(margin_mm)
    text = f"{margin_mm:.3f}".rstrip("0").rstrip(".")
    return f"volume-margin:{text}:{structure_id}" if margin_mm else structure_id


def _margin_crop(values, affine, margin_mm):
    """Dilate in patient millimetres in a bounded ROI, without editing values.

    Tiled EDT uses a physical halo, bounding temporary distances to one tile.
    A sheared grid uses its exact affine metric rather than an axis-spacing EDT.
    """
    bounds = imaging._foreground_bounds(values)
    if bounds is None:
        return np.zeros(3, dtype=int), np.zeros((1, 1, 1), dtype=bool)
    matrix = np.asarray(affine)[:3, :3]
    padding = np.ceil(margin_mm * np.linalg.norm(np.linalg.inv(matrix), axis=1) + 1e-7).astype(int)
    origin = np.maximum(0, bounds[0] - padding)
    high = np.minimum(np.asarray(values.shape), bounds[1] + padding + 1)
    region = tuple(slice(int(a), int(b)) for a, b in zip(origin, high))
    source = values[region]
    output = np.zeros(source.shape, dtype=bool)
    spacing = np.linalg.norm(matrix, axis=0)
    orthogonal = np.allclose(matrix.T @ matrix, np.diag(spacing ** 2), rtol=1e-6, atol=1e-8)
    kernel = None
    if not orthogonal:
        kernel_shape = padding * 2 + 1
        if int(np.prod(kernel_shape)) > 65536:
            raise ValueError("This sheared grid needs too large a display-margin kernel; reduce the margin")
        offsets = np.indices(kernel_shape).reshape(3, -1).T - padding
        kernel = (np.linalg.norm(offsets @ matrix.T, axis=1) <= margin_mm + 1e-7).reshape(kernel_shape)
        if source.size * int(kernel.sum()) > 2_000_000_000:
            raise ValueError("This sheared grid needs too much display-margin work; reduce the margin")
    # Keep distance-transform temporaries near 16 MiB plus a small physical halo.
    block = np.minimum(np.asarray(source.shape), [128, 128, 128])
    if int(np.prod(np.minimum(source.shape, block + 2 * padding))) > 8 * 1024 ** 2:
        raise ValueError("This voxel spacing needs too much display-margin working memory; reduce the margin")
    for i in range(0, source.shape[0], block[0]):
        for j in range(0, source.shape[1], block[1]):
            for k in range(0, source.shape[2], block[2]):
                low = np.asarray([i, j, k]); end = np.minimum(low + block, source.shape)
                halo_low = np.maximum(0, low - padding); halo_high = np.minimum(source.shape, end + padding)
                tile = source[tuple(slice(int(a), int(b)) for a, b in zip(halo_low, halo_high))]
                if not tile.any():
                    continue
                if orthogonal:
                    expanded = ndi.distance_transform_edt(~tile, sampling=spacing) <= margin_mm + 1e-7
                else:
                    expanded = ndi.binary_dilation(tile, structure=kernel)
                inner = tuple(slice(int(a), int(b)) for a, b in zip(low - halo_low, end - halo_low))
                output[tuple(slice(int(a), int(b)) for a, b in zip(low, end))] = expanded[inner]
    return origin, output


def mask_volume(ct_path, mask_path, cache_dir, quality="full", margin_mm=0):
    """Return a binary crop in the chosen native or matched preview grid."""
    margin_mm = display_margin(margin_mm)
    image, affine = imaging._image(Path(ct_path))
    geometry, reference, step, grid_origin = _review_grid(image, affine, quality)
    target_affine = np.asarray(geometry["affine_ras"])
    mask_path = Path(mask_path)
    source_digest = _file_digest(mask_path)
    key = _identity({"implementation": _IMPLEMENTATION, "kind": "mask",
                     "source": source_digest, "reference_geometry": reference,
                     "quality": quality, "geometry": geometry,
                     **({"display_margin_mm": margin_mm, "margin_implementation": "physical-mask-margin-v1"} if margin_mm else {})})
    cached = _read_cached(cache_dir, key)
    if cached is not None:
        return cached
    with _generation_lock(cache_dir, quality):
        cached = _read_cached(cache_dir, key)
        if cached is not None:
            return cached
        values, _, _ = imaging._binary(mask_path, image, affine)
        native_origin = np.zeros(3, dtype=int)
        if margin_mm:
            native_origin, values = _margin_crop(values, affine, margin_mm)
        if quality == "preview":
            values = ndi.affine_transform(values, np.diag(step), grid_origin - native_origin,
                                          output_shape=tuple(geometry["shape"]), output=bool,
                                          order=0, mode="constant", cval=0, prefilter=False)
        bounds = imaging._foreground_bounds(values)
        if bounds is None:
            origin = np.zeros(3, dtype=int)
            cropped = np.zeros((1, 1, 1), dtype=np.uint8)
        else:
            origin, high = bounds
            cropped = values[tuple(slice(int(a), int(b) + 1) for a, b in zip(origin, high))]
            if quality == "full":
                origin = origin + native_origin
        cropped_affine = target_affine.copy()
        cropped_affine[:3, 3] = imaging._world(target_affine, origin)
        descriptor = {"schema_version": 1, "key": key, "kind": "mask", "dtype": "uint8",
                      "shape": list(cropped.shape), "origin_ijk": origin.tolist(),
                      "affine_ras": cropped_affine.tolist(), "slope": 1.0, "intercept": 0.0,
                      "quality": quality, "reference_geometry": reference,
                      "geometry": geometry, "empty": bounds is None,
                      **({"display_margin_mm": margin_mm} if margin_mm else {})}
        return _write(cache_dir, descriptor, cropped, source=(mask_path, source_digest))


def chunk_path(cache_dir, descriptor, key, index):
    """The caller must resolve the descriptor from an authorized case revision."""
    if key != descriptor["key"]:
        raise KeyError("Review volume does not belong to this revision")
    if index < 0 or index >= len(descriptor["chunks"]):
        raise KeyError("Review volume chunk not found")
    return _entry(cache_dir, key) / f"{index}.bin.gz"


def review_frame(ct_path, mask_path, plane, position_mm, parent_mask_path=None, *, overlays=None,
                 native_axis=None, native_index=None):
    """Exact reference-grid HU and binary overlays for a settled patient plane.

    This is the same pixel-centre geometry and interpolation used by the PNG
    endpoint. The browser owns window/level, colors and presentation thereafter.
    """
    ct_path = Path(ct_path)
    image, affine = imaging._image(ct_path)
    meta, mapping = imaging.slice_geometry(image.shape, affine, plane, position_mm,
                                           native_axis=native_axis, native_index=native_index)
    width, height = meta["width"], meta["height"]
    intensity = imaging._reslice(imaging._ct_array(ct_path, image), mapping,
                                 (height, width), order=1, cval=-1024)
    pixels = width * height
    # Group the four IEEE754 byte planes before gzip. This is lossless and
    # exploits the repeated exponent/high bytes instead of interleaving them
    # with noisy low mantissa bytes; masks keep their ordinary pixel order.
    hu_bytes = np.ascontiguousarray(intensity, dtype="<f4").view(np.uint8).reshape(pixels, 4)
    encoded_hu = hu_bytes.T.tobytes(order="C")
    body = [encoded_hu]
    current_offset = parent_offset = None
    layers = None
    if overlays is not None:
        # Stream binary layers through one gzip member. In particular, do not
        # retain 128 full masks or an uncompressed body per cached frame.
        buffer = BytesIO()
        layers = []
        offset = pixels * 4
        with gzip.GzipFile(fileobj=buffer, mode="wb", compresslevel=3, mtime=0) as stream:
            stream.write(encoded_hu)
            for structure_id, current_path, previous_path in overlays:
                layer = {"id": structure_id, "current_offset": offset, "parent_offset": None}
                current = imaging._slice_mask(current_path, image, affine)
                sampled = imaging._reslice(current, mapping, (height, width), order=0, cval=0)
                stream.write(np.asarray(sampled, dtype=np.uint8).tobytes(order="C"))
                offset += pixels
                if previous_path is not None:
                    layer["parent_offset"] = offset
                    previous = imaging._slice_mask(previous_path, image, affine)
                    sampled = imaging._reslice(previous, mapping, (height, width), order=0, cval=0)
                    stream.write(np.asarray(sampled, dtype=np.uint8).tobytes(order="C"))
                    offset += pixels
                layers.append(layer)
        compressed = buffer.getvalue()
        if layers:
            current_offset, parent_offset = layers[0]["current_offset"], layers[0]["parent_offset"]
    elif mask_path is not None:
        current = imaging._slice_mask(mask_path, image, affine)
        sampled = imaging._reslice(current, mapping, (height, width), order=0, cval=0)
        current_offset = pixels * 4
        body.append(np.asarray(sampled, dtype=np.uint8).tobytes(order="C"))
    if overlays is None and parent_mask_path is not None:
        if mask_path is None:
            raise ValueError("A current mask is required for comparison")
        previous = imaging._slice_mask(parent_mask_path, image, affine)
        sampled = imaging._reslice(previous, mapping, (height, width), order=0, cval=0)
        parent_offset = pixels * 5
        body.append(np.asarray(sampled, dtype=np.uint8).tobytes(order="C"))
    meta["overlay_colors"] = {"current": "#38bdf8", "added": "#22c55e", "removed": "#f43f5e"}
    frame = {"meta": meta, "dtype": "float32", "hu_encoding": "byte-shuffle", "pixels": pixels,
             "current_offset": current_offset, "parent_offset": parent_offset}
    if layers is not None:
        frame["overlays"] = layers
    else:
        compressed = gzip.compress(b"".join(body), compresslevel=3, mtime=0)
    return {"body": compressed, "frame": frame}
