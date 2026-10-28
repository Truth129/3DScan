"""Exact, overlap-preserving DICOM SEG export from revision-bound voxel masks.

Selected structures share one binary SEG instance. Masks are encoded one at a
time and packed pixels are streamed through disk, without assembling source CT
pixels or a dense multi-structure labelmap.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from importlib import metadata
from io import BufferedReader
from pathlib import Path
import re
import tempfile

import nibabel as nib
import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError
from pydicom.sr.coding import Code
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, UID, generate_uid

from . import __version__, imaging
from .fileio import hash_file


_VOXEL_TOLERANCE = 1e-3
_GRID_TOLERANCE_VOXELS = 0.05
_GRID_TOLERANCE_MM = 0.05
_MAX_VOXELS = 512 * 1024**2
_MAX_ENCODED_BYTES = 512 * 1024**2
_MAX_ENCODED_FRAMES = 100_000


class DicomSegError(ValueError):
    """A constant, public-safe export explanation without clinical paths or tags."""


@dataclass(frozen=True, slots=True)
class SegmentInput:
    id: str
    name: str
    color: str
    mask_path: Path


@dataclass(frozen=True, slots=True)
class ExportedSegment:
    structure_id: str
    segment_number: int
    voxel_count: int
    mask_sha256: str


@dataclass(frozen=True, slots=True)
class ExportedInstance:
    path: Path
    sop_instance_uid: str
    sha256: str
    size: int
    segments: tuple[ExportedSegment, ...]


@dataclass(frozen=True, slots=True)
class ExportResult:
    revision_id: str
    series_instance_uid: str
    study_instance_uid: str
    frame_of_reference_uid: str
    reference_sha256: str
    files: tuple[ExportedInstance, ...]


@dataclass(frozen=True, slots=True)
class _Stack:
    images: tuple
    slice_axis: int
    row_axis: int
    column_axis: int
    row_sign: int
    column_sign: int
    slice_indices: tuple[int, ...]


def _check_cancelled(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("DICOM SEG export cancelled")


def _stamp(path):
    try:
        value = Path(path).stat()
    except OSError:
        raise DicomSegError("An original export input is unavailable") from None
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _uid(value):
    text = str(value)
    if not UID(text, validation_mode=pydicom.config.IGNORE).is_valid:
        raise DicomSegError("Original CT has a missing or invalid DICOM identity")
    return text


def _axis(vector):
    integer = np.rint(vector).astype(int)
    axes = np.flatnonzero(integer)
    if len(axes) != 1 or abs(integer[axes[0]]) != 1 or not np.allclose(vector, integer, rtol=0, atol=_VOXEL_TOLERANCE):
        raise DicomSegError("Original CT pixels do not match the saved reference grid; masks cannot be resampled for export")
    return int(axes[0]), int(integer[axes[0]])


def _matches_grid(actual, expected, affine):
    # dcm2niix's float32 spacing estimate can accumulate small origin errors
    # across long stacks, especially far from the patient-coordinate origin.
    # Require both subvoxel and physical bounds at every plane corner: accepting
    # metadata rounding must never permit a different integer voxel assignment.
    delta = actual - expected
    return np.max(np.abs(delta)) <= _GRID_TOLERANCE_VOXELS and np.linalg.norm(affine[:3, :3] @ delta) <= _GRID_TOLERANCE_MM


def _source_stack(source_paths, reference, affine, cancelled):
    paths = tuple(Path(path) for path in source_paths)
    if not paths or len(paths) > 100_000 or len(set(paths)) != len(paths):
        raise DicomSegError("Select one complete original classic CT series")
    images, stamps = [], []
    for path in paths:
        _check_cancelled(cancelled)
        stamps.append((path, _stamp(path)))
        try:
            image = pydicom.dcmread(path, stop_before_pixels=True)
        except (OSError, InvalidDicomError):
            raise DicomSegError("An original CT header cannot be read") from None
        if str(image.get("SOPClassUID", "")) != str(CTImageStorage) or image.get("Modality") != "CT" or int(image.get("NumberOfFrames", 1)) != 1:
            raise DicomSegError("DICOM SEG export currently requires original single-frame classic CT images")
        identities = tuple(_uid(image.get(key, "")) for key in ("StudyInstanceUID", "SeriesInstanceUID", "FrameOfReferenceUID", "SOPInstanceUID"))
        if image.file_meta.get("MediaStorageSOPInstanceUID", identities[-1]) != identities[-1]:
            raise DicomSegError("Original CT file and image identities disagree")
        images.append(image)
    for key in ("StudyInstanceUID", "SeriesInstanceUID", "FrameOfReferenceUID", "PatientID", "PatientName", "PatientBirthDate", "PatientSex"):
        if len({str(image.get(key, "")) for image in images}) != 1:
            raise DicomSegError("Original CT images do not belong to one patient, study, series and coordinate frame")
    if len({str(image.SOPInstanceUID) for image in images}) != len(images):
        raise DicomSegError("Original CT contains duplicate source instances")
    try:
        orientations = np.asarray([image.ImageOrientationPatient for image in images], dtype=float)
        positions = np.asarray([image.ImagePositionPatient for image in images], dtype=float)
        spacings = np.asarray([image.PixelSpacing for image in images], dtype=float)
        dimensions = {(int(image.Rows), int(image.Columns)) for image in images}
        thickness = np.asarray([float(image.SliceThickness) for image in images])
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise DicomSegError("Original CT has incomplete or invalid patient geometry") from None
    if (
        orientations.shape != (len(images), 6)
        or positions.shape != (len(images), 3)
        or spacings.shape != (len(images), 2)
        or not all(np.isfinite(array).all() for array in (orientations, positions, spacings, thickness))
        or np.any(spacings <= 0)
        or np.any(thickness <= 0)
        or len(dimensions) != 1
    ):
        raise DicomSegError("Original CT has inconsistent patient geometry")
    rows, columns = dimensions.pop()
    direction = orientations[0].reshape(2, 3)
    if (
        rows < 1
        or columns < 1
        or not np.allclose(orientations, orientations[0], rtol=0, atol=1e-5)
        or not np.allclose(spacings, spacings[0], rtol=0, atol=1e-5)
        or not np.allclose(direction @ direction.T, np.eye(2), rtol=0, atol=1e-4)
    ):
        raise DicomSegError("Original CT has inconsistent slice orientations, dimensions or spacing")
    normal = np.cross(*direction)
    order = np.argsort(positions @ normal)
    images = [images[int(index)] for index in order]
    positions = positions[order]
    if len(images) > 1:
        steps = np.diff(positions, axis=0)
        if np.any(steps @ normal <= 1e-6) or not np.allclose(steps, steps[0], rtol=0, atol=1e-3):
            raise DicomSegError("Original CT has repeated, missing or irregular slice positions")
    inverse = np.linalg.inv(affine)
    to_ras = np.array([-1, -1, 1])
    column_vector = inverse[:3, :3] @ (direction[0] * spacings[0, 1] * to_ras)
    row_vector = inverse[:3, :3] @ (direction[1] * spacings[0, 0] * to_ras)
    column_axis, column_sign = _axis(column_vector)
    row_axis, row_sign = _axis(row_vector)
    if row_axis == column_axis:
        raise DicomSegError("Original CT does not map to independent reference axes")
    slice_axis = ({0, 1, 2} - {row_axis, column_axis}).pop()
    shape = reference.shape
    if shape[row_axis] != rows or shape[column_axis] != columns or shape[slice_axis] != len(images):
        raise DicomSegError("Original CT does not cover the complete saved reference image")
    indices = []
    for image, position in zip(images, positions, strict=True):
        _check_cancelled(cancelled)
        origin = inverse[:3, :3] @ (position * to_ras) + inverse[:3, 3]
        integer = np.rint(origin).astype(int)
        expected = integer.copy()
        expected[row_axis] = rows - 1 if row_sign < 0 else 0
        expected[column_axis] = columns - 1 if column_sign < 0 else 0
        if not _matches_grid(origin, expected, affine) or not 0 <= integer[slice_axis] < shape[slice_axis]:
            raise DicomSegError("Original CT origins do not match the complete saved reference grid within 0.05 voxel and 0.05 mm")
        # A linear map matching all four corners proves every pixel centre in
        # the plane, without retaining a full-volume coordinate/index array.
        for row, column in ((0, 0), (rows - 1, 0), (0, columns - 1), (rows - 1, columns - 1)):
            actual = origin + row * row_vector + column * column_vector
            target = expected.copy()
            target[row_axis] += row * row_sign
            target[column_axis] += column * column_sign
            if not _matches_grid(actual, target, affine):
                raise DicomSegError("Original CT pixels do not match integer reference voxels; masks cannot be resampled for export")
        indices.append(int(integer[slice_axis]))
        # Missing type-2 demographic fields mean unknown, not fabricated values.
        # This only completes the in-memory header required by highdicom.
        for key in ("PatientName", "PatientID", "PatientBirthDate", "PatientSex", "AccessionNumber", "StudyID", "StudyDate", "StudyTime"):
            if key not in image:
                setattr(image, key, "")
    if set(indices) != set(range(shape[slice_axis])):
        raise DicomSegError("Original CT does not cover every saved reference slice exactly once")
    return _Stack(tuple(images), slice_axis, row_axis, column_axis, row_sign, column_sign, tuple(indices)), stamps


def _project(mask, stack):
    first = stack.images[0]
    pixels = np.empty((len(stack.images), int(first.Rows), int(first.Columns)), dtype=np.uint8)
    axes = [axis for axis in range(3) if axis != stack.slice_axis]
    for index, source_index in enumerate(stack.slice_indices):
        selection = [slice(None)] * 3
        selection[stack.slice_axis] = source_index
        plane = mask[tuple(selection)]
        if axes != [stack.row_axis, stack.column_axis]:
            plane = plane.T
        if stack.row_sign < 0:
            plane = plane[::-1]
        if stack.column_sign < 0:
            plane = plane[:, ::-1]
        pixels[index] = plane
    return pixels


def _encode(stack, pixels, segment, *, series_uid, instance_number, source_kind, revision_id):
    try:
        import highdicom as hd
    except ImportError:
        raise DicomSegError("DICOM SEG export requires the qualified highdicom 0.28.2 dependency") from None
    if hd.__version__ != "0.28.2":
        raise DicomSegError("DICOM SEG export requires qualification of this highdicom version")
    automatic = source_kind == "totalsegmentator"
    algorithm_name, algorithm_version = "3DScan correction", __version__
    if automatic:
        algorithm_name = "TotalSegmentator"
        try:
            algorithm_version = metadata.version("TotalSegmentator")
        except metadata.PackageNotFoundError:
            algorithm_version = "unknown"
    algorithm = hd.AlgorithmIdentificationSequence(
        name=algorithm_name,
        version=algorithm_version,
        family=Code("SEGMENTATION", "99SCAN3D", "Image segmentation"),
    )
    color = tuple(int(segment.color[index : index + 2], 16) for index in (1, 3, 5))
    description = hd.seg.SegmentDescription(
        segment_number=1,
        segment_label=segment.name,
        segmented_property_category=Code("REGION", "99SCAN3D", "Image region"),
        segmented_property_type=Code("REGION", "99SCAN3D", "Image region"),
        algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC if automatic else hd.seg.SegmentAlgorithmTypeValues.SEMIAUTOMATIC,
        algorithm_identification=algorithm,
        tracking_uid=generate_uid(entropy_srcs=[str(stack.images[0].SeriesInstanceUID), segment.id]),
        tracking_id=segment.id,
        display_color=hd.color.CIELabColor.from_rgb(*color),
    )
    result = hd.seg.Segmentation(
        source_images=list(stack.images),
        pixel_array=pixels,
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
        segment_descriptions=[description],
        series_instance_uid=series_uid,
        series_number=500,
        sop_instance_uid=generate_uid(),
        instance_number=instance_number,
        manufacturer="3DScan",
        manufacturer_model_name="Revisioned mask export",
        software_versions=__version__,
        device_serial_number="3DScan",
        series_description=f"3DScan segmentations {revision_id[:8]}",
        transfer_syntax_uid=ExplicitVRLittleEndian,
        omit_empty_frames=True,
        content_label="SCAN3D_SEG",
        content_description="Exported voxel masks; clinical review status is unchanged",
    )
    # Single-segment instances let highdicom share the identification macro.
    # Keep segment discovery in the per-frame form of our accepted OsiriX
    # prototype. Move it there without duplicating a functional
    # group in both locations, which DICOM forbids.
    shared = result.SharedFunctionalGroupsSequence[0]
    if "SegmentIdentificationSequence" in shared:
        identification = shared.SegmentIdentificationSequence
        for frame in result.PerFrameFunctionalGroupsSequence:
            frame.SegmentIdentificationSequence = deepcopy(identification)
        del shared.SegmentIdentificationSequence
    # Source demographic encoding need not cover newly supplied segment labels.
    # Re-encode decoded source text and new labels together, without substitution.
    result.SpecificCharacterSet = "ISO_IR 192"
    return result


class _PackedPixels:
    """Append exact bit counts, without padding between segments or frames."""

    def __init__(self, stream):
        self.stream = stream
        self.pending = np.empty(0, dtype=np.uint8)
        self.bit_count = 0

    def append(self, data, bit_count, cancelled):
        self.bit_count += bit_count
        if (self.bit_count + 7) // 8 > _MAX_ENCODED_BYTES:
            raise DicomSegError("Selected masks exceed the bounded DICOM SEG file size; export fewer structures")
        byte_count = (bit_count + 7) // 8
        for offset in range(0, byte_count, 64 * 1024):
            _check_cancelled(cancelled)
            chunk = memoryview(data)[offset : min(offset + 64 * 1024, byte_count)]
            useful_bits = min(len(chunk) * 8, bit_count - offset * 8)
            if not len(self.pending) and useful_bits == len(chunk) * 8:
                self.stream.write(chunk)
                continue
            bits = np.unpackbits(np.frombuffer(chunk, dtype=np.uint8), bitorder="little")[:useful_bits]
            if len(self.pending):
                bits = np.concatenate((self.pending, bits))
            complete = len(bits) // 8 * 8
            self.stream.write(np.packbits(bits[:complete], bitorder="little").tobytes())
            self.pending = bits[complete:].copy()

    def finish(self):
        if len(self.pending):
            self.stream.write(np.packbits(self.pending, bitorder="little").tobytes())
        # Buffered OB writing uses the buffer's exact length in the element
        # header; the value itself must already have DICOM's even-byte padding.
        if self.stream.tell() % 2:
            self.stream.write(b"\x00")
        self.stream.flush()


class _PixelReader(BufferedReader):
    def __init__(self, raw, cancelled):
        super().__init__(raw)
        self.cancelled = cancelled
        self.checked_bytes = 0

    def read(self, size=-1):
        if self.checked_bytes >= 1024**2:
            _check_cancelled(self.cancelled)
            self.checked_bytes = 0
        value = super().read(size)
        self.checked_bytes += len(value)
        return value


def _merge_segment(combined, part, segment_number, used_sources, cancelled):
    frames = part.PerFrameFunctionalGroupsSequence
    if len(combined.PerFrameFunctionalGroupsSequence) + len(frames) > _MAX_ENCODED_FRAMES:
        raise DicomSegError("Selected masks exceed the bounded DICOM SEG frame count; export fewer structures")
    description = part.SegmentSequence[0]
    description.SegmentNumber = segment_number
    combined.SegmentSequence.append(description)
    for index, frame in enumerate(frames):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        frame.SegmentIdentificationSequence[0].ReferencedSegmentNumber = segment_number
        source = str(frame.DerivationImageSequence[0].SourceImageSequence[0].ReferencedSOPInstanceUID)
        used_sources.add(source)
        frame.FrameContentSequence[0].DimensionIndexValues = [segment_number, 1]
        combined.PerFrameFunctionalGroupsSequence.append(frame)


def _combined_header(part):
    # Keep pydicom metadata only: highdicom's instance-specific lookup caches
    # belong to the temporary single-segment image and must not escape it.
    combined = pydicom.Dataset()
    for element in part:
        if element.keyword not in {"PixelData", "SegmentSequence", "PerFrameFunctionalGroupsSequence"}:
            combined.add(deepcopy(element))
    combined.file_meta = deepcopy(part.file_meta)
    combined.preamble = part.preamble
    combined.SegmentSequence = []
    combined.PerFrameFunctionalGroupsSequence = []
    pointers = [int(item.DimensionIndexPointer) for item in combined.DimensionIndexSequence]
    if pointers != [0x0062000B, 0x00200032]:
        raise DicomSegError("DICOM SEG export requires qualification of this highdicom dimension layout")
    return combined


def _finish_dimensions(combined, stack, used_sources, cancelled):
    # Omitted empty planes can give each temporary segment a different local
    # rank. One output instance needs one gap-free rank for each used position.
    used = [str(image.SOPInstanceUID) for image in stack.images if str(image.SOPInstanceUID) in used_sources]
    rank = {uid: index for index, uid in enumerate(used, start=1)}
    for index, frame in enumerate(combined.PerFrameFunctionalGroupsSequence):
        if index % 256 == 0:
            _check_cancelled(cancelled)
        source = str(frame.DerivationImageSequence[0].SourceImageSequence[0].ReferencedSOPInstanceUID)
        frame.FrameContentSequence[0].DimensionIndexValues[1] = rank[source]
    combined.NumberOfFrames = len(combined.PerFrameFunctionalGroupsSequence)
    # Independent binary masks retain overlap. Avoid claiming NO from the first
    # temporary segment without allocating a second full-volume union to prove it.
    combined.SegmentsOverlap = "UNDEFINED" if len(combined.SegmentSequence) > 1 else "NO"


def _export_segmentation(source_paths, reference_path, segments, output_dir, *, revision_id, source_kind, cancelled=None, progress=None) -> ExportResult:
    """Atomically publish a new series; never overwrite inputs or prior exports.

    The caller binds source paths to the selected revision's immutable source
    identity. This adapter additionally proves complete pixel/grid and patient/
    study/frame correspondence. ``progress(completed, total)`` runs between
    structures; cancellation may return true or raise the caller's exception.
    """
    segments = tuple(sorted(segments, key=lambda item: item.id))
    if not 1 <= len(segments) <= 1000 or len({segment.id for segment in segments}) != len(segments):
        raise DicomSegError("Select 1–1000 unique structures for DICOM SEG export")
    for segment in segments:
        if not isinstance(segment.id, str) or not segment.id.strip():
            raise DicomSegError("A selected structure has no stable identity")
        if not isinstance(segment.name, str) or not 1 <= len(segment.name) <= 64 or not segment.name.isprintable() or "\\" in segment.name:
            raise DicomSegError("DICOM segment names must be 1–64 printable characters without backslashes")
        if not isinstance(segment.color, str) or re.fullmatch(r"#[0-9a-fA-F]{6}", segment.color) is None:
            raise DicomSegError("A selected structure has an invalid display color")
    if not isinstance(revision_id, str) or not revision_id or not isinstance(source_kind, str) or not source_kind:
        raise DicomSegError("DICOM SEG export requires a saved revision and its provenance")
    _check_cancelled(cancelled)
    reference_path, output_dir = Path(reference_path), Path(output_dir)
    if output_dir.exists() or output_dir.is_symlink():
        raise DicomSegError("Use a new DICOM SEG export directory; existing exports are immutable")
    reference_stamp = _stamp(reference_path)
    reference_digest = hash_file(reference_path)
    reference, affine = imaging._image(reference_path)
    if int(np.prod(reference.shape, dtype=np.int64)) > _MAX_VOXELS:
        raise DicomSegError("The reference grid exceeds the bounded DICOM SEG export size")
    stack, stamps = _source_stack(source_paths, reference, affine, cancelled)
    stamps.append((reference_path, reference_stamp))
    series_uid = generate_uid()
    output_dir.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    exported_segments = []
    if progress is not None:
        progress(0, len(segments))
    with tempfile.TemporaryDirectory(prefix=".dicom-seg-", dir=output_dir.parent) as temporary:
        staging = Path(temporary)
        spool = staging / ".packed-pixels"
        combined, used_sources = None, set()
        with spool.open("wb") as pixel_stream:
            packed = _PackedPixels(pixel_stream)
            for index, segment in enumerate(segments, start=1):
                _check_cancelled(cancelled)
                mask_path = Path(segment.mask_path)
                stamps.append((mask_path, _stamp(mask_path)))
                mask_digest = hash_file(mask_path)
                mask, _, _ = imaging._binary(mask_path, reference, affine)
                count = int(np.count_nonzero(mask))
                if not count:
                    raise DicomSegError("A selected structure has an empty mask; export only nonempty masks")
                pixels = _project(mask, stack)
                part = _encode(stack, pixels, segment, series_uid=series_uid, instance_number=1, source_kind=source_kind, revision_id=revision_id)
                if combined is None:
                    combined = _combined_header(part)
                _merge_segment(combined, part, index, used_sources, cancelled)
                packed.append(part.PixelData, int(part.NumberOfFrames) * int(part.Rows) * int(part.Columns), cancelled)
                exported_segments.append(ExportedSegment(segment.id, index, count, mask_digest))
                del mask, pixels, part
                if progress is not None:
                    progress(index, len(segments))
            packed.finish()
        _finish_dimensions(combined, stack, used_sources, cancelled)
        _check_cancelled(cancelled)
        name = "segment-0001.dcm"
        destination = staging / name
        with _PixelReader(spool.open("rb", buffering=0), cancelled) as pixel_stream:
            combined.add_new(0x7FE00010, "OB", pixel_stream)
            combined.save_as(destination, enforce_file_format=True)
            del combined.PixelData
        spool.unlink()
        destination.chmod(0o600)
        size = destination.stat().st_size
        if size > _MAX_ENCODED_BYTES:
            raise DicomSegError("Selected masks exceed the bounded DICOM SEG file size; export fewer structures")
        instance = ExportedInstance(output_dir / name, str(combined.SOPInstanceUID), hash_file(destination), size, tuple(exported_segments))
        _check_cancelled(cancelled)
        if any(_stamp(path) != before for path, before in stamps):
            raise DicomSegError("An original CT, reference image or mask changed during DICOM SEG export")
        if output_dir.exists():
            raise DicomSegError("The DICOM SEG export destination is already occupied")
        staging.replace(output_dir)
    return ExportResult(
        revision_id,
        series_uid,
        str(stack.images[0].StudyInstanceUID),
        str(stack.images[0].FrameOfReferenceUID),
        reference_digest,
        (instance,),
    )


def export_segmentation(source_paths, reference_path, segments, output_dir, *, revision_id, source_kind, cancelled=None, progress=None) -> ExportResult:
    """Export exact masks with public-safe validation and atomic publication."""
    try:
        return _export_segmentation(
            source_paths, reference_path, segments, output_dir, revision_id=revision_id, source_kind=source_kind, cancelled=cancelled, progress=progress
        )
    except (DicomSegError, InterruptedError):
        raise
    except (OSError, ValueError, TypeError, AttributeError, OverflowError, KeyError, EOFError, nib.filebasedimages.ImageFileError):
        raise DicomSegError("DICOM SEG could not read or encode these inputs. Check the original CT and saved masks.") from None
