"""Experimental OsiriX MD 14.3 brush reports, used by the app's Orthanc export.

This compatibility profile comes from the returned synthetic SEG-derived
brushes. Source pixel assignment is exact; receiver interpretation on other
source orientations still needs OsiriX qualification before clinical use.
"""

from __future__ import annotations

from copy import deepcopy
import ctypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
import re
import os
import stat
import tempfile
from uuid import uuid4
import zlib

import numpy as np
from nibabel.filebasedimages import ImageFileError
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import BasicTextSRStorage, ExplicitVRLittleEndian, generate_uid

from . import __version__, imaging
from .dicom_seg import DicomSegError, SegmentInput, _source_stack
from .fileio import hash_file
from .osirix_roi_archive import BrushROI, encode_roi_archive


_MAX_INSTANCES = 1000
_MAX_ROIS = 100_000
_MAX_TOTAL_BYTES = 512 * 1024**2
_MAX_PLANE_PIXELS = 16 * 1024**2
_MAX_NATIVE_VOXELS = 512 * 1024**2
_MAX_SPOOL_BYTES = 512 * 1024**2
_MAX_PLANE_ROI_BYTES = 32 * 1024**2


class NativeROIError(ValueError):
    """A public-safe explanation without patient tags or input paths."""


@dataclass(frozen=True, slots=True)
class NativeROI:
    structure_id: str
    name: str
    voxel_count: int
    roi_uuid: str


@dataclass(frozen=True, slots=True)
class NativeROIInstance:
    path: Path
    source_sop_instance_uid: str
    sop_instance_uid: str
    series_instance_uid: str
    sha256: str
    size: int
    rois: tuple[NativeROI, ...]


@dataclass(frozen=True, slots=True)
class NativeExportResult:
    revision_id: str
    series_instance_uids: tuple[str, ...]
    study_instance_uid: str
    frame_of_reference_uid: str
    reference_sha256: str
    files: tuple[NativeROIInstance, ...]
    encoding_version: int = 1


def _cancel(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("Native ROI export cancelled")


def _publish_new(staging, output):
    # POSIX rename() may replace a directory created after an exists() check.
    # Linux's exclusive atomic rename preserves both publication guarantees.
    try:
        rename = ctypes.CDLL(None, use_errno=True).renameat2
    except AttributeError:
        raise NativeROIError("Exclusive native ROI publication requires Linux renameat2") from None
    rename.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    rename.restype = ctypes.c_int
    if rename(-100, os.fsencode(staging), -100, os.fsencode(output), 1) != 0:
        raise NativeROIError("Native ROI destination exists or cannot be published exclusively")


def _stamp(path):
    try:
        value = Path(path).lstat()
    except OSError:
        raise NativeROIError("An original export input is unavailable") from None
    if not stat.S_ISREG(value.st_mode):
        raise NativeROIError("Native ROI inputs must be regular files")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _selection(segments):
    values = tuple(segments)
    if not values or len(values) > 1000 or any(not isinstance(value, SegmentInput) for value in values):
        raise NativeROIError("Select between one and 1000 saved structures")
    for value in values:
        if not isinstance(value.id, str) or not value.id or len(value.id) > 128 or not value.id.isprintable():
            raise NativeROIError("A selected structure identity is invalid")
        if not isinstance(value.name, str) or not 1 <= len(value.name) <= 64 or not value.name.strip() or not value.name.isprintable() or "\\" in value.name:
            raise NativeROIError("Native ROI names must contain 1 to 64 printable characters without backslashes")
        if not isinstance(value.color, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", value.color) is None:
            raise NativeROIError("Native ROI colors must be six-digit hexadecimal RGB values")
    if len({value.id for value in values}) != len(values) or len({value.name for value in values}) != len(values):
        raise NativeROIError("Select structures with distinct identities and names")
    return tuple(sorted(values, key=lambda value: value.id))


def _plane(mask, stack, index):
    # _source_stack proves this integer gather against every source-plane corner
    # using the full RAS affine. No interpolation or display mesh is involved.
    selection = [slice(None)] * 3
    selection[stack.slice_axis] = stack.slice_indices[index]
    plane = mask[tuple(selection)]
    remaining = [axis for axis in range(3) if axis != stack.slice_axis]
    if remaining != [stack.row_axis, stack.column_axis]:
        plane = plane.T
    if stack.row_sign < 0:
        plane = plane[::-1]
    if stack.column_sign < 0:
        plane = plane[:, ::-1]
    return plane


def _code(value, meaning):
    result = Dataset()
    result.CodeValue = value
    # This is the observed receiver namespace, including its nonstandard '_'.
    # Renaming a private scheme would change the compatibility contract.
    result.CodingSchemeDesignator = "99_OSIRIX"
    result.CodeMeaning = meaning
    return result


def _reference(source):
    result = Dataset()
    result.ReferencedSOPClassUID = source.SOPClassUID
    result.ReferencedSOPInstanceUID = source.SOPInstanceUID
    return result


def _report(source, payload, now):
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = BasicTextSRStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    report = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    for keyword in (
        "PatientName",
        "PatientID",
        "PatientBirthDate",
        "PatientSex",
        "StudyInstanceUID",
        "StudyDate",
        "StudyTime",
        "StudyID",
        "AccessionNumber",
        "ReferringPhysicianName",
        "StudyDescription",
        "FrameOfReferenceUID",
    ):
        if keyword in source:
            report.add(deepcopy(source.data_element(keyword)))
    report.SpecificCharacterSet = "ISO_IR 192"
    report.SOPClassUID = BasicTextSRStorage
    report.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    # Fresh series identities avoid inheriting the reference's save history;
    # OsiriX recognizes/merges these reports through its ROI series markers.
    report.SeriesInstanceUID = generate_uid()
    report.SeriesNumber = 5002
    report.InstanceNumber = 0
    report.Modality = "SR"
    report.SeriesDescription = "OsiriX ROI SR"
    report.Manufacturer = "3DScan"
    report.ManufacturerModelName = "Native ROI compatibility writer"
    report.SoftwareVersions = [__version__, "OsiriX ROI profile 1"]
    report.InstanceCreationDate = report.ContentDate = now.strftime("%Y%m%d")
    report.InstanceCreationTime = report.ContentTime = now.strftime("%H%M%S.%f")
    report.TimezoneOffsetFromUTC = "+0000"
    report.CompletionFlag = "PARTIAL"
    report.VerificationFlag = "UNVERIFIED"
    report.ValueType = "CONTAINER"
    report.ContinuityOfContent = "SEPARATE"
    report.ConceptNameCodeSequence = [_code("1", "ROI Annotations")]
    image = Dataset()
    image.RelationshipType = "CONTAINS"
    image.ValueType = "IMAGE"
    image.ConceptNameCodeSequence = [_code("IHE.10", "Image Reference")]
    image.ReferencedSOPSequence = [_reference(source)]
    report.ContentSequence = [image]
    series = Dataset()
    series.SeriesInstanceUID = source.SeriesInstanceUID
    series.ReferencedSOPSequence = [_reference(source)]
    study = Dataset()
    study.StudyInstanceUID = source.StudyInstanceUID
    study.ReferencedSeriesSequence = [series]
    report.CurrentRequestedProcedureEvidenceSequence = [study]
    report.EncapsulatedDocument = payload
    return report


def export_native_rois(source_paths, reference_path, segments, output_dir, *, revision_id, cancelled=None, progress=None):
    """Publish a new immutable experimental native ROI package atomically.

    Uses only original classic CT or MR headers, the saved reference geometry and
    separate binary masks. It performs no network operation or runtime change.
    A caller must retain these exact artifacts for any same-SOP resend.
    """
    try:
        return _export_native_rois(source_paths, reference_path, segments, output_dir, revision_id=revision_id, cancelled=cancelled, progress=progress)
    except (InterruptedError, NativeROIError):
        raise
    except (ImageFileError, ValueError, OSError, TypeError, OverflowError, EOFError, zlib.error):
        raise NativeROIError("Original CT or saved masks cannot be encoded as native ROIs") from None


def _export_native_rois(source_paths, reference_path, segments, output_dir, *, revision_id, cancelled, progress):
    _cancel(cancelled)
    selection = _selection(segments)
    if not isinstance(revision_id, str) or not revision_id or len(revision_id) > 128 or not revision_id.isprintable():
        raise NativeROIError("A saved revision identity is required")
    output = Path(output_dir)
    if output.exists() or output.is_symlink():
        raise NativeROIError("Native ROI export destination already exists")
    source_paths = tuple(Path(path) for path in source_paths)
    if not source_paths or len(source_paths) > 100_000 or len(set(source_paths)) != len(source_paths):
        raise NativeROIError("Select one complete original classic CT or MR series")
    reference_path = Path(reference_path)
    inputs = tuple(dict.fromkeys((*source_paths, reference_path, *(Path(segment.mask_path) for segment in selection))))
    stamps = {path: _stamp(path) for path in inputs}
    try:
        reference, affine = imaging._image(reference_path)
        if len(reference.shape) != 3 or np.prod(reference.shape, dtype=object) > _MAX_NATIVE_VOXELS:
            raise NativeROIError("Saved reference exceeds the native ROI voxel limit")
        stack, _ = _source_stack(source_paths, reference, affine, cancelled)
        shape = int(stack.images[0].Rows), int(stack.images[0].Columns)
        plane_pixels = shape[0] * shape[1]
        if plane_pixels > _MAX_PLANE_PIXELS:
            raise NativeROIError("Source plane exceeds the native ROI pixel limit")
        input_hashes = {path: hash_file(path) for path in inputs}
        reference_sha256 = input_hashes[reference_path]
        output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".osirix-roi-", dir=output.parent) as scratch:
            staging = Path(scratch) / "package"
            staging.mkdir(mode=0o700)
            spool = Path(scratch) / "planes"
            spool.mkdir(mode=0o700)
            by_plane = [[] for _ in stack.images]
            roi_count = spool_bytes = 0
            total_steps = len(selection) + len(stack.images)
            if progress is not None:
                progress(0, total_steps)
            for number, segment in enumerate(selection):
                _cancel(cancelled)
                mask_header, _ = imaging._image(Path(segment.mask_path))
                if np.prod(mask_header.shape, dtype=object) > _MAX_NATIVE_VOXELS:
                    raise NativeROIError("Saved mask exceeds the native ROI voxel limit")
                mask, _, _ = imaging._binary(Path(segment.mask_path), reference, affine)
                if not mask.any():
                    raise NativeROIError("Selected native ROI masks must be nonempty")
                for index in range(len(stack.images)):
                    _cancel(cancelled)
                    plane = _plane(mask, stack, index)
                    count = int(np.count_nonzero(plane))
                    if not count:
                        continue
                    roi_count += 1
                    if roi_count > _MAX_ROIS or (len(by_plane[index]) + 1) * plane_pixels > _MAX_PLANE_ROI_BYTES:
                        raise NativeROIError("Native ROI selection exceeds the brush memory limit")
                    packed = np.packbits(plane, axis=None, bitorder="little")
                    spool_bytes += packed.nbytes
                    if spool_bytes > _MAX_SPOOL_BYTES:
                        raise NativeROIError("Native ROI export exceeds the spool byte limit")
                    path = spool / f"{number:04d}-{index:06d}.bits"
                    path.write_bytes(packed.tobytes())
                    path.chmod(0o600)
                    by_plane[index].append((segment, path, count, str(uuid4())))
                del plane, mask
                if progress is not None:
                    progress(number + 1, total_steps)
            if sum(bool(values) for values in by_plane) > _MAX_INSTANCES:
                raise NativeROIError("Native ROI export exceeds the report instance limit")
            now = datetime.now(timezone.utc)
            seconds = (now - datetime(2001, 1, 1, tzinfo=timezone.utc)).total_seconds()
            # Native OsiriX groups all planes of one structure by this shared
            # timestamp-like identifier, independently of each brush's UUID.
            groups = {segment.id: seconds + (index + 1) * 0.001 for index, segment in enumerate(selection)}
            files, total_bytes = [], 0
            for index, (source, values) in enumerate(zip(stack.images, by_plane, strict=True)):
                _cancel(cancelled)
                if values:
                    brushes, metadata = [], []
                    for segment, path, count, roi_uuid in values:
                        packed = np.frombuffer(path.read_bytes(), dtype=np.uint8)
                        plane = np.unpackbits(packed, count=plane_pixels, bitorder="little").reshape(shape).view(np.bool_)
                        brushes.append(
                            BrushROI(
                                mask_2d=plane,
                                name=segment.name,
                                color=segment.color,
                                study_instance_uid=str(source.StudyInstanceUID),
                                roi_uuid=roi_uuid,
                                pixel_spacing_x=float(source.PixelSpacing[1]),
                                pixel_spacing_y=float(source.PixelSpacing[0]),
                                image_origin=(float(source.ImagePositionPatient[0]), float(source.ImagePositionPatient[1])),
                                group_id=groups[segment.id],
                            )
                        )
                        metadata.append(NativeROI(segment.id, segment.name, count, roi_uuid))
                    payload = encode_roi_archive(brushes, timestamp=now, cancelled=cancelled)
                    report = _report(source, payload, now)
                    filename = f"roi-{len(files) + 1:04d}.dcm"
                    path = staging / filename
                    report.save_as(path, enforce_file_format=True)
                    path.chmod(0o600)
                    size = path.stat().st_size
                    total_bytes += size
                    if total_bytes > _MAX_TOTAL_BYTES:
                        raise NativeROIError("Native ROI export exceeds the encoded byte limit")
                    files.append(
                        NativeROIInstance(
                            output / filename,
                            str(source.SOPInstanceUID),
                            str(report.SOPInstanceUID),
                            str(report.SeriesInstanceUID),
                            hash_file(path),
                            size,
                            tuple(metadata),
                        )
                    )
                    del brushes, payload, report, plane
                if progress is not None:
                    progress(len(selection) + index + 1, total_steps)
            _cancel(cancelled)
            if any(_stamp(path) != original for path, original in stamps.items()):
                raise NativeROIError("An export input changed during native ROI encoding")
            if any(hash_file(path) != digest for path, digest in input_hashes.items()):
                raise NativeROIError("A saved export input changed during native ROI encoding")
            if any(_stamp(path) != original for path, original in stamps.items()):
                raise NativeROIError("An export input changed during native ROI encoding")
            _cancel(cancelled)
            if output.exists() or output.is_symlink():
                raise NativeROIError("Native ROI export destination already exists")
            _publish_new(staging, output)
        first = stack.images[0]
        return NativeExportResult(
            revision_id,
            tuple(item.series_instance_uid for item in files),
            str(first.StudyInstanceUID),
            str(first.FrameOfReferenceUID),
            reference_sha256,
            tuple(files),
        )
    except InterruptedError:
        raise
    except NativeROIError:
        raise
    except (DicomSegError, ValueError, OSError, TypeError, OverflowError):
        raise NativeROIError("Original CT or saved masks cannot be encoded as native ROIs") from None
