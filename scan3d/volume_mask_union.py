"""Bounded, conservative masks used only to present the 3D intensity volume.

Exact native masks remain the measurement/editing authority. A selected union
is cropped before reducing its grid, retaining every foreground voxel centre
through occupancy pooling instead of nearest-neighbour point sampling. The
union clips the volume; a label union colours it. Both are independent of
segmentation colours and use at most 8 MiB on the GPU.
"""

from __future__ import annotations

from concurrent.futures import CancelledError
from contextlib import contextmanager
import fcntl
import gzip
from pathlib import Path
import threading

import numpy as np

from . import imaging, review_volume


_IMPLEMENTATION = "physical-cropped-mask-union-occupancy-v1"
_LABEL_IMPLEMENTATION = "physical-cropped-label-union-occupancy-v1"
_MAX_BYTES = 8 * 1024**2
# uint8 labels: 0 is background, so a label union holds at most 255 structures.
_MAX_LABELS = 255
_STREAM_BYTES = 256 * 1024


def _cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise CancelledError("Volume mask preparation cancelled")


@contextmanager
def _generation_lock(cache_dir, cancelled):
    """A separate generation lane keeps native/overview CT transport responsive."""
    directory = Path(cache_dir)
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / "generation-volume-mask-union.lock").open("a+b") as stream:
        while True:
            _cancelled(cancelled)
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                threading.Event().wait(0.05)
        try:
            _cancelled(cancelled)
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _bounded_shape(shape):
    """Keep the cropped native grid when possible; otherwise reduce uniformly."""
    shape = np.asarray(shape, dtype=np.int64)
    if int(np.prod(shape, dtype=object)) <= _MAX_BYTES:
        return shape.copy()
    # Binary search handles thin or strongly anisotropic crops without allocating
    # a whole-reference intermediate or iterating once per removed voxel.
    low, high = 0.0, 1.0
    result = np.ones(3, dtype=np.int64)
    for _ in range(64):
        scale = (low + high) / 2
        candidate = np.maximum(1, np.floor(shape * scale).astype(np.int64))
        if int(np.prod(candidate, dtype=object)) <= _MAX_BYTES:
            low, result = scale, candidate
        else:
            high = scale
    return result


def _pool_cached_mask(cache_dir, descriptor, output, crop_origin, native_shape, cancelled, value=1):
    """Write ``value`` for streamed native samples into physical voxel-face bins, bounded in RAM."""
    shape = np.asarray(descriptor["shape"], dtype=np.int64)
    offset = np.asarray(descriptor["origin_ijk"], dtype=np.int64) - crop_origin
    target = np.asarray(output.shape, dtype=np.int64)
    flat = output.reshape(-1, order="F")
    plane = int(shape[0]) * int(shape[1])
    for chunk in descriptor["chunks"]:
        _cancelled(cancelled)
        path = review_volume.chunk_path(cache_dir, descriptor, descriptor["key"], chunk["index"])
        with gzip.open(path, "rb") as stream:
            remaining = chunk["byte_length"]
            position = int(chunk["start_k"]) * plane
            while remaining:
                _cancelled(cancelled)
                count = min(remaining, _STREAM_BYTES)
                raw = stream.read(count)
                if len(raw) != count:
                    raise ValueError("Cached native mask has an incorrect chunk length")
                samples = np.frombuffer(raw, dtype=np.uint8)
                if np.any(samples > 1):
                    raise ValueError("Cached native mask must contain only binary values")
                indices = np.flatnonzero(samples) + position
                if indices.size:
                    # Integer arithmetic defines exact centre occupancy even at
                    # bin boundaries. A union always writes 1; a label union
                    # writes each label in order, so later labels win overlaps.
                    i = ((2 * (indices % shape[0] + offset[0]) + 1) * target[0]) // (2 * native_shape[0])
                    j = ((2 * ((indices // shape[0]) % shape[1] + offset[1]) + 1) * target[1]) // (2 * native_shape[1])
                    k = ((2 * (indices // plane + offset[2]) + 1) * target[2]) // (2 * native_shape[2])
                    flat[i + target[0] * (j + target[1] * k)] = value
                position += count
                remaining -= count
            if stream.read(1):
                raise ValueError("Cached native mask has an incorrect chunk length")


def volume_mask_union(reference_path, masks, cache_dir, margin_mm=0, *, reference_image_id=None, cancelled=None, case_id=None):
    """Return a source-bound, content-addressed union presentation descriptor.

    ``masks`` is an iterable of (public structure ID, native binary mask path).
    An empty selection is invalid; an all-empty selection is a valid zero mask.
    Cancellation may leave reusable native presentation caches, but never a
    partially published union. It does not change any source mask or CT.
    """
    selected = {}
    for structure_id, mask_path in masks:
        if not isinstance(structure_id, str) or not structure_id:
            raise ValueError("A valid structure selection is required")
        path = Path(mask_path).resolve()
        if structure_id in selected and selected[structure_id] != path:
            raise ValueError("A structure ID identifies multiple mask sources")
        selected[structure_id] = path
    if not selected:
        raise ValueError("Select at least one structure for volume masking")
    return _presentation_union(reference_path, sorted(selected.items()), cache_dir, margin_mm, reference_image_id, cancelled, labels=False, case_id=case_id)


def volume_label_union(reference_path, masks, cache_dir, *, reference_image_id=None, cancelled=None, case_id=None):
    """Return a source-bound label presentation descriptor for colouring the volume.

    ``masks`` is an ordered iterable of (public structure ID, native binary mask
    path). Voxel value ``n`` marks the ``n``-th structure; zero is background.
    Overlaps take the later structure, so callers list structures that should
    stay visible inside others last. Like the union, it changes no source data.
    """
    ordered = []
    for structure_id, mask_path in masks:
        if not isinstance(structure_id, str) or not structure_id:
            raise ValueError("A valid structure selection is required")
        if any(structure_id == existing for existing, _ in ordered):
            raise ValueError("Each structure can be coloured only once")
        ordered.append((structure_id, Path(mask_path).resolve()))
    if not ordered:
        raise ValueError("Select at least one structure to colour the volume")
    if len(ordered) > _MAX_LABELS:
        raise ValueError(f"Colour at most {_MAX_LABELS} structures in the volume")
    return _presentation_union(reference_path, ordered, cache_dir, 0, reference_image_id, cancelled, labels=True, case_id=case_id)


def _presentation_union(reference_path, selected, cache_dir, margin_mm, reference_image_id, cancelled, *, labels, case_id=None):
    margin_mm = review_volume.display_margin(margin_mm)
    _cancelled(cancelled)
    reference_path = Path(reference_path)
    reference_digest = review_volume._file_digest(reference_path)
    reference_image, reference_affine = imaging._image(reference_path)
    reference = review_volume._geometry(reference_image, reference_affine)
    inputs = []
    for structure_id, path in selected:
        _cancelled(cancelled)
        inputs.append((structure_id, path, review_volume._file_digest(path)))
    identity = {
        "implementation": _LABEL_IMPLEMENTATION if labels else _IMPLEMENTATION,
        "native_implementation": review_volume._IMPLEMENTATION,
        "margin_implementation": "physical-mask-margin-v1",
        "max_bytes": _MAX_BYTES,
        "reference_image_id": reference_image_id,
        "reference_sha256": reference_digest,
        "reference_geometry": reference,
        "display_margin_mm": margin_mm,
        "inputs": [{"id": structure_id, "sha256": digest} for structure_id, _, digest in inputs],
    }
    key = review_volume._identity(identity)

    def validate():
        _cancelled(cancelled)
        if review_volume._file_digest(reference_path) != reference_digest or any(review_volume._file_digest(path) != digest for _, path, digest in inputs):
            raise ValueError("Volume mask input changed while reading; retry the request")

    cached = review_volume._read_cached(cache_dir, key)
    if cached is not None:
        validate()
        return cached
    with _generation_lock(cache_dir, cancelled):
        cached = review_volume._read_cached(cache_dir, key)
        if cached is not None:
            validate()
            return cached
        descriptors = []
        for _, path, _ in inputs:
            _cancelled(cancelled)
            # Native validation and exact physical dilation happen before
            # pooling. Only one original mask is decoded at a time; its crop is
            # an immutable reusable cache, never an edited measurement mask.
            descriptors.append(
                review_volume.mask_volume(
                    reference_path, path, cache_dir, "full", margin_mm, reference_image_id=reference_image_id, validate=validate, case_id=case_id
                )
            )
        validate()
        nonempty = [(label, entry) for label, entry in enumerate(descriptors, start=1) if not entry["empty"]]
        if nonempty:
            crop_origin = np.min([entry["origin_ijk"] for _, entry in nonempty], axis=0)
            high = np.max([np.asarray(entry["origin_ijk"]) + entry["shape"] for _, entry in nonempty], axis=0)
            native_shape = high - crop_origin
        else:
            crop_origin = np.zeros(3, dtype=np.int64)
            native_shape = np.ones(3, dtype=np.int64)
        target_shape = _bounded_shape(native_shape)
        output = np.zeros(tuple(target_shape), dtype=np.uint8, order="F")
        for label, descriptor in nonempty:
            _pool_cached_mask(cache_dir, descriptor, output, crop_origin, native_shape, cancelled, label if labels else 1)
        step = native_shape / target_shape
        origin = crop_origin + (step - 1) / 2
        affine = reference_affine.copy()
        affine[:3, :3] = reference_affine[:3, :3] @ np.diag(step)
        affine[:3, 3] = imaging._world(reference_affine, origin)
        geometry = {
            "shape": target_shape.tolist(),
            "affine_ras": affine.tolist(),
            "spacing": np.linalg.norm(affine[:3, :3], axis=0).tolist(),
            "bounds_ras": imaging._bounds(target_shape, affine).tolist(),
            "center_ras": imaging._world(affine, (target_shape - 1) / 2).tolist(),
        }
        descriptor = {
            "schema_version": 1,
            "key": key,
            "kind": "mask",
            "dtype": "uint8",
            "quality": "preview",
            "presentation_only": True,
            "volume_mask_union": not labels,
            "approximate": bool(np.any(target_shape != native_shape)),
            "pooling": "last-label-centre-occupancy" if labels else "foreground-centre-occupancy",
            "shape": target_shape.tolist(),
            "affine_ras": affine.tolist(),
            "geometry": geometry,
            "reference_geometry": reference,
            "reference_image_id": reference_image_id,
            "origin_ijk": origin.tolist(),
            "native_crop_origin_ijk": crop_origin.tolist(),
            "native_crop_shape": native_shape.tolist(),
            "selected_structure_ids": sorted(structure_id for structure_id, _, _ in inputs),
            "display_margin_mm": margin_mm,
            "slope": 1.0,
            "intercept": 0.0,
            "empty": not nonempty,
        }
        if labels:
            descriptor["volume_label_union"] = True
            descriptor["labels"] = [structure_id for structure_id, _, _ in inputs]
        return review_volume._write(cache_dir, descriptor, output, validate=validate, case_id=case_id)
