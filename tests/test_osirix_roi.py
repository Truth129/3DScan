"""Independent native OsiriX archive readback and atomic export gates."""

from dataclasses import FrozenInstanceError, replace
import os
import plistlib
import struct
from uuid import UUID

import nibabel as nib
import numpy as np
import pydicom
import pytest

from scan3d import osirix_roi
from scan3d.fileio import hash_file
from test_dicom_seg import fixture


def export(fixture, **kwargs):
    return osirix_roi.export_native_rois(
        fixture["paths"][::-1], fixture["reference"], fixture["segments"][::-1], fixture["output"], revision_id="saved-native-revision", **kwargs
    )


def _lz4_block(data, expected):
    """Decode raw LZ4 tokens without importing the writer or its codec."""
    output, cursor = bytearray(), 0
    while cursor < len(data):
        token = data[cursor]
        cursor += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                extra = data[cursor]
                cursor += 1
                literal_length += extra
                if extra != 255:
                    break
        assert cursor + literal_length <= len(data)
        output.extend(data[cursor : cursor + literal_length])
        cursor += literal_length
        if cursor == len(data):
            break
        assert cursor + 2 <= len(data)
        offset = int.from_bytes(data[cursor : cursor + 2], "little")
        cursor += 2
        assert 0 < offset <= len(output)
        match_length = (token & 15) + 4
        if token & 15 == 15:
            while True:
                extra = data[cursor]
                cursor += 1
                match_length += extra
                if extra != 255:
                    break
        assert len(output) + match_length <= expected
        for _ in range(match_length):
            output.append(output[-offset])
    assert len(output) == expected
    return bytes(output)


def _apple_lz4(data, expected):
    output, cursor = bytearray(), 0
    while True:
        magic = data[cursor : cursor + 4]
        cursor += 4
        if magic == b"bv4$":
            break
        assert magic in (b"bv4-", b"bv41")
        decoded_size = struct.unpack_from("<I", data, cursor)[0]
        cursor += 4
        if magic == b"bv41":
            stored_size = struct.unpack_from("<I", data, cursor)[0]
            cursor += 4
            block = data[cursor : cursor + stored_size]
            assert len(block) == stored_size
            output.extend(_lz4_block(block, decoded_size))
            cursor += stored_size
        else:
            output.extend(data[cursor : cursor + decoded_size])
            cursor += decoded_size
        assert len(output) <= expected
    assert cursor == len(data) and len(output) == expected
    return bytes(output)


def _roi_archive(dataset):
    payload = dataset.EncapsulatedDocument
    assert payload.startswith(b"bplist00")
    try:
        archive = plistlib.loads(payload)
    except plistlib.InvalidFileException:
        assert payload[-1:] == b"\0"
        archive = plistlib.loads(payload[:-1])
    assert archive["$archiver"] == "NSKeyedArchiver" and archive["$version"] == 100000
    objects = archive["$objects"]
    assert objects[0] == "$null"

    def dereference(value):
        if isinstance(value, plistlib.UID):
            assert 0 <= value.data < len(objects)
            return objects[value.data]
        return value

    root = dereference(archive["$top"]["root"])
    assert dereference(root["$class"])["$classname"] == "NSArray"
    result = []
    for entry in root["NS.objects"]:
        roi = dereference(entry)
        assert dereference(roi["$class"])["$classname"] == "ROI"
        result.append({key: dereference(value) for key, value in roi.items() if key != "$class"})
    return result


def _plane(roi, shape):
    assert roi["type"] == 20
    assert (roi["textureWidth"], roi["textureHeight"], roi["textureBuffer"]) == (1, 1, b"\0")
    width, height = int(roi["textureWidthCompressed"]), int(roi["textureHeightCompressed"])
    left, top = int(roi["textureUpLeftCornerX"]), int(roi["textureUpLeftCornerY"])
    assert width > 0 and height > 0 and width % 4 == height % 4 == 0
    assert int(roi["textureDownRightCornerX"]) == left + width - 1
    assert int(roi["textureDownRightCornerY"]) == top + height - 1
    assert roi["textureBufferCompressedSize"] == width * height
    raw = _apple_lz4(roi["textureBufferCompressed"], width * height)
    crop = np.frombuffer(raw, dtype=np.uint8).reshape(height, width)
    assert set(np.unique(crop)) <= {0, 255}
    y, x = np.nonzero(crop)
    assert np.all((x + left >= 0) & (x + left < shape[1]) & (y + top >= 0) & (y + top < shape[0]))
    result = np.zeros(shape, dtype=bool)
    result[y + top, x + left] = True
    return result


def independent_masks(result, images):
    source_indices = {str(image.SOPInstanceUID): index for index, image in enumerate(images)}
    masks, identifiers, groups = {}, set(), {}
    for instance in result.files:
        dataset = pydicom.dcmread(instance.path)
        index = source_indices[instance.source_sop_instance_uid]
        source = images[index]
        assert dataset.SOPClassUID == "1.2.840.10008.5.1.4.1.1.88.11" and dataset.Modality == "SR"
        assert dataset.SeriesDescription == "OsiriX ROI SR"
        assert dataset.SeriesNumber == 5002 and dataset.InstanceNumber == 0
        assert dataset.SOPInstanceUID == dataset.file_meta.MediaStorageSOPInstanceUID == instance.sop_instance_uid
        assert dataset.SeriesInstanceUID == instance.series_instance_uid
        assert dataset.StudyInstanceUID == source.StudyInstanceUID == result.study_instance_uid
        assert dataset.StudyDescription == source.StudyDescription
        assert "PixelData" not in dataset
        evidence = dataset.CurrentRequestedProcedureEvidenceSequence
        assert len(evidence) == 1 and evidence[0].StudyInstanceUID == source.StudyInstanceUID
        series = evidence[0].ReferencedSeriesSequence
        assert len(series) == 1 and series[0].SeriesInstanceUID == source.SeriesInstanceUID
        assert len(series[0].ReferencedSOPSequence) == 1
        links = [series[0].ReferencedSOPSequence[0]]
        image_nodes = [item for item in dataset.ContentSequence if item.ValueType == "IMAGE"]
        assert len(image_nodes) == 1
        assert len(image_nodes[0].ReferencedSOPSequence) == 1
        links.append(image_nodes[0].ReferencedSOPSequence[0])
        for link in links:
            assert link.ReferencedSOPInstanceUID == source.SOPInstanceUID
            assert link.ReferencedSOPClassUID == source.SOPClassUID
            assert "ReferencedFrameNumber" not in link
        rois = _roi_archive(dataset)
        assert len(rois) == len(instance.rois) > 0
        metadata = {item.name: item for item in instance.rois}
        assert len(metadata) == len(rois)
        for roi in rois:
            entry = metadata[roi["name"]]
            assert str(UUID(roi["UUID"])) == str(UUID(entry.roi_uuid))
            assert entry.roi_uuid not in identifiers
            identifiers.add(entry.roi_uuid)
            assert np.isfinite(roi["groupID"]) and roi["groupID"] > 0
            if entry.structure_id in groups:
                assert roi["groupID"] == groups[entry.structure_id]
            groups[entry.structure_id] = roi["groupID"]
            assert roi["savedStudyInstanceUID"] == source.StudyInstanceUID
            assert roi["pixelSpacingX"] == pytest.approx(float(source.PixelSpacing[1]), abs=1e-6)
            assert roi["pixelSpacingY"] == pytest.approx(float(source.PixelSpacing[0]), abs=1e-6)
            # MD 14.3 retains raw LPS x/y when a saved study is bound. Plane
            # identity comes from IMAGE/evidence links, not the unset z value.
            origin = np.fromstring(roi["imageOrigin"].strip("{}"), sep=",")
            np.testing.assert_allclose(origin, np.asarray(source.ImagePositionPatient, dtype=float)[:2], rtol=0, atol=1e-5)
            assert roi["zLocation"] == float(np.finfo(np.float32).tiny)
            pixels = _plane(roi, (int(source.Rows), int(source.Columns)))
            assert entry.voxel_count == int(pixels.sum())
            volume = masks.setdefault(entry.structure_id, np.zeros((len(images), *pixels.shape), dtype=bool))
            assert not volume[index].any()
            volume[index] = pixels
    assert len(set(groups.values())) == len(groups)
    return masks


def assert_unpublished(fixture):
    assert not fixture["output"].exists()
    assert not [path for path in fixture["output"].parent.iterdir() if path.is_dir()]


def test_exact_native_brushes_preserve_holes_overlap_and_source_links(fixture):
    inputs = [*fixture["paths"], fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]
    before = {path: hash_file(path) for path in inputs}
    result = export(fixture)
    assert result.revision_id == "saved-native-revision" and result.encoding_version == 1
    assert len(result.files) == 9 and sum(len(instance.rois) for instance in result.files) == 15
    assert len({instance.sop_instance_uid for instance in result.files}) == len(result.files)
    assert len({instance.source_sop_instance_uid for instance in result.files}) == len(result.files)
    assert {instance.sop_instance_uid for instance in result.files}.isdisjoint(str(image.SOPInstanceUID) for image in fixture["images"])
    assert result.series_instance_uids == tuple(instance.series_instance_uid for instance in result.files)
    assert len(set(result.series_instance_uids)) == len(result.files)
    assert fixture["images"][0].SeriesInstanceUID not in result.series_instance_uids
    assert result.frame_of_reference_uid == fixture["images"][0].FrameOfReferenceUID
    assert result.reference_sha256 == before[fixture["reference"]]
    masks = independent_masks(result, fixture["images"])
    np.testing.assert_array_equal(masks["ring"], fixture["truth"][..., 0])
    np.testing.assert_array_equal(masks["block"], fixture["truth"][..., 1])
    assert int(masks["ring"].sum()) == 2202 and int(masks["block"].sum()) == 2016
    assert np.count_nonzero(masks["ring"] & masks["block"]) == 529
    for instance in result.files:
        assert instance.path.parent == fixture["output"]
        assert instance.sha256 == hash_file(instance.path) and instance.size == instance.path.stat().st_size
        assert instance.path.stat().st_mode & 0o777 == 0o600
    assert set(fixture["output"].iterdir()) == {instance.path for instance in result.files}
    assert {path: hash_file(path) for path in inputs} == before
    with pytest.raises(FrozenInstanceError):
        result.revision_id = "changed"


def test_native_labels_colors_and_fresh_export_identities(fixture):
    fixture["segments"] = [replace(segment, name=f"Synthetic {segment.name} Δ", color="#40a0Ff") for segment in fixture["segments"]]
    first = export(fixture)
    first_masks = independent_masks(first, fixture["images"])
    for instance in first.files:
        for roi in _roi_archive(pydicom.dcmread(instance.path)):
            assert roi["name"] in {segment.name for segment in fixture["segments"]}
            assert (roi["color.red"], roi["color.green"], roi["color.blue"]) == (64 * 257, 160 * 257, 255 * 257)
    fixture["output"] = fixture["output"].with_name("second-export")
    second = export(fixture)
    second_masks = independent_masks(second, fixture["images"])
    assert set(first.series_instance_uids).isdisjoint(second.series_instance_uids)
    assert {item.sop_instance_uid for item in first.files}.isdisjoint(item.sop_instance_uid for item in second.files)
    assert {roi.roi_uuid for item in first.files for roi in item.rois}.isdisjoint(roi.roi_uuid for item in second.files for roi in item.rois)
    for structure_id in first_masks:
        np.testing.assert_array_equal(second_masks[structure_id], first_masks[structure_id])


@pytest.mark.parametrize("order,flip", [((0, 1, 2), True), ((2, 0, 1), False), ((1, 2, 0), True)])
def test_native_axis_orders_and_reflections_preserve_source_pixels(fixture, order, flip):
    for path in [fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]:
        image = nib.load(path)
        values = np.asanyarray(image.dataobj).transpose(order).copy()
        affine = image.affine.copy()
        affine[:3, :3] = affine[:3, list(order)]
        if flip:
            values = values[::-1]
            affine[:3, 3] += affine[:3, 0] * (values.shape[0] - 1)
            affine[:3, 0] *= -1
        nib.save(nib.Nifti1Image(values, affine), path)
    masks = independent_masks(export(fixture), fixture["images"])
    np.testing.assert_array_equal(masks["ring"], fixture["truth"][..., 0])
    np.testing.assert_array_equal(masks["block"], fixture["truth"][..., 1])


def test_constant_gantry_shear_preserves_exact_native_brushes(fixture):
    orientation = np.asarray(fixture["images"][0].ImageOrientationPatient).reshape(2, 3)
    drift = 0.3 * orientation[0] + 0.15 * orientation[1]
    for index, path in enumerate(fixture["paths"]):
        image = pydicom.dcmread(path)
        image.ImagePositionPatient = [pydicom.valuerep.format_number_as_ds(float(value)) for value in np.asarray(image.ImagePositionPatient) + index * drift]
        image.save_as(path, enforce_file_format=True)
        fixture["images"][index] = image
    for path in [fixture["reference"], *(segment.mask_path for segment in fixture["segments"])]:
        image = nib.load(path)
        affine = image.affine.copy()
        affine[:3, 1] += drift * [-1, -1, 1]
        nib.save(nib.Nifti1Image(np.asanyarray(image.dataobj), affine), path)
    masks = independent_masks(export(fixture), fixture["images"])
    np.testing.assert_array_equal(masks["ring"], fixture["truth"][..., 0])
    np.testing.assert_array_equal(masks["block"], fixture["truth"][..., 1])


@pytest.mark.parametrize("row,column", [(0, 0), (47, 63)])
def test_single_edge_voxel_preserves_zero_padding_and_plane_membership(fixture, row, column):
    segment = fixture["segments"][0]
    image = nib.load(segment.mask_path)
    values = np.zeros(image.shape, dtype=np.uint8)
    values[63 - column, 5, row] = 1
    nib.save(nib.Nifti1Image(values, image.affine), segment.mask_path)
    fixture["segments"] = [segment]
    result = export(fixture)
    assert len(result.files) == 1 and len(result.files[0].rois) == 1
    assert result.files[0].source_sop_instance_uid == fixture["images"][5].SOPInstanceUID
    masks = independent_masks(result, fixture["images"])
    expected = np.zeros(fixture["truth"].shape[:3], dtype=bool)
    expected[5, row, column] = True
    np.testing.assert_array_equal(masks[segment.id], expected)


def test_source_ct_pixels_are_never_decoded(fixture, monkeypatch):
    original = pydicom.dcmread
    source_paths = set(fixture["paths"])
    reads = []

    def read(path, *args, **kwargs):
        if path in source_paths:
            reads.append(kwargs.get("stop_before_pixels"))
        return original(path, *args, **kwargs)

    def forbidden(_):
        pytest.fail("Native ROI export must not load CT pixel arrays")

    monkeypatch.setattr(pydicom, "dcmread", read)
    monkeypatch.setattr(pydicom.dataset.Dataset, "pixel_array", property(forbidden))
    export(fixture)
    assert len(reads) == len(source_paths) and all(reads)


@pytest.mark.parametrize("value", [0, 2, float("nan")])
def test_empty_or_nonbinary_masks_fail_without_publishing(fixture, value):
    path = fixture["segments"][0].mask_path
    image = nib.load(path)
    values = np.zeros(image.shape, dtype=np.float32)
    values[0, 0, 0] = value
    nib.save(nib.Nifti1Image(values, image.affine), path)
    with pytest.raises(osirix_roi.NativeROIError):
        export(fixture)
    assert_unpublished(fixture)


@pytest.mark.parametrize("target", ["mask", "reference"])
def test_mismatched_mask_or_source_grid_is_refused(fixture, target):
    paths = [fixture["segments"][0].mask_path] if target == "mask" else [fixture["reference"], *(item.mask_path for item in fixture["segments"])]
    for path in paths:
        image = nib.load(path)
        affine = image.affine.copy()
        affine[:3, 3] += affine[:3, 0] * 0.25
        nib.save(nib.Nifti1Image(np.asanyarray(image.dataobj), affine), path)
    with pytest.raises(osirix_roi.NativeROIError):
        export(fixture)
    assert_unpublished(fixture)


@pytest.mark.parametrize("changes", [{"name": ""}, {"name": "bad\\name"}, {"color": "#gg0000"}])
def test_invalid_display_metadata_is_refused(fixture, changes):
    fixture["segments"][0] = replace(fixture["segments"][0], **changes)
    with pytest.raises(osirix_roi.NativeROIError):
        export(fixture)
    assert_unpublished(fixture)


@pytest.mark.parametrize("selection", ["empty", "duplicate", "missing"])
def test_invalid_structure_selection_is_refused(fixture, selection):
    if selection == "empty":
        fixture["segments"] = []
    elif selection == "duplicate":
        fixture["segments"] = [fixture["segments"][0]] * 2
    else:
        fixture["segments"][0] = replace(fixture["segments"][0], mask_path=fixture["output"].parent / "missing.nii.gz")
    with pytest.raises(osirix_roi.NativeROIError):
        export(fixture)
    assert_unpublished(fixture)


@pytest.mark.parametrize("existing", ["directory", "symlink"])
def test_existing_destinations_are_immutable(fixture, existing):
    destination = fixture["output"]
    if existing == "directory":
        destination.mkdir()
        sentinel = destination / "sentinel"
        sentinel.write_bytes(b"existing export")
    else:
        sentinel = destination.with_name("sentinel")
        sentinel.write_bytes(b"existing export")
        destination.symlink_to(sentinel)
    with pytest.raises(osirix_roi.NativeROIError):
        export(fixture)
    assert sentinel.read_bytes() == b"existing export"


@pytest.mark.parametrize("midway", [False, True])
def test_cancellation_never_publishes_a_partial_series(fixture, midway):
    cancel = [not midway]

    def progress(completed, total):
        assert 0 <= completed <= total
        if completed:
            cancel[0] = True

    with pytest.raises(InterruptedError):
        export(fixture, cancelled=lambda: cancel[0], progress=progress)
    assert_unpublished(fixture)


@pytest.mark.parametrize("target", ["source", "reference", "mask"])
def test_changed_inputs_are_rejected_before_publication(fixture, target):
    path = {"source": fixture["paths"][0], "reference": fixture["reference"], "mask": sorted(fixture["segments"], key=lambda item: item.id)[0].mask_path}[
        target
    ]
    changed = []

    def progress(completed, total):
        if completed and not changed:
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
            changed.append(True)

    with pytest.raises(osirix_roi.NativeROIError):
        export(fixture, progress=progress)
    assert changed
    assert_unpublished(fixture)


@pytest.mark.parametrize(
    "bound,limit",
    [("_MAX_INSTANCES", 8), ("_MAX_ROIS", 14), ("_MAX_TOTAL_BYTES", 100), ("_MAX_NATIVE_VOXELS", 100)],
)
def test_native_export_resource_bounds_fail_atomically(fixture, monkeypatch, bound, limit):
    monkeypatch.setattr(osirix_roi, bound, limit)
    with pytest.raises(osirix_roi.NativeROIError):
        export(fixture)
    assert_unpublished(fixture)
