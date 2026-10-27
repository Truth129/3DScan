"""Conservative static CT/MR ingestion and isolated dcm2niix conversion."""

from __future__ import annotations

from collections import defaultdict
from importlib.metadata import version as package_version
from pathlib import Path
import json
import os
import re
import shutil
import subprocess
import tempfile

import nibabel as nib
import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError


_TAGS = [
    "SeriesInstanceUID",
    "SOPInstanceUID",
    "SOPClassUID",
    "SeriesDescription",
    "Modality",
    "ImagePositionPatient",
    "ImageOrientationPatient",
    "PixelSpacing",
    "SliceThickness",
    "Rows",
    "Columns",
    "NumberOfFrames",
    "FrameOfReferenceUID",
    "ImageType",
    "SpacingBetweenSlices",
    "StudyInstanceUID",
    "AcquisitionNumber",
    "TemporalPositionIdentifier",
    "NumberOfTemporalPositions",
    "ConvolutionKernel",
    "ReconstructionAlgorithm",
    "RescaleSlope",
    "RescaleIntercept",
    "RescaleType",
    "BitsAllocated",
    "BitsStored",
    "HighBit",
    "PixelRepresentation",
    "SamplesPerPixel",
    "PhotometricInterpretation",
    "KVP",
    "WindowCenter",
    "WindowWidth",
    "VOILUTFunction",
    "SequenceName",
    "ScanningSequence",
    "SequenceVariant",
    "ScanOptions",
    "MRAcquisitionType",
    "EchoTime",
    "EchoNumbers",
    "RepetitionTime",
    "InversionTime",
    "TriggerTime",
    "CardiacNumberOfImages",
    "ComplexImageComponent",
    "AcquisitionContrast",
    "DiffusionBValue",
    "DiffusionDirectionality",
    "DiffusionGradientOrientation",
    "MRDiffusionSequence",
    "SharedFunctionalGroupsSequence",
    "PerFrameFunctionalGroupsSequence",
    "DimensionIndexSequence",
    "DimensionOrganizationType",
    "ConcatenationUID",
    "ConcatenationFrameOffsetNumber",
    "InConcatenationNumber",
    "InConcatenationTotalNumber",
    "SOPInstanceUIDOfConcatenationSource",
    "PixelPresentation",
    "VolumetricProperties",
    "VolumeBasedCalculationTechnique",
]


def _conversion_log(output_dir: Path, stdout, stderr, outcome: str, *, append=False) -> str:
    """Keep converter details locally, without placing patient data in job errors."""

    def decoded(value):
        return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value or ""

    name = "dcm2niix.log"
    flags = os.O_WRONLY | os.O_CREAT | (os.O_APPEND if append else os.O_TRUNC) | os.O_NOFOLLOW
    descriptor = os.open(output_dir / name, flags, 0o600)
    with os.fdopen(descriptor, "a" if append else "w", encoding="utf-8") as log:
        os.fchmod(log.fileno(), 0o600)
        log.write(f"{outcome}\n\nstdout:\n{decoded(stdout)}\n\nstderr:\n{decoded(stderr)}\n")
    return f"Technical details are saved locally in {name}."


def _converter_output(completed) -> bytes:
    return b"\n".join(value if isinstance(value, bytes) else (value or "").encode() for value in (completed.stdout, completed.stderr)).lower()


def _compressed_inputs(items) -> bool:
    return any(ds.file_meta.get("TransferSyntaxUID", pydicom.uid.ExplicitVRLittleEndian).is_compressed for _, ds in items)


def _decoder_failure(completed, items) -> bool:
    if not completed.returncode or not _compressed_inputs(items):
        return False
    return any(
        marker in _converter_output(completed)
        for marker in (
            b"unsupported transfer syntax",
            b"not compiled to decompress",
            b"not compiled with openjpeg",
            b"without openjpeg",
            b"jpeg2000 support not enabled",
            b"jpeg 2000 support not enabled",
            b"openjpeg failed to read the header",
            b"failed to decode frame",
        )
    )


def _normalize_compressed(items, destination: Path) -> dict:
    """Decode into NEW files: selected inputs may be hard links to originals."""
    destination.mkdir()
    count = 0
    for index, (path, _) in enumerate(items):
        normalized = destination / f"{index:06d}.dcm"
        ds = pydicom.dcmread(str(path))
        if ds.file_meta.TransferSyntaxUID.is_compressed:
            original_uid = str(ds.SOPInstanceUID)
            ds.decompress(decoding_plugin="gdcm", generate_instance_uid=False, as_rgb=False)
            if str(ds.SOPInstanceUID) != original_uid:
                raise ValueError("Independent decompression changed the source instance identifier")
            ds.save_as(str(normalized), enforce_file_format=True)
            count += 1
        else:
            shutil.copyfile(path, normalized)
    return {
        "name": "pydicom-gdcm",
        "pydicom_version": pydicom.__version__,
        "gdcm_version": package_version("python-gdcm"),
        "decoding_plugin": "gdcm",
        "normalized_transfer_syntax": str(pydicom.uid.ExplicitVRLittleEndian),
        "decoded_slice_count": count,
        "resampled": False,
        "recompressed": False,
        "source_instances_preserved": True,
    }


def _converter_options(modality: str) -> list[str]:
    if modality == "MR":
        # MRI dimensions must never be force-merged. Use standard DICOM display
        # rescale (rather than Philips private precise scaling), so declared VOI
        # values and the converted scalar samples remain in the same units.
        return ["-z", "y", "-b", "n", "-m", "n", "-p", "n", "-l", "o", "-x", "i", "-f", "reference"]
    return ["-z", "y", "-b", "n", "-m", "y", "-f", "reference"]


def _run_converter(executable, source, target, output_dir, *, append=False, modality="CT"):
    try:
        completed = subprocess.run([executable, *_converter_options(modality), "-o", str(target), str(source)], capture_output=True, timeout=600)
    except subprocess.TimeoutExpired as exc:
        details = _conversion_log(output_dir, exc.stdout, exc.stderr, "Timed out after 600 seconds", append=append)
        raise RuntimeError(f"DICOM conversion timed out after 10 minutes. {details}") from exc
    details = _conversion_log(
        output_dir,
        completed.stdout,
        completed.stderr,
        f"{'Normalized retry' if append else 'Primary conversion'} dcm2niix exit code: {completed.returncode}",
        append=append,
    )
    return completed, details


def _save_conversion_manifest(output_dir, manifest):
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    descriptor = os.open(output_dir / "conversion.json", flags, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(manifest, stream, indent=2, allow_nan=False)
        stream.write("\n")


def _source_overlay_indices(items, required: set[int]) -> set[int]:
    """Read only standard overlay groups, without loading image Pixel Data."""
    tags = [pydicom.tag.Tag(group, 0x3000) for group in range(0x6000, 0x6020, 2)]
    found = set()
    for path, _ in items:
        # Do not stop at Pixel Data: valid files can store overlay elements
        # afterward. specific_tags skips the image samples themselves.
        header = pydicom.dcmread(str(path), specific_tags=tags)
        for index, tag in enumerate(tags, 1):
            element = header.get(tag)
            if element is not None and isinstance(element.value, bytes) and element.value:
                found.add(index)
        if required <= found:
            break
    return found


def _is_native_overlay(path: Path, reference) -> bool:
    """Recognize the pinned converter's identity-scaled binary ROI raster.

    Validation reads the compressed raster sequentially in bounded blocks; a
    large source annotation must not allocate another CT-sized mask array.
    """
    try:
        overlay = nib.load(str(path))
        if (
            len(overlay.shape) != 3
            or overlay.shape != reference.shape
            or overlay.get_data_dtype() != np.dtype("uint8")
            or not np.allclose(overlay.affine, reference.affine, rtol=0, atol=1e-4)
            or overlay.dataobj.slope != 1
            or overlay.dataobj.inter != 0
        ):
            return False
        remaining = int(np.prod(overlay.shape))
        with nib.openers.ImageOpener(str(path), "rb") as stream:
            stream.seek(overlay.dataobj.offset)
            while remaining:
                count = min(remaining, 4 * 1024 * 1024)
                block = stream.read(count)
                if len(block) != count or np.any(np.frombuffer(block, dtype=np.uint8) > 1):
                    return False
                remaining -= count
        return True
    except (OSError, ValueError, TypeError, EOFError, nib.filebasedimages.ImageFileError):
        return False


def _conversion_image_outputs(target: Path, items) -> tuple[list[Path], list[dict]]:
    """Separate proven DICOM overlay derivatives without guessing among CTs.

    Pinned dcm2niix creates `_ROI1` through `_ROI16` for Overlay Data groups
    6000 through 601e. Only the exact requested reference plus these derivatives
    may be reduced to one image; arbitrary extra volumes remain an error.
    """
    volumes = sorted(target.glob("*.nii.gz"))
    roi_pattern = re.compile(r"reference_ROI([1-9]|1[0-6])\.nii\.gz")
    annotations = [(path, roi_pattern.fullmatch(path.name)) for path in volumes]
    if any(path.name.startswith("reference_ROI") and match is None for path, match in annotations):
        return [], []
    if not any(match for _, match in annotations):
        return volumes, []
    reference = target / "reference.nii.gz"
    if reference not in volumes or any(path != reference and match is None for path, match in annotations):
        return [], []  # Missing primary or genuine/unknown additional images.
    try:
        required = {int(match.group(1)) for _, match in annotations if match is not None}
        source_indices = _source_overlay_indices(items, required)
        image = nib.load(str(reference))
    except (OSError, ValueError, TypeError, EOFError, nib.filebasedimages.ImageFileError):
        return [], []
    ignored = []
    for path, match in annotations:
        if path == reference:
            continue
        index = int(match.group(1))
        if index not in source_indices or not _is_native_overlay(path, image):
            return [], []
        ignored.append({"file": path.name, "kind": "dicom_overlay", "overlay_group": f"{0x6000 + 2 * (index - 1):04x}", "binary_grid_verified": True})
    return [reference], ignored


def _conversion_failure(completed, items, details: str) -> str:
    # Converter messages may contain patient names, paths and DICOM metadata.
    # Only expose fixed diagnostic text; keep their original content in the log.
    output = _converter_output(completed)
    unsupported = any(
        marker in output
        for marker in (
            b"unsupported transfer syntax",
            b"not compiled to decompress",
            b"not compiled with openjpeg",
            b"without openjpeg",
            b"jpeg2000 support not enabled",
            b"jpeg 2000 support not enabled",
        )
    )
    if unsupported:
        syntaxes = {str(ds.file_meta.get("TransferSyntaxUID", "")) for _, ds in items}
        if syntaxes & {"1.2.840.10008.1.2.4.90", "1.2.840.10008.1.2.4.91"}:
            return (
                "The installed DICOM converter cannot decode this scan's JPEG 2000 compression. "
                "Use a dcm2niix build with OpenJPEG support, then retry processing. " + details
            )
        return (
            "The installed DICOM converter does not support this scan's compression/transfer syntax. "
            "Use a dcm2niix build with the required decoder, then retry processing. " + details
        )
    if completed.returncode == 2:
        modality = str(items[0][1].get("Modality", "CT"))
        return (
            f"dcm2niix found no readable {modality} images in the selected series. "
            "Check that the export includes image pixel data and a supported compression format. " + details
        )
    return f"dcm2niix failed (exit {completed.returncode}). {details}"


def _read(path):
    return pydicom.dcmread(str(path), stop_before_pixels=True, specific_tags=_TAGS)


def _validate(items):
    from .modalities import get_modality_adapter

    modalities = {str(ds.get("Modality", "")) for _, ds in items}
    if len(modalities) == 1:
        modality = next(iter(modalities))
        try:
            adapter = get_modality_adapter(modality)
        except ValueError:
            return (
                items,
                None,
                [f"Only conventional CT and static MR image series are supported; no import adapter is enabled for {modality or 'unknown modality'}"],
            )
        return adapter.validate_series(items)
    return _validate_conventional_ct(items)


def _validate_conventional_ct(items):
    errors = []
    if any(str(ds.get("Modality", "")) != "CT" for _, ds in items):
        errors.append("Only CT image series are supported")
    if any(str(ds.get("SOPClassUID", "")) != "1.2.840.10008.5.1.4.1.1.2" for _, ds in items):
        errors.append("Only conventional single-frame CT image storage is supported")
    if any(int(ds.get("NumberOfFrames", 1)) != 1 for _, ds in items):
        errors.append("Enhanced or multi-frame CT requires a supported single-frame export")
    if len(items) < 2:
        errors.append("At least two CT slices are required")
    if any(set(map(str, ds.get("ImageType", []))) & {"LOCALIZER", "SCOUT"} for _, ds in items):
        errors.append("Localizer/scout series cannot be reconstructed as a CT volume")
    # Tube-current/exposure modulation is expected along a CT volume and must
    # not split it. Before allowing dcm2niix to merge, reject clinically distinct
    # acquisition/reconstruction/temporal data and incompatible intensity encoding.
    consistent = (
        "StudyInstanceUID",
        "AcquisitionNumber",
        "TemporalPositionIdentifier",
        "NumberOfTemporalPositions",
        "ConvolutionKernel",
        "ReconstructionAlgorithm",
        "ImageType",
        "RescaleSlope",
        "RescaleIntercept",
        "RescaleType",
        "BitsAllocated",
        "BitsStored",
        "PixelRepresentation",
        "PhotometricInterpretation",
        "KVP",
    )
    for attribute in consistent:
        values = {str(ds.get(attribute, "")).strip() for _, ds in items}
        if len(values) > 1:
            errors.append(f"Inconsistent {attribute}; select one acquisition, temporal phase and reconstruction")
    return _validate_spatial_stack(items, errors)


def _tokens(value) -> set[str]:
    if isinstance(value, str):
        return {part.strip().upper() for part in value.split("\\") if part.strip()}
    try:
        return {str(part).strip().upper() for part in value if str(part).strip()}
    except TypeError:
        return set()


def _unknown_optional_value(value) -> bool:
    """DICOM Type 2/3 zero-length values carry no acquisition information."""
    return value is None or isinstance(value, str) and not value.strip() or isinstance(value, (list, tuple, pydicom.multival.MultiValue)) and not value


def _mr_consistency_value(ds, attribute):
    value = ds.get(attribute)
    if _unknown_optional_value(value):
        return None
    if attribute in {"EchoTime", "RepetitionTime", "InversionTime", "TriggerTime"}:
        # Equivalent DS encodings (e.g. 3 and 3.0) describe the same timing.
        try:
            return float(value)
        except (TypeError, ValueError, OverflowError):
            pass  # The timing validation adds the specific invalid-value error.
    return str(value).strip()


def _validate_conventional_mr(items, *, enhanced_frames=False, source_frame_keys=None):
    """Accept one homogeneous scalar anatomical volume per real series UID.

    Extra dimensions are rejected rather than guessed or collapsed. Enhanced
    functional groups, Siemens mosaics and quantitative/complex/diffusion data
    need their own ingestion contracts before they can be offered safely.
    """
    if not enhanced_frames and any(str(ds.get("SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.4.1" for _, ds in items):
        from .enhanced_mr import validate_series

        return validate_series(items)
    errors = []
    if any(str(ds.get("Modality", "")) != "MR" for _, ds in items):
        errors.append("Only MR images may be combined into an MR volume")
    storage = "1.2.840.10008.5.1.4.1.1.4.1" if enhanced_frames else "1.2.840.10008.5.1.4.1.1.4"
    if any(str(ds.get("SOPClassUID", "")) != storage for _, ds in items):
        errors.append("Only classic single-frame MR image storage is supported; Enhanced MR requires a supported single-frame export")
    try:
        single_frame = all(int(ds.get("NumberOfFrames", 1)) == 1 for _, ds in items)
    except (ValueError, TypeError, OverflowError):
        single_frame = False
    if not single_frame:
        errors.append("Enhanced or multi-frame MR is unsupported; select one static single-frame volume")
    if len(items) < 2:
        errors.append("At least two MR slices are required")
    forbidden = {
        "LOCALIZER",
        "SCOUT",
        "SURVEY",
        "MOSAIC",
        "PROJECTION",
        "PROJECTION IMAGE",
        "MIP",
        "MINIP",
        "PHASE",
        "P",
        "REAL",
        "R",
        "IMAGINARY",
        "I",
        "PHASE MAP",
        "PHASE SUBTRACT",
        "DIFFUSION",
        "DWI",
        "DTI",
        "ADC",
        "TRACEW",
        "FA",
        "PERFUSION",
        "DYNAMIC",
        "CINE",
        "T1 MAP",
        "T2 MAP",
        "DENSITY MAP",
        "VELOCITY MAP",
    }
    for _, ds in items:
        image_type = _tokens(ds.get("ImageType", []))
        if image_type & forbidden or any("DIFFUSION" in value or value.endswith(" MAP") for value in image_type):
            errors.append(
                "MR localizer, mosaic, projection, dynamic, diffusion, parametric or complex images are unsupported; select a static anatomical magnitude volume"
            )
            break
    if any(str(ds.get("ComplexImageComponent", "MAGNITUDE")).upper() != "MAGNITUDE" for _, ds in items):
        errors.append("Only MR magnitude images are supported; complex/phase components require a separate adapter")
    if any(
        any(name in ds for name in ("DiffusionBValue", "DiffusionDirectionality", "DiffusionGradientOrientation", "MRDiffusionSequence"))
        or str(ds.get("AcquisitionContrast", "")).upper() in {"DIFFUSION", "PERFUSION", "FLOW_ENCODED", "MIXED"}
        for _, ds in items
    ):
        errors.append("Diffusion, perfusion and flow-encoded MR acquisitions are unsupported")
    try:
        counts = [
            int(ds[attribute].value)
            for _, ds in items
            for attribute in ("NumberOfTemporalPositions", "TemporalPositionIdentifier", "CardiacNumberOfImages")
            if attribute in ds and not _unknown_optional_value(ds[attribute].value)
        ]
        dynamic = any(count != 1 for count in counts)
    except (ValueError, TypeError, OverflowError):
        dynamic = True
    if dynamic:
        errors.append("Dynamic/cine MR is unsupported; export one static temporal volume")
    for attribute in ("EchoTime", "RepetitionTime", "InversionTime", "TriggerTime"):
        try:
            values = [float(ds[attribute].value) for _, ds in items if attribute in ds and not _unknown_optional_value(ds[attribute].value)]
            if values and (not np.isfinite(values).all() or any(value < 0 for value in values)):
                raise ValueError()
        except (ValueError, TypeError, OverflowError):
            errors.append(f"Invalid MR {attribute}; select one finite acquisition timing")
    if any(len(_tokens(ds.get("EchoNumbers", []))) > 1 for _, ds in items):
        errors.append("Multiple MR echo numbers are unsupported; select one echo volume")
    consistent = (
        "StudyInstanceUID",
        "AcquisitionNumber",
        "TemporalPositionIdentifier",
        "NumberOfTemporalPositions",
        "ImageType",
        "EchoTime",
        "EchoNumbers",
        "RepetitionTime",
        "InversionTime",
        "TriggerTime",
        "SequenceName",
        "ScanningSequence",
        "SequenceVariant",
        "ScanOptions",
        "MRAcquisitionType",
        "ComplexImageComponent",
        "AcquisitionContrast",
        "RescaleType",
        "BitsAllocated",
        "BitsStored",
        "HighBit",
        "PixelRepresentation",
        "SamplesPerPixel",
        "PhotometricInterpretation",
    )
    for attribute in consistent:
        if len({_mr_consistency_value(ds, attribute) for _, ds in items}) > 1:
            errors.append(f"Inconsistent {attribute}; select one MR sequence, echo, temporal volume and component")
    try:
        slopes = np.asarray([float(ds.get("RescaleSlope", 1)) for _, ds in items])
        intercepts = np.asarray([float(ds.get("RescaleIntercept", 0)) for _, ds in items])
        if not np.isfinite(slopes).all() or not np.isfinite(intercepts).all() or np.any(slopes == 0):
            raise ValueError()
        if not np.all(slopes == slopes[0]) or not np.all(intercepts == intercepts[0]):
            errors.append("Inconsistent MR rescale slope/intercept; select a uniform signal encoding")
    except (ValueError, TypeError, OverflowError):
        errors.append("MR rescale slope/intercept must be finite with a nonzero slope")
    try:
        scalar = all(
            int(ds.get("SamplesPerPixel", 0)) == 1
            and str(ds.get("PhotometricInterpretation", "")) in {"MONOCHROME1", "MONOCHROME2"}
            and int(ds.get("BitsAllocated", 0)) == 16
            and 1 <= int(ds.get("BitsStored", 0)) <= 16
            and int(ds.get("HighBit", -1)) == int(ds.get("BitsStored", 0)) - 1
            and int(ds.get("PixelRepresentation", -1)) in {0, 1}
            for _, ds in items
        )
    except (ValueError, TypeError, OverflowError):
        scalar = False
    if not scalar:
        errors.append("MR requires scalar 16-bit MONOCHROME1 or MONOCHROME2 images with valid stored bits")
    return _validate_spatial_stack(items, errors, modality="MR", source_frame_keys=source_frame_keys)


def _validate_spatial_stack(items, errors, *, modality="CT", source_frame_keys=None):
    """Shared physical geometry checks; modality validators own acquisition rules."""
    required = ("ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing", "Rows", "Columns")
    if any(any(name not in ds for name in required) for _, ds in items):
        errors.append("Missing patient position, orientation or pixel geometry")
        return items, None, errors
    try:
        orientations = np.asarray([ds.ImageOrientationPatient for _, ds in items], dtype=float)
        positions = np.asarray([ds.ImagePositionPatient for _, ds in items], dtype=float)
        spacing = np.asarray([ds.PixelSpacing for _, ds in items], dtype=float)
        if (
            orientations.shape != (len(items), 6)
            or positions.shape != (len(items), 3)
            or spacing.shape != (len(items), 2)
            or not np.isfinite(orientations).all()
            or not np.isfinite(positions).all()
            or not np.isfinite(spacing).all()
            or np.any(spacing <= 0)
        ):
            raise ValueError()
    except (ValueError, TypeError):
        errors.append("Invalid image geometry values")
        return items, None, errors
    first = orientations[0]
    row, col = first[:3], first[3:]
    if not np.allclose([np.linalg.norm(row), np.linalg.norm(col), row @ col], [1, 1, 0], atol=1e-4):
        errors.append("Direction cosines are not orthonormal")
    if not np.allclose(orientations, first, rtol=0, atol=1e-4):
        errors.append("Slice orientations differ within the series")
    if not np.allclose(spacing, spacing[0], rtol=0, atol=1e-4):
        errors.append("Pixel spacing differs within the series")
    dimensions = {(int(ds.Rows), int(ds.Columns)) for _, ds in items}
    if len(dimensions) != 1 or any(a < 1 or b < 1 for a, b in dimensions):
        errors.append("Image dimensions differ or are invalid")
    frame_ids = {str(ds.get("FrameOfReferenceUID", "")) for _, ds in items}
    if len(frame_ids) > 1 or "" in frame_ids:
        errors.append("Series has missing or multiple patient coordinate frames")
    sop_ids = [str(ds.get("SOPInstanceUID", "")) for _, ds in items]
    identifiers = source_frame_keys if source_frame_keys is not None else sop_ids
    if any(not x for x in sop_ids) or len(identifiers) != len(items) or len(set(identifiers)) != len(identifiers):
        errors.append("Missing or duplicate SOP instance identifiers")
    normal = np.cross(row, col)
    distances = positions @ normal
    order = np.argsort(distances)
    positions = positions[order]
    distances = distances[order]
    diffs = np.diff(distances)
    z_spacing = float(np.median(diffs)) if diffs.size else float(items[0][1].get("SliceThickness", 1))
    if diffs.size:
        if np.any(diffs < 0.001):
            errors.append("Repeated slice positions; select one phase/reconstruction")
        elif not np.allclose(diffs, z_spacing, rtol=0.02, atol=0.05):
            errors.append("Nonuniform slice spacing or missing slices")
        try:
            declared_intervals = [
                abs(float(ds.SpacingBetweenSlices))
                for _, ds in items
                if "SpacingBetweenSlices" in ds and not (modality == "MR" and _unknown_optional_value(ds.SpacingBetweenSlices))
            ]
        except (TypeError, ValueError, OverflowError):
            errors.append("Invalid declared slice interval")
            declared_intervals = []
        if declared_intervals and (not np.isfinite(declared_intervals).all() or not np.allclose(declared_intervals, z_spacing, rtol=0.02, atol=0.05)):
            errors.append("Patient positions disagree with the declared slice interval; check missing slices")
        residual = np.diff(positions, axis=0) - diffs[:, None] * normal
        if np.any(np.linalg.norm(residual, axis=1) > 0.05):
            errors.append(
                "Gantry tilt or in-plane slice drift is unsupported; use a corrected reconstruction"
                if modality == "CT"
                else "In-plane MR slice drift is unsupported; select a regular spatial stack"
            )
    ordered = [items[int(i)] for i in order]
    return ordered, [float(spacing[0, 1]), float(spacing[0, 0]), z_spacing], errors


def _finite_number(value) -> float | None:
    try:
        result = float(value)
        return result if np.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _voi_pair(ds) -> tuple[float, float] | None:
    function = ds.get("VOILUTFunction")
    if not _unknown_optional_value(function) and str(function).strip().upper() not in {"LINEAR", "LINEAR_EXACT"}:
        # The scalar viewer currently uses a linear window. A nonlinear source
        # VOI must not be silently reinterpreted as that linear display policy.
        return None
    center, width = ds.get("WindowCenter"), ds.get("WindowWidth")
    centers = list(center) if isinstance(center, pydicom.multival.MultiValue) else [center]
    widths = list(width) if isinstance(width, pydicom.multival.MultiValue) else [width]
    for raw_center, raw_width in zip(centers, widths):
        level, window = _finite_number(raw_center), _finite_number(raw_width)
        if level is not None and window is not None and window > 0:
            return level, window
    return None


def _mr_series_metadata(items) -> dict:
    first = items[0][1]
    windows = [_voi_pair(ds) for _, ds in items]
    window = windows[0] if all(value == windows[0] for value in windows) else None
    function = first.get("VOILUTFunction")
    return {
        "sequence_name": str(first.get("SequenceName", "")) or None,
        "scanning_sequence": "\\".join(sorted(_tokens(first.get("ScanningSequence", [])))) or None,
        "sequence_variant": "\\".join(sorted(_tokens(first.get("SequenceVariant", [])))) or None,
        "acquisition_type": str(first.get("MRAcquisitionType", "")) or None,
        "echo_time_ms": _finite_number(first.get("EchoTime")),
        "echo_number": _finite_number(first.get("EchoNumbers")),
        "repetition_time_ms": _finite_number(first.get("RepetitionTime")),
        "inversion_time_ms": _finite_number(first.get("InversionTime")),
        "photometric_interpretation": str(first.get("PhotometricInterpretation", "")) or None,
        "window_center": window[0] if window else None,
        "window_width": window[1] if window else None,
        "voi_lut_function": "LINEAR" if _unknown_optional_value(function) else str(function),
    }


def _indexed_frame_count(ds) -> int:
    # A malformed multi-frame declaration must not prevent other series from
    # being listed. The modality validator already records its blocking error.
    try:
        return max(1, int(ds.get("NumberOfFrames", 1)))
    except (TypeError, ValueError, OverflowError):
        return 1


def index_dicom(directory: Path) -> list[dict]:
    directory = Path(directory)
    if not directory.is_dir():
        raise ValueError("DICOM input must be a directory")
    groups = defaultdict(list)
    for path in sorted(directory.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        try:
            ds = _read(path)
            uid = str(ds.get("SeriesInstanceUID", ""))
            if uid:
                groups[uid].append((path.resolve(), ds))
        except (InvalidDicomError, OSError, ValueError, EOFError):
            continue
    result = []
    for uid, items in sorted(groups.items()):
        ordered, spacing, warnings = _validate(items)
        first = ordered[0][1]
        modality = str(first.get("Modality", ""))
        record = {
            "uid": uid,
            "description": str(first.get("SeriesDescription", f"{modality or 'DICOM'} series")),
            "modality": modality,
            "study_uid": str(first.StudyInstanceUID) if first.get("StudyInstanceUID") else None,
            "frame_of_reference_uid": str(first.FrameOfReferenceUID) if first.get("FrameOfReferenceUID") else None,
            "sop_class_uids": sorted({str(ds.get("SOPClassUID", "")) for _, ds in items}),
            "slice_count": sum(_indexed_frame_count(ds) for _, ds in ordered),
            "spacing": spacing,
            "warnings": warnings,
            "supported": not warnings,
            "unsupported_reason": "; ".join(warnings) or None,
            "files": [str(path) for path, _ in ordered],
        }
        if modality == "MR":
            if all(str(ds.get("SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.4.1" for _, ds in ordered):
                from .enhanced_mr import resolve_stack

                record.update(storage_kind="enhanced_mr", acquired_frame_count=record["slice_count"], temporal_position_index=None)
                try:
                    stack = resolve_stack(ordered)
                    record.update(_mr_series_metadata(stack.frame_items))
                    record["temporal_position_index"] = stack.temporal_position_index
                    if not stack.errors:
                        record["frame_selector"] = {
                            "source_sop_instance_uid": stack.frames[0].source_sop_instance_uid,
                            **({"temporal_position_index": stack.temporal_position_index} if stack.temporal_position_index is not None else {}),
                            **({"stack_id": stack.stack_id} if stack.stack_id is not None else {}),
                        }
                except (ValueError, TypeError, OverflowError):
                    record.update(_mr_series_metadata(ordered))
            else:
                record.update(_mr_series_metadata(ordered))
        result.append(record)
    return result


def convert_series(files, output_dir: Path) -> Path:
    paths = [Path(path).resolve() for path in files]
    if not paths:
        raise ValueError("No DICOM files were selected")
    items = [(path, _read(path)) for path in paths]
    if len({str(ds.get("SeriesInstanceUID", "")) for _, ds in items}) != 1:
        raise ValueError("Select exactly one DICOM series")
    items, spacing, errors = _validate(items)
    if errors:
        raise ValueError("; ".join(errors))
    modality = str(items[0][1].get("Modality", "CT"))
    enhanced = None
    frame_items = items
    if str(items[0][1].get("SOPClassUID", "")) == "1.2.840.10008.5.1.4.1.1.4.1":
        from .enhanced_mr import resolve_stack

        enhanced = resolve_stack(items)
        frame_items = enhanced.frame_items
    executable = os.environ.get("SCAN3D_DCM2NIIX") or shutil.which("dcm2niix")
    if not executable:
        raise RuntimeError("dcm2niix is required for DICOM conversion; run the environment setup")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "reference.nii.gz"
    if destination.exists():
        raise ValueError(f"Reference {modality} already exists; use a new processing directory")
    with tempfile.TemporaryDirectory(prefix=".dicom-", dir=output_dir) as temporary:
        staging = Path(temporary)
        source = staging / "selected"
        target = staging / "converted"
        source.mkdir()
        target.mkdir()
        for index, (path, _) in enumerate(items):
            copied = source / f"{index:06d}.dcm"
            try:
                os.link(path, copied)
            except OSError:
                shutil.copyfile(path, copied)
        completed, details = _run_converter(executable, source, target, output_dir, modality=modality)
        primary = completed
        normalizer = None
        if _decoder_failure(completed, items):
            normalized = staging / "decoded"
            try:
                normalizer = _normalize_compressed(items, normalized)
            except Exception as exc:
                _conversion_log(output_dir, "", str(exc), "Independent GDCM decoding failed", append=True)
                raise RuntimeError(_conversion_failure(primary, items, details) + " Independent GDCM decoding also failed.") from exc
            # A failed native attempt can leave partial files. Never let those
            # participate in validating the independent-decoder retry.
            target = staging / "decoded-converted"
            target.mkdir()
            completed, details = _run_converter(executable, normalized, target, output_dir, append=True, modality=modality)
        if completed.returncode:
            raise RuntimeError(_conversion_failure(completed, items, details))
        volumes, ignored_outputs = _conversion_image_outputs(target, items)
        if modality == "MR" and (list(target.glob("*.bval")) or list(target.glob("*.bvec"))):
            raise ValueError("Converted MR includes diffusion gradient data; select a static anatomical volume. " + details)
        if len(volumes) != 1:
            raise ValueError(f"Selected DICOM files did not convert into exactly one 3D {modality} volume. " + details)
        image = nib.load(str(volumes[0]))
        ds = frame_items[0][1]
        expected_voxels = int(ds.Rows) * int(ds.Columns) * len(frame_items)
        if len(image.shape) != 3 or int(np.prod(image.shape)) != expected_voxels:
            raise ValueError(f"Converted {modality} dimensions do not match the selected DICOM slices")
        expected_volume = float(np.prod(spacing))
        if not np.isclose(abs(np.linalg.det(image.affine[:3, :3])), expected_volume, rtol=0.005):
            raise ValueError(f"Converted {modality} physical spacing does not match DICOM geometry")
        orientation = np.asarray(ds.ImageOrientationPatient, dtype=float)
        center_lps = (
            (np.asarray(frame_items[0][1].ImagePositionPatient, dtype=float) + np.asarray(frame_items[-1][1].ImagePositionPatient, dtype=float)) / 2
            + orientation[:3] * spacing[0] * (int(ds.Columns) - 1) / 2
            + orientation[3:] * spacing[1] * (int(ds.Rows) - 1) / 2
        )
        center_ras = image.affine @ np.r_[(np.asarray(image.shape) - 1) / 2, 1]
        if not np.allclose(center_ras[:3], center_lps * [-1, -1, 1], atol=0.1, rtol=0):
            raise ValueError(f"Converted {modality} patient coordinates do not match the selected series")
        if enhanced is not None:
            from .enhanced_mr import verify_converted_grid

            verify_converted_grid(enhanced, image)
        converter_version = re.search(rb"dcm2niix version (v[0-9.]+)", _converter_output(primary))
        _save_conversion_manifest(
            output_dir,
            {
                "schema_version": 1,
                "converter": "dcm2niix",
                "converter_version": converter_version.group(1).decode() if converter_version else None,
                "source_transfer_syntaxes": sorted({str(ds.file_meta.TransferSyntaxUID) for _, ds in items}),
                "slice_count": len(frame_items),
                "primary_exit_code": primary.returncode,
                "final_exit_code": completed.returncode,
                "normalizer": normalizer,
                **({"ignored_outputs": ignored_outputs} if ignored_outputs else {}),
                **(
                    {
                        "modality": "MR",
                        "converter_options": _converter_options(modality),
                        "intensity_scaling": "standard_dicom_rescale",
                        "rescale_slope": float(ds.get("RescaleSlope", 1)),
                        "rescale_intercept": float(ds.get("RescaleIntercept", 0)),
                        "acquisition": _mr_series_metadata(frame_items),
                    }
                    if modality == "MR"
                    else {}
                ),
                **(
                    {
                        "storage_kind": "enhanced_mr",
                        "source_instance_count": len(items),
                        "source_frame_order": [
                            {"sop_instance_uid": frame.source_sop_instance_uid, "frame_index": frame.frame_index} for frame in enhanced.frames
                        ],
                    }
                    if enhanced
                    else {}
                ),
            },
        )
        shutil.move(str(volumes[0]), destination)
    return destination
