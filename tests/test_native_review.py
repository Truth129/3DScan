"""Native drawing planes retain exact voxel identity on oblique/reflected CT."""
import gzip
import json
from io import BytesIO

import nibabel as nib
import numpy as np
from PIL import Image
import pytest
from fastapi.testclient import TestClient

from scan3d import imaging, review_volume
from scan3d.api import create_app
from scan3d.config import Settings


@pytest.fixture
def source(tmp_path):
    angle = .37
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0],
                              [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-1., 2., 3.])
    affine[:3, 3] = [40, -50, 60]
    values = np.arange(11 * 13 * 17, dtype=np.int16).reshape((11, 13, 17)) - 1000
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units("mm")
    path = tmp_path / "ct.nii.gz"
    nib.save(image, path)
    return path, values


@pytest.mark.parametrize("axis", [0, 1, 2])
@pytest.mark.parametrize("requested_index", [0, 4, -1])
def test_native_transform_keeps_one_exact_voxel_axis_and_png_hu_parity(source, axis, requested_index):
    path, values = source
    image, affine = imaging._image(path)
    index = image.shape[axis] - 1 if requested_index == -1 else requested_index
    meta, mapping = imaging.slice_geometry(image.shape, affine, "axial", 0, native_axis=axis, native_index=index)
    np.testing.assert_array_equal(mapping[axis], [0, 0, 1, index])
    np.testing.assert_allclose(np.linalg.inv(affine) @ np.asarray(meta["pixel_to_ras"]), mapping, atol=1e-12)
    assert meta["native"] == {"axis": axis, "index": index}
    result = review_volume.review_frame(path, None, "axial", 0, native_axis=axis, native_index=index)
    frame = result["frame"]
    raw = gzip.decompress(result["body"])
    pixels = frame["pixels"]
    hu = np.frombuffer(raw, dtype=np.uint8, count=pixels * 4).reshape(4, pixels).T.copy().view("<f4").reshape(meta["height"], meta["width"])
    expected = imaging._reslice(values.astype(np.float32), mapping, hu.shape, 1, -1024)
    np.testing.assert_array_equal(hu, expected)
    assert np.any(hu != -1024), "A valid native boundary plane became blank"
    png = imaging.slice_image(path, None, "axial", 0, 400, 40, native_axis=axis, native_index=index)
    assert png["meta"] == frame["meta"]
    rgb = np.asarray(Image.open(BytesIO(png["image"])))
    np.testing.assert_array_equal(rgb[..., 0], np.rint(np.clip((hu + 160) / 400, 0, 1) * 255).astype(np.uint8))


@pytest.mark.parametrize("axis,index", [(None, 2), (0, None), (3, 2), (True, 2), (0, -1), (0, 100), (0, 1.5)])
def test_native_geometry_rejects_invalid_pair(source, axis, index):
    image, affine = imaging._image(source[0])
    with pytest.raises(ValueError, match="native slice"):
        imaging.slice_geometry(image.shape, affine, "axial", 0, native_axis=axis, native_index=index)


def test_native_http_geometry_and_frame_cache_are_isolated(tmp_path):
    app = create_app(Settings(tmp_path / "data", (tmp_path,)))
    with TestClient(app, base_url="http://localhost") as client:
        case = client.post("/api/cases/demo").json()
        prefix = f"/api/cases/{case['id']}/revisions/{case['active_revision_id']}"
        for axis in range(3):
            query = {"native_axis": axis, "native_index": 20}
            png = client.get(prefix + "/slice", params=query)
            meta = client.get(prefix + "/slice-meta", params=query)
            frame = client.get(prefix + "/review-frame", params=query)
            assert png.status_code == meta.status_code == frame.status_code == 200
            assert json.loads(png.headers["X-Slice-Meta"]) == meta.json() == json.loads(frame.headers["X-CT-Frame"])["meta"]
            assert meta.json()["native"] == {"axis": axis, "index": 20}
        for query in ({"native_axis": 0}, {"native_index": 2}, {"native_axis": 4, "native_index": 2}):
            assert client.get(prefix + "/review-frame", params=query).status_code == 400


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_native_frame_matches_original_voxels_without_interpolation(source, axis):
    path, values = source
    index = values.shape[axis] // 2
    result = review_volume.review_frame(path, None, "axial", 0, native_axis=axis, native_index=index)
    meta = result["frame"]["meta"]
    expected = np.take(values, index, axis=axis).T
    assert (meta["height"], meta["width"]) == expected.shape
    raw = gzip.decompress(result["body"])
    hu = np.frombuffer(raw, np.uint8).reshape(4, -1).T.copy().view("<f4").reshape(expected.shape)
    np.testing.assert_array_equal(hu, expected)
    basis = np.asarray(meta["pixel_to_ras"])[:3, :2]
    expected_spacing = np.linalg.norm(nib.load(path).affine[:3, :3], axis=0)
    np.testing.assert_allclose(np.linalg.norm(basis, axis=0), np.delete(expected_spacing, axis))


def test_native_and_anatomical_long_scans_have_no_768_pixel_cap():
    affine = np.diag([1.26953125, -1.26953125, 1., 1.])
    shape = (512, 512, 967)
    native, _ = imaging.slice_geometry(shape, affine, "coronal", 0, native_axis=1, native_index=200)
    assert (native["width"], native["height"]) == (512, 967)
    anatomical, _ = imaging.slice_geometry(shape, affine, "coronal", -200)
    assert (anatomical["width"], anatomical["height"]) == (650, 967)
    assert anatomical["extent_mm"] == [650., 967.]


@pytest.mark.parametrize("native", [False, True])
def test_unbounded_full_frame_is_rejected_before_sampling(native):
    kwargs = {"native_axis": 2, "native_index": 0} if native else {}
    with pytest.raises(ValueError, match="frame limit"):
        imaging.slice_geometry((4096, 4096, 2), np.eye(4), "axial", 0, **kwargs)
