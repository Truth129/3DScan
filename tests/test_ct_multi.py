"""Multiple CT overlays preserve independent masks, HU and revision identity."""

import gzip
from io import BytesIO
import json
import os
import uuid

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
from PIL import Image
import pytest
from scipy import ndimage as ndi

from scan3d import imaging, review_volume
from scan3d.api import _FrameCache, create_app
from scan3d.config import Settings


def save(path, values, affine):
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units("mm")
    nib.save(image, path)
    return path


@pytest.fixture
def source(tmp_path):
    angle = np.deg2rad(19)
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0],
                              [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-1.1, 1.4, 2.3])
    affine[:3, 3] = [31, -12, 70]
    shape = (13, 15, 9)
    ii, jj, kk = np.indices(shape)
    values = (ii * 17 + jj * 5 + kk * 2 - 700).astype(np.int16)
    first, second, previous = [np.zeros(shape, np.uint8) for _ in range(3)]
    first[2:10, 3:12, 1:8] = 1
    second[5:12, 5:14, 2:9] = 1
    previous[1:8, 3:12, 1:8] = 1
    ct = save(tmp_path / "ct.nii", values, affine)
    paths = [save(tmp_path / f"mask-{i}.nii", mask, affine) for i, mask in enumerate((first, second, previous))]
    return ct, paths, (values, first, second, previous)


def unpack(result):
    frame = result["frame"]
    count = frame["pixels"]
    raw = gzip.decompress(result["body"])
    shape = frame["meta"]["height"], frame["meta"]["width"]
    hu = np.frombuffer(raw, np.uint8, count=count * 4).reshape(4, count).T.copy().reshape(-1).view("<f4").reshape(shape)
    def mask(offset):
        return None if offset is None else np.frombuffer(raw, np.uint8, count=count, offset=offset).reshape(shape).astype(bool)
    return hu, [(layer["id"], mask(layer["current_offset"]), mask(layer["parent_offset"]))
                for layer in frame["overlays"]]


@pytest.mark.parametrize("plane,axis", [("axial", 2), ("coronal", 1), ("sagittal", 0)])
def test_multi_frame_independent_oblique_masks_and_unchanged_hu(source, plane, axis):
    ct, (first_path, second_path, previous_path), (values, first, second, previous) = source
    position = imaging.geometry(ct)["center_ras"][axis]
    result = review_volume.review_frame(ct, None, plane, position,
        overlays=(("total:liver", first_path, previous_path), ("total:spleen", second_path, None)))
    frame = result["frame"]
    hu, layers = unpack(result)
    assert [layer[0] for layer in layers] == ["total:liver", "total:spleen"]
    assert frame["current_offset"] == frame["pixels"] * 4
    assert frame["parent_offset"] == frame["pixels"] * 5
    assert frame["overlays"][1] == {"id": "total:spleen", "current_offset": frame["pixels"] * 6,
                                     "parent_offset": None}
    assert len(gzip.decompress(result["body"])) == frame["pixels"] * 7
    assert (layers[0][1] & layers[1][1]).any(), "Overlap must remain present in both binary masks"
    mapping = np.linalg.inv(nib.load(ct).affine) @ np.asarray(frame["meta"]["pixel_to_ras"])
    for actual, volume in ((hu, values.astype(np.float32)), (layers[0][1], first),
                           (layers[0][2], previous), (layers[1][1], second)):
        expected = ndi.affine_transform(volume, mapping[:3, [1, 0]], mapping[:3, 3],
            output_shape=hu.shape, order=1 if actual is hu else 0,
            mode="constant", cval=-1024 if actual is hu else 0, prefilter=False)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=6.2e-5)
    legacy = review_volume.review_frame(ct, first_path, plane, position, previous_path)
    assert "overlays" not in legacy["frame"]
    assert legacy["frame"]["meta"] == frame["meta"]
    assert gzip.decompress(legacy["body"])[:frame["pixels"] * 4] == gzip.decompress(result["body"])[:frame["pixels"] * 4]
    np.testing.assert_array_equal(np.asarray(nib.load(first_path).dataobj), first)


def test_multi_png_uses_anatomy_colors_contours_and_per_structure_comparison(source):
    ct, (first_path, second_path, previous_path), _ = source
    position = imaging.geometry(ct)["center_ras"][2]
    result = review_volume.review_frame(ct, None, "axial", position, overlays=(
        ("total:liver", first_path, previous_path), ("total:spleen", second_path, None)))
    hu, layers = unpack(result)
    colors = [imaging.presentation_color({"id": "total:liver", "color": "#000000"}),
              imaging.presentation_color({"id": "total:spleen", "color": "#000000"})]
    assert colors == ["#cc6619", "#8019e6"]
    png = imaging.slice_image(ct, None, "axial", position, 400, -500, overlays=(
        ("total:liver", first_path, previous_path, colors[0]),
        ("total:spleen", second_path, None, colors[1])))
    grayscale = np.rint(np.clip((hu - (-500 - 400 / 2)) / 400, 0, 1) * 255).astype(np.uint8)
    expected = np.repeat(grayscale[..., None], 3, axis=2).astype(np.float32)
    def paint(selected, color, alpha):
        expected[selected] = expected[selected] * (1 - alpha) + np.asarray(color) * alpha
        expected[selected & ~ndi.binary_erosion(selected)] = color
    for (_, current, previous), color in zip(layers, colors):
        paint(current, tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)), .22)
        if previous is not None:
            assert (previous & ~current).any() and (current & ~previous).any()
            paint(previous & ~current, (244, 63, 94), .45)
            paint(current & ~previous, (34, 197, 94), .45)
    actual = np.asarray(Image.open(BytesIO(png["image"])))
    np.testing.assert_array_equal(actual, np.rint(expected).astype(np.uint8))
    assert {tuple(color) for color in actual.reshape(-1, 3)}.issuperset({(204, 102, 25), (128, 25, 230)})
    assert png["meta"]["overlays"] == [{"id": "total:liver", "color": colors[0]},
                                        {"id": "total:spleen", "color": colors[1]}]


@pytest.fixture
def api(source, tmp_path):
    ct, (first, second, previous), _ = source
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "missing"))
    service = app.state.service
    case = service.new_case("Multi-overlay fixture")
    parent_id, revision_id = uuid.uuid4().hex, uuid.uuid4().hex
    structures = [{"id": "total:liver", "mask_file": str(first), "color": "#000000", "group": "Abdominal organs"},
                  {"id": "total:spleen", "mask_file": str(second), "color": "#ffffff", "group": "Abdominal organs"}]
    service.store.save("revisions", {"case_id": case["id"], "ct_file": str(ct), "id": parent_id,
        "parent_id": None, "structures": [{**structures[0], "mask_file": str(previous)}]})
    service.store.save("revisions", {"case_id": case["id"], "ct_file": str(ct), "id": revision_id,
        "parent_id": parent_id, "structures": structures})
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, service, case["id"], revision_id


def test_api_multi_legacy_identity_dedup_and_unknown_rejected_before_decode(api, source, monkeypatch):
    client, service, case_id, revision_id = api
    ct, paths, _ = source
    prefix = f"/api/cases/{case_id}/revisions/{revision_id}"
    position = imaging.geometry(ct)["center_ras"][2]
    base = [("plane", "axial"), ("position_mm", str(position)), ("compare", "true")]
    single = client.get(prefix + "/review-frame", params=[*base, ("structure_id", "total:liver")])
    old = review_volume.review_frame(ct, paths[0], "axial", position, paths[2])
    assert json.loads(single.headers["x-ct-frame"]) == old["frame"]
    assert single.content == gzip.decompress(old["body"])
    query = [*base, ("structure_ids", "total:liver"), ("structure_ids", "total:spleen"),
             ("structure_ids", "total:liver")]
    response = client.get(prefix + "/review-frame", params=query)
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    frame = json.loads(response.headers["x-ct-frame"])
    assert [layer["id"] for layer in frame["overlays"]] == ["total:liver", "total:spleen"]
    assert frame["overlays"][1]["parent_offset"] is None
    assert response.content[:frame["pixels"] * 4] == single.content[:frame["pixels"] * 4]
    assert str(ct.parent) not in response.headers["x-ct-frame"]
    png = client.get(prefix + "/slice", params=query)
    meta = client.get(prefix + "/slice-meta", params=query)
    assert png.status_code == meta.status_code == 200
    assert json.loads(png.headers["x-slice-meta"]) == meta.json()
    assert meta.json()["overlays"] == [{"id": "total:liver", "color": "#cc6619"},
                                         {"id": "total:spleen", "color": "#8019e6"}]
    def forbidden(*args, **kwargs):
        pytest.fail("An invalid selection must fail before decoding CT or masks")
    monkeypatch.setattr(imaging, "_ct_array", forbidden)
    monkeypatch.setattr(imaging, "_slice_mask", forbidden)
    invalid = [*base, ("structure_ids", "total:liver"), ("structure_ids", "other-case:unknown")]
    too_many = [*base, *(("structure_ids", f"unknown:{i}") for i in range(129))]
    for endpoint in ("/review-frame", "/slice", "/slice-meta"):
        assert client.get(prefix + endpoint, params=invalid).status_code == 400
        assert client.get(prefix + endpoint, params=too_many).status_code == 400
    other = service.new_case("Other case")
    assert client.get(f"/api/cases/{other['id']}/revisions/{revision_id}/review-frame", params=query).status_code == 404


def test_multi_frame_cache_keys_include_order_ct_current_and_parent_versions(api, source, monkeypatch):
    client, _, case_id, revision_id = api
    ct, paths, _ = source
    original = review_volume.review_frame
    calls = []
    def render(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(review_volume, "review_frame", render)
    prefix = f"/api/cases/{case_id}/revisions/{revision_id}/review-frame"
    base = [("plane", "axial"), ("position_mm", str(imaging.geometry(ct)["center_ras"][2])), ("compare", "true")]
    selected = [("structure_ids", "total:liver"), ("structure_ids", "total:spleen")]
    first = client.get(prefix, params=[*base, *selected])
    assert first.status_code == 200
    assert client.get(prefix, params=[*base, *selected]).content == first.content
    assert len(calls) == 1
    reversed_order = client.get(prefix, params=[*base, *reversed(selected)])
    assert [layer["id"] for layer in json.loads(reversed_order.headers["x-ct-frame"])["overlays"]] == ["total:spleen", "total:liver"]
    assert len(calls) == 2
    previous = first.content
    for path in (ct, paths[1], paths[2]):
        stat = path.stat()
        image = nib.load(path)
        values = np.asarray(image.dataobj).copy()
        values = values + 30 if path == ct else np.zeros_like(values)
        replacement = save(path.with_name("replacement.nii"), values, image.affine)
        replacement.replace(path)
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        assert path.stat().st_size == stat.st_size
        result = client.get(prefix, params=[*base, *selected])
        assert result.status_code == 200 and result.content != previous
        previous = result.content
    assert len(calls) == 5


def test_compressed_frame_cache_is_byte_bounded_and_skips_oversized_frames():
    cache = _FrameCache(max_bytes=24, max_entries=18)
    calls = []
    def result(size):
        calls.append(size)
        return {"body": b"x" * size, "frame": {}}
    cache.get_or_create("a", lambda: result(10))
    cache.get_or_create("b", lambda: result(10))
    cache.get_or_create("a", lambda: result(10))
    assert calls == [10, 10] and cache.byte_length == 24
    cache.get_or_create("c", lambda: result(10))
    assert list(cache.entries) == ["a", "c"] and cache.byte_length <= cache.max_bytes
    cache.get_or_create("large", lambda: result(25))
    cache.get_or_create("large", lambda: result(25))
    assert "large" not in cache.entries and calls[-2:] == [25, 25]
