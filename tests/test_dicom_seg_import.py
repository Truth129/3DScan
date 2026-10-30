"""Synthetic source-bound SEG decoding, geometry, memory and mutation gates."""

from copy import deepcopy
from pathlib import Path

import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import Dataset
from pydicom.pixels import pack_bits
from pydicom.uid import DeflatedExplicitVRLittleEndian, ImplicitVRLittleEndian, generate_uid
import pytest

from synthetic_dicom import source_fixture, native_grid
from scan3d import dicom_seg, dicom_seg_import
from scan3d.fileio import hash_file


@pytest.fixture
def fixture(tmp_path):
    images, pixels, truth = source_fixture()
    values, masks, affine = native_grid(images, pixels, truth)
    reference = tmp_path / "reference.nii.gz"
    nib.save(nib.Nifti1Image(values, affine), reference)
    paths, segments = [], []
    for index, image in enumerate(images):
        path = tmp_path / f"ct-{index}.dcm"
        image.save_as(path, enforce_file_format=True)
        paths.append(path)
    for index in range(2):
        path = tmp_path / f"mask-{index}.nii.gz"
        nib.save(nib.Nifti1Image(masks[..., index].astype(np.uint8), affine), path)
        segments.append(dicom_seg.SegmentInput(str(index), f"Imported {index}", ("#ff4040", "#00dcff")[index], path))
    result = dicom_seg.export_segmentation(paths, reference, segments, tmp_path / "export", revision_id="synthetic", source_kind="totalsegmentator")
    return dict(paths=paths, reference=reference, seg=result.files[0].path, truth=masks, images=images, output=tmp_path / "imported")


def run(fixture, **kwargs):
    return dicom_seg_import.import_segmentation(fixture["paths"], fixture["reference"], [fixture["seg"]], fixture["output"], **kwargs)


def modify(fixture, callback):
    ds = pydicom.dcmread(fixture["seg"])
    callback(ds)
    ds.save_as(fixture["seg"], enforce_file_format=True)
    return ds


def test_import_exact_separate_masks_names_colors_and_source_identity(fixture):
    before = {p: hash_file(p) for p in [*fixture["paths"], fixture["reference"], fixture["seg"]]}
    progress = []
    structures = run(fixture, progress=lambda *args: progress.append(args))
    assert progress == [(0, 2), (1, 2), (2, 2)]
    assert len(structures) == 2
    for index, entry in enumerate(structures):
        mask = nib.load(entry["mask_file"])
        np.testing.assert_array_equal(mask.dataobj, fixture["truth"][..., index])
        np.testing.assert_allclose(mask.affine, nib.load(fixture["reference"]).affine, rtol=0, atol=1e-6)
        assert entry["name"] == f"Imported {index}"
        assert entry["color"] == ("#ff4040", "#00dcff")[index]
        assert entry["dicom_seg"]["seg_sha256"] == before[fixture["seg"]]
        assert entry["dicom_seg"]["mask_sha256"] == hash_file(Path(entry["mask_file"]))
        assert entry["dicom_seg"]["segment_number"] == index + 1
        assert Path(entry["mask_file"]).stat().st_mode & 0o777 == 0o600
    assert np.count_nonzero(np.asanyarray(nib.load(structures[0]["mask_file"]).dataobj) & np.asanyarray(nib.load(structures[1]["mask_file"]).dataobj)) == 529
    assert {p: hash_file(p) for p in before} == before


@pytest.mark.parametrize("format_number", [lambda number: f"{number:06d}", lambda number: f"+{number}"])
def test_valid_integer_string_frame_counts_preserve_masks(fixture, format_number):
    modify(fixture, lambda ds: setattr(ds, "NumberOfFrames", format_number(int(ds.NumberOfFrames))))
    for index, entry in enumerate(run(fixture)):
        np.testing.assert_array_equal(nib.load(entry["mask_file"]).dataobj, fixture["truth"][..., index])


def test_geometry_only_frames_and_shuffled_sparse_order(fixture):
    def change(ds):
        pixels = ds.pixel_array
        order = np.arange(len(pixels))[::-1]
        ds.PerFrameFunctionalGroupsSequence = [ds.PerFrameFunctionalGroupsSequence[int(i)] for i in order]
        ds.PixelData = pack_bits(pixels[order])
        for group in ds.PerFrameFunctionalGroupsSequence:
            del group.DerivationImageSequence

    modify(fixture, change)
    for index, entry in enumerate(run(fixture)):
        np.testing.assert_array_equal(nib.load(entry["mask_file"]).dataobj, fixture["truth"][..., index])


def test_cropped_reflected_frames_with_non_byte_aligned_sizes(fixture):
    def change(ds):
        pixels = ds.pixel_array[:, 11:38, 13:50][:, ::-1, ::-1]
        shared = ds.SharedFunctionalGroupsSequence[0]
        directions = np.asarray(shared.PlaneOrientationSequence[0].ImageOrientationPatient).reshape(2, 3)
        spacing = np.asarray(shared.PixelMeasuresSequence[0].PixelSpacing)
        for group in ds.PerFrameFunctionalGroupsSequence:
            position = np.asarray(group.PlanePositionSequence[0].ImagePositionPatient)
            group.PlanePositionSequence[0].ImagePositionPatient = (position + 49 * spacing[1] * directions[0] + 37 * spacing[0] * directions[1]).tolist()
        shared.PlaneOrientationSequence[0].ImageOrientationPatient = (-directions).ravel().tolist()
        ds.Rows, ds.Columns = pixels.shape[1:]
        ds.PixelData = pack_bits(pixels)

    modify(fixture, change)
    # Only the cropped footprint contributes voxels; holes remain independent.
    for index, entry in enumerate(run(fixture)):
        expected = fixture["truth"][..., index].copy()
        expected[:14] = 0  # Native i is reversed source column.
        expected[51:] = 0
        expected[:, :, :11] = 0
        expected[:, :, 38:] = 0
        np.testing.assert_array_equal(nib.load(entry["mask_file"]).dataobj, expected)


@pytest.mark.parametrize("field", ["StudyInstanceUID", "FrameOfReferenceUID"])
def test_reject_wrong_study_or_frame(fixture, field):
    modify(fixture, lambda ds: setattr(ds, field, generate_uid()))
    with pytest.raises(ValueError, match="study and coordinate frame"):
        run(fixture)
    assert not fixture["output"].exists()


@pytest.mark.parametrize(
    "change, message",
    [
        (lambda ds: setattr(ds, "PatientID", "OTHER-SYNTHETIC"), "patient identity"),
        (lambda ds: setattr(ds.ReferencedSeriesSequence[0], "SeriesInstanceUID", generate_uid()), "different source CT series"),
        (lambda ds: setattr(ds.ReferencedSeriesSequence[0].ReferencedInstanceSequence[0], "ReferencedSOPInstanceUID", generate_uid()), "foreign"),
        (lambda ds: delattr(ds, "ReferencedSeriesSequence"), "functional-group"),
        (lambda ds: setattr(ds, "SegmentationType", "FRACTIONAL"), "Only BINARY"),
        (lambda ds: setattr(ds, "SegmentationType", "LABELMAP"), "Only BINARY"),
        (lambda ds: setattr(ds, "BitsAllocated", 8), "binary pixel encoding"),
        (lambda ds: setattr(ds, "NumberOfFrames", int(ds.NumberOfFrames) + 1), "length|metadata"),
        (lambda ds: setattr(ds, "PixelData", ds.PixelData + b"\x00\x00"), "Pixel Data length"),
        (lambda ds: setattr(ds.PerFrameFunctionalGroupsSequence[0].SegmentIdentificationSequence[0], "ReferencedSegmentNumber", 99), "unknown segment"),
        (
            lambda ds: setattr(
                ds.PerFrameFunctionalGroupsSequence[0].DerivationImageSequence[0].SourceImageSequence[0], "ReferencedSOPInstanceUID", generate_uid()
            ),
            "source reference",
        ),
    ],
)
def test_reject_unsafe_metadata_and_wrong_source(fixture, change, message):
    modify(fixture, change)
    with pytest.raises(ValueError, match=message):
        run(fixture)
    assert not fixture["output"].exists()


def test_reject_shifted_resampled_or_outside_geometry(fixture):
    original = pydicom.dcmread(fixture["seg"])
    for offset in (0.25, 1000):
        ds = deepcopy(original)
        ds.PerFrameFunctionalGroupsSequence[0].PlanePositionSequence[0].ImagePositionPatient[0] += offset
        ds.save_as(fixture["seg"], enforce_file_format=True)
        with pytest.raises(ValueError, match="native CT voxels"):
            run(fixture)
    ds = deepcopy(original)
    ds.SharedFunctionalGroupsSequence[0].PixelMeasuresSequence[0].PixelSpacing[0] *= 2
    ds.save_as(fixture["seg"], enforce_file_format=True)
    with pytest.raises(ValueError, match="reference grid|native CT"):
        run(fixture)


def test_reject_duplicate_plane_and_shared_per_frame_ambiguity(fixture):
    original = pydicom.dcmread(fixture["seg"])
    ds = deepcopy(original)
    ds.PerFrameFunctionalGroupsSequence[1] = deepcopy(ds.PerFrameFunctionalGroupsSequence[0])
    ds.save_as(fixture["seg"], enforce_file_format=True)
    with pytest.raises(ValueError, match="repeats a segment"):
        run(fixture)
    ds = deepcopy(original)
    ds.SharedFunctionalGroupsSequence[0].SegmentIdentificationSequence = deepcopy(ds.PerFrameFunctionalGroupsSequence[0].SegmentIdentificationSequence)
    ds.save_as(fixture["seg"], enforce_file_format=True)
    with pytest.raises(ValueError, match="repeats a functional group"):
        run(fixture)


def test_cancellation_and_changed_inputs_never_publish(fixture):
    with pytest.raises(InterruptedError):
        run(fixture, cancelled=lambda: True)
    assert not fixture["output"].exists()

    def progress(done, total):
        if done == 1:
            with fixture["seg"].open("ab") as stream:
                stream.write(b"\x00\x00")

    with pytest.raises(ValueError, match="changed during import"):
        run(fixture, progress=progress)
    assert not fixture["output"].exists()
    assert not list(fixture["output"].parent.glob(".dicom-seg-import-*"))


def test_bounds_before_pixel_decode_and_existing_destination(fixture, monkeypatch):
    monkeypatch.setattr(dicom_seg_import, "_pixel_frames", lambda *a, **k: pytest.fail("must reject before pixel decoding"))
    monkeypatch.setattr(dicom_seg_import, "MAX_VOXELS", 1)
    with pytest.raises(ValueError, match="bounded import grid"):
        run(fixture)
    monkeypatch.setattr(dicom_seg_import, "MAX_VOXELS", 512 * 1024**2)
    monkeypatch.setattr(dicom_seg_import, "MAX_FRAME_PIXELS", 1)
    with pytest.raises(ValueError, match="bounded decoded frame"):
        run(fixture)
    fixture["output"].mkdir()
    sentinel = fixture["output"] / "keep.txt"
    sentinel.write_text("kept")
    with pytest.raises(ValueError, match="new DICOM SEG import directory"):
        run(fixture)
    assert sentinel.read_text() == "kept"


def test_reject_duplicate_sop_and_unsupported_transfer_syntax(fixture):
    copy = fixture["seg"].with_name("copy.dcm")
    copy.write_bytes(fixture["seg"].read_bytes())
    with pytest.raises(ValueError, match="repeated SOP"):
        dicom_seg_import.import_segmentation(fixture["paths"], fixture["reference"], [fixture["seg"], copy], fixture["output"])
    modify(fixture, lambda ds: setattr(ds.file_meta, "TransferSyntaxUID", DeflatedExplicitVRLittleEndian))
    with pytest.raises(ValueError, match="uncompressed little-endian"):
        run(fixture)


def test_sheared_ct_stack_preserves_full_affine_without_resampling(fixture, tmp_path):
    reference = nib.load(fixture["reference"])
    affine = reference.affine.copy()
    shear_lps = np.array([0.12, -0.08, 0.04])
    affine[:3, 1] += shear_lps * [-1, -1, 1]
    nib.save(nib.Nifti1Image(np.asanyarray(reference.dataobj), affine), fixture["reference"])
    for index, path in enumerate(fixture["paths"]):
        image = pydicom.dcmread(path)
        image.ImagePositionPatient = (np.asarray(image.ImagePositionPatient) + index * shear_lps).tolist()
        image.save_as(path, enforce_file_format=True)
    segments = []
    for index in range(2):
        path = tmp_path / f"sheared-mask-{index}.nii.gz"
        nib.save(nib.Nifti1Image(fixture["truth"][..., index].astype(np.uint8), affine), path)
        segments.append(dicom_seg.SegmentInput(str(index), f"Sheared {index}", "#ff4040", path))
    result = dicom_seg.export_segmentation(
        fixture["paths"], fixture["reference"], segments, tmp_path / "sheared-export", revision_id="sheared", source_kind="direct_edit"
    )
    fixture["seg"] = result.files[0].path
    for index, entry in enumerate(run(fixture)):
        restored = nib.load(entry["mask_file"])
        np.testing.assert_array_equal(restored.dataobj, fixture["truth"][..., index])
        np.testing.assert_allclose(restored.affine, nib.load(fixture["reference"]).affine, rtol=0, atol=1e-6)


def test_transposed_seg_pixels_and_per_frame_geometry(fixture):
    def change(ds):
        pixels = ds.pixel_array.transpose(0, 2, 1)
        shared = ds.SharedFunctionalGroupsSequence[0]
        directions = np.asarray(shared.PlaneOrientationSequence[0].ImageOrientationPatient).reshape(2, 3)[::-1].ravel().tolist()
        spacing = list(shared.PixelMeasuresSequence[0].PixelSpacing)[::-1]
        for group in ds.PerFrameFunctionalGroupsSequence:
            group.PlaneOrientationSequence = deepcopy(shared.PlaneOrientationSequence)
            group.PlaneOrientationSequence[0].ImageOrientationPatient = directions
            group.PixelMeasuresSequence = deepcopy(shared.PixelMeasuresSequence)
            group.PixelMeasuresSequence[0].PixelSpacing = spacing
        del shared.PlaneOrientationSequence
        del shared.PixelMeasuresSequence
        ds.Rows, ds.Columns = pixels.shape[1:]
        ds.PixelData = pack_bits(pixels)

    modify(fixture, change)
    for index, entry in enumerate(run(fixture)):
        np.testing.assert_array_equal(nib.load(entry["mask_file"]).dataobj, fixture["truth"][..., index])


def test_empty_segment_and_multiple_file_bundle_keep_distinct_identity(fixture, tmp_path):
    ds = pydicom.dcmread(fixture["seg"])
    empty = deepcopy(ds.SegmentSequence[0])
    empty.SegmentNumber, empty.SegmentLabel = 10, "Empty source segment"
    ds.SegmentSequence.append(empty)
    ds.save_as(fixture["seg"], enforce_file_format=True)
    other = deepcopy(ds)
    other.SOPInstanceUID = other.file_meta.MediaStorageSOPInstanceUID = generate_uid()
    path = tmp_path / "other-seg.dcm"
    other.save_as(path, enforce_file_format=True)
    structures = dicom_seg_import.import_segmentation(fixture["paths"], fixture["reference"], [fixture["seg"], path], fixture["output"])
    assert len(structures) == len({entry["id"] for entry in structures}) == 6
    for entry in structures:
        if entry["dicom_seg"]["segment_number"] == 10:
            assert not np.any(np.asanyarray(nib.load(entry["mask_file"]).dataobj))


def test_no_dense_ct_or_full_seg_pixel_array_and_one_mask_at_a_time(fixture, monkeypatch):
    real = dicom_seg_import._pixel_frames
    calls = []

    def bounded(item, frames):
        calls.append(len(frames))
        yield from real(item, frames)

    monkeypatch.setattr(dicom_seg_import, "_pixel_frames", bounded)
    monkeypatch.setattr(pydicom.Dataset, "pixel_array", property(lambda _: pytest.fail("must not decode the complete SEG")))
    monkeypatch.setattr(nib.Nifti1Image, "get_fdata", lambda *a, **k: pytest.fail("must not decode source CT"))
    assert len(run(fixture)) == 2
    assert len(calls) == 2 and all(0 < n < 24 for n in calls)


def test_shared_segment_identification_and_implicit_vr(fixture):
    def change(ds):
        frames = [i for i, group in enumerate(ds.PerFrameFunctionalGroupsSequence) if int(group.SegmentIdentificationSequence[0].ReferencedSegmentNumber) == 1]
        pixels = ds.pixel_array[frames]
        ds.SegmentSequence = [ds.SegmentSequence[0]]
        ds.PerFrameFunctionalGroupsSequence = [ds.PerFrameFunctionalGroupsSequence[i] for i in frames]
        ds.SharedFunctionalGroupsSequence[0].SegmentIdentificationSequence = deepcopy(ds.PerFrameFunctionalGroupsSequence[0].SegmentIdentificationSequence)
        for group in ds.PerFrameFunctionalGroupsSequence:
            del group.SegmentIdentificationSequence
        ds.NumberOfFrames = len(frames)
        ds.PixelData = pack_bits(pixels)
        ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian

    modify(fixture, change)
    structures = run(fixture)
    assert len(structures) == 1
    np.testing.assert_array_equal(nib.load(structures[0]["mask_file"]).dataobj, fixture["truth"][..., 0])


@pytest.mark.parametrize("limit", ["MAX_SEGMENTS", "MAX_FRAMES", "MAX_HEADER_BYTES", "MAX_FILE_BYTES"])
def test_metadata_and_bundle_limits_reject_before_allocating_masks(fixture, monkeypatch, limit):
    monkeypatch.setattr(dicom_seg_import, limit, 1)
    monkeypatch.setattr(dicom_seg_import, "_pixel_frames", lambda *a, **k: pytest.fail("must reject before mask decoding"))
    with pytest.raises(ValueError):
        run(fixture)
    assert not fixture["output"].exists()


def test_missing_or_invalid_reference_inputs_have_public_errors(fixture):
    fixture["reference"].write_bytes(b"invalid synthetic reference")
    with pytest.raises(ValueError, match="cannot be decoded"):
        run(fixture)
    assert not fixture["output"].exists()


def test_initial_fingerprint_race_has_no_path_in_public_error(fixture, monkeypatch):
    real = dicom_seg_import.hash_file

    def racing(path):
        if path == fixture["seg"]:
            raise FileNotFoundError("SYNTHETIC PRIVATE PATH MUST NOT APPEAR")
        return real(path)

    monkeypatch.setattr(dicom_seg_import, "hash_file", racing)
    with pytest.raises(ValueError, match="unavailable or changed") as error:
        run(fixture)
    assert "PRIVATE PATH" not in str(error.value)
    assert not fixture["output"].exists()


def test_repeated_transfer_syntax_rejected_before_pydicom_dataset_read(fixture, monkeypatch):
    import struct

    data = fixture["seg"].read_bytes()
    uid = str(DeflatedExplicitVRLittleEndian).encode()
    if len(uid) % 2:
        uid += b"\x00"
    element = struct.pack("<HH2sH", 2, 0x10, b"UI", len(uid)) + uid
    # Appending a second transfer-syntax declaration inside file metadata must
    # not let pydicom perform whole-dataset deflate expansion after preflight.
    marker = struct.pack("<HH", 2, 0x12)
    offset = data.index(marker, 132)
    fixture["seg"].write_bytes(data[:offset] + element + data[offset:])
    monkeypatch.setattr(dicom_seg_import, "read_partial", lambda *a, **k: pytest.fail("must reject before pydicom dataset parsing"))
    with pytest.raises(ValueError, match="repeats its file transfer syntax"):
        run(fixture)


def test_concatenated_instances_are_not_silently_split_into_new_segments(fixture):
    modify(fixture, lambda ds: setattr(ds, "ConcatenationUID", generate_uid()))
    with pytest.raises(ValueError, match="reassembly"):
        run(fixture)
