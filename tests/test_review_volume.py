"""Native CT transport must preserve HU, reference coordinates and isolation."""

from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import threading
import uuid

from fastapi.testclient import TestClient
from io import BytesIO
import nibabel as nib
import numpy as np
from PIL import Image
import pytest
from scipy import ndimage as ndi

from scan3d import imaging, review_volume
from scan3d.api import create_app
from scan3d.config import Settings


def save(path, values, affine, slope=1.0, intercept=0.0, units="mm"):
    values = np.asarray(values)
    header = None
    if values.dtype.byteorder == ">":
        header = nib.Nifti1Header(endianness=">")
        header.set_data_dtype(values.dtype)
    image = nib.Nifti1Image(values, affine, header)
    image.header.set_xyzt_units(units)
    image.header.set_slope_inter(slope, intercept)
    nib.save(image, path)
    return path


@pytest.fixture
def source(tmp_path):
    angle = np.deg2rad(31)
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0],
                              [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-0.8, 1.4, 2.2])
    affine[:3, 3] = [31, -52, 87]
    values = (np.arange(7 * 9 * 35).reshape((7, 9, 35)) - 1100).astype(np.int16)
    mask = np.zeros(values.shape, np.uint8)
    mask[2:6, 3:8, 5:29] = 1
    ct = save(tmp_path / "ct.nii.gz", values, affine)
    segmentation = save(tmp_path / "mask.nii.gz", mask, affine)
    return ct, segmentation, values, mask


def decoded(cache, descriptor):
    blocks = []
    for chunk in descriptor["chunks"]:
        path = review_volume.chunk_path(cache, descriptor, descriptor["key"], chunk["index"])
        raw = gzip.decompress(path.read_bytes())
        assert len(raw) == chunk["byte_length"]
        blocks.append(raw)
    return np.frombuffer(b"".join(blocks), dtype=np.dtype(descriptor["dtype"]).newbyteorder("<")).reshape(
        descriptor["shape"], order="F")


@pytest.mark.parametrize("dtype,slope,intercept", [
    ("int16", 1, 0), ("uint16", 1, -1024), (">i2", .1, 1024.3),
    ("float32", .3, -17.7), ("float64", 1, 0), ("uint8", 2, -1024),
])
def test_native_slabs_exact_float32_hu_and_little_endian(tmp_path, dtype, slope, intercept):
    values = np.linspace(0, 4095, 7 * 9 * 35).reshape((7, 9, 35)).astype(dtype)
    ct = save(tmp_path / "ct.nii", values, np.diag([-0.8, 1.4, 2.2, 1]), slope, intercept)
    cache = tmp_path / "cache"
    descriptor = review_volume.ct_volume(ct, cache)
    reconstructed = decoded(cache, descriptor)
    hu = (reconstructed.astype(np.float64) * descriptor["slope"] + descriptor["intercept"]).astype(np.float32)
    np.testing.assert_array_equal(hu, np.asarray(nib.load(ct).dataobj, dtype=np.float32))
    assert len(descriptor["chunks"]) == 3
    assert [chunk["depth"] for chunk in descriptor["chunks"]] == [16, 16, 3]
    item_size = np.dtype(descriptor["dtype"]).itemsize
    assert descriptor["byte_length"] == values.size * item_size
    assert [chunk["byte_offset"] for chunk in descriptor["chunks"]] == [0, 7 * 9 * 16 * item_size,
                                                                            7 * 9 * 32 * item_size]
    assert descriptor["dtype"] == ("uint16" if dtype == "uint16" else "int16" if dtype in ("int16", ">i2") else "float32")


def test_oblique_reflected_crop_roundtrip_and_empty(source, tmp_path):
    ct, mask_path, _, mask = source
    cache = tmp_path / "cache"
    descriptor = review_volume.mask_volume(ct, mask_path, cache)
    assert descriptor["origin_ijk"] == [2, 3, 5]
    assert descriptor["shape"] == [4, 5, 24]
    np.testing.assert_array_equal(decoded(cache, descriptor), mask[2:6, 3:8, 5:29])
    affine = np.asarray(imaging.geometry(ct)["affine_ras"])
    expected = affine.copy()
    expected[:3, 3] = (affine @ [2, 3, 5, 1])[:3]
    np.testing.assert_allclose(descriptor["affine_ras"], expected)
    assert descriptor["geometry"] == imaging.geometry(ct)
    # Reinserting the crop is an exact binary round trip on the original grid.
    reconstructed = np.zeros(mask.shape, np.uint8)
    reconstructed[2:6, 3:8, 5:29] = decoded(cache, descriptor)
    np.testing.assert_array_equal(reconstructed, mask)
    save(mask_path, np.zeros(mask.shape, np.uint8), nib.load(ct).affine)
    empty = review_volume.mask_volume(ct, mask_path, cache)
    assert empty["empty"] and empty["shape"] == [1, 1, 1] and empty["origin_ijk"] == [0, 0, 0]
    assert not decoded(cache, empty).any()


def test_units_geometry_validation_and_invalid_values(source, tmp_path):
    ct, mask_path, values, mask = source
    cache = tmp_path / "cache"
    affine = np.diag([.001, -.002, .003, 1])
    save(ct, values, affine, units="meter")
    descriptor = review_volume.ct_volume(ct, cache)
    np.testing.assert_allclose(np.diag(descriptor["affine_ras"]), [1, -2, 3, 1], atol=1e-6)
    with pytest.raises(ValueError, match="geometry"):
        review_volume.mask_volume(ct, mask_path, cache)
    save(mask_path, mask * 2, affine, units="meter")
    with pytest.raises(ValueError, match="binary"):
        review_volume.mask_volume(ct, mask_path, cache)
    values = values.astype(np.float32)
    values[1, 2, 3] = np.nan
    save(ct, values, affine)
    with pytest.raises(ValueError, match="nonfinite"):
        review_volume.ct_volume(ct, cache)
    assert not list(cache.glob("volumes/*/.*"))


def test_cache_reuse_corruption_repair_and_same_mtime_replacement(source, tmp_path, monkeypatch):
    ct, _, values, _ = source
    cache = tmp_path / "cache"
    plain = tmp_path / "reference.nii"
    save(plain, values, nib.load(ct).affine)
    first = review_volume.ct_volume(plain, cache)
    first_path = review_volume.chunk_path(cache, first, first["key"], 0)
    original = first_path.read_bytes()
    timestamp = first_path.stat().st_mtime_ns
    # Warm access performs no native decode or compression.
    original_write = review_volume._write
    def no_write(*args, **kwargs):
        pytest.fail("Warm descriptor regenerated its volume")
    monkeypatch.setattr(review_volume, "_write", no_write)
    assert review_volume.ct_volume(plain, cache) == first
    assert first_path.stat().st_mtime_ns == timestamp
    monkeypatch.setattr(review_volume, "_write", original_write)
    first_path.write_bytes(b"broken!" + original[7:])
    repaired = review_volume.ct_volume(plain, cache)
    assert repaired["key"] == first["key"]
    np.testing.assert_array_equal(decoded(cache, repaired), values)
    stat = plain.stat()
    replacement = save(tmp_path / "replacement.nii", values + 200, nib.load(plain).affine)
    replacement.replace(plain)
    os.utime(plain, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert plain.stat().st_size == stat.st_size
    changed = review_volume.ct_volume(plain, cache)
    assert changed["key"] != first["key"]
    np.testing.assert_array_equal(decoded(cache, changed), values + 200)


def test_concurrent_requests_share_one_generation(source, tmp_path, monkeypatch):
    ct, _, _, _ = source
    original = review_volume._write
    calls = []
    lock = threading.Lock()
    def write(*args, **kwargs):
        with lock:
            calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(review_volume, "_write", write)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: review_volume.ct_volume(ct, tmp_path / "cache"), range(4)))
    assert len(calls) == 1 and all(result == results[0] for result in results)


def test_preview_selection_is_not_blocked_by_native_volume_generation(source, tmp_path, monkeypatch):
    """A bulk opt-in download must not stall the interactive overview lane."""
    ct, mask, _, _ = source
    original = review_volume._write
    full_started = threading.Event()
    release_full = threading.Event()
    def write(cache, descriptor, *args, **kwargs):
        if descriptor["quality"] == "full":
            full_started.set()
            if not release_full.wait(5):
                raise AssertionError("Full-volume test generation was not released")
        return original(cache, descriptor, *args, **kwargs)
    monkeypatch.setattr(review_volume, "_write", write)
    with ThreadPoolExecutor(max_workers=2) as pool:
        full = pool.submit(review_volume.ct_volume, ct, tmp_path / "cache", "full")
        try:
            assert full_started.wait(2)
            preview = pool.submit(review_volume.mask_volume, ct, mask, tmp_path / "cache", "preview")
            assert preview.result(timeout=2)["quality"] == "preview"
            assert not full.done()
        finally:
            release_full.set()
        assert full.result(timeout=2)["quality"] == "full"


def test_small_preview_preserves_exact_oblique_fov_and_mask_grid(tmp_path):
    shape = (83, 61, 221)
    ii, jj, kk = np.indices(shape)
    values = (ii * 3 + jj * 2 + kk - 900).astype(np.int16)
    affine = np.array([[-.8, -.3, 0, 31], [-.2, 1.4, 0, -52], [0, 0, 2.2, 87], [0, 0, 0, 1]])
    ct = save(tmp_path / "ct.nii.gz", values, affine, .5, 17)
    binary = (ii > 7) & (ii < 62) & (jj > 11) & (jj < 43) & (kk > 34) & (kk < 175)
    mask_path = save(tmp_path / "mask.nii.gz", binary.astype(np.uint8), affine)
    cache = tmp_path / "cache"
    preview = review_volume.ct_volume(ct, cache, "preview")
    assert max(preview["shape"]) == 192
    assert preview["quality"] == "preview" and preview["dtype"] == "int16" and preview["approximate"]
    assert preview["slope"] == 1 and preview["intercept"] == 0
    reference = imaging.geometry(ct)
    np.testing.assert_allclose(imaging._bounds(preview["shape"], np.asarray(preview["affine_ras"])),
                               reference["bounds_ras"], atol=1e-10)
    assert preview["reference_geometry"] == reference
    step = np.asarray(shape) / preview["shape"]
    origin = (step - 1) / 2
    pi, pj, pk = np.indices(preview["shape"], dtype=np.float64)
    expected_hu = ((pi * step[0] + origin[0]) * 3 + (pj * step[1] + origin[1]) * 2
                   + pk * step[2] + origin[2] - 900) * .5 + 17
    np.testing.assert_allclose(decoded(cache, preview), expected_hu.astype(np.float32), rtol=0, atol=.5001)
    mask = review_volume.mask_volume(ct, mask_path, cache, "preview")
    sampled = ndi.affine_transform(binary, np.diag(step), origin, output_shape=preview["shape"],
                                   output=bool, order=0, mode="constant", cval=0, prefilter=False)
    unboxed = np.zeros(preview["shape"], np.uint8)
    low = mask["origin_ijk"]
    unboxed[tuple(slice(a, a + n) for a, n in zip(low, mask["shape"]))] = decoded(cache, mask)
    np.testing.assert_array_equal(unboxed, sampled)
    np.testing.assert_allclose(np.asarray(mask["affine_ras"])[:3, 3],
                               imaging._world(np.asarray(preview["affine_ras"]), low))
    assert mask["geometry"] == preview["geometry"]
    assert mask["reference_geometry"] == reference


def test_preview_large_hu_falls_back_to_float32_without_clipping(tmp_path):
    values = np.full((11, 12, 13), 1_000_123, dtype=np.float32)
    ct = save(tmp_path / "large-hu.nii.gz", values, np.eye(4))
    cache = tmp_path / "cache"
    preview = review_volume.ct_volume(ct, cache, "preview")
    assert preview["dtype"] == "float32" and preview["preview_quantization_hu"] == 0
    np.testing.assert_array_equal(decoded(cache, preview), values)


@pytest.mark.parametrize("plane,axis", [("axial", 2), ("coronal", 1), ("sagittal", 0)])
def test_raw_settled_frame_matches_png_geometry_hu_and_comparison(source, tmp_path, plane, axis):
    ct, mask_path, _, old = source
    changed = old.copy()
    changed[2] = 0
    changed[6, 3:8, 5:29] = 1
    current_path = save(tmp_path / "changed.nii.gz", changed, nib.load(ct).affine)
    position = imaging.geometry(ct)["center_ras"][axis]
    result = review_volume.review_frame(ct, current_path, plane, position, mask_path)
    frame = result["frame"]
    meta = frame["meta"]
    raw = gzip.decompress(result["body"])
    count = frame["pixels"]
    shape = (meta["height"], meta["width"])
    assert frame["hu_encoding"] == "byte-shuffle"
    hu_bytes = np.frombuffer(raw, dtype=np.uint8, count=count * 4).reshape(4, count).T.copy()
    hu = hu_bytes.reshape(-1).view("<f4").reshape(shape)
    current = np.frombuffer(raw, dtype=np.uint8, count=count, offset=frame["current_offset"]).reshape(shape).astype(bool)
    parent = np.frombuffer(raw, dtype=np.uint8, count=count, offset=frame["parent_offset"]).reshape(shape).astype(bool)
    assert len(raw) == count * 6
    png = imaging.slice_image(ct, current_path, plane, position, 400, -700, mask_path)
    assert meta == png["meta"]
    grayscale = np.rint(np.clip((hu - (-700 - 400 / 2)) / 400, 0, 1) * 255).astype(np.uint8)
    expected = np.repeat(grayscale[..., None], 3, axis=2).astype(np.float32)
    for selected, color, alpha in ((current, [56, 189, 248], .22),
                                   (parent & ~current, [244, 63, 94], .45),
                                   (current & ~parent, [34, 197, 94], .45)):
        expected[selected] = expected[selected] * (1 - alpha) + np.asarray(color) * alpha
        expected[selected & ~ndi.binary_erosion(selected)] = color
    np.testing.assert_array_equal(np.rint(expected).astype(np.uint8), np.asarray(Image.open(BytesIO(png["image"]))))
    with pytest.raises(ValueError, match="outside"):
        review_volume.review_frame(ct, None, plane, -999999)


def test_revision_scoped_api_raw_bytes_parent_crop_and_case_isolation(source, tmp_path):
    ct, mask_path, values, mask = source
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "missing"))
    service = app.state.service
    case = service.new_case("Volume transport fixture")
    parent_id, current_id = uuid.uuid4().hex, uuid.uuid4().hex
    base = {"case_id": case["id"], "ct_file": str(ct), "geometry": imaging.geometry(ct), "structures": [
        {"id": "total:liver", "mask_file": str(mask_path)}]}
    service.store.save("revisions", {**base, "id": parent_id, "parent_id": None})
    new_mask = mask.copy()
    new_mask[2] = 0
    new_path = save(tmp_path / "new.nii.gz", new_mask, nib.load(ct).affine)
    service.store.save("revisions", {**base, "id": current_id, "parent_id": parent_id,
                                    "structures": [{"id": "total:liver", "mask_file": str(new_path)}]})
    prefix = f"/api/cases/{case['id']}/revisions/{current_id}"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(prefix + "/ct-volume")
        assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
        descriptor = response.json()
        assert str(tmp_path) not in json.dumps(descriptor)
        chunks = [client.get(chunk["url"]) for chunk in descriptor["chunks"]]
        assert all(chunk.status_code == 200 and chunk.headers["content-encoding"] == "gzip"
                   and "private" in chunk.headers["cache-control"] and "immutable" in chunk.headers["cache-control"]
                   for chunk in chunks)
        # HTTP clients decompress Content-Encoding automatically, like fetch().
        np.testing.assert_array_equal(np.frombuffer(b"".join(c.content for c in chunks), dtype="<i2").reshape(
            values.shape, order="F"), values)
        masks = client.get(prefix + "/mask-volume", params={"structure_id": "total:liver", "compare": True}).json()
        assert masks["current"]["origin_ijk"] == [3, 3, 5]
        assert masks["parent"]["origin_ijk"] == [2, 3, 5]
        assert masks["current"]["key"] != masks["parent"]["key"]
        assert client.get(masks["parent"]["chunks"][0]["url"]).content == mask[2:6, 3:8, 5:21].tobytes(order="F")
        without_comparison = client.get(prefix + "/mask-volume", params={"structure_id": "total:liver"}).json()
        assert without_comparison["parent"] is None
        assert client.get(prefix + "/mask-volume", params={"structure_id": "total:unknown"}).status_code == 400
        assert client.get(prefix + f"/ct-volume/{'0' * 64}/chunks/0").status_code == 404
        assert client.get(prefix + f"/ct-volume/{descriptor['key']}/chunks/999").status_code == 404
        assert client.get(prefix + f"/mask-volume/{masks['parent']['key']}/chunks/0",
                          params={"structure_id": "total:liver"}).status_code == 404
        other = service.new_case("Other case")
        wrong_prefix = f"/api/cases/{other['id']}/revisions/{current_id}"
        assert client.get(wrong_prefix + "/ct-volume").status_code == 404
        assert client.get(wrong_prefix + f"/ct-volume/{descriptor['key']}/chunks/0").status_code == 404
        assert client.get(wrong_prefix + "/mask-volume", params={"structure_id": "total:liver"}).status_code == 404
        parent_ct = client.get(f"/api/cases/{case['id']}/revisions/{parent_id}/ct-volume").json()
        assert parent_ct["key"] == descriptor["key"]  # Shared CT, independent revision/mask identities.
        preview = client.get(prefix + "/ct-volume?quality=preview").json()
        assert preview["quality"] == "preview" and preview["key"] != descriptor["key"]
        assert "quality=preview" in preview["chunks"][0]["url"]
        assert client.get(preview["chunks"][0]["url"]).status_code == 200
        selected_preview = client.get(prefix + "/mask-volume", params={
            "structure_id": "total:liver", "quality": "preview", "compare": True}).json()
        assert selected_preview["current"]["quality"] == "preview"
        assert client.get(selected_preview["current"]["chunks"][0]["url"]).status_code == 200
        frame_response = client.get(prefix + "/review-frame", params={"plane": "axial",
            "position_mm": imaging.geometry(ct)["center_ras"][2], "structure_id": "total:liver", "compare": True})
        assert frame_response.status_code == 200 and frame_response.headers["content-encoding"] == "gzip"
        frame = json.loads(frame_response.headers["x-ct-frame"])
        assert len(frame_response.content) == frame["pixels"] * 6
        assert frame["current_offset"] == frame["pixels"] * 4 and frame["parent_offset"] == frame["pixels"] * 5
        assert client.get(wrong_prefix + "/review-frame").status_code == 404
