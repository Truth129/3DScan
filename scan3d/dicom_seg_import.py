"""Bounded, source-bound binary DICOM SEG import without mask resampling.

SEG planes may be cropped, reordered or reflected, but every pixel must map to
one native reference voxel. Source references and geometry are checked together;
matching dimensions or names alone never authorizes an import.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from io import BufferedReader
from pathlib import Path
import re
import shutil
import struct
import tempfile

import nibabel as nib
import numpy as np
import pydicom
from pydicom.filereader import read_partial
from pydicom.uid import ExplicitVRLittleEndian, ImplicitVRLittleEndian, SegmentationStorage, UID

from .dicom_seg import DicomSegError, _axis, _matches_grid, _source_stack, _stamp
from .fileio import hash_file

MAX_FILES = 64
MAX_SEGMENTS = 1000
MAX_FRAMES = 100_000
MAX_FILE_BYTES = 512 * 1024**2
MAX_HEADER_BYTES = 64 * 1024**2
MAX_VOXELS = 512 * 1024**2
MAX_FRAME_PIXELS = 16 * 1024**2
_NATIVE_SYNTAXES = {str(ExplicitVRLittleEndian), str(ImplicitVRLittleEndian)}
_LONG_VRS = {b"OB", b"OD", b"OF", b"OL", b"OV", b"OW", b"SQ", b"UC", b"UR", b"UT", b"UN"}


@dataclass(frozen=True, slots=True)
class _Frame:
    index: int
    segment: int
    source_uid: str
    origin: tuple[int, int, int]
    row_axis: int
    column_axis: int
    row_sign: int
    column_sign: int


@dataclass(frozen=True, slots=True)
class _Input:
    path: Path
    header: pydicom.Dataset
    digest: str
    frames: tuple[_Frame, ...]
    pixel_offset: int


def _cancel(cancelled):
    if cancelled is not None and cancelled():
        raise InterruptedError("DICOM SEG import cancelled")


def _error(message):
    return DicomSegError(message)


def _identity(value):
    result = str(value)
    if not UID(result, validation_mode=pydicom.config.IGNORE).is_valid:
        raise _error("DICOM SEG contains a missing or invalid DICOM identity")
    return result


def _integer(value, minimum, maximum, message):
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        raise _error(message) from None
    if re.fullmatch(r"[+-]?[0-9]+", str(value).strip()) is None or not minimum <= number <= maximum:
        raise _error(message)
    return number


class _HeaderReader(BufferedReader):
    def read(self, size=-1):
        if size < 0 or self.tell() + size > MAX_HEADER_BYTES:
            raise _error("DICOM SEG metadata exceeds the bounded header size")
        return super().read(size)


def _transfer_syntax(path):
    # Reject deflated datasets before handing them to pydicom: its whole-dataset
    # decompression would bypass our frame and metadata expansion limits.
    syntax = None
    with path.open("rb") as stream:
        if stream.read(132)[128:] != b"DICM":
            raise _error("Upload a Part 10 DICOM SEG file with file metadata")
        while stream.tell() < MAX_HEADER_BYTES:
            tag = stream.read(4)
            if len(tag) != 4 or struct.unpack("<HH", tag)[0] != 2:
                break
            vr, size = stream.read(2), stream.read(2)
            if len(size) != 2:
                break
            if vr in _LONG_VRS:
                raw = stream.read(4)
                if len(raw) != 4:
                    break
                length = struct.unpack("<I", raw)[0]
            else:
                length = struct.unpack("<H", size)[0]
            if length > MAX_HEADER_BYTES - stream.tell():
                break
            if struct.unpack("<HH", tag) == (2, 0x10):
                if syntax is not None:
                    raise _error("DICOM SEG repeats its file transfer syntax")
                if length > 64:
                    break
                syntax = stream.read(length).rstrip(b"\x00 ").decode("ascii", errors="ignore")
                if syntax not in _NATIVE_SYNTAXES:
                    raise _error("DICOM SEG import supports uncompressed little-endian binary SEG files")
            else:
                stream.seek(length, 1)
    if syntax is not None:
        return syntax
    raise _error("DICOM SEG file metadata is missing a supported transfer syntax")


def _header(path):
    syntax = _transfer_syntax(path)
    pixel = {}
    with _HeaderReader(path.open("rb", buffering=0)) as stream:

        def stop(tag, vr, length):
            if tag in {0x7FE00010, 0x7FE00009, 0x7FE00008}:
                pixel.update(tag=int(tag), length=length, offset=stream.tell())
                return True
            return False

        try:
            header = read_partial(stream, stop_when=stop, force=False)
        except (OSError, EOFError, ValueError, KeyError, TypeError, struct.error):
            raise _error("DICOM SEG metadata cannot be read or exceeds its bounds") from None
    if str(header.file_meta.get("TransferSyntaxUID", "")) != syntax:
        raise _error("DICOM SEG transfer syntax changed while reading")
    if pixel.get("tag") != 0x7FE00010:
        raise _error("DICOM SEG has no native binary Pixel Data")
    return header, pixel


def _single(value, keyword, *, optional=False):
    sequence = value.get(keyword)
    if sequence is None:
        if optional:
            return None
        raise _error("DICOM SEG is missing required functional-group metadata")
    if len(sequence) != 1:
        raise _error("DICOM SEG has ambiguous functional-group metadata")
    return sequence[0]


def _macro(shared, frame, keyword, *, optional=False):
    if keyword in shared and keyword in frame:
        raise _error("DICOM SEG repeats a functional group in shared and per-frame metadata")
    return _single(frame if keyword in frame else shared, keyword, optional=optional)


def _vector(value, length):
    try:
        result = np.asarray(value, dtype=float)
    except (TypeError, ValueError, OverflowError):
        raise _error("DICOM SEG has invalid patient geometry") from None
    if result.shape != (length,) or not np.isfinite(result).all():
        raise _error("DICOM SEG has invalid patient geometry")
    return result


def _references(header, images):
    first = images[0]
    if _identity(header.get("StudyInstanceUID", "")) != str(first.StudyInstanceUID) or _identity(header.get("FrameOfReferenceUID", "")) != str(
        first.FrameOfReferenceUID
    ):
        raise _error("DICOM SEG does not belong to this CT study and coordinate frame")
    if str(header.get("PatientID", "")) != str(first.get("PatientID", "")):
        raise _error("DICOM SEG patient identity does not match the original CT")
    series = _single(header, "ReferencedSeriesSequence")
    if _identity(series.get("SeriesInstanceUID", "")) != str(first.SeriesInstanceUID):
        raise _error("DICOM SEG references a different source CT series")
    known = {str(image.SOPInstanceUID) for image in images}
    entries = series.get("ReferencedInstanceSequence", [])
    if not entries or len(entries) > len(known):
        raise _error("DICOM SEG needs explicit source CT instance references")
    selected = set()
    for entry in entries:
        uid = _identity(entry.get("ReferencedSOPInstanceUID", ""))
        if str(entry.get("ReferencedSOPClassUID", "")) != str(first.SOPClassUID) or uid not in known or uid in selected:
            raise _error("DICOM SEG contains foreign, repeated or unsupported source image references")
        selected.add(uid)
    if header.get("StudiesContainingOtherReferencedInstancesSequence"):
        raise _error("DICOM SEG references images outside the selected source study")
    return selected


def _native_axis(vector):
    try:
        return _axis(vector)
    except DicomSegError:
        raise _error("DICOM SEG pixels do not match unit native CT voxel steps; resampling is unsupported") from None


def _plan(path, stack, shape, affine, cancelled=None):
    header, pixel = _header(path)
    if str(header.get("SOPClassUID", "")) != str(SegmentationStorage) or str(header.get("Modality", "")) != "SEG":
        raise _error("Upload DICOM Segmentation Storage files")
    sop = _identity(header.get("SOPInstanceUID", ""))
    _identity(header.get("SeriesInstanceUID", ""))
    if (
        str(header.file_meta.get("MediaStorageSOPClassUID", "")) != str(SegmentationStorage)
        or str(header.file_meta.get("MediaStorageSOPInstanceUID", "")) != sop
    ):
        raise _error("DICOM SEG file and dataset identities disagree")
    if str(header.get("SegmentationType", "")) != "BINARY":
        raise _error("Only BINARY DICOM SEG is supported; fractional and labelmap segmentations are not thresholded or converted")
    if "ConcatenationUID" in header:
        raise _error("Concatenated DICOM SEG instances need reassembly before import")
    for keyword, expected in (("SamplesPerPixel", 1), ("BitsAllocated", 1), ("BitsStored", 1), ("HighBit", 0), ("PixelRepresentation", 0)):
        if header.get(keyword) != expected:
            raise _error("DICOM SEG has an unsupported binary pixel encoding")
    if header.get("PhotometricInterpretation") != "MONOCHROME2" or str(header.get("LossyImageCompression", "00")) != "00":
        raise _error("DICOM SEG has an unsupported or lossy pixel encoding")
    rows = _integer(header.get("Rows"), 1, 65535, "DICOM SEG has invalid frame dimensions")
    columns = _integer(header.get("Columns"), 1, 65535, "DICOM SEG has invalid frame dimensions")
    count = _integer(header.get("NumberOfFrames"), 1, MAX_FRAMES, "DICOM SEG exceeds the bounded frame count")
    bit_count = count * rows * columns
    expected_bytes = ((bit_count + 7) // 8 + 1) // 2 * 2
    if rows * columns > MAX_FRAME_PIXELS or expected_bytes > MAX_FILE_BYTES:
        raise _error("DICOM SEG exceeds the bounded decoded frame or pixel size")
    if pixel["length"] != expected_bytes or pixel["offset"] + expected_bytes > path.stat().st_size:
        raise _error("DICOM SEG Pixel Data length does not match its declared frames")
    descriptions = header.get("SegmentSequence", [])
    if not 1 <= len(descriptions) <= MAX_SEGMENTS:
        raise _error("DICOM SEG exceeds the supported segment count")
    segments = set()
    for description in descriptions:
        number = _integer(description.get("SegmentNumber"), 1, 65535, "DICOM SEG has an invalid segment number")
        label = str(description.get("SegmentLabel", ""))
        if number in segments or not label.strip() or len(label) > 256 or not label.isprintable():
            raise _error("DICOM SEG contains duplicate segment numbers or invalid labels")
        segments.add(number)
    selected = _references(header, stack.images)
    shared = _single(header, "SharedFunctionalGroupsSequence")
    groups = header.get("PerFrameFunctionalGroupsSequence", [])
    if len(groups) != count:
        raise _error("DICOM SEG frame metadata does not match its declared frame count")
    native_sources = {index: str(image.SOPInstanceUID) for image, index in zip(stack.images, stack.slice_indices, strict=True)}
    inverse = np.linalg.inv(affine)
    to_ras = np.array([-1, -1, 1])
    frames, used = [], set()
    for index, group in enumerate(groups):
        if index % 256 == 0:
            _cancel(cancelled)
        number = _integer(_macro(shared, group, "SegmentIdentificationSequence").get("ReferencedSegmentNumber"), 1, 65535, "Invalid frame segment number")
        if number not in segments:
            raise _error("DICOM SEG frame references an unknown segment")
        measures = _macro(shared, group, "PixelMeasuresSequence")
        spacing = _vector(measures.get("PixelSpacing"), 2)
        directions = _vector(_macro(shared, group, "PlaneOrientationSequence").get("ImageOrientationPatient"), 6).reshape(2, 3)
        position = _vector(_macro(shared, group, "PlanePositionSequence").get("ImagePositionPatient"), 3)
        if np.any(spacing <= 0) or not np.allclose(directions @ directions.T, np.eye(2), rtol=0, atol=1e-4):
            raise _error("DICOM SEG has invalid frame orientations or pixel spacing")
        column_vector = inverse[:3, :3] @ (directions[0] * spacing[1] * to_ras)
        row_vector = inverse[:3, :3] @ (directions[1] * spacing[0] * to_ras)
        column_axis, column_sign = _native_axis(column_vector)
        row_axis, row_sign = _native_axis(row_vector)
        if {row_axis, column_axis} != {stack.row_axis, stack.column_axis}:
            raise _error("DICOM SEG pixels do not occupy a native source CT plane")
        origin = inverse[:3, :3] @ (position * to_ras) + inverse[:3, 3]
        integer = np.rint(origin).astype(int)
        for row, column in ((0, 0), (rows - 1, 0), (0, columns - 1), (rows - 1, columns - 1)):
            actual = origin + row * row_vector + column * column_vector
            target = integer.copy()
            target[row_axis] += row * row_sign
            target[column_axis] += column * column_sign
            if not _matches_grid(actual, target, affine) or np.any(target < 0) or np.any(target >= shape):
                raise _error("DICOM SEG pixels do not match native CT voxels within 0.05 voxel and 0.05 mm; resampling is unsupported")
        source_uid = native_sources.get(int(integer[stack.slice_axis]))
        if source_uid not in selected:
            raise _error("DICOM SEG plane has no matching referenced source CT image")
        derivation = _macro(shared, group, "DerivationImageSequence", optional=True)
        if derivation is not None:
            source = _single(derivation, "SourceImageSequence")
            if (
                str(source.get("ReferencedSOPClassUID", "")) != str(stack.images[0].SOPClassUID)
                or str(source.get("ReferencedSOPInstanceUID", "")) != source_uid
                or "ReferencedFrameNumber" in source
            ):
                raise _error("DICOM SEG frame source reference disagrees with its physical CT plane")
        identity = number, source_uid
        if identity in used:
            raise _error("DICOM SEG repeats a segment on the same source CT plane")
        used.add(identity)
        frames.append(_Frame(index, number, source_uid, tuple(int(v) for v in integer), row_axis, column_axis, row_sign, column_sign))
    return _Input(path, header, hash_file(path), tuple(frames), pixel["offset"])


def _pixel_frames(item, frames):
    # Binary frames are contiguous bits, without byte padding between frames.
    # pydicom 3.0's indexed iterator truncates some non-byte-aligned frames;
    # reading the validated native payload directly also avoids full decoding.
    rows, columns = int(item.header.Rows), int(item.header.Columns)
    frame_bits = rows * columns
    with item.path.open("rb") as stream:
        for frame in frames:
            start = frame.index * frame_bits
            offset, skip = divmod(start, 8)
            size = (skip + frame_bits + 7) // 8
            stream.seek(item.pixel_offset + offset)
            packed = stream.read(size)
            if len(packed) != size:
                raise _error("DICOM SEG Pixel Data ended before its declared frames")
            bits = np.unpackbits(np.frombuffer(packed, dtype=np.uint8), bitorder="little")
            yield bits[skip : skip + frame_bits].reshape(rows, columns)


def _color(description, number):
    value = description.get("RecommendedDisplayCIELabValue")
    if value is None:
        return ("#e57373", "#64b5f6", "#81c784", "#ffb74d", "#ba68c8", "#4dd0e1")[(number - 1) % 6]
    try:
        from highdicom.color import CIELabColor

        rgb = CIELabColor.from_dicom_value([int(v) for v in value]).to_rgb()
        return "#" + "".join(f"{int(v):02x}" for v in rgb)
    except (TypeError, ValueError, OverflowError):
        raise _error("DICOM SEG has an invalid recommended segment color") from None


def _code(description, keyword):
    code = _single(description, keyword, optional=True)
    if code is None:
        return None
    return {key: str(code.get(key, "")) for key in ("CodeValue", "CodingSchemeDesignator", "CodingSchemeVersion", "CodeMeaning")}


def import_segmentation(source_paths, reference_path, seg_paths, output_dir, *, cancelled=None, progress=None):
    """Publish independent binary masks atomically for one verified classic CT or MR series.

    Only native little-endian BINARY SEG is accepted. Geometry-only per-frame
    binding is allowed when the study/frame/series/SOP references and native
    plane mapping prove the source unambiguously. ``progress(done, total)`` is
    called between segments; output_dir must not already exist.
    """
    paths = tuple(Path(path) for path in seg_paths)
    source_paths = tuple(Path(path) for path in source_paths)
    reference_path, output_dir = Path(reference_path), Path(output_dir)
    if not 1 <= len(source_paths) <= 100_000 or len(set(source_paths)) != len(source_paths):
        raise _error("Select one complete original classic CT or MR series")
    if not 1 <= len(paths) <= MAX_FILES or len(set(paths)) != len(paths):
        raise _error("Select 1–64 distinct DICOM SEG files")
    if output_dir.exists() or output_dir.is_symlink():
        raise _error("Use a new DICOM SEG import directory")
    _cancel(cancelled)
    try:
        for path in (*paths, reference_path, *source_paths):
            if path.is_symlink() or not path.is_file():
                raise _error("A DICOM SEG import input is unavailable or is a symbolic link")
        if any(path.stat().st_size > MAX_FILE_BYTES for path in paths) or sum(path.stat().st_size for path in paths) > MAX_FILE_BYTES:
            raise _error("DICOM SEG files exceed the 512 MiB import limit")
        stamps = {path: _stamp(path) for path in (*paths, reference_path, *source_paths)}
        digests = {path: hash_file(path) for path in (*paths, reference_path, *source_paths)}
    except OSError:
        raise _error("A DICOM SEG import input is unavailable or changed while being fingerprinted") from None
    try:
        reference = nib.load(reference_path)
        shape, affine = np.asarray(reference.shape), reference.affine
        if len(shape) != 3 or any(int(size) < 1 for size in shape) or int(np.prod(shape, dtype=object)) > MAX_VOXELS:
            raise _error("Reference CT exceeds the bounded import grid")
        if not np.isfinite(affine).all() or abs(np.linalg.det(affine[:3, :3])) < 1e-9:
            raise _error("Reference CT has invalid patient geometry")
        try:
            stack, _ = _source_stack(source_paths, reference, affine, cancelled)
        except DicomSegError:
            raise _error("The original classic series does not match the selected native reference grid") from None
        inputs = []
        for path in paths:
            _cancel(cancelled)
            inputs.append(_plan(path, stack, shape, affine, cancelled))
        if len({str(item.header.SOPInstanceUID) for item in inputs}) != len(inputs):
            raise _error("DICOM SEG input contains repeated SOP instance identities")
        total = sum(len(item.header.SegmentSequence) for item in inputs)
        if total > MAX_SEGMENTS or sum(len(item.frames) for item in inputs) > MAX_FRAMES:
            raise _error("DICOM SEG bundle exceeds the supported segment or frame count")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        staged = Path(tempfile.mkdtemp(prefix=".dicom-seg-import-", dir=output_dir.parent))
        structures = []
        try:
            if progress:
                progress(0, total)
            for item in inputs:
                header = item.header
                namespace = hashlib.sha256(str(header.SOPInstanceUID).encode()).hexdigest()[:32]
                for description in header.SegmentSequence:
                    _cancel(cancelled)
                    number = int(description.SegmentNumber)
                    frames = [frame for frame in item.frames if frame.segment == number]
                    mask = np.zeros(tuple(shape), dtype=np.uint8)
                    pixels = _pixel_frames(item, frames)
                    try:
                        for frame, plane in zip(frames, pixels, strict=True):
                            _cancel(cancelled)
                            if plane.shape != (int(header.Rows), int(header.Columns)) or plane.dtype.kind not in "uib" or np.any(plane > 1):
                                raise _error("DICOM SEG decoded pixels do not match its binary frame declarations")
                            selection = list(frame.origin)
                            for axis, sign, length in (
                                (frame.row_axis, frame.row_sign, int(header.Rows)),
                                (frame.column_axis, frame.column_sign, int(header.Columns)),
                            ):
                                start = frame.origin[axis]
                                stop = start + sign * length
                                selection[axis] = slice(start, None if stop < 0 else stop, sign)
                            remaining = [axis for axis in range(3) if isinstance(selection[axis], slice)]
                            mask[tuple(selection)] = plane if remaining == [frame.row_axis, frame.column_axis] else plane.T
                    finally:
                        pixels.close()
                    filename = f"segment-{namespace}-{number}.nii.gz"
                    target = staged / filename
                    image = nib.Nifti1Image(mask, affine, reference.header.copy())
                    image.set_data_dtype(np.uint8)
                    image.set_sform(affine, code=2)
                    image.set_qform(None, code=0)
                    nib.save(image, target)
                    target.chmod(0o600)
                    structures.append(
                        {
                            "id": f"dicom_seg:{namespace}:{number}",
                            "task": "dicom_seg",
                            "group": "Imported segmentations",
                            "name": str(description.SegmentLabel),
                            "color": _color(description, number),
                            "mask_file": str(output_dir / filename),
                            "dicom_seg": {
                                "sop_instance_uid": str(header.SOPInstanceUID),
                                "series_instance_uid": str(header.SeriesInstanceUID),
                                "segment_number": number,
                                "seg_sha256": item.digest,
                                "mask_sha256": hash_file(target),
                                "algorithm_type": str(description.get("SegmentAlgorithmType", "")),
                                "algorithm_name": str(description.get("SegmentAlgorithmName", "")),
                                "property_category": _code(description, "SegmentedPropertyCategoryCodeSequence"),
                                "property_type": _code(description, "SegmentedPropertyTypeCodeSequence"),
                            },
                        }
                    )
                    del mask, image
                    if progress:
                        progress(len(structures), total)
            for path, stamp in stamps.items():
                _cancel(cancelled)
                if _stamp(path) != stamp or hash_file(path) != digests[path]:
                    raise _error("An original CT, reference image or SEG file changed during import")
            if output_dir.exists():
                raise _error("The DICOM SEG import destination is already occupied")
            staged.rename(output_dir)
        finally:
            if staged.exists():
                shutil.rmtree(staged)
        return structures
    except (DicomSegError, InterruptedError):
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, EOFError, struct.error, nib.filebasedimages.ImageFileError):
        raise _error("DICOM SEG cannot be decoded with the supported binary native-grid profile") from None
