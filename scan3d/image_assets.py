"""Immutable source-image identity shared by viewing and segmentation.

The first adapter remains conventional CT. Image identity and ownership do not
depend on a renderer, segmentation task, filesystem location or revision ID.
Legacy revision records are projected into this contract without rewriting
their stored JSON or source files.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import re

import nibabel as nib
import numpy as np

from .domain import Modality


_ID = re.compile(r"[a-f0-9]{32}")
_DIGEST = re.compile(r"[a-f0-9]{64}")
_SOURCE_FIELDS = {"series_uid", "study_uid", "frame_of_reference_uid", "sop_class_uids"}


@lru_cache(maxsize=128)
def _content_digest(path, device, inode, size, modified_ns, changed_ns):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def image_digest(path):
    path = Path(path).resolve()
    stat = path.stat()
    return _content_digest(str(path), stat.st_dev, stat.st_ino, stat.st_size,
                           stat.st_mtime_ns, stat.st_ctime_ns)


@lru_cache(maxsize=128)
def _header_metadata(path, device, inode, size, modified_ns, changed_ns, content_digest):
    from . import imaging

    image = nib.load(path)
    proxy = image.dataobj
    geometry = imaging.geometry(Path(path))
    scalar = {"dtype": image.get_data_dtype().newbyteorder("=").name,
              "slope": float(proxy.slope), "intercept": float(proxy.inter)}
    stat = Path(path).stat()
    if (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns) != (
            device, inode, size, modified_ns, changed_ns):
        raise ValueError("Source image changed while reading its header")
    return geometry, scalar


def _image_metadata(path, content_digest):
    path = Path(path).resolve()
    stat = path.stat()
    geometry, scalar = _header_metadata(str(path), stat.st_dev, stat.st_ino, stat.st_size,
                                        stat.st_mtime_ns, stat.st_ctime_ns, content_digest)
    # Callers may assemble new records from these values; cached metadata must
    # never become mutable state shared between assets or revisions.
    return deepcopy(geometry), dict(scalar)


def _identity(record):
    value = {"schema_version": 1, "case_id": record["case_id"],
             "kind": record["kind"], "modality": record["modality"],
             "source": record["source"],
             "content_sha256": record["content_sha256"],
             "frame_selector": record.get("frame_selector", {})}
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()[:32]


def _same_geometry(left, right):
    try:
        return (list(left["shape"]) == list(right["shape"])
                and np.allclose(left["affine_ras"], right["affine_ras"], rtol=0, atol=1e-4))
    except (KeyError, TypeError, ValueError):
        return False


def _selectors(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Image frame selectors must be an object")
    for key, item in value.items():
        if (not isinstance(key, str) or not key or len(key) > 80
                or isinstance(item, (dict, list, bool))
                or item is not None and not isinstance(item, (str, int, float))
                or isinstance(item, float) and not math.isfinite(item)
                or key.endswith("_index") and (type(item) is not int or item < 0)):
            raise ValueError("Invalid image frame selector")
    return deepcopy(value)


def create_image_asset(case_id, file_path, *, acquisition=None, source=None,
                       frame_selector=None, modality: Modality | str = Modality.CT,
                       presentation_metadata=None):
    from .modalities import get_modality_adapter

    if not isinstance(case_id, str) or _ID.fullmatch(case_id) is None:
        raise ValueError("Invalid image case identifier")
    adapter = get_modality_adapter(modality)
    path = Path(file_path).resolve()
    digest = image_digest(path)
    geometry, scalar = _image_metadata(path, digest)
    metadata = deepcopy(source or {})
    if not isinstance(metadata, dict) or set(metadata) - _SOURCE_FIELDS:
        raise ValueError("Invalid image source metadata")
    if "series_uid" not in metadata and (acquisition or {}).get("series_uid"):
        metadata["series_uid"] = acquisition["series_uid"]
    metadata.setdefault("series_uid", None)
    if isinstance(metadata.get("sop_class_uids"), list):
        metadata["sop_class_uids"] = sorted(set(metadata["sop_class_uids"]))
    record = dict(schema_version=1, case_id=case_id, kind=adapter.kind.value,
                  modality=adapter.modality.value, content_sha256=digest,
                  source=metadata, frame_selector=_selectors(frame_selector),
                  geometry=geometry,
                  scalar={**scalar, "units": adapter.units},
                  presentation=(adapter.presentation() if adapter.modality == Modality.CT else
                                adapter.presentation_for_image(path, metadata=presentation_metadata)),
                  capabilities=[value.value for value in adapter.capabilities],
                  file_path=str(path))
    record["id"] = _identity(record)
    validate_image_asset(record)
    return record


def validate_image_asset(record):
    from .modalities import get_modality_adapter, normalize_presentation

    if not isinstance(record, dict) or record.get("schema_version") != 1:
        raise ValueError("Unsupported image asset schema")
    if (not isinstance(record.get("case_id"), str) or _ID.fullmatch(record["case_id"]) is None
            or not isinstance(record.get("id"), str) or _ID.fullmatch(record["id"]) is None
            or not isinstance(record.get("content_sha256"), str)
            or _DIGEST.fullmatch(record["content_sha256"]) is None):
        raise ValueError("Invalid image asset identity")
    adapter = get_modality_adapter(record.get("modality"))
    source = record.get("source")
    if not isinstance(source, dict) or set(source) - _SOURCE_FIELDS:
        raise ValueError("Invalid image source metadata")
    for key, value in source.items():
        values = value if key == "sop_class_uids" else [value]
        if not isinstance(values, list) or any(item is not None and (not isinstance(item, str)
                or not item or len(item) > 256) for item in values):
            raise ValueError("Invalid image source identifier")
    _selectors(record.get("frame_selector"))
    geometry = record.get("geometry")
    if (not isinstance(geometry, dict) or len(geometry.get("shape", [])) != 3
            or any(type(n) is not int or n < 1 for n in geometry["shape"])
            or not _same_geometry(geometry, geometry)):
        raise ValueError("Invalid image geometry")
    affine = np.asarray(geometry["affine_ras"], dtype=float)
    if (affine.shape != (4, 4) or not np.isfinite(affine).all()
            or not np.allclose(affine[3], [0, 0, 0, 1], rtol=0, atol=1e-9)
            or abs(np.linalg.det(affine[:3, :3])) < 1e-9):
        raise ValueError("Invalid image patient transform")
    scalar = record.get("scalar")
    try:
        valid_scalar = (np.dtype(scalar["dtype"]).kind in "biuf"
                        and math.isfinite(scalar["slope"]) and scalar["slope"] != 0
                        and math.isfinite(scalar["intercept"]) and scalar["units"] == adapter.units)
    except (KeyError, TypeError, ValueError):
        valid_scalar = False
    try:
        presentation = normalize_presentation(record.get("presentation"))
        valid_presentation = presentation["units"] == adapter.units
    except (TypeError, ValueError):
        valid_presentation = False
    capabilities = record.get("capabilities")
    valid_capabilities = (isinstance(capabilities, list)
                          and all(isinstance(value, str) for value in capabilities)
                          and len(capabilities) == len(set(capabilities))
                          and set(capabilities) <= set(adapter.capabilities))
    if (record.get("kind") != adapter.kind or not valid_scalar
            or not isinstance(record.get("presentation"), dict) or not valid_presentation
            or not valid_capabilities
            or not isinstance(record.get("file_path"), str)
            or not Path(record["file_path"]).is_absolute()
            or record["id"] != _identity(record)):
        raise ValueError("Invalid image asset metadata or identity")
    return record


def public_image_asset(record):
    """Whitelist the contract; filesystem paths never enter API responses."""
    return deepcopy({key: record[key] for key in (
        "schema_version", "id", "case_id", "kind", "modality", "content_sha256",
        "source", "frame_selector", "geometry", "scalar", "presentation", "capabilities")})


def assert_reference_image(asset, file_path, geometry=None):
    """Check reference pixels/grid before accepting another derived revision."""
    validate_image_asset(asset)
    digest = image_digest(file_path)
    if digest != asset["content_sha256"]:
        raise ValueError("The source image differs from this revision's reference image")
    actual_geometry, actual_scalar = _image_metadata(file_path, digest)
    if not _same_geometry(asset["geometry"], actual_geometry):
        raise ValueError("Image geometry differs from the source image header")
    if any(asset["scalar"][key] != value for key, value in actual_scalar.items()):
        raise ValueError("Image scalar encoding differs from the source image header")
    if geometry is not None and not _same_geometry(asset["geometry"], geometry):
        raise ValueError("Image geometry differs from this revision's reference image")
    return asset


def bind_structures(structures, reference_image_id):
    """Bind legacy entries by copy, rejecting explicit cross-image reuse."""
    if not isinstance(reference_image_id, str) or _ID.fullmatch(reference_image_id) is None:
        raise ValueError("Invalid reference image identifier")
    result = deepcopy(structures)
    for entry in result:
        if entry.get("reference_image_id", reference_image_id) != reference_image_id:
            raise ValueError("Segment belongs to another reference image")
        entry["reference_image_id"] = reference_image_id
    return result


def reference_image(revision):
    """Resolve new or legacy references without modifying any persisted record."""
    snapshot = revision.get("image_asset")
    if snapshot is None:
        asset = create_image_asset(revision["case_id"], revision["ct_file"],
                                   acquisition=revision.get("acquisition"))
    else:
        if not isinstance(snapshot, dict):
            raise ValueError("Invalid revision image metadata")
        asset = {**deepcopy(snapshot), "file_path": str(Path(revision["ct_file"]).resolve())}
        validate_image_asset(asset)
    if (asset["case_id"] != revision["case_id"]
            or revision.get("reference_image_id", asset["id"]) != asset["id"]
            or not _same_geometry(asset["geometry"], revision["geometry"])):
        raise ValueError("Revision does not match its reference image")
    assert_reference_image(asset, revision["ct_file"], revision["geometry"])
    bind_structures(revision.get("structures", []), asset["id"])
    return asset
