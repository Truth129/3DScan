"""Cache correctness is physical correctness: reuse must never hide mask edits."""

from concurrent.futures import ThreadPoolExecutor
import json
import struct

import nibabel as nib
import numpy as np
import pytest

from scan3d import surfaces


def mask_file(path, offset=0):
    values = np.zeros((26, 28, 24), np.uint8)
    values[4 + offset : 19, 5:22, 3:19] = 1
    values[11:16, 12:16, 8:12] = 0  # enclosed cavity must preserve inward faces
    angle = np.deg2rad(27)
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-0.8, 1.3, 2.1])
    affine[:3, 3] = [27, -54, 82]
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units("mm")
    nib.save(image, path)
    return path


def structure(path, identity="total:liver"):
    return {"id": identity, "name": identity, "group": "Organs", "color": "#b25566", "mask_file": str(path)}


def glb_document(path):
    data = path.read_bytes()
    length = struct.unpack_from("<I", data, 12)[0]
    return json.loads(data[20 : 20 + length])


def assert_display_matches(arrays, expected):
    """Quantized display arrays decode to the float mesh within the grid bound."""
    assert isinstance(arrays, surfaces._Quantized)
    positions, normals, faces = surfaces._float_arrays(arrays)
    assert np.abs(positions - expected[0]).max() <= arrays.scale / 2
    assert np.abs(normals - expected[1]).max() <= 0.5 / 127 + 1e-6
    np.testing.assert_array_equal(faces, expected[2])


def test_cache_reuses_unchanged_structure_and_only_rebuilds_changed_mask(tmp_path, monkeypatch):
    first = mask_file(tmp_path / "first.nii.gz")
    second = mask_file(tmp_path / "second.nii.gz", offset=1)
    cache = tmp_path / "cache"
    original = surfaces._contour
    calls = []

    def contour(path):
        calls.append(str(path))
        return original(path)

    monkeypatch.setattr(surfaces, "_contour", contour)
    entries = [structure(first), structure(second, "total:kidney_left")]
    initial = surfaces.build_model(entries, tmp_path / "initial.glb", cache_dir=cache)
    assert initial["cache"]["base_misses"] == 2
    assert initial["cache"]["surface_misses"] == 2
    repeated = surfaces.build_model(entries, tmp_path / "repeated.glb", cache_dir=cache)
    assert repeated["cache"]["surface_hits"] == 2
    assert len(calls) == 2
    assert (tmp_path / "initial.glb").read_bytes() == (tmp_path / "repeated.glb").read_bytes()
    original_keys = {item["id"]: item["key"] for item in repeated["surface_assets"]}

    # A new revision may copy masks to a different directory; identity is bytes,
    # never file path or revision, so unchanged copies remain cache hits.
    copied = tmp_path / "copied.nii.gz"
    copied.write_bytes(first.read_bytes())
    entries[0] = structure(copied)
    mask_file(second, offset=2)
    changed = surfaces.build_model(entries, tmp_path / "changed.glb", cache_dir=cache)
    assert changed["cache"]["surface_hits"] == 1
    assert changed["cache"]["base_misses"] == 1
    assert len(calls) == 3
    keys = {item["id"]: item["key"] for item in changed["surface_assets"]}
    assert keys["total:liver"] == original_keys["total:liver"]
    assert keys["total:kidney_left"] != original_keys["total:kidney_left"]
    assert all(value >= 0 for value in changed["timings_seconds"].values())


def test_detailed_export_reuses_base_with_exact_oblique_reflected_geometry(tmp_path, monkeypatch):
    path = mask_file(tmp_path / "mask.nii.gz")
    entry = structure(path)
    cache = tmp_path / "cache"
    direct_display = surfaces._mesh(path, detailed=False)
    direct_detailed = surfaces._mesh(path, detailed=True)
    display = surfaces.prepare_surface(entry, cache)
    assert display["cache"]["base_misses"] == 1

    def no_decompression(*_):
        raise AssertionError("A detailed export must not reload the mask")

    monkeypatch.setattr(surfaces, "_binary", no_decompression)
    detailed = surfaces.prepare_surface(entry, cache, detailed=True)
    assert detailed["cache"]["base_hits"] == 1
    assert detailed["cache"]["surface_misses"] == 1
    assert detailed["timings_seconds"]["contour"] == 0
    assert_display_matches(surfaces._asset_arrays(display["path"]), direct_display)
    # Detailed exports stay exact, interoperable float32.
    for expected, actual in zip(direct_detailed, surfaces._asset_arrays(detailed["path"]), strict=True):
        np.testing.assert_array_equal(actual, expected)
    report = surfaces.build_model([entry], tmp_path / "combined.glb", detailed=True, cache_dir=cache)
    assert report["cache"]["surface_hits"] == 1
    document = glb_document(tmp_path / "combined.glb")
    assert document["extras"]["coordinates"] == "scene=(-R,S,A)/1000"
    assert document["materials"][0]["doubleSided"] is False


def test_cache_invalidates_meshing_parameters_versions_and_presentation(tmp_path, monkeypatch):
    path = mask_file(tmp_path / "mask.nii.gz")
    entry = structure(path)
    cache = tmp_path / "cache"
    initial = surfaces.prepare_surface(entry, cache)
    monkeypatch.setattr(surfaces, "_DISPLAY_LOCAL_ERROR_MM", 0.125)
    parameter = surfaces.prepare_surface(entry, cache)
    assert parameter["key"] != initial["key"]
    assert parameter["cache"]["base_hits"] == 1
    entry["color"] = "#123456"
    recolored = surfaces.prepare_surface(entry, cache)
    assert recolored["key"] != parameter["key"]
    assert recolored["cache"]["base_hits"] == 1
    monkeypatch.setattr(surfaces, "_MESH_IMPLEMENTATION", "future-implementation")
    future = surfaces.prepare_surface(entry, cache)
    assert future["key"] != recolored["key"]
    assert future["cache"]["base_misses"] == 1


def test_corrupt_assets_and_contours_regenerate(tmp_path):
    from pathlib import Path

    path = mask_file(tmp_path / "mask.nii.gz")
    entry = structure(path)
    cache = tmp_path / "cache"
    initial = surfaces.prepare_surface(entry, cache)
    asset = Path(initial["path"])
    original = asset.read_bytes()
    asset.write_bytes(b"damaged")
    recovered = surfaces.prepare_surface(entry, cache)
    assert recovered["cache"]["surface_misses"] == 1
    assert recovered["cache"]["base_hits"] == 1
    assert asset.read_bytes() == original
    # An absent sidecar after a crash is an invalid entry, never trusted.
    contour = next((cache / "contours").rglob("*.npz"))
    contour.write_bytes(b"damaged")
    asset.unlink()
    recovered = surfaces.prepare_surface(entry, cache)
    assert recovered["cache"]["base_misses"] == 1
    assert asset.read_bytes() == original
    assert not list(cache.rglob("*.writing"))


def test_concurrent_identical_requests_extract_once_and_cancel_is_atomic(tmp_path, monkeypatch):
    path = mask_file(tmp_path / "mask.nii.gz")
    entry = structure(path)
    cache = tmp_path / "cache"
    original = surfaces._contour
    calls = []

    def contour(path):
        calls.append(path)
        return original(path)

    monkeypatch.setattr(surfaces, "_contour", contour)
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _: surfaces.prepare_surface(entry, cache), range(3)))
    assert len(calls) == 1
    assert sum(result["cache"]["surface_misses"] for result in results) == 1
    assert len({result["key"] for result in results}) == 1
    output = tmp_path / "existing.glb"
    surfaces.build_model([entry], output, cache_dir=cache)
    before = output.read_bytes()

    def cancel(message):
        if "Packaging surface" in message:
            raise InterruptedError("Cancelled")

    with pytest.raises(InterruptedError):
        surfaces.build_model([entry], output, cache_dir=cache, progress=cancel)
    assert output.read_bytes() == before
    assert not list(tmp_path.rglob("*.writing"))


def test_surface_keys_reject_paths(tmp_path):
    for key in ("../mask", "a" * 63, "A" * 64, "/tmp/anything"):
        with pytest.raises(ValueError):
            surfaces.surface_asset_path(tmp_path, key)


def test_predecoded_mask_is_reused_without_second_decode_and_keeps_geometry(tmp_path, monkeypatch):
    path = mask_file(tmp_path / "mask.nii.gz")
    entry = structure(path)
    decoded = surfaces._binary(path)
    expected = surfaces._mesh(path, detailed=False)

    def forbidden(*_):
        raise AssertionError("Surface must reuse the measurement worker's mask decode")

    monkeypatch.setattr(surfaces, "_binary", forbidden)
    prepared = surfaces.prepare_surface(entry, tmp_path / "cache", decoded_mask=decoded)
    assert prepared["cache"]["base_misses"] == 1
    assert_display_matches(surfaces._asset_arrays(prepared["path"]), expected)
    # Cache hits must not inspect the supplied tuple or perform any mask decode.
    cached = surfaces.prepare_surface(entry, tmp_path / "cache", decoded_mask=object())
    assert cached["key"] == prepared["key"]
    assert cached["cache"]["surface_hits"] == 1
    detailed = surfaces.prepare_surface(entry, tmp_path / "cache", detailed=True, decoded_mask=object())
    assert detailed["cache"]["base_hits"] == 1


def test_import_existing_display_reuses_exact_same_keys_without_mask_decode(tmp_path, monkeypatch):
    first = mask_file(tmp_path / "first.nii.gz")
    second = mask_file(tmp_path / "second.nii.gz", offset=1)
    entries = [structure(first), structure(second, "total:kidney_left")]
    model = tmp_path / "legacy.glb"
    original = surfaces.build_model(entries, model)
    cache = tmp_path / "cache"
    binary_before = model.read_bytes()
    original_binary_loader = surfaces._binary

    def forbidden(*_):
        raise AssertionError("Legacy display migration must not decompress masks")

    monkeypatch.setattr(surfaces, "_binary", forbidden)
    imported = surfaces.import_display_assets(entries, model, cache, original["display_simplification"])
    assert imported["cache"]["surface_misses"] == 2
    assert imported["cache"]["base_misses"] == 0
    assert imported["timings_seconds"]["contour"] == 0
    assert not (cache / "contours").exists()
    assert model.read_bytes() == binary_before
    combined = surfaces.build_model(entries, tmp_path / "repackaged.glb", cache_dir=cache)
    assert combined["cache"]["surface_hits"] == 2
    assert (tmp_path / "repackaged.glb").read_bytes() == binary_before
    repeated = surfaces.import_display_assets(entries, model, cache, original["display_simplification"])
    assert repeated["cache"]["surface_hits"] == 2
    for imported_asset, prepared in zip(imported["surface_assets"], [surfaces.prepare_surface(entry, cache) for entry in entries]):
        assert imported_asset["key"] == prepared["key"]
        assert prepared["cache"]["surface_hits"] == 1

    monkeypatch.setattr(surfaces, "_binary", original_binary_loader)
    mask_file(second, offset=2)
    changed = surfaces.build_model(entries, tmp_path / "changed.glb", cache_dir=cache)
    assert changed["cache"]["surface_hits"] == 1
    assert changed["cache"]["surface_misses"] == 1
    assert changed["cache"]["base_misses"] == 1
    first_asset = changed["surface_assets"][0]
    for before, after in zip(surfaces._asset_arrays(imported["surface_assets"][0]["path"]), surfaces._asset_arrays(first_asset["path"])):
        np.testing.assert_array_equal(after, before)


def test_import_existing_display_rejects_mismatched_parameters_and_identities(tmp_path):
    path = mask_file(tmp_path / "mask.nii.gz")
    entry = structure(path)
    model = tmp_path / "legacy.glb"
    original = surfaces.build_model([entry], model)
    parameters = original["display_simplification"]
    with pytest.raises(ValueError, match="simplification"):
        surfaces.import_display_assets([entry], model, tmp_path / "cache", {**parameters, "local_error_mm": 1})
    with pytest.raises(ValueError, match="identities"):
        surfaces.import_display_assets([structure(path, "wrong-id")], model, tmp_path / "cache", parameters)
    assert not list((tmp_path / "cache").rglob("*.glb"))
    detailed = tmp_path / "detailed.glb"
    surfaces.build_model([entry], detailed, detailed=True)
    with pytest.raises(ValueError, match="display model"):
        surfaces.import_display_assets([entry], detailed, tmp_path / "cache", parameters)
    # Truncated model bytes must be rejected, never silently repackaged.
    model.write_bytes(model.read_bytes()[:-16])
    with pytest.raises(ValueError, match="GLB"):
        surfaces.import_display_assets([entry], model, tmp_path / "cache", parameters)
