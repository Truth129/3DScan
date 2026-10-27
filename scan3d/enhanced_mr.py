"""Resolve one Enhanced MR instance into a conservative static spatial stack.

Only headers are inspected here. Source SOP/series identifiers and frame indices
remain original; resolved frame headers are internal views, never new DICOMs.
See DICOM PS3.3 C.7.6.16, C.7.6.17 and C.8.13.5 for functional groups/dimensions.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from itertools import product
from pathlib import Path

import nibabel as nib
import numpy as np
from pydicom.pixels import pixel_array
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.tag import Tag


ENHANCED_MR_STORAGE = "1.2.840.10008.5.1.4.1.1.4.1"
_SPATIAL_DIMENSIONS = {Tag(0x0020, 0x9057), Tag(0x0020, 0x0032), Tag(0x0020, 0x9113)}
_GLOBAL_FIELDS = (
    "SeriesInstanceUID",
    "StudyInstanceUID",
    "SOPClassUID",
    "SOPInstanceUID",
    "FrameOfReferenceUID",
    "Modality",
    "Rows",
    "Columns",
    "SamplesPerPixel",
    "PhotometricInterpretation",
    "BitsAllocated",
    "BitsStored",
    "HighBit",
    "PixelRepresentation",
    "ImageType",
    "SequenceName",
    "ScanningSequence",
    "SequenceVariant",
    "ScanOptions",
    "MRAcquisitionType",
    "AcquisitionNumber",
    "EchoNumbers",
    "EchoTime",
    "RepetitionTime",
    "InversionTime",
    "WindowCenter",
    "WindowWidth",
    "VOILUTFunction",
    "ComplexImageComponent",
    "AcquisitionContrast",
    "PixelPresentation",
    "VolumetricProperties",
    "VolumeBasedCalculationTechnique",
    "RescaleSlope",
    "RescaleIntercept",
    "RescaleType",
)


@dataclass(frozen=True, slots=True)
class MRFrame:
    source_path: Path
    source_sop_instance_uid: str
    frame_index: int  # Zero based index into the original instance's Pixel Data.
    header: Dataset
    stack_id: str | None
    temporal_position_index: int | None
    in_stack_position_number: int | None
    dimension_indices: tuple[int, ...]

    @property
    def source_key(self) -> tuple[str, int]:
        return self.source_sop_instance_uid, self.frame_index


@dataclass(frozen=True, slots=True)
class EnhancedMRStack:
    original_items: tuple[tuple[Path, Dataset], ...]
    frames: tuple[MRFrame, ...]  # Physical slice order, independent of storage order.
    spacing: tuple[float, float, float] | None
    errors: tuple[str, ...]

    @property
    def frame_items(self) -> list[tuple[Path, Dataset]]:
        return [(frame.source_path, frame.header) for frame in self.frames]

    @property
    def temporal_position_index(self) -> int | None:
        values = {frame.temporal_position_index for frame in self.frames}
        return next(iter(values)) if len(values) == 1 else None

    @property
    def stack_id(self) -> str | None:
        values = {frame.stack_id for frame in self.frames}
        return next(iter(values)) if len(values) == 1 else None


def _sequence_item(container: Dataset, keyword: str, *, required=False) -> Dataset | None:
    if keyword not in container:
        if required:
            raise ValueError(f"Enhanced MR is missing {keyword} functional group")
        return None
    value = container[keyword].value
    if not isinstance(value, (Sequence, list, tuple)):
        raise ValueError(f"Invalid Enhanced MR {keyword} functional group")
    if len(value) != 1 or not isinstance(value[0], Dataset):
        raise ValueError(f"Enhanced MR {keyword} requires exactly one functional-group item")
    return value[0]


def _macro(shared: Dataset, frame: Dataset, keyword: str, *, required=False) -> Dataset | None:
    if keyword in shared and keyword in frame:
        raise ValueError(f"Enhanced MR {keyword} is ambiguously present in shared and per-frame groups")
    return _sequence_item(frame if keyword in frame else shared, keyword, required=required)


def _copy_fields(target: Dataset, source: Dataset | None, fields: tuple[str, ...]) -> None:
    if source is not None:
        for name in fields:
            if name in source:
                target.add(deepcopy(source[name]))


def _positive_integer(value, label: str) -> int:
    try:
        number = int(value)
        if isinstance(value, bool) or number < 1 or float(value) != number:
            raise ValueError()
        return number
    except (TypeError, ValueError, OverflowError):
        raise ValueError(f"Enhanced MR {label} must be a positive integer") from None


def _optional_index(content, keyword):
    value = content.get(keyword)
    return None if value is None or value == "" else _positive_integer(value, keyword)


def _validate_rwvm(mapping: Dataset | None, header: Dataset) -> None:
    if mapping is None:
        return
    units = _sequence_item(mapping, "MeasurementUnitsCodeSequence", required=True)
    if str(units.get("CodingSchemeDesignator", "")) != "UCUM" or str(units.get("CodeValue", "")) != "1":
        raise ValueError("Quantitative Enhanced MR real-world units require a separate image adapter")
    if "RealWorldValueLUTData" in mapping:
        raise ValueError("Nonlinear Enhanced MR real-world mapping requires a separate image adapter")
    try:
        mapped = np.asarray([float(mapping.RealWorldValueSlope), float(mapping.RealWorldValueIntercept)])
        display = np.asarray([float(header.RescaleSlope), float(header.RescaleIntercept)])
    except (AttributeError, TypeError, ValueError, OverflowError):
        raise ValueError("Enhanced MR real-world mapping is incomplete") from None
    if not np.isfinite(mapped).all() or not np.allclose(mapped, display, rtol=1e-9, atol=1e-8):
        raise ValueError("Enhanced MR real-world and standard rescale mappings disagree")


def _frames(items: list[tuple[Path, Dataset]]) -> list[MRFrame]:
    if not items or any(str(ds.get("Modality", "")) != "MR" or str(ds.get("SOPClassUID", "")) != ENHANCED_MR_STORAGE for _, ds in items):
        raise ValueError("Do not combine classic, Enhanced MR or other image storage within one volume")
    if len(items) != 1:
        raise ValueError("Enhanced MR currently requires one complete instance per volume; multi-instance concatenations need a separate import adapter")
    path, source = items[0]
    if any(
        name in source
        for name in (
            "ConcatenationUID",
            "ConcatenationFrameOffsetNumber",
            "InConcatenationNumber",
            "InConcatenationTotalNumber",
            "SOPInstanceUIDOfConcatenationSource",
        )
    ):
        raise ValueError("Enhanced MR concatenations are unsupported; export one complete instance")
    count = _positive_integer(source.get("NumberOfFrames"), "NumberOfFrames")
    if count < 2:
        raise ValueError("At least two Enhanced MR spatial frames are required")
    shared = _sequence_item(source, "SharedFunctionalGroupsSequence", required=True)
    per_frame = source.get("PerFrameFunctionalGroupsSequence", [])
    if len(per_frame) != count or any(not isinstance(item, Dataset) for item in per_frame):
        raise ValueError("Enhanced MR per-frame functional-group count does not match NumberOfFrames")
    indices = source.get("DimensionIndexSequence", [])
    if not indices:
        raise ValueError("Enhanced MR requires explicit dimension indices for frame isolation")
    try:
        pointers = tuple(Tag(item.DimensionIndexPointer) for item in indices)
    except (AttributeError, TypeError, ValueError):
        raise ValueError("Enhanced MR dimension index pointers are invalid") from None
    if len(set(pointers)) != len(pointers):
        raise ValueError("Enhanced MR dimension index pointers must be unique")
    if str(source.get("DimensionOrganizationType", "")) not in {"", "3D", "3D_TEMPORAL"}:
        raise ValueError("Tiled Enhanced MR is unsupported")
    result = []
    for index, group in enumerate(per_frame):
        header = Dataset()
        header.file_meta = source.file_meta
        _copy_fields(header, source, _GLOBAL_FIELDS)
        content = _macro(shared, group, "FrameContentSequence", required=True)
        raw_indices = content.get("DimensionIndexValues", [])
        values = [raw_indices] if isinstance(raw_indices, int) else list(raw_indices)
        if len(values) != len(pointers):
            raise ValueError("Enhanced MR DimensionIndexValues do not match the declared dimension pointers")
        dimension_values = tuple(_positive_integer(value, "dimension index") for value in values)
        position = _macro(shared, group, "PlanePositionSequence", required=True)
        orientation = _macro(shared, group, "PlaneOrientationSequence", required=True)
        measures = _macro(shared, group, "PixelMeasuresSequence", required=True)
        _copy_fields(header, position, ("ImagePositionPatient",))
        _copy_fields(header, orientation, ("ImageOrientationPatient",))
        _copy_fields(header, measures, ("PixelSpacing", "SliceThickness", "SpacingBetweenSlices"))
        transform = _macro(shared, group, "PixelValueTransformationSequence", required=True)
        if any(name not in transform or transform.get(name) is None for name in ("RescaleSlope", "RescaleIntercept")):
            raise ValueError("Enhanced MR pixel-value transformation is incomplete")
        _copy_fields(header, transform, ("RescaleSlope", "RescaleIntercept", "RescaleType"))
        frame_type = _macro(shared, group, "MRImageFrameTypeSequence", required=True)
        _copy_fields(
            header, frame_type, ("ComplexImageComponent", "AcquisitionContrast", "PixelPresentation", "VolumetricProperties", "VolumeBasedCalculationTechnique")
        )
        if "FrameType" not in frame_type:
            raise ValueError("Enhanced MR FrameType is required")
        header.ImageType = frame_type.FrameType
        if str(header.get("ComplexImageComponent", "")) != "MAGNITUDE":
            raise ValueError("Enhanced MR supports magnitude frames only; phase/complex components need a separate adapter")
        if str(header.get("PixelPresentation", "")) != "MONOCHROME":
            raise ValueError("Enhanced MR requires scalar MONOCHROME pixel presentation")
        if str(header.get("VolumetricProperties", "")) != "VOLUME" or str(header.get("VolumeBasedCalculationTechnique", "NONE")) != "NONE":
            raise ValueError("Enhanced MR projections, sampled images and calculated maps are unsupported")
        if _macro(shared, group, "MRDiffusionSequence") is not None:
            raise ValueError("Enhanced diffusion MR requires a separate import adapter")
        _copy_fields(header, _macro(shared, group, "MRTimingAndRelatedParametersSequence"), ("RepetitionTime",))
        echo = _macro(shared, group, "MREchoSequence")
        if echo is not None and "EffectiveEchoTime" in echo:
            header.EchoTime = echo.EffectiveEchoTime
        modifier = _macro(shared, group, "MRModifierSequence")
        if modifier is not None and "InversionTimes" in modifier:
            times = modifier.InversionTimes
            header.InversionTimes = times
            if isinstance(times, (int, float)):
                header.InversionTime = times
            elif len(times) == 1:
                header.InversionTime = times[0]
        _copy_fields(header, _macro(shared, group, "FrameVOILUTSequence"), ("WindowCenter", "WindowWidth", "VOILUTFunction"))
        _validate_rwvm(_macro(shared, group, "RealWorldValueMappingSequence"), header)
        result.append(
            MRFrame(
                Path(path),
                str(source.get("SOPInstanceUID", "")),
                index,
                header,
                str(content.StackID) if content.get("StackID") is not None else None,
                _optional_index(content, "TemporalPositionIndex"),
                _optional_index(content, "InStackPositionNumber"),
                dimension_values,
            )
        )
    if len({frame.stack_id for frame in result}) != 1:
        raise ValueError("Mixed Enhanced MR stacks require separate volumes")
    if len({frame.temporal_position_index for frame in result}) != 1:
        raise ValueError("Mixed Enhanced MR temporal positions require separate static volumes")
    for offset, pointer in enumerate(pointers):
        if pointer not in _SPATIAL_DIMENSIONS and len({frame.dimension_indices[offset] for frame in result}) != 1:
            raise ValueError("Mixed non-spatial Enhanced MR dimensions require separate volumes")
    inversion_times = {str(frame.header.get("InversionTimes", "")) for frame in result}
    if len(inversion_times) != 1:
        raise ValueError("Mixed Enhanced MR inversion timing requires separate volumes")
    return result


def resolve_stack(items: list[tuple[Path, Dataset]]) -> EnhancedMRStack:
    from .dicom import _validate_conventional_mr

    frames = _frames(items)
    frame_items = [(frame.source_path, frame.header) for frame in frames]
    ordered, spacing, errors = _validate_conventional_mr(frame_items, enhanced_frames=True, source_frame_keys=[frame.source_key for frame in frames])
    by_header = {id(frame.header): frame for frame in frames}
    return EnhancedMRStack(tuple(items), tuple(by_header[id(header)] for _, header in ordered), tuple(spacing) if spacing is not None else None, tuple(errors))


def validate_series(items):
    try:
        stack = resolve_stack(items)
        return list(stack.original_items), list(stack.spacing) if stack.spacing is not None else None, list(stack.errors)
    except (ValueError, TypeError, OverflowError) as exc:
        return items, None, [str(exc) if isinstance(exc, ValueError) else "Invalid Enhanced MR functional-group values"]


def verify_converted_grid(stack: EnhancedMRStack, image: nib.spatialimages.SpatialImage) -> None:
    """Verify complete grid coverage and bounded source-pixel landmarks.

    A converter may legitimately flip rows or permute storage axes. Its affine
    must map the original complete voxel grid onto the same physical locations;
    sparse pixel checks also detect an affine flip without the matching data flip.
    At most three original raster frames and three converted planes are decoded.
    """
    if stack.errors or stack.spacing is None:
        raise ValueError("An invalid Enhanced MR frame stack cannot be converted")
    first = stack.frames[0].header
    source_shape = np.asarray([int(first.Columns), int(first.Rows), len(stack.frames)])
    orientation = np.asarray(first.ImageOrientationPatient, dtype=float)
    row, column = orientation[:3], orientation[3:]
    flip = np.diag([-1.0, -1.0, 1.0])
    expected = np.eye(4)
    expected[:3, :3] = flip @ np.column_stack((row * stack.spacing[0], column * stack.spacing[1], np.cross(row, column) * stack.spacing[2]))
    expected[:3, 3] = flip @ np.asarray(first.ImagePositionPatient, float)
    factor = {"unknown": 1.0, "mm": 1.0, "meter": 1000.0, "micron": 0.001}.get(image.header.get_xyzt_units()[0])
    if factor is None:
        raise ValueError("Converted Enhanced MR has unsupported spatial units")
    converted_affine = np.asarray(image.affine, float).copy()
    converted_affine[:3, :] *= factor
    try:
        mapping = np.linalg.solve(converted_affine, expected)
    except (ValueError, np.linalg.LinAlgError):
        raise ValueError("Converted Enhanced MR has an invalid patient transform") from None
    matrix = mapping[:3, :3]
    axes = np.argmax(np.abs(matrix), axis=0)
    signed_permutation = np.zeros((3, 3))
    signed_permutation[axes, np.arange(3)] = np.sign(matrix[axes, np.arange(3)])
    if len(set(axes)) != 3 or not np.allclose(matrix, signed_permutation, rtol=0, atol=1e-4):
        raise ValueError("Converted Enhanced MR voxel axes do not match the source physical grid")
    corners = np.asarray(list(product(*[(0, int(size) - 1) for size in source_shape])))
    mapped = nib.affines.apply_affine(mapping, corners)
    rounded = np.rint(mapped).astype(int)
    converted_corners = set(product(*[(0, int(size) - 1) for size in image.shape]))
    reconstructed = nib.affines.apply_affine(converted_affine, rounded)
    source_world = nib.affines.apply_affine(expected, corners)
    if set(map(tuple, rounded)) != converted_corners or not np.allclose(reconstructed, source_world, rtol=0, atol=0.01):
        raise ValueError("Converted Enhanced MR physical extent or voxel centres do not match the source")
    native_frame_axis = int(axes[2])
    other_axes = [axis for axis in range(3) if axis != native_frame_axis]
    rows = sorted({0, int(first.Rows) - 1, int(first.Rows) // 4, int(first.Rows) // 2, int(first.Rows) * 3 // 4})
    columns = sorted({0, int(first.Columns) - 1, int(first.Columns) // 4, int(first.Columns) // 2, int(first.Columns) * 3 // 4})
    raster = np.asarray(list(product(rows, columns)), dtype=int)
    for slice_index in sorted({0, len(stack.frames) // 2, len(stack.frames) - 1}):
        frame = stack.frames[slice_index]
        logical = np.column_stack((raster[:, 1], raster[:, 0], np.full(len(raster), slice_index)))
        locations = np.rint(nib.affines.apply_affine(mapping, logical)).astype(int)
        plane_index = int(locations[0, native_frame_axis])
        selector = [slice(None)] * 3
        selector[native_frame_axis] = plane_index
        converted = np.asanyarray(image.dataobj[tuple(selector)])
        actual = converted[tuple(locations[:, axis] for axis in other_axes)]
        try:
            original = pixel_array(frame.source_path, index=frame.frame_index, raw=True)
        except Exception as exc:
            raise ValueError("Could not verify Enhanced MR source frame pixels with the installed DICOM decoder") from exc
        if original.shape != (int(first.Rows), int(first.Columns)):
            raise ValueError("Enhanced MR source pixels do not match their frame geometry")
        expected_values = original[raster[:, 0], raster[:, 1]].astype(float) * float(frame.header.RescaleSlope) + float(frame.header.RescaleIntercept)
        if not np.isfinite(actual).all() or not np.allclose(actual, expected_values, rtol=1e-6, atol=1e-4):
            raise ValueError("Converted Enhanced MR signal landmarks do not match their source patient coordinates")
