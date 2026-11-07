"""Exact source-grid export, overlap, provenance and bounded-memory gates."""

from dataclasses import FrozenInstanceError, replace
import copy
from pathlib import Path
import shutil
import weakref

import nibabel as nib
import numpy as np
import pydicom
import pytest

from synthetic_dicom import as_classic_mr, native_grid, source_fixture
from scan3d import dicom, dicom_seg
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
        segments.append(dicom_seg.SegmentInput(name, name.capitalize(), color, path, automatic=True))
    return dict(paths=paths, images=images, reference=reference, segments=segments, truth=truth, output=tmp_path / "export")


@pytest.fixture
def mr_fixture(fixture):
    for image, path in zip(fixture["images"], fixture["paths"], strict=True):
        as_classic_mr(image).save_as(path, enforce_file_format=True)
    return fixture


def export(fixture, **kwargs):
    return dicom_seg.export_segmentation(
        fixture["paths"][::-1],
        fixture["reference"],
        fixture["segments"],
        fixture["output"],
        revision_id="revision-test",
        **kwargs,
    )


def independent_pixels(path, images, segment_number=1):
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
        assert "SegmentIdentificationSequence" not in shared
        identification = group.SegmentIdentificationSequence
        number = int(identification[0].ReferencedSegmentNumber)
        assert 1 <= number <= len(seg.SegmentSequence)
        assert np.allclose(group.PlanePositionSequence[0].ImagePositionPatient, images[index].ImagePositionPatient, rtol=0, atol=1e-6)
        assert np.allclose(shared.PlaneOrientationSequence[0].ImageOrientationPatient, images[index].ImageOrientationPatient, rtol=0, atol=1e-6)
        assert np.allclose(shared.PixelMeasuresSequence[0].PixelSpacing, images[index].PixelSpacing, rtol=0, atol=1e-6)
        if number == segment_number:
            result[index] = frame
    return seg, result


def test_each_segment_names_its_own_model_in_a_shared_revision(fixture):
    block, ring = fixture["segments"]
    segments = (replace(block, id="mrsegmentator:liver"), replace(ring, id="total:liver"))
    result = export({**fixture, "segments": segments})
    seg = pydicom.dcmread(result.files[0].path)
    numbers = {entry.structure_id: entry.segment_number for entry in result.files[0].segments}
    assert seg.SegmentSequence[numbers["mrsegmentator:liver"] - 1].SegmentAlgorithmName == "MRSegmentator"
    assert seg.SegmentSequence[numbers["total:liver"] - 1].SegmentAlgorithmName == "TotalSegmentator"
    assert {item.SegmentAlgorithmType for item in seg.SegmentSequence} == {"AUTOMATIC"}


def test_exact_independent_masks_preserve_overlap_and_source_geometry(fixture):
    before = {path: hash_file(path) for path in [*fixture["paths"], fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]}
    progress = []
    result = export(fixture, progress=lambda *args: progress.append(args))
    assert progress == [(0, 2), (1, 2), (2, 2)]
    assert len(result.files) == 1
    instance = result.files[0]
    assert [item.structure_id for item in instance.segments] == ["block", "ring"]
    outputs = {}
    for entry in instance.segments:
        seg, pixels = independent_pixels(instance.path, fixture["images"], entry.segment_number)
        index = 1 if entry.structure_id == "block" else 0
        assert np.array_equal(pixels, fixture["truth"][..., index])
        assert seg.SeriesInstanceUID == result.series_instance_uid
        assert seg.SeriesDescription == f"3DScan segmentations {result.revision_id[:8]}"
        assert seg.StudyInstanceUID == result.study_instance_uid == fixture["images"][0].StudyInstanceUID
        assert seg.StudyDescription == fixture["images"][0].StudyDescription
        assert seg.FrameOfReferenceUID == result.frame_of_reference_uid == fixture["images"][0].FrameOfReferenceUID
        assert seg.SegmentSequence[0].SegmentAlgorithmType == "AUTOMATIC"
        assert seg.SegmentSequence[0].SegmentAlgorithmName == "TotalSegmentator"
        assert int(seg.NumberOfFrames) < len(fixture["images"]) * len(instance.segments)
        assert instance.sha256 == hash_file(instance.path) and instance.size == instance.path.stat().st_size
        assert entry.voxel_count == int(pixels.sum())
        assert instance.path.stat().st_mode & 0o777 == 0o600
        outputs[entry.structure_id] = pixels
    assert np.count_nonzero(outputs["ring"] & outputs["block"]) == 529
    assert len({entry.sop_instance_uid for entry in result.files}) == 1
    # Unranked, the export cannot say whether segments overlap.
    assert seg.SegmentsOverlap == "UNDEFINED" and seg.ContentDescription == "Exported voxel masks; clinical review status is unchanged"
    assert result.reference_sha256 == before[fixture["reference"]]
    assert {path: hash_file(path) for path in before} == before
    with pytest.raises(FrozenInstanceError):
        result.revision_id = "changed"


def test_per_frame_segment_discovery_and_highdicom_readback_preserve_a_sparse_mask(fixture):
    import highdicom as hd

    truth = fixture["truth"].copy()
    truth[np.arange(len(truth)) != 5] = False
    for segment in fixture["segments"]:
        image = nib.load(segment.mask_path)
        values = np.asanyarray(image.dataobj).copy()
        # The fixture's native order is reversed-column, source-slice, row.
        values[:, np.arange(values.shape[1]) != 5, :] = 0
        nib.save(nib.Nifti1Image(values, image.affine), segment.mask_path)
    result = export(fixture)
    source_ids = [str(image.SOPInstanceUID) for image in fixture["images"]]
    instance = result.files[0]
    for entry in instance.segments:
        index = 1 if entry.structure_id == "block" else 0
        expected = truth[..., index]
        # This reader requires identification on every frame, the layout OsiriX accepted.
        dataset, decoded = independent_pixels(instance.path, fixture["images"], entry.segment_number)
        np.testing.assert_array_equal(decoded, expected)
        assert int(dataset.NumberOfFrames) == 2
        restored = hd.seg.segread(instance.path)
        native = restored.get_pixels_by_source_instance(source_ids, segment_numbers=[entry.segment_number], assert_missing_frames_are_empty=True)
        np.testing.assert_array_equal(native[..., 0], expected)
        assert len(restored.SegmentSequence) == 2


@pytest.mark.parametrize("winner,loser", [("ring", "block"), ("block", "ring")])
def test_ranked_export_keeps_each_overlap_voxel_only_in_the_higher_rank(fixture, winner, loser):
    import highdicom as hd

    saved = {segment.id: hash_file(segment.mask_path) for segment in fixture["segments"]}
    truth = {"ring": fixture["truth"][..., 0], "block": fixture["truth"][..., 1]}
    result = export(fixture, overlap_priority=[winner, loser])
    instance = result.files[0]
    first, second = instance.segments
    assert (first.structure_id, first.segment_number, first.removed_voxel_count) == (winner, 1, 0)
    assert (second.structure_id, second.segment_number, second.removed_voxel_count) == (loser, 2, 529)
    seg, kept = independent_pixels(instance.path, fixture["images"], 1)
    trimmed = independent_pixels(instance.path, fixture["images"], 2)[1]
    np.testing.assert_array_equal(kept, truth[winner])
    np.testing.assert_array_equal(trimmed, truth[loser] & ~truth[winner])
    assert first.voxel_count == int(kept.sum()) and second.voxel_count == int(trimmed.sum())
    assert int(seg.NumberOfFrames) == sum(int(np.count_nonzero(np.any(pixels, axis=(1, 2)))) for pixels in (kept, trimmed))
    assert seg.SegmentsOverlap == "NO"
    assert seg.SeriesDescription == f"3DScan segmentations {result.revision_id[:8]} overlaps resolved"
    assert seg.ContentDescription == "Overlaps resolved by rank; saved masks and review are unchanged"
    assert [item.TrackingID for item in seg.SegmentSequence] == [winner, loser]
    assert [item.SegmentAlgorithmType for item in seg.SegmentSequence] == ["AUTOMATIC", "SEMIAUTOMATIC"]
    # Hashes still name the saved masks, which the export never rewrites.
    assert {item.structure_id: item.mask_sha256 for item in instance.segments} == saved
    assert {segment.id: hash_file(segment.mask_path) for segment in fixture["segments"]} == saved
    restored = hd.seg.segread(instance.path)
    source_ids = [str(image.SOPInstanceUID) for image in fixture["images"]]
    for number, expected in ((1, kept), (2, trimmed)):
        native = restored.get_pixels_by_source_instance(source_ids, segment_numbers=[number], assert_missing_frames_are_empty=True)
        np.testing.assert_array_equal(native[..., 0], expected)


def test_ranked_export_of_separate_masks_changes_no_voxels_or_provenance(fixture):
    ring, block = fixture["segments"]
    image = nib.load(block.mask_path)
    values = np.asanyarray(image.dataobj) & ~np.asanyarray(nib.load(ring.mask_path).dataobj)
    nib.save(nib.Nifti1Image(values, image.affine), block.mask_path)
    instance = export(fixture, overlap_priority=["block", "ring"]).files[0]
    assert [item.removed_voxel_count for item in instance.segments] == [0, 0]
    seg = independent_pixels(instance.path, fixture["images"], 2)[0]
    assert seg.SegmentsOverlap == "NO"
    assert [item.SegmentAlgorithmType for item in seg.SegmentSequence] == ["AUTOMATIC", "AUTOMATIC"]


def test_ranked_export_stops_instead_of_emptying_a_structure(fixture):
    fixture["segments"].append(replace(fixture["segments"][0], id="copy", name="Copy"))
    with pytest.raises(dicom_seg.DicomSegError, match="Overlap rank 3 would lose every voxel"):
        export(fixture, overlap_priority=["ring", "block", "copy"])
    assert not fixture["output"].exists()
    assert not list(fixture["output"].parent.glob(".dicom-seg-*"))


@pytest.mark.parametrize("ranking", [["ring"], ["ring", "ring"], ["ring", "other"]])
def test_ranking_must_name_every_selected_structure_once(fixture, ranking):
    with pytest.raises(dicom_seg.DicomSegError, match="Rank every selected structure"):
        export(fixture, overlap_priority=ranking)
    assert not fixture["output"].exists()


def test_a_single_structure_has_nothing_to_rank(fixture):
    fixture["segments"] = fixture["segments"][:1]
    with pytest.raises(dicom_seg.DicomSegError, match="Rank every selected structure"):
        export(fixture, overlap_priority=["ring"])


def test_a_mask_is_semiautomatic_unless_shown_to_be_the_models_own(fixture):
    segment = fixture["segments"][0]
    fixture["segments"] = [dicom_seg.SegmentInput(segment.id, segment.name, segment.color, segment.mask_path)]
    seg = pydicom.dcmread(export(fixture).files[0].path)
    assert seg.SegmentSequence[0].SegmentAlgorithmType == "SEMIAUTOMATIC"
    assert seg.SegmentSequence[0].SegmentAlgorithmName == "3DScan correction"


def test_each_segment_of_one_instance_states_its_own_provenance(fixture):
    ring, block = fixture["segments"]
    fixture["segments"] = [ring, replace(block, automatic=False)]
    seg = pydicom.dcmread(export(fixture).files[0].path)
    assert {item.TrackingID: (item.SegmentAlgorithmType, item.SegmentAlgorithmName) for item in seg.SegmentSequence} == {
        "ring": ("AUTOMATIC", "TotalSegmentator"),
        "block": ("SEMIAUTOMATIC", "3DScan correction"),
    }


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
    assert len(result.files) == 1 and len(result.files[0].segments) == 2 and previous_mask[0]() is None and previous_projection[0]() is None


def test_eight_segments_share_one_instance_with_continuous_odd_sized_frame_bits(tmp_path):
    import highdicom as hd

    images, _, _ = source_fixture()
    values = np.arange(len(images) * 9, dtype=np.int16).reshape(len(images), 3, 3)
    truth = np.zeros((*values.shape, 8), dtype=bool)
    for number in range(8):
        planes = [4] if number == 7 else [number, (number + 2) % 12, (number + 5) % 12]
        for offset, plane in enumerate(planes):
            truth[plane, (number + offset) % 3, (2 * number + offset) % 3, number] = True
            truth[plane, 1, 1, number] = True
    assert np.any(truth.sum(axis=-1) > 1)
    paths = []
    for index, image in enumerate(images):
        image.Rows = image.Columns = 3
        image.PixelData = values[index].astype("<i2").tobytes()
        path = tmp_path / f"source-{index:02d}.dcm"
        image.save_as(path, enforce_file_format=True)
        paths.append(path)
    native_values, masks, affine = native_grid(images, values, truth)
    reference = tmp_path / "reference.nii.gz"
    nib.save(nib.Nifti1Image(native_values, affine), reference)
    segments = []
    for index in range(8):
        path = tmp_path / f"mask-{index}.nii.gz"
        nib.save(nib.Nifti1Image(masks[..., index].astype(np.uint8), affine), path)
        segments.append(dicom_seg.SegmentInput(f"segment-{index}", f"Synthetic mask {index}", "#40a0ff", path))
    result = dicom_seg.export_segmentation(paths[::-1], reference, segments[::-1], tmp_path / "export", revision_id="eight-segments")
    assert len(result.files) == 1 and len(result.files[0].segments) == 8
    instance = result.files[0]
    dataset = pydicom.dcmread(instance.path)
    assert len(dataset.SegmentSequence) == 8 and int(dataset.NumberOfFrames) == 22
    assert dataset.SegmentsOverlap == "UNDEFINED"
    assert [int(item.SegmentNumber) for item in dataset.SegmentSequence] == list(range(1, 9))
    source_ids = [str(image.SOPInstanceUID) for image in images]
    source_indices = {uid: index for index, uid in enumerate(source_ids)}
    ranks, expected_frames, dimension_values = {}, [], set()
    for frame in dataset.PerFrameFunctionalGroupsSequence:
        number = int(frame.SegmentIdentificationSequence[0].ReferencedSegmentNumber)
        source = str(frame.DerivationImageSequence[0].SourceImageSequence[0].ReferencedSOPInstanceUID)
        dimensions = tuple(int(value) for value in frame.FrameContentSequence[0].DimensionIndexValues)
        assert dimensions[0] == number
        assert source not in ranks or ranks[source] == dimensions[1]
        ranks[source] = dimensions[1]
        assert dimensions not in dimension_values
        dimension_values.add(dimensions)
        expected_frames.append(truth[source_indices[source], :, :, number - 1])
    assert set(ranks.values()) == set(range(1, len(ranks) + 1))
    assert {str(item.DimensionOrganizationUID) for item in dataset.DimensionIndexSequence} == {
        str(dataset.DimensionOrganizationSequence[0].DimensionOrganizationUID)
    }
    packed = np.packbits(np.stack(expected_frames).reshape(-1), bitorder="little").tobytes()
    expected_bytes = packed + (b"\x00" if len(packed) % 2 else b"")
    assert dataset.PixelData == expected_bytes and len(expected_bytes) == 26
    for entry in instance.segments:
        _, actual = independent_pixels(instance.path, images, entry.segment_number)
        np.testing.assert_array_equal(actual, truth[..., entry.segment_number - 1])
        assert entry.voxel_count == int(actual.sum())
        assert entry.mask_sha256 == hash_file(segments[entry.segment_number - 1].mask_path)
    decoded = hd.seg.segread(instance.path).get_pixels_by_source_instance(source_ids, segment_numbers=list(range(1, 9)), assert_missing_frames_are_empty=True)
    np.testing.assert_array_equal(decoded, truth)
    assert list(instance.path.parent.iterdir()) == [instance.path]


@pytest.mark.parametrize(
    "bound,limit,match", [("_MAX_ENCODED_BYTES", 100, "file size"), ("_MAX_ENCODED_BYTES", 10_000, "file size"), ("_MAX_ENCODED_FRAMES", 1, "frame count")]
)
def test_packed_pixels_final_metadata_and_frame_count_respect_the_instance_bounds(fixture, monkeypatch, bound, limit, match):
    monkeypatch.setattr(dicom_seg, bound, limit)
    with pytest.raises(dicom_seg.DicomSegError, match=match):
        export(fixture)
    assert not fixture["output"].exists()
    assert not list(fixture["output"].parent.glob(".dicom-seg-*"))


@pytest.mark.parametrize("order,flip", [((0, 1, 2), True), ((2, 0, 1), False), ((1, 2, 0), True)])
def test_reference_axis_orders_and_reflections_match_patient_pixels(fixture, order, flip):
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


@pytest.mark.parametrize(
    "slice_count,spacing,position_decimals,origin_z",
    [(12, 0.3333, 3, -20), (500, 0.5, None, -1200)],
)
def test_real_converter_rounding_preserves_every_ct_sample_and_seg_bit(tmp_path, monkeypatch, slice_count, spacing, position_decimals, origin_z):
    bundled = Path(__file__).resolve().parents[1] / ".tools/bin/dcm2niix"
    executable = str(bundled) if bundled.is_file() else shutil.which("dcm2niix")
    if executable is None:
        pytest.skip("Native dcm2niix is not installed")
    monkeypatch.setenv("SCAN3D_DCM2NIIX", executable)
    template = source_fixture()[0][0]
    direction = np.round(np.asarray(template.ImageOrientationPatient, dtype=float), 6)
    normal = np.cross(direction[:3], direction[3:])
    values = np.arange(slice_count * 8 * 8, dtype=np.int16).reshape(slice_count, 8, 8)
    paths, images = [], []
    for index, pixels in enumerate(values):
        image = copy.deepcopy(template)
        image.SOPInstanceUID = image.file_meta.MediaStorageSOPInstanceUID = pydicom.uid.generate_uid()
        image.Rows = image.Columns = 8
        image.InstanceNumber, image.ImagesInAcquisition = index + 1, slice_count
        image.ImageOrientationPatient = [pydicom.valuerep.format_number_as_ds(float(value)) for value in direction]
        position = np.array([44, -70, origin_z]) + index * spacing * normal
        image.ImagePositionPatient = [
            pydicom.valuerep.format_number_as_ds(float(value)) if position_decimals is None else f"{value:.{position_decimals}f}" for value in position
        ]
        image.SliceThickness = image.SpacingBetweenSlices = str(spacing)
        image.PixelData = pixels.astype("<i2").tobytes()
        path = tmp_path / f"source-{index:04d}.dcm"
        image.save_as(path, enforce_file_format=True)
        paths.append(path)
        images.append(image)
    before = {path: hash_file(path) for path in paths}
    reference_path = dicom.convert_series(paths, tmp_path / "converted")
    reference = nib.load(reference_path)
    native_values = np.asanyarray(reference.dataobj)
    mask_path = tmp_path / "mask.nii.gz"
    nib.save(nib.Nifti1Image((native_values % 11 < 2).astype(np.uint8), reference.affine), mask_path)

    # Check every source pixel independently, including the accumulated spacing error an earlier 0.001-voxel gate rejected.
    inverse = np.linalg.inv(reference.affine)
    row, column = np.indices((8, 8))
    seen = np.zeros(reference.shape, dtype=np.uint8)
    max_error = 0.0
    for index, image in enumerate(images):
        lps = (
            np.asarray(image.ImagePositionPatient, dtype=float)
            + column[..., None] * float(image.PixelSpacing[1]) * direction[:3]
            + row[..., None] * float(image.PixelSpacing[0]) * direction[3:]
        )
        coordinates = (lps * [-1, -1, 1]) @ inverse[:3, :3].T + inverse[:3, 3]
        integer = np.rint(coordinates).astype(int)
        max_error = max(max_error, float(np.abs(coordinates - integer).max()))
        indices = tuple(integer.transpose(2, 0, 1))
        np.testing.assert_array_equal(native_values[indices], values[index])
        seen[indices] += 1
    assert 1e-3 < max_error < 0.05
    assert np.all(seen == 1)
    result = dicom_seg.export_segmentation(
        paths[::-1],
        reference_path,
        [dicom_seg.SegmentInput("rounding", "Synthetic rounding mask", "#ff4040", mask_path)],
        tmp_path / "export",
        revision_id="synthetic-rounded-source",
    )
    _, decoded = independent_pixels(result.files[0].path, images)
    np.testing.assert_array_equal(decoded, values % 11 < 2)
    assert {path: hash_file(path) for path in paths} == before


@pytest.mark.parametrize("scale,axis,offset", [(1.0, 1, 0.04), (0.1, 0, 0.06)])
def test_reference_rounding_limits_apply_in_both_voxels_and_millimetres(fixture, scale, axis, offset):
    # Thick slices isolate the millimetre limit and fine ones the voxel limit; masks move too, so only the source check can refuse.
    for path in fixture["paths"]:
        image = pydicom.dcmread(path)
        for field in ("ImagePositionPatient", "PixelSpacing"):
            setattr(image, field, [pydicom.valuerep.format_number_as_ds(float(value) * scale) for value in getattr(image, field)])
        image.SliceThickness = pydicom.valuerep.format_number_as_ds(float(image.SliceThickness) * scale)
        image.save_as(path, enforce_file_format=True)
    for path in [fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]:
        image = nib.load(path)
        affine = image.affine.copy()
        affine[:3] *= scale
        displacement = offset * affine[:3, axis]
        affine[:3, 3] += displacement
        nib.save(nib.Nifti1Image(np.asanyarray(image.dataobj), affine), path)
    assert (offset < 0.05) != (np.linalg.norm(displacement) < 0.05)
    with pytest.raises(dicom_seg.DicomSegError, match="origins"):
        export(fixture)
    assert not fixture["output"].exists()


def test_rounding_tolerance_cannot_hide_far_corner_spacing_drift(fixture):
    for path in [fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]:
        image = nib.load(path)
        affine = image.affine.copy()
        delta = 0.0009 * affine[:3, 0]
        affine[:3, 0] += delta
        # Every plane origin stays on its voxel; only the far corner drifts.
        affine[:3, 3] -= (image.shape[0] - 1) * delta
        nib.save(nib.Nifti1Image(np.asanyarray(image.dataobj), affine), path)
    with pytest.raises(dicom_seg.DicomSegError, match="pixels"):
        export(fixture)
    assert not fixture["output"].exists()


def test_classic_mr_source_exports_exact_masks(mr_fixture):
    instance = export(mr_fixture).files[0]
    for entry in instance.segments:
        seg, pixels = independent_pixels(instance.path, mr_fixture["images"], entry.segment_number)
        assert np.array_equal(pixels, mr_fixture["truth"][..., 1 if entry.structure_id == "block" else 0])
    assert {item.ReferencedSOPClassUID for item in seg.ReferencedSeriesSequence[0].ReferencedInstanceSequence} == {pydicom.uid.MRImageStorage}


@pytest.mark.parametrize("problem", ["patient", "frame", "source_class", "modality", "orientation", "duplicate", "irregular"])
def test_inconsistent_or_unsupported_sources_are_rejected(fixture, problem):
    path = fixture["paths"][2]
    image = pydicom.dcmread(path)
    if problem == "patient":
        image.PatientID = "different-synthetic-patient"
    elif problem == "frame":
        image.FrameOfReferenceUID = pydicom.uid.generate_uid()
    elif problem == "source_class":
        image.SOPClassUID = image.file_meta.MediaStorageSOPClassUID = pydicom.uid.EnhancedCTImageStorage
    elif problem == "modality":
        image.Modality = "MR"
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


def test_non_latin_segment_labels_are_preserved_as_utf8(fixture):
    label = "左肺 🫀"
    for path in fixture["paths"]:
        source = pydicom.dcmread(path)
        source.PatientName = "SYNTHETIC^Müller"
        source.StudyDescription = "Étude synthétique"
        source.save_as(path, enforce_file_format=True)
    segment = fixture["segments"][0]
    fixture["segments"] = [dicom_seg.SegmentInput(segment.id, label, segment.color, segment.mask_path)]
    result = export(fixture)
    seg = pydicom.dcmread(result.files[0].path)
    assert seg.SegmentSequence[0].SegmentLabel == label
    assert seg.SpecificCharacterSet == "ISO_IR 192"
    assert seg.PatientName == "SYNTHETIC^Müller"
    assert seg.StudyDescription == "Étude synthétique"


def test_unexpected_reader_errors_do_not_expose_clinical_paths(fixture, monkeypatch):
    def fail(*args, **kwargs):
        raise nib.filebasedimages.ImageFileError("clinical-path-and-private-tags")

    monkeypatch.setattr(dicom_seg.imaging, "_image", fail)
    with pytest.raises(dicom_seg.DicomSegError) as error:
        export(fixture)
    assert "clinical-path-and-private-tags" not in str(error.value)
    assert "original CT and saved masks" in str(error.value)
