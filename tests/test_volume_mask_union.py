"""A 3D clipping mask is bounded, source-owned and conservative in patient space."""

from concurrent.futures import CancelledError, ThreadPoolExecutor
import gzip
import hashlib
import json
import os
import threading
import uuid

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest
from scipy.spatial import cKDTree

from scan3d import imaging, review_volume, volume_mask_union as union
from scan3d.api import create_app
from scan3d.config import Settings


def save(path, values, affine):
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units("mm")
    nib.save(image, path)
    return path


def decode(cache, descriptor):
    raw = b"".join(
        gzip.decompress(review_volume.chunk_path(cache, descriptor, descriptor["key"], chunk["index"]).read_bytes()) for chunk in descriptor["chunks"]
    )
    assert len(raw) == descriptor["byte_length"] <= union._MAX_BYTES
    return np.frombuffer(raw, dtype=np.uint8).reshape(descriptor["shape"], order="F")


def fixture(tmp_path, affine=None, shape=(19, 23, 29)):
    affine = np.eye(4) if affine is None else affine
    first = np.zeros(shape, np.uint8)
    first[2:11, 3:13, 5:16] = 1
    second = np.zeros(shape, np.uint8)
    second[7:16, 9:20, 11:25] = 1
    ct = save(tmp_path / "ct.nii", np.zeros(shape, np.int16), affine)
    a = save(tmp_path / "a.nii", first, affine)
    b = save(tmp_path / "b.nii", second, affine)
    return ct, [("a", a), ("b", b)], first, second, tmp_path / "cache"


@pytest.mark.parametrize("shear", [False, True])
@pytest.mark.parametrize("margin", [0, 2.5])
def test_union_geometry_overlap_and_native_margin_are_exact(tmp_path, shear, margin):
    angle = np.deg2rad(31)
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-0.8, 1.4, 2.2])
    if shear:
        affine[:3, 1] += affine[:3, 0] * 0.23
    affine[:3, 3] = [43, -17, 91]
    ct, masks, a, b, cache = fixture(tmp_path, affine)
    original_hashes = {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in [ct, *(p for _, p in masks)]}
    result = union.volume_mask_union(ct, masks, cache, margin, reference_image_id="source-one")
    binary = a | b
    actual_affine = imaging._image(ct)[1]
    points = np.indices(binary.shape).reshape(3, -1).T
    world = imaging._world(actual_affine, points)
    expected = (cKDTree(world[binary.reshape(-1) == 1]).query(world)[0] <= margin + 1e-7).reshape(binary.shape)
    origin = np.asarray(result["native_crop_origin_ijk"])
    high = origin + result["native_crop_shape"]
    np.testing.assert_array_equal(decode(cache, result), expected[tuple(slice(a, b) for a, b in zip(origin, high))])
    np.testing.assert_allclose(result["affine_ras"], actual_affine @ np.array([[1, 0, 0, origin[0]], [0, 1, 0, origin[1]], [0, 0, 1, origin[2]], [0, 0, 0, 1]]))
    assert result["reference_geometry"] == imaging.geometry(ct)
    assert result["reference_image_id"] == "source-one"
    assert result["quality"] == "preview" and result["presentation_only"] and result["volume_mask_union"]
    assert result["display_margin_mm"] == margin and not result["approximate"]
    assert {path: hashlib.sha256(path.read_bytes()).hexdigest() for path in original_hashes} == original_hashes


@pytest.mark.parametrize("margin", [0, 1.6])
def test_conservative_reduction_keeps_thin_foreground_and_voxel_faces(tmp_path, monkeypatch, margin):
    # Nearest point sampling can miss this single-voxel thread. Occupancy must
    # preserve every source centre, including both scan boundaries, on a rotated
    # reflected anisotropic lattice. Force a tiny budget to exercise reduction.
    monkeypatch.setattr(union, "_MAX_BYTES", 257)
    monkeypatch.setattr(union, "_STREAM_BYTES", 17)
    angle = np.deg2rad(27)
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-0.7, 1.3, 2.1])
    shape = (31, 37, 41)
    values = np.zeros(shape, np.uint8)
    values[0, 0, 0] = values[-1, -1, -1] = 1
    values[7, 11, :] = 1
    ct = save(tmp_path / "ct.nii", np.zeros(shape, np.int16), affine)
    path = save(tmp_path / "mask.nii.gz", values, affine)
    cache = tmp_path / "cache"
    result = union.volume_mask_union(ct, [("thread", path)], cache, margin)
    pooled = decode(cache, result)
    assert pooled.size <= 257 and result["approximate"]
    source_affine = imaging._image(ct)[1]
    np.testing.assert_allclose(imaging._bounds(pooled.shape, np.asarray(result["affine_ras"])), imaging._bounds(shape, source_affine), atol=1e-5)
    points = np.indices(shape).reshape(3, -1).T
    all_world = imaging._world(source_affine, points)
    native_expanded = cKDTree(all_world[values.reshape(-1) == 1]).query(all_world)[0] <= margin + 1e-7
    world = all_world[native_expanded]
    inverse = np.linalg.inv(result["affine_ras"])
    target = np.floor(imaging._world(inverse, world) + 0.5).astype(int)
    assert np.all(pooled[tuple(target.T)] == 1)
    expected = np.zeros_like(pooled)
    expected[tuple(target.T)] = 1
    np.testing.assert_array_equal(pooled, expected)


def test_crop_before_reduction_keeps_small_selection_at_native_resolution(tmp_path):
    ct, masks, a, _, cache = fixture(tmp_path)
    result = union.volume_mask_union(ct, masks[:1], cache)
    assert result["shape"] == [9, 10, 11] and not result["approximate"]
    np.testing.assert_array_equal(decode(cache, result), a[2:11, 3:13, 5:16])


@pytest.mark.parametrize("shape", [(512, 512, 967), (1, 1, 100_000_000), (2, 13_000_000, 3), (1, 1, 1)])
def test_budget_is_hard_bounded_for_large_and_thin_grids(shape):
    target = union._bounded_shape(shape)
    assert all(1 <= a <= b for a, b in zip(target, shape))
    assert int(np.prod(target)) <= 8 * 1024**2


def test_empty_selection_and_empty_masks_are_explicit(tmp_path):
    ct, masks, a, _, cache = fixture(tmp_path)
    with pytest.raises(ValueError, match="at least one"):
        union.volume_mask_union(ct, [], cache)
    save(masks[0][1], np.zeros_like(a), np.eye(4))
    result = union.volume_mask_union(ct, masks[:1], cache, 2.5)
    assert result["empty"] and result["shape"] == [1, 1, 1]
    assert not decode(cache, result).any()


def test_content_selection_reference_margin_and_algorithm_bound_cache(tmp_path, monkeypatch):
    ct, masks, a, _, cache = fixture(tmp_path)
    result = union.volume_mask_union(ct, masks, cache, reference_image_id="source-one")
    generate = review_volume.mask_volume
    monkeypatch.setattr(review_volume, "mask_volume", lambda *a, **k: pytest.fail("Warm union decoded masks"))
    assert union.volume_mask_union(ct, masks[::-1] + masks[:1], cache, reference_image_id="source-one") == result
    monkeypatch.setattr(review_volume, "mask_volume", generate)
    changed = [
        union.volume_mask_union(ct, masks[:1], cache, reference_image_id="source-one"),
        union.volume_mask_union(ct, masks, cache, reference_image_id="source-two"),
        union.volume_mask_union(ct, masks, cache, 1, reference_image_id="source-one"),
    ]
    stat = masks[0][1].stat()
    replacement = save(tmp_path / "replacement.nii", np.zeros_like(a), np.eye(4))
    replacement.replace(masks[0][1])
    os.utime(masks[0][1], ns=(stat.st_atime_ns, stat.st_mtime_ns))
    changed.append(union.volume_mask_union(ct, masks, cache, reference_image_id="source-one"))
    save(ct, np.ones(a.shape, np.int16), np.eye(4))
    changed.append(union.volume_mask_union(ct, masks, cache, reference_image_id="source-one"))
    monkeypatch.setattr(union, "_IMPLEMENTATION", "next-algorithm")
    changed.append(union.volume_mask_union(ct, masks, cache, reference_image_id="source-one"))
    assert len({entry["key"] for entry in [result, *changed]}) == 7


def test_cache_corruption_is_repaired_and_bad_sources_rejected(tmp_path):
    ct, masks, a, _, cache = fixture(tmp_path)
    result = union.volume_mask_union(ct, masks, cache)
    path = review_volume.chunk_path(cache, result, result["key"], 0)
    path.write_bytes(b"corrupt")
    repaired = union.volume_mask_union(ct, masks, cache)
    assert repaired == result
    assert decode(cache, repaired).any()
    manifest = review_volume._entry(cache, result["key"]) / "manifest.json"
    record = json.loads(manifest.read_text())
    record["descriptor"]["affine_ras"][0][3] += 10
    manifest.write_text(json.dumps(record))
    assert union.volume_mask_union(ct, masks, cache) == result
    save(masks[0][1], a * 2, np.eye(4))
    with pytest.raises(ValueError, match="binary"):
        union.volume_mask_union(ct, masks, cache)
    save(masks[0][1], a, np.diag([-1, 1, 1, 1]))
    with pytest.raises(ValueError, match="geometry"):
        union.volume_mask_union(ct, masks, cache)
    assert not list(cache.glob("volumes/*/.*"))


def test_concurrent_selection_shares_one_union_generation(tmp_path, monkeypatch):
    ct, masks, _, _, cache = fixture(tmp_path)
    original = review_volume._write
    calls = []

    def write(cache, descriptor, *args, **kwargs):
        if descriptor.get("volume_mask_union"):
            calls.append(1)
        return original(cache, descriptor, *args, **kwargs)

    monkeypatch.setattr(review_volume, "_write", write)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: union.volume_mask_union(ct, masks, cache), range(4)))
    assert len(calls) == 1 and all(entry == results[0] for entry in results)


def test_cancelled_publication_leaves_no_union_and_keeps_originals(tmp_path, monkeypatch):
    ct, masks, _, _, cache = fixture(tmp_path)
    stopped = threading.Event()
    original = review_volume._write
    key = None

    def write(cache, descriptor, *args, **kwargs):
        nonlocal key
        if descriptor.get("volume_mask_union"):
            key = descriptor["key"]
            stopped.set()
        return original(cache, descriptor, *args, **kwargs)

    monkeypatch.setattr(review_volume, "_write", write)
    with pytest.raises(CancelledError):
        union.volume_mask_union(ct, masks, cache, cancelled=stopped.is_set)
    assert key and not review_volume._entry(cache, key).exists()
    assert not list(cache.glob("volumes/*/.*"))
    monkeypatch.setattr(review_volume, "_write", original)
    assert union.volume_mask_union(ct, masks, cache)["key"] == key


def test_changed_input_before_publish_is_rejected(tmp_path, monkeypatch):
    ct, masks, a, _, cache = fixture(tmp_path)
    original = review_volume._write

    def write(cache, descriptor, *args, **kwargs):
        if descriptor.get("volume_mask_union"):
            save(masks[0][1], np.zeros_like(a), np.eye(4))
        return original(cache, descriptor, *args, **kwargs)

    monkeypatch.setattr(review_volume, "_write", write)
    with pytest.raises(ValueError, match="input changed"):
        union.volume_mask_union(ct, masks, cache)
    assert not list(cache.glob("volumes/*/.*"))
    assert not any(json.loads(p.read_text())["descriptor"].get("volume_mask_union") for p in cache.glob("volumes/*/*/manifest.json"))


def test_cancelled_native_decode_does_not_publish_derivatives(tmp_path, monkeypatch):
    ct, masks, _, _, cache = fixture(tmp_path)
    stopped = threading.Event()
    original = imaging._binary

    def binary(*args, **kwargs):
        result = original(*args, **kwargs)
        stopped.set()
        return result

    monkeypatch.setattr(imaging, "_binary", binary)
    with pytest.raises(CancelledError):
        union.volume_mask_union(ct, masks, cache, cancelled=stopped.is_set)
    assert not list(cache.glob("volumes/*/*/manifest.json"))


def test_api_union_chunks_case_selection_source_and_margin_ownership(tmp_path):
    ct, masks, a, b, _ = fixture(tmp_path)
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "missing"))
    service = app.state.service
    case = service.new_case("Union transport")
    revision = {
        "case_id": case["id"],
        "id": uuid.uuid4().hex,
        "ct_file": str(ct),
        "geometry": imaging.geometry(ct),
        "structures": [{"id": i, "mask_file": str(p)} for i, p in masks],
    }
    service.store.save("revisions", revision)
    prefix = f"/api/cases/{case['id']}/revisions/{revision['id']}"
    params = [("structure_ids", "b"), ("structure_ids", "a"), ("structure_ids", "a"), ("margin_mm", 0)]
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(prefix + "/volume-mask", params=params)
        assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
        result = response.json()
        descriptor = result["current"]
        assert result["source_id"] == "volume-mask-union:" + descriptor["key"]
        assert descriptor["reference_image_id"] == service.store.resolve_reference(revision)["id"]
        assert str(tmp_path) not in json.dumps(result)
        chunks = [client.get(chunk["url"]) for chunk in descriptor["chunks"]]
        assert all(
            chunk.status_code == 200 and chunk.headers["content-encoding"] == "gzip" and chunk.headers["cache-control"] == "private, no-cache"
            for chunk in chunks
        )
        revalidated = client.get(descriptor["chunks"][0]["url"], headers={"if-none-match": chunks[0].headers["etag"]})
        assert revalidated.status_code == 304 and not revalidated.content and revalidated.headers["cache-control"] == "private, no-cache"
        actual = np.frombuffer(b"".join(c.content for c in chunks), np.uint8).reshape(descriptor["shape"], order="F")
        np.testing.assert_array_equal(actual, (a | b)[2:16, 3:20, 5:25])
        chunk = descriptor["chunks"][0]["url"]
        assert client.get(chunk.replace("margin_mm=0.0", "margin_mm=2.5")).status_code == 404
        assert client.get(chunk.replace("structure_ids=b", "structure_ids=unknown")).status_code == 400
        assert client.get(chunk.replace("structure_ids=b&", "")).status_code == 404
        assert client.get(prefix + "/volume-mask").status_code == 400
        assert client.get(prefix + "/volume-mask", params={"structure_ids": "unknown"}).status_code == 400
        other = service.new_case("Other source")
        assert client.get(chunk.replace(case["id"], other["id"])).status_code == 404
        assert client.get(prefix + "/volume-mask", params={"structure_ids": "a", "margin_mm": 5.1}).status_code == 422
        revision["structures"][0]["reference_image_id"] = "foreign-source"
        service.store.save("revisions", revision)
        assert client.get(prefix + "/volume-mask", params=params).status_code == 400


def test_api_union_preserves_mr_reference_and_presentation(tmp_path):
    from scan3d.image_assets import create_image_asset

    ct, masks, _, _, _ = fixture(tmp_path)
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "missing"))
    service = app.state.service
    case = service.new_case("MR union transport")
    asset = create_image_asset(case["id"], ct, modality="MR")
    service.store.register_image_asset(asset)
    revision = {
        "case_id": case["id"],
        "id": uuid.uuid4().hex,
        "ct_file": str(ct),
        "reference_image_id": asset["id"],
        "image_asset": asset,
        "geometry": imaging.geometry(ct),
        "structures": [{"id": i, "mask_file": str(p), "reference_image_id": asset["id"]} for i, p in masks],
    }
    service.store.save("revisions", revision)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(f"/api/cases/{case['id']}/revisions/{revision['id']}/volume-mask", params=[("structure_ids", "a"), ("structure_ids", "b")])
        assert response.status_code == 200, response.text
        descriptor = response.json()["current"]
        assert descriptor["reference_image_id"] == asset["id"] and descriptor["modality"] == "MR"
        assert descriptor["scalar"]["units"] == "signal" and descriptor["presentation"] == asset["presentation"]
        assert client.get(descriptor["chunks"][0]["url"]).status_code == 200


def test_label_union_numbers_structures_in_order_and_later_labels_win_overlaps(tmp_path):
    ct, masks, a, b, cache = fixture(tmp_path)
    assert ((a == 1) & (b == 1)).any(), "The fixture must overlap"

    def labelled(first, second):
        expected = np.zeros(a.shape, np.uint8)
        expected[first == 1] = 1
        expected[second == 1] = 2
        return expected[2:16, 3:20, 5:25]

    result = union.volume_label_union(ct, masks, cache, reference_image_id="source-one")
    np.testing.assert_array_equal(decode(cache, result), labelled(a, b))
    assert result["labels"] == ["a", "b"] and result["selected_structure_ids"] == ["a", "b"]
    assert result["volume_label_union"] and not result["volume_mask_union"] and result["presentation_only"]
    assert result["display_margin_mm"] == 0 and result["reference_image_id"] == "source-one"
    reversed_order = union.volume_label_union(ct, masks[::-1], cache, reference_image_id="source-one")
    np.testing.assert_array_equal(decode(cache, reversed_order), labelled(b, a))
    assert reversed_order["labels"] == ["b", "a"] and reversed_order["key"] != result["key"]
    plain = union.volume_mask_union(ct, masks, cache, reference_image_id="source-one")
    assert plain["key"] not in {result["key"], reversed_order["key"]} and "labels" not in plain


def test_label_union_reduction_keeps_labels_and_validates_selection(tmp_path, monkeypatch):
    ct, masks, a, b, cache = fixture(tmp_path)
    monkeypatch.setattr(union, "_MAX_BYTES", 600)
    reduced = union.volume_label_union(ct, masks, cache)
    values = decode(cache, reduced)
    assert reduced["approximate"] and int(np.prod(reduced["shape"])) <= 600
    assert set(np.unique(values)) == {0, 1, 2}
    with pytest.raises(ValueError, match="at least one"):
        union.volume_label_union(ct, [], cache)
    with pytest.raises(ValueError, match="only once"):
        union.volume_label_union(ct, [masks[0], masks[0]], cache)
    with pytest.raises(ValueError, match="valid structure"):
        union.volume_label_union(ct, [("", masks[0][1])], cache)
    monkeypatch.setattr(union, "_MAX_LABELS", 1)
    with pytest.raises(ValueError, match="at most 1"):
        union.volume_label_union(ct, masks, cache)


def test_label_union_cache_rejects_edited_or_digest_stripped_labels(tmp_path):
    ct, masks, _, _, cache = fixture(tmp_path)
    result = union.volume_label_union(ct, masks, cache)
    manifest = review_volume._entry(cache, result["key"]) / "manifest.json"
    for tamper in ("swap", "strip"):
        record = json.loads(manifest.read_text())
        if tamper == "swap":
            record["descriptor"]["labels"] = record["descriptor"]["labels"][::-1]
        else:
            del record["descriptor_sha256"]
        manifest.write_text(json.dumps(record))
        assert review_volume._read_cached(cache, result["key"]) is None
        assert union.volume_label_union(ct, masks, cache) == result


def test_api_label_union_keeps_request_order_and_chunk_ownership(tmp_path):
    ct, masks, a, b, _ = fixture(tmp_path)
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "missing"))
    service = app.state.service
    case = service.new_case("Label transport")
    revision = {
        "case_id": case["id"],
        "id": uuid.uuid4().hex,
        "ct_file": str(ct),
        "geometry": imaging.geometry(ct),
        "structures": [{"id": i, "mask_file": str(p)} for i, p in masks],
    }
    service.store.save("revisions", revision)
    prefix = f"/api/cases/{case['id']}/revisions/{revision['id']}"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.get(prefix + "/volume-labels", params=[("structure_ids", "b"), ("structure_ids", "a"), ("structure_ids", "b")])
        assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
        result = response.json()
        descriptor = result["current"]
        assert result["source_id"] == "volume-label-union:" + descriptor["key"]
        assert descriptor["labels"] == ["b", "a"] and str(tmp_path) not in json.dumps(result)
        chunks = [client.get(chunk["url"]) for chunk in descriptor["chunks"]]
        assert all(chunk.status_code == 200 and chunk.headers["content-encoding"] == "gzip" for chunk in chunks)
        actual = np.frombuffer(b"".join(c.content for c in chunks), np.uint8).reshape(descriptor["shape"], order="F")
        expected = np.zeros(a.shape, np.uint8)
        expected[b == 1] = 1
        expected[a == 1] = 2
        np.testing.assert_array_equal(actual, expected[2:16, 3:20, 5:25])
        chunk = descriptor["chunks"][0]["url"]
        assert "structure_ids=b&structure_ids=a" in chunk
        assert client.get(chunk.replace("structure_ids=b&structure_ids=a", "structure_ids=a&structure_ids=b")).status_code == 404
        assert client.get(chunk.replace("structure_ids=b&", "structure_ids=unknown&")).status_code == 400
        assert client.get(prefix + "/volume-labels").status_code == 400
        other = service.new_case("Other source")
        assert client.get(chunk.replace(case["id"], other["id"])).status_code == 404
