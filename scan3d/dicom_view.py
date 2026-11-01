"""One rendered 3D view as an uncalibrated Secondary Capture in its source study.

The capture comes from a browser, so its PNG is checked chunk by chunk before
any decoder sees it. Patient and study elements are copied exactly from the
source headers: a render must file under the same study as its CT, and one odd
demographic value must not block a picture that does not depend on it.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
import struct
import tempfile
import threading
import zlib

import numpy as np
from PIL import Image
import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.uid import ExplicitVRLittleEndian, UID, generate_uid

from . import __version__
from .fileio import hash_file

_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_MAX_AXIS = 16384
_MAX_PIXELS = 36 * 1024**2  # Covers an 8K render.
_MAX_SOURCES = 100_000
_BLOCK = 1024 * 1024
# Animation, and a colour key that would silently turn one colour transparent.
_REFUSED_CHUNKS = {b"acTL", b"fcTL", b"fdAT", b"tRNS"}
# What Pillow raises for a damaged file. Most are not ValueError, so left alone
# they would surface as a logged 500 rather than a refused capture.
_DECODE_ERRORS = (OSError, SyntaxError, EOFError, ValueError, struct.error, zlib.error, Image.DecompressionBombError)
# Copied after construction, because highdicom's validators refuse legal-in-the-
# wild values here, and one odd date must not block a render that ignores it.
_COPIED = (
    "PatientName",
    "PatientID",
    "PatientBirthDate",
    "PatientSex",
    "IssuerOfPatientID",
    "AccessionNumber",
    "StudyID",
    "StudyDate",
    "StudyTime",
    "ReferringPhysicianName",
    "StudyDescription",
)
_ONE_PATIENT = ("StudyInstanceUID", "SeriesInstanceUID", "PatientID", "IssuerOfPatientID", "PatientName", "PatientBirthDate", "PatientSex")
_UNREADABLE = "The captured view is not a complete PNG image. Capture the view again."
_UNSUPPORTED = "The captured view must be a still 8-bit RGB or RGBA PNG. Capture the view again."
_TOO_LARGE = "The captured view has too many pixels. Make the pane smaller and capture again."
_BLANK = "The captured view is blank. Capture the view again."
_COMMENTS = "Rendered 3D view from 3DScan. Not calibrated: do not measure. No orientation labels. Research use only."

# A maximum-size decode peaks at several hundred MiB, and each request runs on
# its own server thread alongside the job worker.
_DECODE_SLOT = threading.BoundedSemaphore(1)


class DicomViewError(ValueError):
    """A constant, public-safe explanation without clinical paths or tags."""


@dataclass(frozen=True, slots=True)
class ViewExportResult:
    revision_id: str
    series_instance_uid: str
    study_instance_uid: str
    sop_instance_uid: str
    rows: int
    columns: int
    path: Path
    sha256: str
    size: int


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("DICOM view export cancelled")


def _stamp(path):
    try:
        value = Path(path).stat()
    except OSError:
        raise DicomViewError("An original export input is unavailable") from None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _uid(value):
    text = str(value)
    if not UID(text, validation_mode=pydicom.config.IGNORE).is_valid:
        raise DicomViewError("The original CT has a missing or invalid DICOM identity")
    return text


def _check_header(data):
    if len(data) != 13:
        raise DicomViewError(_UNREADABLE)
    width, height, depth, colour = struct.unpack(">IIBB", data[:10])
    if not (0 < width and 0 < height):
        raise DicomViewError(_UNREADABLE)
    if depth != 8 or colour not in (2, 6):
        raise DicomViewError(_UNSUPPORTED)
    if width > _MAX_AXIS or height > _MAX_AXIS or width * height > _MAX_PIXELS:
        raise DicomViewError(_TOO_LARGE)
    return width, height


def _check_chunks(stream):
    """Walk the file before decoding it, so its header bounds the decode.

    This also proves what the decoder does not: it skips IDAT checksums and
    accepts a file that stops before IEND.
    """
    if stream.read(len(_SIGNATURE)) != _SIGNATURE:
        raise DicomViewError(_UNREADABLE)
    size = None
    while True:
        prefix = stream.read(8)
        if len(prefix) != 8:
            raise DicomViewError(_UNREADABLE)
        length, kind = struct.unpack(">I4s", prefix)
        if (kind == b"IHDR") != (size is None) or (kind == b"IHDR" and length != 13):
            raise DicomViewError(_UNREADABLE)
        if kind in _REFUSED_CHUNKS:
            raise DicomViewError(_UNSUPPORTED)
        checksum = zlib.crc32(kind)
        if kind == b"IHDR":
            header = stream.read(length)
            size = _check_header(header)
            checksum = zlib.crc32(header, checksum)
        else:
            remaining = length
            while remaining:
                block = stream.read(min(remaining, _BLOCK))
                if not block:
                    raise DicomViewError(_UNREADABLE)
                checksum = zlib.crc32(block, checksum)
                remaining -= len(block)
        if stream.read(4) != struct.pack(">I", checksum):
            raise DicomViewError(_UNREADABLE)
        if kind == b"IEND":
            if length or stream.read(1):
                raise DicomViewError(_UNREADABLE)
            return size


def _flatten(pixels):
    """Blend over black, since DICOM RGB has no alpha; opaque pixels stay identical."""
    if pixels.shape[2] == 3:
        return pixels
    colour, alpha = pixels[..., :3], pixels[..., 3]
    if alpha.min() == 255:
        return colour
    # uint16 holds 255 * 255 + 127, in a third of a float64's memory.
    blended = (colour.astype(np.uint16) * alpha[..., np.newaxis].astype(np.uint16) + 127) // 255
    return blended.astype(np.uint8)


def read_view_png(path) -> np.ndarray:
    """Decode a capture to (rows, columns, 3) uint8, or refuse with a public reason."""
    with _DECODE_SLOT:
        try:
            with Path(path).open("rb") as stream:
                width, height = _check_chunks(stream)
                stream.seek(0)
                with Image.open(stream, formats=("PNG",)) as image:
                    image.load()
                    if image.mode not in ("RGB", "RGBA") or image.size != (width, height) or getattr(image, "is_animated", False):
                        raise DicomViewError(_UNSUPPORTED)
                    pixels = np.asarray(image)
            rgb = _flatten(pixels)
            if not np.ptp(rgb, axis=(0, 1)).any():
                raise DicomViewError(_BLANK)
            return rgb
        except DicomViewError:
            raise
        except _DECODE_ERRORS:
            raise DicomViewError(_UNREADABLE) from None


def _source_headers(paths, cancelled):
    """Read every header: a mixed series must not borrow its first file's patient."""
    if not paths or len(paths) > _MAX_SOURCES or len(set(paths)) != len(paths):
        raise DicomViewError("Select one complete original CT series")
    reference, identity, burned_in, stamps = None, None, False, []
    for path in paths:
        _check_cancelled(cancelled)
        stamps.append((path, _stamp(path)))
        try:
            image = pydicom.dcmread(path, stop_before_pixels=True)
        except (OSError, InvalidDicomError):
            raise DicomViewError("An original CT header cannot be read") from None
        sop_uid = _uid(image.get("SOPInstanceUID", ""))
        _uid(image.get("StudyInstanceUID", ""))
        _uid(image.get("SeriesInstanceUID", ""))
        if image.file_meta.get("MediaStorageSOPInstanceUID", sop_uid) != sop_uid:
            raise DicomViewError("Original CT file and image identities disagree")
        current = tuple(str(image.get(key, "")) for key in _ONE_PATIENT)
        if identity is None:
            reference, identity = image, current
        elif current != identity:
            raise DicomViewError("Original CT images do not belong to one patient, study and series")
        burned_in = burned_in or str(image.get("BurnedInAnnotation", "")).strip().upper() == "YES"
    return reference, burned_in, stamps


def _encode(reference, pixels, *, revision_id, burned_in):
    try:
        import highdicom as hd
    except ImportError:
        raise DicomViewError("DICOM view export requires the qualified highdicom 0.28.2 dependency") from None
    if hd.__version__ != "0.28.2":
        raise DicomViewError("DICOM view export requires qualification of this highdicom version")
    template = deepcopy(reference)
    for keyword in _COPIED:
        if keyword in template:
            del template[keyword]
    # highdicom refuses a series time later than its own content time, which
    # it reads from the clock inside the constructor.
    moment = datetime.now()
    result = hd.sc.SCImage.from_ref_dataset(
        ref_dataset=template,
        pixel_array=pixels,
        photometric_interpretation="RGB",
        bits_allocated=8,
        coordinate_system="PATIENT",
        series_instance_uid=generate_uid(),
        series_number=501,
        sop_instance_uid=generate_uid(),
        instance_number=1,
        manufacturer="3DScan",
        patient_orientation=("L", "F"),
        transfer_syntax_uid=ExplicitVRLittleEndian,
        series_description=f"3DScan view {revision_id[:8]}",
        manufacturer_model_name="Rendered view export",
        software_versions=__version__,
        device_serial_number="3DScan",
        series_date=moment.date(),
        series_time=moment.time(),
        # Given as an argument, not set afterwards: from_ref_dataset would
        # otherwise validate the source's own value, and legal ISO 2022 and
        # empty values fail that check.
        specific_character_set="ISO_IR 192",
    )
    for keyword in (*_COPIED, "Laterality"):
        if keyword in reference:
            result.add(deepcopy(reference[keyword]))
    # highdicom insists on a value for a PATIENT image, but no pair is true of a
    # render: the camera can face anywhere, and leaving these would label every
    # view with a false orientation. Type 2 allows empty for unknown.
    result.PatientOrientation = None
    result.ConversionType = "WSD"
    # The render can show source pixels, so their burned-in text survives into it.
    result.BurnedInAnnotation = "YES" if burned_in else "NO"
    result.ImageComments = _COMMENTS
    result.DerivationDescription = f"Rendered 3D view of 3DScan revision {revision_id} from source series {reference.SeriesInstanceUID}"
    return result


def _export_view(source_paths, png_path, output_dir, *, revision_id, cancelled=None) -> ViewExportResult:
    if not isinstance(revision_id, str) or not revision_id:
        raise DicomViewError("DICOM view export requires a saved revision")
    _check_cancelled(cancelled)
    paths = tuple(Path(path) for path in source_paths)
    png_path, output_dir = Path(png_path), Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise DicomViewError("Use a new DICOM view export directory; existing exports are immutable")
    png_stamp = _stamp(png_path)
    reference, burned_in, stamps = _source_headers(paths, cancelled)
    stamps.append((png_path, png_stamp))
    pixels = read_view_png(png_path)
    _check_cancelled(cancelled)
    image = _encode(reference, pixels, revision_id=revision_id, burned_in=burned_in)
    _check_cancelled(cancelled)
    output_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    name = "view-0001.dcm"
    with tempfile.TemporaryDirectory(prefix=".dicom-view-", dir=output_dir.parent) as temporary:
        staging = Path(temporary)
        destination = staging / name
        image.save_as(destination, enforce_file_format=True)
        destination.chmod(0o600)
        result = ViewExportResult(
            revision_id,
            str(image.SeriesInstanceUID),
            str(image.StudyInstanceUID),
            str(image.SOPInstanceUID),
            int(image.Rows),
            int(image.Columns),
            output_dir / name,
            hash_file(destination),
            destination.stat().st_size,
        )
        _check_cancelled(cancelled)
        if any(_stamp(path) != before for path, before in stamps):
            raise DicomViewError("An original CT or the captured view changed during DICOM view export")
        if output_dir.exists():
            raise DicomViewError("The DICOM view export destination is already occupied")
        staging.replace(output_dir)
    return result


def export_view(source_paths, png_path, output_dir, *, revision_id, cancelled=None) -> ViewExportResult:
    """Atomically publish a new one-instance series; never overwrite an export.

    The caller binds source paths and the capture to the saved revision. This
    adapter proves one patient, study and series, and reads the pixels itself.
    """
    try:
        return _export_view(source_paths, png_path, output_dir, revision_id=revision_id, cancelled=cancelled)
    except (DicomViewError, InterruptedError):
        raise
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, KeyError, EOFError):
        raise DicomViewError("The DICOM view could not be encoded from these inputs. Check the original CT.") from None
