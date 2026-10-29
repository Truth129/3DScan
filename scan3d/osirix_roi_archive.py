"""Write native OsiriX brush archives from exact source-image pixels.

The named-key profile follows synthetic OsiriX MD 14.3 saved ROIs. Apple LZ4
stored blocks keep the payload portable without a platform-specific encoder.
This private format still requires OsiriX import/display/save qualification.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import plistlib
import re
import struct
from typing import Callable, Sequence
from uuid import UUID

import numpy as np


_MAX_PIXELS = 16 * 1024**2
_MAX_AXIS = 16_384
_MAX_ROIS = 1_000
_MAX_ARCHIVE_BYTES = 32 * 1024**2
_APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
_UNSET_Z_LOCATION = 1.1754943508222875e-38


class RoiArchiveError(ValueError):
    """An export explanation without source paths or patient metadata."""


@dataclass(frozen=True, slots=True)
class BrushROI:
    mask_2d: np.ndarray | Sequence[Sequence[bool | int | float]]
    name: str
    color: str
    study_instance_uid: str
    roi_uuid: str
    pixel_spacing_x: float
    pixel_spacing_y: float
    image_origin: tuple[float, float]
    group_id: float | None = None


@dataclass(frozen=True, slots=True)
class _PreparedBrush:
    brush: BrushROI
    mask: np.ndarray
    left: int
    top: int
    width: int
    height: int
    uuid: UUID
    origin: tuple[float, float]
    spacing: tuple[float, float]
    color: tuple[int, int, int]
    group_id: float


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("Native ROI export cancelled")


def _text(value, *, nonempty=False, max_bytes=1_024):
    if not isinstance(value, str) or (nonempty and not value) or "\0" in value:
        raise RoiArchiveError("Native ROI text is invalid")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise RoiArchiveError("Native ROI text is invalid") from exc
    if size > max_bytes:
        raise RoiArchiveError("Native ROI text exceeds the supported length")
    return size


def _finite_float(value):
    if isinstance(value, (bool, str, bytes)):
        raise RoiArchiveError("Native ROI geometry is invalid")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise RoiArchiveError("Native ROI geometry is invalid") from exc
    if not math.isfinite(result):
        raise RoiArchiveError("Native ROI geometry must be finite")
    return result


def _prepare(brush):
    if not isinstance(brush, BrushROI):
        raise RoiArchiveError("Native ROI inputs are invalid")
    _text(brush.name, nonempty=True, max_bytes=256)
    if len(brush.name) > 64 or not brush.name.isprintable() or "\\" in brush.name:
        raise RoiArchiveError("Native ROI names must be printable labels of at most 64 characters")
    if not isinstance(brush.color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", brush.color):
        raise RoiArchiveError("Native ROI color must use six hexadecimal digits")
    study_uid = brush.study_instance_uid
    if not isinstance(study_uid, str) or len(study_uid) > 64 or not re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.(?:0|[1-9][0-9]*))+", study_uid):
        raise RoiArchiveError("Native ROI study UID is invalid")
    try:
        if not isinstance(brush.roi_uuid, str):
            raise ValueError
        roi_uuid = UUID(brush.roi_uuid)
    except (ValueError, TypeError, AttributeError) as exc:
        raise RoiArchiveError("Native ROI UUID is invalid") from exc
    if roi_uuid.int == 0:
        raise RoiArchiveError("Native ROI UUID must be nonzero")
    group_id = _finite_float(brush.group_id) if brush.group_id is not None else float((roi_uuid.int & ((1 << 52) - 1)) or 1)
    if group_id <= 0:
        raise RoiArchiveError("Native ROI group identifier must be positive")
    spacing = (_finite_float(brush.pixel_spacing_x), _finite_float(brush.pixel_spacing_y))
    if min(spacing) <= 0:
        raise RoiArchiveError("Native ROI pixel spacing must be positive")
    try:
        if len(brush.image_origin) != 2:
            raise RoiArchiveError("Native ROI image origin must contain two coordinates")
        origin = tuple(_finite_float(value) for value in brush.image_origin)
    except TypeError as exc:
        raise RoiArchiveError("Native ROI image origin must contain two coordinates") from exc
    # Inspect nested sequence sizes before NumPy can allocate its conversion.
    if not isinstance(brush.mask_2d, np.ndarray):
        try:
            rows = len(brush.mask_2d)
            if rows == 0 or rows > _MAX_AXIS:
                raise RoiArchiveError("Native ROI source dimensions exceed the supported bounds")
            columns = len(brush.mask_2d[0])
            if columns == 0 or columns > _MAX_AXIS or rows * columns > _MAX_PIXELS:
                raise RoiArchiveError("Native ROI source dimensions exceed the supported bounds")
            if any(len(row) != columns for row in brush.mask_2d):
                raise RoiArchiveError("Native ROI masks must be rectangular")
        except TypeError as exc:
            raise RoiArchiveError("Native ROI masks must be two-dimensional") from exc
    try:
        mask = np.asarray(brush.mask_2d)
    except (TypeError, ValueError) as exc:
        raise RoiArchiveError("Native ROI masks must be rectangular binary arrays") from exc
    if mask.ndim != 2 or not all(0 < size <= _MAX_AXIS for size in mask.shape) or mask.size > _MAX_PIXELS:
        raise RoiArchiveError("Native ROI source dimensions exceed the supported bounds")
    if mask.dtype.kind not in "buif" or not np.all((mask == 0) | (mask == 1)):
        raise RoiArchiveError("Native ROI masks must contain only binary zero and one")
    occupied_rows = np.flatnonzero(np.any(mask, axis=1))
    occupied_columns = np.flatnonzero(np.any(mask, axis=0))
    if occupied_rows.size == 0:
        raise RoiArchiveError("Native ROI masks must be nonempty")
    top = max(0, int(occupied_rows[0]) - 2)
    left = max(0, int(occupied_columns[0]) - 2)
    # The compressed MD profile uses a small zero border and four-pixel blocks.
    # Padding may extend outside the source only where its pixels remain zero.
    width = (int(occupied_columns[-1]) - left + 3 + 3) // 4 * 4
    height = (int(occupied_rows[-1]) - top + 3 + 3) // 4 * 4
    if width * height > _MAX_PIXELS:
        raise RoiArchiveError("Native ROI texture exceeds the supported bounds")
    color = tuple(int(brush.color[index : index + 2], 16) * 257 for index in (1, 3, 5))
    return _PreparedBrush(brush, mask, left, top, width, height, roi_uuid, origin, spacing, color, group_id)


class _Archive:
    def __init__(self):
        self.objects = ["$null"]
        self.classes = {}

    def add(self, value):
        ref = plistlib.UID(len(self.objects))
        self.objects.append(value)
        return ref

    def class_ref(self, name):
        if name not in self.classes:
            self.classes[name] = self.add({"$classes": [name, *(["NSArray"] if name == "NSMutableArray" else []), "NSObject"], "$classname": name})
        return self.classes[name]

    def array(self, values, *, mutable=False):
        return self.add({"NS.objects": values, "$class": self.class_ref("NSMutableArray" if mutable else "NSArray")})

    def date(self, seconds):
        return self.add({"NS.time": seconds, "$class": self.class_ref("NSDate")})

    def roi(self, prepared, seconds):
        brush = prepared.brush
        source = prepared.mask
        texture = np.zeros((prepared.height, prepared.width), dtype=np.uint8)
        copied_height = min(prepared.height, source.shape[0] - prepared.top)
        copied_width = min(prepared.width, source.shape[1] - prepared.left)
        texture[:copied_height, :copied_width] = source[prepared.top : prepared.top + copied_height, prepared.left : prepared.left + copied_width]
        texture *= 255
        raw = texture.tobytes(order="C")
        # Apple's COMPRESSION_LZ4 frame permits stored blocks (bv4-) followed
        # by a little-endian decoded length and bv4$ end-of-stream marker.
        framed = b"bv4-" + struct.pack("<I", len(raw)) + raw + b"bv4$"
        empty_array = self.array([], mutable=True)
        nil = plistlib.UID(0)
        date = self.date(seconds)
        value = {
            "$class": self.class_ref("ROI"),
            "type": 20.0,
            "name": self.add(brush.name),
            "comments": self.add(""),
            "username": self.add("3DScan"),
            "UUID": self.add(str(prepared.uuid).upper()),
            "savedStudyInstanceUID": self.add(brush.study_instance_uid),
            "imageOrigin": self.add("{" + ", ".join(format(coordinate, ".17g") for coordinate in prepared.origin) + "}"),
            "pixelSpacingX": prepared.spacing[0],
            "pixelSpacingY": prepared.spacing[1],
            "textureWidth": 1,
            "textureHeight": 1,
            "textureBuffer": self.add(b"\0"),
            "textureWidthCompressed": prepared.width,
            "textureHeightCompressed": prepared.height,
            "textureBufferCompressedSize": len(raw),
            "textureBufferCompressed": self.add(framed),
            "textureUpLeftCornerX": prepared.left,
            "textureUpLeftCornerY": prepared.top,
            "textureDownRightCornerX": prepared.left + prepared.width - 1,
            "textureDownRightCornerY": prepared.top + prepared.height - 1,
            "points": empty_array,
            "zPositions": self.array([], mutable=True),
            "stringTags": self.array([], mutable=True),
            "rect": self.add("{{0, 0}, {0, 0}}"),
            "creationDate": date,
            "modificationDate": date,
            "creationTimeStamp": seconds,
            # MD saves one shared group identifier per structure across slices.
            # Standalone callers may instead leave each brush ungrouped.
            "groupID": prepared.group_id,
            "zLocation": _UNSET_Z_LOCATION,
            "color.red": float(prepared.color[0]),
            "color.green": float(prepared.color[1]),
            "color.blue": float(prepared.color[2]),
            "layerColor": nil,
            "opacity": 0.30000001192092896,
            "thickness": 5.0,
            "fill": 0.0,
            "needQuartz": 0.0,
            "roiRotation": 0.0,
            "offsetTextBox_x": 0.0,
            "offsetTextBox_y": 0.0,
            "preferredWW": 0.0,
            "preferredWL": 0.0,
            "originalIndexForAlias": -1,
            "_calciumThreshold": 0,
            "thickSlabMode": 0,
            "thickSlabCount": 0,
            "displayTextualData": True,
            "textNameOnly": True,
            "selectable": True,
            "locked": False,
            "hidden": False,
            "isAliased": False,
            "is3DMeasure": False,
            "isLayerOpacityConstant": False,
            "displayedInVR": False,
            "dontDisplayInKeyImagesWindow": False,
            "dontSavePreferredWLWW": False,
            "canColorizeLayer": False,
            "canResizeLayer": False,
            "_isSpline": False,
            "_hasIsSpline": False,
            "_displayCalciumScoring": False,
        }
        value.update({f"textualBoxLine{index}": nil for index in range(1, 10)})
        return self.add(value)


def encode_roi_archive(brushes: Sequence[BrushROI], *, timestamp: datetime | None = None, cancelled: Callable[[], bool] | None = None) -> bytes:
    """Encode nonempty brushes on one source-image grid as a binary plist.

    Geometry is supplied in DICOM source pixel coordinates; image_origin is the
    first two DICOM LPS ImagePositionPatient coordinates, not a mask-grid origin.
    A shared positive group_id binds a structure across source-image archives.
    A timezone-aware timestamp makes synthetic output reproducible when supplied.
    """
    _check_cancelled(cancelled)
    try:
        count = len(brushes)
    except TypeError as exc:
        raise RoiArchiveError("Native ROI inputs must be a bounded sequence") from exc
    if not 0 < count <= _MAX_ROIS:
        raise RoiArchiveError("Native ROI count exceeds the supported bounds")
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    if not isinstance(timestamp, datetime) or timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise RoiArchiveError("Native ROI timestamp must include a timezone")
    seconds = (timestamp.astimezone(timezone.utc) - _APPLE_EPOCH).total_seconds()
    prepared = []
    uuids = set()
    estimated_size = 1_024
    shape = None
    for brush in brushes:
        _check_cancelled(cancelled)
        item = _prepare(brush)
        if shape is None:
            shape = item.mask.shape
        if shape != item.mask.shape:
            raise RoiArchiveError("Native ROI masks must share one source-image grid")
        if item.uuid in uuids:
            raise RoiArchiveError("Native ROI UUIDs must be unique within an archive")
        uuids.add(item.uuid)
        # Covers every fixed key/object, object-table offsets and bounded text.
        estimated_size += item.width * item.height + _text(item.brush.name) + len(item.brush.study_instance_uid) + 8_192
        if estimated_size > _MAX_ARCHIVE_BYTES:
            raise RoiArchiveError("Native ROI archive exceeds the supported size")
        prepared.append(item)
    archive = _Archive()
    rois = []
    for item in prepared:
        _check_cancelled(cancelled)
        rois.append(archive.roi(item, seconds))
    root = archive.array(rois)
    data = plistlib.dumps(
        {"$version": 100_000, "$archiver": "NSKeyedArchiver", "$top": {"root": root}, "$objects": archive.objects},
        fmt=plistlib.FMT_BINARY,
        sort_keys=False,
    )
    if len(data) > _MAX_ARCHIVE_BYTES:
        raise RoiArchiveError("Native ROI archive exceeds the supported size")
    _check_cancelled(cancelled)
    return data
