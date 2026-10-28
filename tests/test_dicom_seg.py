"""Exact source-grid export, overlap, provenance and bounded-memory gates."""

from dataclasses import FrozenInstanceError
from itertools import permutations
import weakref

import nibabel as nib
import numpy as np
import pydicom
import pytest

from osirix_seg_smoke import native_grid, source_fixture
from scan3d import dicom_seg
from scan3d.fileio import hash_file


@pytest.fixture
def fixture(tmp_path):
    images, values, truth = source_fixture()
    pixels, masks, affine = native_grid(images, values, truth)
    reference = tmp_path / "reference.nii.gz"
    nib.save(nib.Nifti1Image(pixels, affine), reference)
    paths, segments = [], []
    for index, image in enumerate(images):
        path = tmp_path / f"image-{index:03d}.dcm"
        image.save_as(path, enforce_file_format=True)
        paths.append(path)
    for index, name, color in [(0, "ring", "#ff4040"), (1, "block", "#00dcff")]:
        path = tmp_path / f"{name}.nii.gz"
        nib.save(nib.Nifti1Image(masks[..., index].astype(np.uint8), affine), path)
        segments.append(dicom_seg.SegmentInput(name, name.capitalize(), color, path))
    return dict(paths=paths, images=images, reference=reference, segments=segments, truth=truth, output=tmp_path / "export")


def export(fixture, **kwargs):
    return dicom_seg.export_segmentation(
        fixture["paths"][::-1],
        fixture["reference"],
        fixture["segments"],
        fixture["output"],
        revision_id="revision-test",
        source_kind="totalsegmentator",
        **kwargs,
    )


def independent_pixels(path, images):
    seg = pydicom.dcmread(path)
    sources = {str(image.SOPInstanceUID): index for index, image in enumerate(images)}
    result = np.zeros((len(images), seg.Rows, seg.Columns), dtype=bool)
    frame_size = int(seg.Rows) * int(seg.Columns)
    frames = np.unpackbits(np.frombuffer(seg.PixelData, dtype=np.uint8), bitorder="little")[: int(seg.NumberOfFrames) * frame_size]
    frames = frames.reshape(int(seg.NumberOfFrames), seg.Rows, seg.Columns)
    shared = seg.SharedFunctionalGroupsSequence[0]
    for frame, group in zip(frames, seg.PerFrameFunctionalGroupsSequence, strict=True):
        source = group.DerivationImageSequence[0].SourceImageSequence[0]
        index = sources[str(source.ReferencedSOPInstanceUID)]
        assert source.SpatialLocationsPreserved == "YES"
        identification = getattr(group, "SegmentIdentificationSequence", shared.SegmentIdentificationSequence)
        assert int(identification[0].ReferencedSegmentNumber) == 1
        assert np.allclose(group.PlanePositionSequence[0].ImagePositionPatient, images[index].ImagePositionPatient, rtol=0, atol=1e-6)
        assert np.allclose(shared.PlaneOrientationSequence[0].ImageOrientationPatient, images[index].ImageOrientationPatient, rtol=0, atol=1e-6)
        assert np.allclose(shared.PixelMeasuresSequence[0].PixelSpacing, images[index].PixelSpacing, rtol=0, atol=1e-6)
        result[index] = frame
    return seg, result


def test_exact_independent_masks_preserve_overlap_and_source_geometry(fixture):
    before = {path: hash_file(path) for path in [*fixture["paths"], fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]}
    progress = []
    result = export(fixture, progress=lambda *args: progress.append(args))
    assert progress == [(0, 2), (1, 2), (2, 2)]
    assert [file.structure_id for file in result.files] == ["block", "ring"]
    outputs = {}
    for entry in result.files:
        seg, pixels = independent_pixels(entry.path, fixture["images"])
        index = 1 if entry.structure_id == "block" else 0
        assert np.array_equal(pixels, fixture["truth"][..., index])
        assert seg.SeriesInstanceUID == result.series_instance_uid
        assert seg.SeriesDescription == f"3DScan segmentations {result.revision_id[:8]}"
        assert seg.StudyInstanceUID == result.study_instance_uid == fixture["images"][0].StudyInstanceUID
        assert seg.FrameOfReferenceUID == result.frame_of_reference_uid == fixture["images"][0].FrameOfReferenceUID
        assert seg.SegmentSequence[0].SegmentAlgorithmType == "AUTOMATIC"
        assert seg.SegmentSequence[0].SegmentAlgorithmName == "TotalSegmentator"
        assert int(seg.NumberOfFrames) < len(fixture["images"])
        assert entry.sha256 == hash_file(entry.path) and entry.size == entry.path.stat().st_size
        assert entry.voxel_count == int(pixels.sum())
        assert entry.path.stat().st_mode & 0o777 == 0o600
        outputs[entry.structure_id] = pixels
    assert np.count_nonzero(outputs["ring"] & outputs["block"]) == 529
    assert len({entry.sop_instance_uid for entry in result.files}) == 2
    assert result.reference_sha256 == before[fixture["reference"]]
    assert {path: hash_file(path) for path in before} == before
    with pytest.raises(FrozenInstanceError):
        result.revision_id = "changed"


@pytest.mark.parametrize("source_kind", ["refinement", "slicer_correction", "interactive", "direct_edit", "include_all_outputs"])
def test_corrected_or_mixed_provenance_is_not_declared_fully_automatic(fixture, source_kind):
    result = dicom_seg.export_segmentation(
        fixture["paths"], fixture["reference"], fixture["segments"][:1], fixture["output"], revision_id="saved-revision", source_kind=source_kind
    )
    seg = pydicom.dcmread(result.files[0].path)
    assert seg.SegmentSequence[0].SegmentAlgorithmType == "SEMIAUTOMATIC"
    assert seg.SegmentSequence[0].SegmentAlgorithmName == "3DScan correction"


def test_masks_are_processed_one_at_a_time(fixture, monkeypatch):
    original_binary, original_encode = dicom_seg.imaging._binary, dicom_seg._encode
    previous_mask, previous_projection = [None], [None]

    def binary(*args):
        assert previous_mask[0] is None or previous_mask[0]() is None
        assert previous_projection[0] is None or previous_projection[0]() is None
        result = original_binary(*args)
        previous_mask[0] = weakref.ref(result[0])
        return result

    def encode(stack, pixels, *args, **kwargs):
        assert pixels.ndim == 3 and pixels.dtype == np.uint8
        assert pixels.nbytes == np.prod(fixture["truth"].shape[:3])
        previous_projection[0] = weakref.ref(pixels)
        return original_encode(stack, pixels, *args, **kwargs)

    monkeypatch.setattr(dicom_seg.imaging, "_binary", binary)
    monkeypatch.setattr(dicom_seg, "_encode", encode)
    result = export(fixture)
    assert len(result.files) == 2 and previous_mask[0]() is None and previous_projection[0]() is None


@pytest.mark.parametrize("order", list(permutations(range(3))))
@pytest.mark.parametrize("flip", [False, True])
def test_all_reference_axis_orders_and_reflections_match_patient_pixels(fixture, order, flip):
    for path in [fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]:
        image = nib.load(path)
        data = np.asanyarray(image.dataobj).transpose(order).copy()
        affine = image.affine.copy()
        affine[:3, :3] = affine[:3, list(order)]
        if flip:
            data = data[::-1]
            affine[:3, 3] += affine[:3, 0] * (data.shape[0] - 1)
            affine[:3, 0] *= -1
        nib.save(nib.Nifti1Image(data, affine), path)
    fixture["segments"] = fixture["segments"][:1]
    result = export(fixture)
    _, pixels = independent_pixels(result.files[0].path, fixture["images"])
    assert np.array_equal(pixels, fixture["truth"][..., 0])


def test_constant_gantry_shear_uses_full_affine_without_resampling(fixture):
    first = fixture["images"][0]
    orientation = np.asarray(first.ImageOrientationPatient).reshape(2, 3)
    drift = 0.3 * orientation[0] + 0.15 * orientation[1]
    for index, path in enumerate(fixture["paths"]):
        image = pydicom.dcmread(path)
        position = np.asarray(image.ImagePositionPatient) + index * drift
        image.ImagePositionPatient = [pydicom.valuerep.format_number_as_ds(float(value)) for value in position]
        image.save_as(path, enforce_file_format=True)
        fixture["images"][index] = image
    for path in [fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]:
        image = nib.load(path)
        affine = image.affine.copy()
        affine[:3, 1] += drift * [-1, -1, 1]
        nib.save(nib.Nifti1Image(np.asanyarray(image.dataobj), affine), path)
    fixture["segments"] = fixture["segments"][:1]
    result = export(fixture)
    _, pixels = independent_pixels(result.files[0].path, fixture["images"])
    assert np.array_equal(pixels, fixture["truth"][..., 0])


@pytest.mark.parametrize("problem", ["patient", "frame", "source_class", "orientation", "spacing", "duplicate", "missing", "irregular"])
def test_inconsistent_or_unsupported_sources_are_rejected(fixture, problem):
    path = fixture["paths"][2]
    image = pydicom.dcmread(path)
    if problem == "patient":
        image.PatientID = "different-synthetic-patient"
    elif problem == "frame":
        image.FrameOfReferenceUID = pydicom.uid.generate_uid()
    elif problem == "source_class":
        image.SOPClassUID = image.file_meta.MediaStorageSOPClassUID = pydicom.uid.EnhancedCTImageStorage
    elif problem == "orientation":
        image.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    elif problem == "spacing":
        image.PixelSpacing = [2, 2]
    elif problem == "duplicate":
        fixture["paths"].append(path)
    elif problem == "missing":
        fixture["paths"].pop(2)
    else:
        image.ImagePositionPatient[0] += 0.5
    image.save_as(path, enforce_file_format=True)
    with pytest.raises(ValueError):
        export(fixture)
    assert not fixture["output"].exists()


@pytest.mark.parametrize("problem", ["offset", "nonbinary", "empty", "mask_grid"])
def test_masks_cannot_be_resampled_or_silently_skipped(fixture, problem):
    path = fixture["reference"] if problem == "offset" else fixture["segments"][0].mask_path
    image = nib.load(path)
    data, affine = np.asanyarray(image.dataobj).copy(), image.affine.copy()
    if problem in ("offset", "mask_grid"):
        affine[:3, 3] += 0.2 * affine[:3, 0]
    elif problem == "empty":
        data[:] = 0
    else:
        data.flat[0] = 2
    nib.save(nib.Nifti1Image(data, affine), path)
    with pytest.raises(ValueError):
        export(fixture)
    assert not fixture["output"].exists()
    assert not list(fixture["output"].parent.glob(".dicom-seg-*"))


def test_cancellation_and_input_changes_never_publish_partial_exports(fixture):
    calls = []

    def progress(completed, total):
        calls.append(completed)

    with pytest.raises(InterruptedError):
        export(fixture, progress=progress, cancelled=lambda: bool(calls and calls[-1] == 1))
    assert not fixture["output"].exists()
    assert not list(fixture["output"].parent.glob(".dicom-seg-*"))

    def modify(completed, total):
        if completed == total:
            image = pydicom.dcmread(fixture["paths"][0])
            image.SeriesDescription = "changed synthetic input"
            image.save_as(fixture["paths"][0], enforce_file_format=True)

    with pytest.raises(ValueError, match="changed during"):
        export(fixture, progress=modify)
    assert not fixture["output"].exists()


def test_existing_exports_are_immutable(fixture):
    result = export(fixture)
    before = [entry.path.read_bytes() for entry in result.files]
    with pytest.raises(ValueError, match="immutable"):
        export(fixture)
    assert [entry.path.read_bytes() for entry in result.files] == before


@pytest.mark.parametrize("label", ["左肺", "Heart 🫀"])
def test_non_latin_segment_labels_are_preserved_as_utf8(fixture, label):
    for path in fixture["paths"]:
        source = pydicom.dcmread(path)
        source.PatientName = "SYNTHETIC^Müller"
        source.save_as(path, enforce_file_format=True)
    segment = fixture["segments"][0]
    fixture["segments"] = [dicom_seg.SegmentInput(segment.id, label, segment.color, segment.mask_path)]
    result = export(fixture)
    seg = pydicom.dcmread(result.files[0].path)
    assert seg.SegmentSequence[0].SegmentLabel == label
    assert seg.SpecificCharacterSet == "ISO_IR 192"
    assert seg.PatientName == "SYNTHETIC^Müller"


def test_unexpected_reader_errors_do_not_expose_clinical_paths(fixture, monkeypatch):
    def fail(*args, **kwargs):
        raise nib.filebasedimages.ImageFileError("clinical-path-and-private-tags")

    monkeypatch.setattr(dicom_seg.imaging, "_image", fail)
    with pytest.raises(dicom_seg.DicomSegError) as error:
        export(fixture)
    assert "clinical-path-and-private-tags" not in str(error.value)
    assert "original CT and saved masks" in str(error.value)
