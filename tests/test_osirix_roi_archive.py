"""Native private-format writer limits and exact source-pixel placement."""

from dataclasses import replace
from datetime import datetime, timezone
import plistlib
import struct
from uuid import uuid4

import numpy as np
import pytest

from scan3d import osirix_roi_archive as archive_writer
from scan3d.osirix_roi_archive import BrushROI, RoiArchiveError, encode_roi_archive


_TIMESTAMP = datetime(2026, 10, 29, 0, 0, tzinfo=timezone.utc)


def _brush(mask=None, **changes):
    if mask is None:
        mask = np.zeros((13, 19), dtype=bool)
        mask[2:8, 4:12] = True
        mask[4:6, 6:8] = False
    return replace(
        BrushROI(mask, "Synthetic ring", "#12abEf", "1.2.826.0.1.3680043.10.1234", str(uuid4()), 0.8, 1.1, (12.25, -18.75)),
        **changes,
    )


def _objects(data):
    archive = plistlib.loads(data)
    objects = archive["$objects"]
    root = objects[archive["$top"]["root"].data]
    assert objects[root["$class"].data]["$classname"] == "NSArray"
    return objects, [objects[ref.data] for ref in root["NS.objects"]]


def _source_mask(objects, roi, shape):
    framed = objects[roi["textureBufferCompressed"].data]
    assert framed[:4] == b"bv4-"
    size = struct.unpack("<I", framed[4:8])[0]
    assert len(framed) == size + 12
    assert framed[-4:] == b"bv4$"
    assert size == roi["textureBufferCompressedSize"]
    texture = np.frombuffer(framed[8:-4], dtype=np.uint8).reshape(roi["textureHeightCompressed"], roi["textureWidthCompressed"])
    assert set(np.unique(texture)).issubset({0, 255})
    top = roi["textureUpLeftCornerY"]
    left = roi["textureUpLeftCornerX"]
    assert roi["textureDownRightCornerX"] == left + texture.shape[1] - 1
    assert roi["textureDownRightCornerY"] == top + texture.shape[0] - 1
    assert all(size % 4 == 0 for size in texture.shape)
    copied_rows = min(shape[0] - top, texture.shape[0])
    copied_columns = min(shape[1] - left, texture.shape[1])
    assert not np.any(texture[copied_rows:, :])
    assert not np.any(texture[:, copied_columns:])
    result = np.zeros(shape, dtype=bool)
    result[top : top + copied_rows, left : left + copied_columns] = texture[:copied_rows, :copied_columns] != 0
    return result


def test_hole_overlap_and_edge_pixels_survive_distinct_native_brushes():
    first = _brush()
    second_mask = np.zeros(first.mask_2d.shape, dtype=np.uint8)
    second_mask[0:7, 0:8] = 1
    second_mask[-1, -1] = 1
    second = _brush(second_mask, name="Synthetic overlapping block")
    objects, rois = _objects(encode_roi_archive([first, second], timestamp=_TIMESTAMP))
    for brush, roi in zip((first, second), rois, strict=True):
        np.testing.assert_array_equal(_source_mask(objects, roi, brush.mask_2d.shape), brush.mask_2d)
        assert roi["textureWidth"] == roi["textureHeight"] == 1
        assert objects[roi["textureBuffer"].data] == b"\0"
        assert roi["type"] == 20.0 and isinstance(roi["type"], float)
        assert roi["color.red"] == 0x12 * 257
        assert roi["color.green"] == 0xAB * 257
        assert roi["color.blue"] == 0xEF * 257
        assert objects[roi["username"].data] == "3DScan"
        assert objects[roi["comments"].data] == ""
        assert roi["locked"] is False
        assert roi["zLocation"] == np.finfo(np.float32).tiny
        assert objects[roi["imageOrigin"].data] == "{12.25, -18.75}"
        assert objects[roi["savedStudyInstanceUID"].data] == brush.study_instance_uid
    assert np.any(first.mask_2d & second_mask)
    assert rois[0]["groupID"] != rois[1]["groupID"]


def test_random_strided_source_grids_preserve_all_pixels():
    generator = np.random.default_rng(2941)
    for rows, columns in ((1, 1), (2, 17), (17, 2), (13, 29), (31, 34)):
        dense = generator.random((columns * 2, rows * 2)) > 0.7
        mask = dense[::2, ::2].T
        mask[0, 0] = mask[-1, -1] = True
        objects, rois = _objects(encode_roi_archive([_brush(mask)], timestamp=_TIMESTAMP))
        np.testing.assert_array_equal(_source_mask(objects, rois[0], mask.shape), mask)


def test_fixed_timestamp_is_reproducible_and_class_hierarchy_matches_profile():
    brush = _brush(name="Synthetic Ångström")
    data = encode_roi_archive([brush], timestamp=_TIMESTAMP)
    assert encode_roi_archive([brush], timestamp=_TIMESTAMP) == data
    objects, rois = _objects(data)
    roi = rois[0]
    assert objects[roi["name"].data] == brush.name
    array = objects[roi["points"].data]
    assert objects[array["$class"].data]["$classes"] == ["NSMutableArray", "NSArray", "NSObject"]
    assert array["NS.objects"] == []
    date = objects[roi["creationDate"].data]
    assert objects[date["$class"].data]["$classname"] == "NSDate"
    assert date["NS.time"] == roi["creationTimeStamp"]


@pytest.mark.parametrize("mask", [[[0, 1, 2]], [[0.0, float("nan")]], [[0.0, float("inf")]], [[0.0, -1.0]], [["0", "1"]], [[0, 1], [0]]])
def test_nonbinary_or_ragged_masks_are_refused(mask):
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([_brush(mask)])


@pytest.mark.parametrize(
    "changes",
    [
        {"name": ""},
        {"name": "x" * 65},
        {"name": "hidden\nline"},
        {"name": "hidden\\separator"},
        {"name": "\ud800"},
        {"color": "red"},
        {"study_instance_uid": "1.02.3"},
        {"roi_uuid": "not-a-uuid"},
        {"roi_uuid": "00000000-0000-0000-0000-000000000000"},
        {"pixel_spacing_x": 0},
        {"pixel_spacing_y": -1},
        {"pixel_spacing_x": float("inf")},
        {"image_origin": (0, float("nan"))},
        {"image_origin": (0,)},
        {"image_origin": (0, 0, 0)},
        {"group_id": 0},
        {"group_id": -1},
        {"group_id": float("nan")},
        {"group_id": float("inf")},
        {"group_id": True},
    ],
)
def test_invalid_labels_identity_and_geometry_are_refused(changes):
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([_brush(**changes)])


def test_empty_duplicate_and_mixed_source_grids_are_refused():
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([])
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([_brush(np.zeros((10, 10), dtype=bool))])
    brush = _brush()
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([brush, brush])
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([brush, _brush(np.ones((9, 9), dtype=bool))])


def test_source_dimensions_and_aggregate_size_are_checked_before_texture_allocation(monkeypatch):
    too_wide = np.broadcast_to(np.array(True), (1, archive_writer._MAX_AXIS + 1))
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([_brush(too_wide)])
    too_many_pixels = np.broadcast_to(np.array(True), (4_097, 4_097))
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([_brush(too_many_pixels)])
    brush = _brush()
    monkeypatch.setattr(archive_writer, "_MAX_ARCHIVE_BYTES", 8_000)

    def unexpected_allocation(*args, **kwargs):
        raise AssertionError("A texture must not be allocated before the size bound passes")

    monkeypatch.setattr(archive_writer.np, "zeros", unexpected_allocation)
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([brush])


def test_count_timestamp_and_cancellation_are_checked():
    brush = _brush()
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([brush] * (archive_writer._MAX_ROIS + 1))
    with pytest.raises(RoiArchiveError):
        encode_roi_archive([brush], timestamp=datetime(2026, 10, 29))
    with pytest.raises(InterruptedError):
        encode_roi_archive([brush], cancelled=lambda: True)


def test_nested_binary_sequences_are_supported():
    mask = [[1, 0, 1], [0, 1, 0]]
    objects, rois = _objects(encode_roi_archive([_brush(mask)]))
    np.testing.assert_array_equal(_source_mask(objects, rois[0], (2, 3)), mask)


def test_explicit_structure_group_is_preserved_across_image_archives():
    group = 814_948_557.125
    first = _brush(group_id=group)
    second = _brush(group_id=group, image_origin=(12.25, -15.75))
    first_objects, first_rois = _objects(encode_roi_archive([first], timestamp=_TIMESTAMP))
    second_objects, second_rois = _objects(encode_roi_archive([second], timestamp=_TIMESTAMP))
    assert first_rois[0]["groupID"] == second_rois[0]["groupID"] == group
    assert isinstance(first_rois[0]["groupID"], float)
    assert first_objects[first_rois[0]["UUID"].data] != second_objects[second_rois[0]["UUID"].data]
