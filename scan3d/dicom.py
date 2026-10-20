"""Conservative conventional CT series ingestion and isolated dcm2niix conversion."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import os
import shutil
import subprocess
import tempfile

import nibabel as nib
import numpy as np
import pydicom
from pydicom.errors import InvalidDicomError


_TAGS = ["SeriesInstanceUID", "SOPInstanceUID", "SOPClassUID", "SeriesDescription", "Modality",
         "ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing", "SliceThickness",
         "Rows", "Columns", "NumberOfFrames", "FrameOfReferenceUID", "ImageType", "SpacingBetweenSlices",
         "StudyInstanceUID", "AcquisitionNumber", "TemporalPositionIdentifier", "NumberOfTemporalPositions",
         "ConvolutionKernel", "ReconstructionAlgorithm", "RescaleSlope", "RescaleIntercept", "RescaleType",
         "BitsAllocated", "BitsStored", "PixelRepresentation", "PhotometricInterpretation", "KVP"]


def _read(path):
    return pydicom.dcmread(str(path), stop_before_pixels=True, specific_tags=_TAGS)


def _validate(items):
    errors = []
    if any(str(getattr(ds, "Modality", "")) != "CT" for _, ds in items):
        errors.append("Only CT image series are supported")
    if any(str(getattr(ds, "SOPClassUID", "")) != "1.2.840.10008.5.1.4.1.1.2" for _, ds in items):
        errors.append("Only conventional single-frame CT image storage is supported")
    if any(int(getattr(ds, "NumberOfFrames", 1)) != 1 for _, ds in items):
        errors.append("Enhanced or multi-frame CT requires a supported single-frame export")
    if len(items) < 2:
        errors.append("At least two CT slices are required")
    if any(set(map(str, getattr(ds, "ImageType", []))) & {"LOCALIZER", "SCOUT"} for _, ds in items):
        errors.append("Localizer/scout series cannot be reconstructed as a CT volume")
    # Tube-current/exposure modulation is expected along a CT volume and must
    # not split it. Before allowing dcm2niix to merge, reject clinically distinct
    # acquisition/reconstruction/temporal data and incompatible intensity encoding.
    consistent = ("StudyInstanceUID", "AcquisitionNumber", "TemporalPositionIdentifier", "NumberOfTemporalPositions",
                  "ConvolutionKernel", "ReconstructionAlgorithm", "ImageType", "RescaleSlope", "RescaleIntercept",
                  "RescaleType", "BitsAllocated", "BitsStored", "PixelRepresentation", "PhotometricInterpretation", "KVP")
    for attribute in consistent:
        values = {str(getattr(ds, attribute, "")).strip() for _, ds in items}
        if len(values) > 1:
            errors.append(f"Inconsistent {attribute}; select one acquisition, temporal phase and reconstruction")
    required = ("ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing", "Rows", "Columns")
    if any(any(not hasattr(ds, name) for name in required) for _, ds in items):
        errors.append("Missing patient position, orientation or pixel geometry")
        return items, None, errors
    try:
        orientations = np.asarray([ds.ImageOrientationPatient for _, ds in items], dtype=float)
        positions = np.asarray([ds.ImagePositionPatient for _, ds in items], dtype=float)
        spacing = np.asarray([ds.PixelSpacing for _, ds in items], dtype=float)
        if (orientations.shape != (len(items), 6) or positions.shape != (len(items), 3)
                or spacing.shape != (len(items), 2) or not np.isfinite(orientations).all()
                or not np.isfinite(positions).all() or not np.isfinite(spacing).all()
                or np.any(spacing <= 0)):
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
    frame_ids = {str(getattr(ds, "FrameOfReferenceUID", "")) for _, ds in items}
    if len(frame_ids) > 1 or "" in frame_ids:
        errors.append("Series has missing or multiple patient coordinate frames")
    sop_ids = [str(getattr(ds, "SOPInstanceUID", "")) for _, ds in items]
    if any(not x for x in sop_ids) or len(set(sop_ids)) != len(sop_ids):
        errors.append("Missing or duplicate SOP instance identifiers")
    normal = np.cross(row, col)
    distances = positions @ normal
    order = np.argsort(distances)
    positions = positions[order]
    distances = distances[order]
    diffs = np.diff(distances)
    z_spacing = float(np.median(diffs)) if diffs.size else float(getattr(items[0][1], "SliceThickness", 1))
    if diffs.size:
        if np.any(diffs < 0.001):
            errors.append("Repeated slice positions; select one phase/reconstruction")
        elif not np.allclose(diffs, z_spacing, rtol=0.02, atol=0.05):
            errors.append("Nonuniform slice spacing or missing slices")
        declared_intervals = [abs(float(ds.SpacingBetweenSlices)) for _, ds in items
                              if hasattr(ds, "SpacingBetweenSlices")]
        if declared_intervals and (not np.isfinite(declared_intervals).all()
                or not np.allclose(declared_intervals, z_spacing, rtol=0.02, atol=0.05)):
            errors.append("Patient positions disagree with the declared slice interval; check missing slices")
        residual = np.diff(positions, axis=0) - diffs[:, None] * normal
        if np.any(np.linalg.norm(residual, axis=1) > 0.05):
            errors.append("Gantry tilt or in-plane slice drift is unsupported; use a corrected reconstruction")
    ordered = [items[int(i)] for i in order]
    return ordered, [float(spacing[0, 1]), float(spacing[0, 0]), z_spacing], errors


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
            uid = str(getattr(ds, "SeriesInstanceUID", ""))
            if uid:
                groups[uid].append((path.resolve(), ds))
        except (InvalidDicomError, OSError, ValueError, EOFError):
            continue
    result = []
    for uid, items in sorted(groups.items()):
        ordered, spacing, warnings = _validate(items)
        first = ordered[0][1]
        result.append({"uid": uid, "description": str(getattr(first, "SeriesDescription", "CT series")),
                       "modality": str(getattr(first, "Modality", "")),
                       "slice_count": sum(int(getattr(ds, "NumberOfFrames", 1)) for _, ds in ordered),
                       "spacing": spacing, "warnings": warnings,
                       "files": [str(path) for path, _ in ordered]})
    return result


def convert_series(files, output_dir: Path) -> Path:
    paths = [Path(path).resolve() for path in files]
    if not paths:
        raise ValueError("No DICOM files were selected")
    items = [(path, _read(path)) for path in paths]
    if len({str(getattr(ds, "SeriesInstanceUID", "")) for _, ds in items}) != 1:
        raise ValueError("Select exactly one DICOM series")
    items, spacing, errors = _validate(items)
    if errors:
        raise ValueError("; ".join(errors))
    executable = os.environ.get("SCAN3D_DCM2NIIX") or shutil.which("dcm2niix")
    if not executable:
        raise RuntimeError("dcm2niix is required for DICOM conversion; run the environment setup")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    destination = output_dir / "reference.nii.gz"
    if destination.exists():
        raise ValueError("Reference CT already exists; use a new processing directory")
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
        completed = subprocess.run([executable, "-z", "y", "-b", "n", "-m", "y", "-f", "reference",
                                    "-o", str(target), str(source)], capture_output=True, timeout=600)
        if completed.returncode:
            raise RuntimeError(f"dcm2niix failed (exit {completed.returncode}); verify the selected CT series")
        volumes = list(target.glob("*.nii.gz"))
        if len(volumes) != 1:
            raise ValueError("Selected DICOM files did not convert into exactly one 3D CT volume")
        image = nib.load(str(volumes[0]))
        ds = items[0][1]
        expected_voxels = int(ds.Rows) * int(ds.Columns) * len(items)
        if len(image.shape) != 3 or int(np.prod(image.shape)) != expected_voxels:
            raise ValueError("Converted CT dimensions do not match the selected DICOM slices")
        expected_volume = float(np.prod(spacing))
        if not np.isclose(abs(np.linalg.det(image.affine[:3, :3])), expected_volume, rtol=0.005):
            raise ValueError("Converted CT physical spacing does not match DICOM geometry")
        orientation = np.asarray(ds.ImageOrientationPatient, dtype=float)
        center_lps = ((np.asarray(items[0][1].ImagePositionPatient, dtype=float)
                       + np.asarray(items[-1][1].ImagePositionPatient, dtype=float)) / 2
                      + orientation[:3] * spacing[0] * (int(ds.Columns) - 1) / 2
                      + orientation[3:] * spacing[1] * (int(ds.Rows) - 1) / 2)
        center_ras = image.affine @ np.r_[(np.asarray(image.shape) - 1) / 2, 1]
        if not np.allclose(center_ras[:3], center_lps * [-1, -1, 1], atol=0.1, rtol=0):
            raise ValueError("Converted CT patient coordinates do not match the selected series")
        shutil.move(str(volumes[0]), destination)
    return destination
