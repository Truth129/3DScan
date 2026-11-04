"""Header/frame boundaries for static Enhanced MR without patient data."""

from copy import deepcopy
import json
from pathlib import Path
import shutil
import tempfile

import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.sequence import Sequence
from pydicom.tag import Tag
from pydicom.uid import generate_uid
import pytest

from scan3d import dicom
from scan3d.enhanced_mr import ENHANCED_MR_STORAGE, resolve_stack
from test_mr_dicom import write_mr_series


def _group(keyword, **fields):
    item = Dataset()
    item.update(fields)
    group = Dataset()
    group.update({keyword: Sequence([item])})
    return group


def write_enhanced_mr(path, *, shared_geometry=False, reflected=False, temporal_indices=None, count=5):
    """Write one original, scalar Enhanced MR instance; return its Path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as temporary:
        folder = write_mr_series(Path(temporary) / "classic", reflected=reflected, count=count)
        slices = sorted((pydicom.dcmread(name) for name in folder.glob("*.dcm")), key=lambda ds: int(ds.InstanceNumber))
        source = deepcopy(slices[0])
        pixels = np.stack([ds.pixel_array for ds in slices])
    source.filename = str(path)
    source.SOPClassUID = source.file_meta.MediaStorageSOPClassUID = ENHANCED_MR_STORAGE
    source.SOPInstanceUID = source.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    source.NumberOfFrames = count
    for name in (
        "ImagePositionPatient",
        "ImageOrientationPatient",
        "PixelSpacing",
        "SliceThickness",
        "SpacingBetweenSlices",
        "RescaleSlope",
        "RescaleIntercept",
        "EchoTime",
        "RepetitionTime",
        "InversionTime",
    ):
        del source[name]
    source.ImageType = ["ORIGINAL", "PRIMARY", "T1", "NONE"]
    source.DimensionOrganizationType = "3D"
    organization = Dataset()
    organization.DimensionOrganizationUID = generate_uid()
    source.DimensionOrganizationSequence = Sequence([organization])
    dimensions = []
    pointers = [Tag(0x0020, 0x9056), Tag(0x0020, 0x9057)]
    if temporal_indices is not None:
        assert len(temporal_indices) == count
        pointers.append(Tag(0x0020, 0x9128))
    for pointer in pointers:
        dimension = Dataset()
        dimension.DimensionIndexPointer = pointer
        dimension.FunctionalGroupPointer = Tag(0x0020, 0x9111)
        dimension.DimensionOrganizationUID = organization.DimensionOrganizationUID
        dimensions.append(dimension)
    source.DimensionIndexSequence = Sequence(dimensions)
    shared = _group("MRTimingAndRelatedParametersSequence", RepetitionTime=8)
    frames = []
    for index, ds in enumerate(slices):
        temporal = temporal_indices[index] if temporal_indices is not None else 1
        frame = _group(
            "FrameContentSequence",
            StackID="1",
            InStackPositionNumber=index + 1,
            TemporalPositionIndex=temporal,
            DimensionIndexValues=[1, index + 1] + ([temporal] if temporal_indices is not None else []),
        )
        frame.update(_group("PlanePositionSequence", ImagePositionPatient=list(ds.ImagePositionPatient)))
        geometry = _group("PlaneOrientationSequence", ImageOrientationPatient=list(ds.ImageOrientationPatient))
        geometry.update(
            _group("PixelMeasuresSequence", PixelSpacing=list(ds.PixelSpacing), SliceThickness=ds.SliceThickness, SpacingBetweenSlices=ds.SpacingBetweenSlices)
        )
        geometry.update(_group("PixelValueTransformationSequence", RescaleSlope=ds.RescaleSlope, RescaleIntercept=ds.RescaleIntercept, RescaleType="US"))
        (shared if shared_geometry else frame).update(geometry)
        frame.update(
            _group(
                "MRImageFrameTypeSequence",
                FrameType=["ORIGINAL", "PRIMARY", "T1", "NONE"],
                PixelPresentation="MONOCHROME",
                VolumetricProperties="VOLUME",
                VolumeBasedCalculationTechnique="NONE",
                ComplexImageComponent="MAGNITUDE",
                AcquisitionContrast="T1",
            )
        )
        frame.update(_group("MREchoSequence", EffectiveEchoTime=3.0))
        frames.append(frame)
    source.SharedFunctionalGroupsSequence = Sequence([shared])
    source.PerFrameFunctionalGroupsSequence = Sequence(frames)
    source.PixelData = pixels.astype("<u2").tobytes()
    source.save_as(path, enforce_file_format=True)
    return path


def _read(path):
    return [(path, dicom._read(path))]


@pytest.mark.parametrize("shared_geometry,temporal", [(False, 1), (True, 6)])
def test_enhanced_static_frame_views_keep_original_source_identity_and_metadata(tmp_path, shared_geometry, temporal):
    path = write_enhanced_mr(tmp_path / "source.dcm", shared_geometry=shared_geometry, temporal_indices=[temporal] * 5)
    items = _read(path)
    before = deepcopy(items[0][1])
    stack = resolve_stack(items)
    assert stack.errors == () and stack.spacing == pytest.approx([0.7, 0.8, 1.6])
    assert len(stack.original_items) == 1 and len(stack.frames) == 5
    assert stack.temporal_position_index == temporal and stack.stack_id == "1"
    assert [frame.frame_index for frame in stack.frames] == list(range(5))
    assert all(frame.source_sop_instance_uid == before.SOPInstanceUID for frame in stack.frames)
    assert all(frame.header.SOPClassUID == ENHANCED_MR_STORAGE for frame in stack.frames)
    assert items[0][1] == before
    stack.frames[0].header.ImageType = ["DERIVED", "PRIMARY"]
    assert stack.frames[1].header.ImageType == ["ORIGINAL", "PRIMARY", "T1", "NONE"]
    assert items[0][1] == before
    (record,) = dicom.index_dicom(tmp_path)
    assert record["supported"] and record["storage_kind"] == "enhanced_mr"
    assert record["slice_count"] == record["acquired_frame_count"] == 5 and len(record["files"]) == 1
    assert record["temporal_position_index"] == temporal
    assert record["frame_selector"] == {"source_sop_instance_uid": before.SOPInstanceUID, "stack_id": "1", "temporal_position_index": temporal}


@pytest.mark.parametrize(
    "change,diagnostic",
    [
        (lambda ds: ds.PerFrameFunctionalGroupsSequence[2].MREchoSequence[0].update({"EffectiveEchoTime": 4}), "EchoTime"),
        (lambda ds: ds.PerFrameFunctionalGroupsSequence[2].PixelValueTransformationSequence[0].update({"RescaleSlope": 0.5}), "rescale"),
        (lambda ds: ds.PerFrameFunctionalGroupsSequence[2].FrameContentSequence[0].update({"StackID": "2"}), "stacks"),
        (lambda ds: ds.PerFrameFunctionalGroupsSequence[2].FrameContentSequence[0].update({"TemporalPositionIndex": 2}), "temporal"),
        (lambda ds: ds.PerFrameFunctionalGroupsSequence[2].MRImageFrameTypeSequence[0].update({"VolumeBasedCalculationTechnique": "MIP"}), "projections"),
        (lambda ds: ds.PerFrameFunctionalGroupsSequence[2].update(_group("MRDiffusionSequence", DiffusionBValue=1000)), "diffusion"),
        (lambda ds: ds.PerFrameFunctionalGroupsSequence.pop(), "count"),
        (
            lambda ds: ds.SharedFunctionalGroupsSequence[0].update(
                {"PlaneOrientationSequence": deepcopy(ds.PerFrameFunctionalGroupsSequence[0].PlaneOrientationSequence)}
            ),
            "ambiguous",
        ),
    ],
)
def test_enhanced_mixed_or_ambiguous_headers_are_rejected_without_source_mutation(tmp_path, change, diagnostic):
    path = write_enhanced_mr(tmp_path / "source.dcm")
    items = _read(path)
    change(items[0][1])
    before = deepcopy(items[0][1])
    try:
        errors = resolve_stack(items).errors
    except ValueError as exc:
        errors = [str(exc)]
    assert any(diagnostic.lower() in error.lower() for error in errors), errors
    assert items[0][1] == before


@pytest.mark.parametrize("marker,value", [("ConcatenationUID", generate_uid()), ("InConcatenationNumber", 1)])
def test_enhanced_partial_concatenations_remain_explicitly_unsupported(tmp_path, marker, value):
    path = write_enhanced_mr(tmp_path / "source.dcm")
    ds = pydicom.dcmread(path)
    ds.update({marker: value})
    ds.save_as(path, enforce_file_format=True)
    (record,) = dicom.index_dicom(tmp_path)
    assert not record["supported"] and "concatenation" in record["unsupported_reason"]


@pytest.mark.skipif(not shutil.which("dcm2niix"), reason="Native dcm2niix is not installed")
def test_native_enhanced_conversion_stages_one_original_and_preserves_fractional_signal(tmp_path, monkeypatch):
    path = write_enhanced_mr(tmp_path / "source.dcm", shared_geometry=True, reflected=True)
    original = path.read_bytes()
    (record,) = dicom.index_dicom(tmp_path)
    native_run = dicom.subprocess.run
    staged_counts = []

    def run(command, **kwargs):
        staged_counts.append(len(list(Path(command[-1]).glob("*.dcm"))))
        return native_run(command, **kwargs)

    monkeypatch.setattr(dicom.subprocess, "run", run)
    converted = dicom.convert_series(record["files"], tmp_path / "converted")
    assert staged_counts == [1] and path.read_bytes() == original
    assert np.any(np.asarray(nib.load(converted).dataobj) != np.rint(np.asarray(nib.load(converted).dataobj)))
    manifest = json.loads((converted.parent / "conversion.json").read_text())
    assert manifest["storage_kind"] == "enhanced_mr" and manifest["slice_count"] == 5
    assert manifest["source_instance_count"] == 1
    assert [item["frame_index"] for item in manifest["source_frame_order"]] == list(range(5))


@pytest.mark.skipif(not shutil.which("dcm2niix"), reason="Native dcm2niix is not installed")
def test_enhanced_physical_order_can_differ_from_source_frame_storage_order(tmp_path):
    path = write_enhanced_mr(tmp_path / "source.dcm")
    ds = pydicom.dcmread(path)
    pixels = ds.pixel_array.copy()
    ds.PerFrameFunctionalGroupsSequence = Sequence(list(reversed(ds.PerFrameFunctionalGroupsSequence)))
    ds.PixelData = pixels[::-1].astype("<u2").tobytes()
    ds.save_as(path, enforce_file_format=True)
    original = path.read_bytes()
    stack = resolve_stack(_read(path))
    assert stack.errors == () and [frame.frame_index for frame in stack.frames] == [4, 3, 2, 1, 0]
    (record,) = dicom.index_dicom(tmp_path)
    converted = dicom.convert_series(record["files"], tmp_path / "converted")
    assert path.read_bytes() == original
    manifest = json.loads((converted.parent / "conversion.json").read_text())
    assert [frame["frame_index"] for frame in manifest["source_frame_order"]] == [4, 3, 2, 1, 0]
