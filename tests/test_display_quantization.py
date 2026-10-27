"""Display GLBs use KHR_mesh_quantization for transfer; masks and detailed exports do not.

The synthetic browser fixtures in web/tests/fixtures come from
``write_browser_fixtures``; a test below keeps them identical to the writer.
After an intended format change, regenerate them from the repository root with
``PYTHONPATH=.:tests python -c "import test_display_quantization as t; t.write_browser_fixtures(t.FIXTURES)"``
and bump ``surfaces._DISPLAY_ASSET_IMPLEMENTATION``.
"""

from pathlib import Path
import json
import struct

import nibabel as nib
import numpy as np
import pytest

from scan3d import surfaces


STEP = surfaces._POSITION_STEP_M
FIXTURES = Path(__file__).resolve().parents[1] / "web" / "tests" / "fixtures"
# Off-grid patient RAS mm boxes, far from the origin like a table-offset scan.
LIVER = ([101.3, -52.7, 1187.9], [163.1, 18.45, 1251.25])
AORTA = ([-20.2, 30.05, 1100.33], [5.71, 60.6, 1260.4])


def scene(ras):
    """Patient RAS millimetres to glTF scene metres: (-R, S, A) / 1000."""
    ras = np.asarray(ras, float)
    return np.stack([-ras[..., 0], ras[..., 2], ras[..., 1]], axis=-1) / 1000


def box(low, high):
    """A closed, outward-wound box shaped like _mesh_arrays output (float32 scene metres)."""
    corners = scene([[(high if index & bit else low)[axis] for axis, bit in enumerate((1, 2, 4))] for index in range(8)])
    center = corners.mean(axis=0)
    faces = []
    for a, b, c, d in ((0, 2, 6, 4), (1, 3, 7, 5), (0, 1, 5, 4), (2, 3, 7, 6), (0, 1, 3, 2), (4, 5, 7, 6)):
        for triangle in ((a, b, c), (a, c, d)):
            points = corners[list(triangle)]
            outward = np.cross(points[1] - points[0], points[2] - points[0]) @ (points.mean(axis=0) - center) > 0
            faces.append(triangle if outward else triangle[::-1])
    normals = (corners - center) / np.linalg.norm(corners - center, axis=1, keepdims=True)
    return corners.astype("<f4"), normals.astype("<f4"), np.array(faces, "<u4")


def structure(identity, mask="unused.nii.gz", color="#b25566"):
    return {"id": identity, "name": identity, "group": "Organs", "color": color, "mask_file": str(mask)}


def mask_file(path, offset=0):
    values = np.zeros((26, 28, 24), np.uint8)
    values[4 + offset:19, 5:22, 3:19] = 1
    values[11:16, 12:16, 8:12] = 0
    angle = np.deg2rad(27)
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0],
                              [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-0.8, 1.3, 2.1])
    affine[:3, 3] = [27, -54, 882]
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units("mm")
    nib.save(image, path)
    return path


def write_glb(path, document, binary):
    encoded = json.dumps(document, separators=(",", ":"), ensure_ascii=True).encode()
    encoded += b" " * (-len(encoded) % 4)
    Path(path).write_bytes(struct.pack("<III", 0x46546C67, 2, 28 + len(encoded) + len(binary))
                           + struct.pack("<II", len(encoded), 0x4E4F534A) + encoded
                           + struct.pack("<II", len(binary), 0x004E4942) + bytes(binary))
    return path


def legacy_display_model(structure_arrays, path):
    """The pre-quantization display format: float32/uint32 and untransformed nodes."""
    surfaces._write_model(structure_arrays, path, detailed=True)
    document, binary = surfaces._read_glb(path)
    document["extras"]["detailed"] = False
    return write_glb(path, document, binary)


def values(document, binary, index):
    """Independent glTF accessor reading, including strided and normalized data."""
    accessor = document["accessors"][index]
    view = document["bufferViews"][accessor["bufferView"]]
    dtype = np.dtype({5120: "i1", 5123: "<u2", 5125: "<u4", 5126: "<f4"}[accessor["componentType"]])
    columns = 3 if accessor["type"] == "VEC3" else 1
    width = view.get("byteStride", columns * dtype.itemsize) // dtype.itemsize
    array = np.frombuffer(binary, dtype, accessor["count"] * width, view["byteOffset"]).reshape(-1, width)[:, :columns]
    return np.maximum(array / 127, -1) if accessor.get("normalized") else array


def decode(document, binary, node, part=0):
    """World positions per glTF/KHR_mesh_quantization: translation + scale * POSITION."""
    primitive = document["meshes"][node["mesh"]]["primitives"][part]
    positions = values(document, binary, primitive["attributes"]["POSITION"])
    return (positions * node.get("scale", [1, 1, 1]) + node.get("translation", [0, 0, 0]),
            values(document, binary, primitive["attributes"]["NORMAL"]),
            values(document, binary, primitive["indices"]).reshape(-1, 3))


def sheet(rows, columns, spacing=.0007):
    """A rippled open surface in scene metres, triangulated row by row like a contour."""
    y, x = np.mgrid[0:rows, 0:columns].astype(float) * spacing
    positions = np.stack([x.ravel() - .1, .002 * np.sin(40 * x.ravel()) + 1.1, y.ravel() + .05], axis=1)
    index = np.arange(rows * columns).reshape(rows, columns)
    a, b, c, d = (corner.ravel() for corner in (index[:-1, :-1], index[:-1, 1:], index[1:, :-1], index[1:, 1:]))
    faces = np.stack([np.stack([a, c, b], 1), np.stack([b, c, d], 1)], 1).reshape(-1, 3)
    normals = np.tile([0, 1, 0], (rows * columns, 1))
    return positions.astype("<f4"), normals.astype("<f4"), faces.astype("<u4")


def write_browser_fixtures(directory):
    """Synthetic surfaces shared with web/tests/quantized-surfaces.test.ts."""
    both = [(structure("total:liver"), box(*LIVER)), (structure("total:aorta", color="#d0463c"), box(*AORTA))]
    surfaces._write_model(both, Path(directory) / "quantized-model.glb")
    legacy_display_model(both, Path(directory) / "legacy-model.glb")
    surfaces._write_model(both[:1], Path(directory) / "quantized-surface.glb")
    # A six-vertex limit exercises the multi-primitive path of >65535-vertex meshes.
    split = surfaces._quantize(box(*AORTA), limit=6)
    surfaces._write_model([(structure("total:aorta", color="#d0463c"), split)], Path(directory) / "quantized-split-surface.glb")


def test_display_glb_is_quantized_aligned_and_decodes_within_the_grid_bound(tmp_path):
    liver, aorta = box(*LIVER), box(*AORTA)
    path = tmp_path / "model.glb"
    report = surfaces._write_model([(structure("total:liver"), liver), (structure("total:aorta"), aorta)], path)
    assert report["display_quantization"] == {"encoding": "KHR_mesh_quantization", "position_step_mm": STEP * 1000,
                                              "position_error_bound_mm": pytest.approx(STEP * 1000 * 3 ** .5 / 2),
                                              "normals": "int8"}
    document, binary = surfaces._read_glb(path)
    assert document["extensionsUsed"] == document["extensionsRequired"] == ["KHR_mesh_quantization"]
    assert document["extras"] == {"units": "metres", "coordinates": "scene=(-R,S,A)/1000",
                                  "detailed": False, "source": "segmentation masks"}
    assert all(view["byteOffset"] % 4 == 0 for view in document["bufferViews"])
    errors = []
    for node, expected in zip(document["nodes"], (liver, aorta), strict=True):
        primitive = document["meshes"][node["mesh"]]["primitives"][0]
        position, normal, index = (document["accessors"][primitive["attributes"]["POSITION"]],
                                   document["accessors"][primitive["attributes"]["NORMAL"]],
                                   document["accessors"][primitive["indices"]])
        assert (position["componentType"], position.get("normalized", False)) == (5123, False)
        assert (normal["componentType"], normal["normalized"]) == (5120, True)
        assert index["componentType"] == 5123
        views = [document["bufferViews"][item["bufferView"]] for item in (position, normal, index)]
        assert [view.get("byteStride") for view in views] == [8, 4, None]
        assert [view["byteLength"] for view in views] == [8 * position["count"], 4 * normal["count"], 2 * index["count"]]
        assert node["scale"] == [STEP] * 3 and all((value / STEP).is_integer() for value in node["translation"])
        grid = values(document, binary, primitive["attributes"]["POSITION"])
        assert position["min"] == grid.min(axis=0).tolist() and position["max"] == grid.max(axis=0).tolist()
        positions, normals, faces = decode(document, binary, node)
        np.testing.assert_array_equal(faces, expected[2])
        assert np.abs(normals - expected[1]).max() <= .5 / 127 + 1e-7
        errors.append(np.abs(positions - expected[0]).max() * 1000)
        # A positive uniform scale keeps outward winding and normal directions.
        a, b, c = (positions[faces[:, i]] - positions.mean(axis=0) for i in range(3))
        assert np.einsum("ij,ij->i", a, np.cross(b, c)).sum() > 0
        arrays = surfaces._primitive_arrays(document, binary, primitive, node)
        assert isinstance(arrays, surfaces._Quantized)
        np.testing.assert_allclose(surfaces._float_arrays(arrays)[0], positions, rtol=0, atol=1e-12)
    assert 0 < max(errors) <= STEP * 1000 / 2  # 0.0153 mm per axis


def test_quantization_is_idempotent_on_one_absolute_grid():
    rng = np.random.default_rng(7)
    points = rng.uniform([-.35, -.2, .9], [.25, .3, 1.4], (600, 3)).astype("<f4")
    normals = rng.normal(size=(600, 3))
    normals = (normals / np.linalg.norm(normals, axis=1, keepdims=True)).astype("<f4")
    faces = np.arange(600, dtype="<u4").reshape(-1, 3)
    quantized = surfaces._quantize((points, normals, faces))
    positions, decoded_normals, decoded_faces = surfaces._float_arrays(quantized)
    assert quantized.scale == STEP and np.abs(positions - points).max() <= STEP / 2
    assert np.abs(decoded_normals - normals).max() <= .5 / 127 + 1e-7
    np.testing.assert_array_equal(decoded_faces, faces)
    # Encoding decoded data again reproduces the exact integers and transform.
    again = surfaces._quantize(surfaces._float_arrays(quantized))
    np.testing.assert_array_equal(again.positions, quantized.positions)
    np.testing.assert_array_equal(again.normals, quantized.normals)
    assert (again.translation, again.scale, again.parts) == (quantized.translation, quantized.scale, ((600, 200),))
    # A structure with another extent snaps shared boundary vertices identically.
    other = np.concatenate([points[:40], rng.uniform([.3, -.6, .5], [.5, -.4, .7], (20, 3)).astype("<f4")])
    neighbour = surfaces._quantize((other, normals[:60], faces[:20]))
    assert neighbour.translation != quantized.translation
    np.testing.assert_array_equal(surfaces._float_arrays(neighbour)[0][:40], positions[:40])


def test_structures_longer_than_the_uint16_range_use_a_coarser_power_of_two_step():
    points = np.array([[0, 0, 0], [0, 2.5, 0], [.3, 1, .2]], "<f4") + np.array([.1, -1.2, .4], "<f4")
    quantized = surfaces._quantize((points, np.zeros((3, 3), "<f4"), np.array([[0, 1, 2]], "<u4")))
    assert quantized.scale == 2 * STEP and quantized.positions.max() <= 65535
    assert np.abs(surfaces._float_arrays(quantized)[0] - points).max() <= STEP


def test_meshes_beyond_uint16_indices_split_into_seamless_primitives(tmp_path):
    single = sheet(255, 257)  # exactly 65535 vertices: one primitive, original order
    assert surfaces._quantize(single).parts == ((65535, len(single[2])),)
    arrays = sheet(300, 300)
    entry = structure("total:skin", tmp_path / "mask.bin")
    (tmp_path / "mask.bin").write_bytes(b"synthetic mask identity")
    model = tmp_path / "model.glb"
    report = surfaces._write_model([(entry, arrays)], model)
    document, binary = surfaces._read_glb(model)
    primitives = document["meshes"][0]["primitives"]
    assert len(primitives) == 2 and report["triangles"] == len(arrays[2])
    corners, vertices = [], 0
    for part, primitive in enumerate(primitives):
        position = document["accessors"][primitive["attributes"]["POSITION"]]
        assert primitive["material"] == 0 and position["count"] <= 65535
        assert document["accessors"][primitive["indices"]]["componentType"] == 5123
        positions, _, faces = decode(document, binary, document["nodes"][0], part)
        corners.append(positions[faces])
        vertices += position["count"]
    # Consecutive face ranges keep every triangle, in order, within the grid bound.
    assert np.abs(np.concatenate(corners) - arrays[0][arrays[2]]).max() <= STEP / 2
    assert 0 < vertices - len(arrays[0]) <= 2 * 300  # only seam rows repeat
    merged = surfaces._asset_arrays(model)
    assert merged.parts == tuple((document["accessors"][p["attributes"]["POSITION"]]["count"],
                                  document["accessors"][p["indices"]]["count"] // 3) for p in primitives)
    again = surfaces._quantize(surfaces._float_arrays(merged))
    assert again.parts == merged.parts and again.translation == merged.translation
    np.testing.assert_array_equal(again.positions, merged.positions)
    np.testing.assert_array_equal(again.faces, merged.faces)
    # Parts pass through repackaging and display import byte for byte.
    surfaces._write_model([(entry, merged)], tmp_path / "repackaged.glb")
    assert (tmp_path / "repackaged.glb").read_bytes() == model.read_bytes()
    parameters = surfaces._display_simplification()
    for source in (model, legacy_display_model([(entry, arrays)], tmp_path / "legacy.glb")):
        cache = tmp_path / f"cache-{source.stem}"
        imported = surfaces.import_display_assets([entry], source, cache, parameters)
        assert Path(imported["surface_assets"][0]["path"]).read_bytes() == model.read_bytes()
        assert imported["surface_assets"][0]["triangles"] == len(arrays[2])
    # A small limit splits ordinary meshes the same way, for browser fixtures.
    split = surfaces._quantize(box(*LIVER), limit=6)
    assert len(split.parts) > 1 and max(vertices for vertices, _ in split.parts) <= 6
    positions, _, faces = surfaces._float_arrays(split)
    original = box(*LIVER)
    assert np.abs(positions[faces] - original[0][original[2]]).max() <= STEP / 2


def test_legacy_float_models_read_and_repackage_exactly_like_fresh_arrays(tmp_path):
    liver, aorta = box(*LIVER), box(*AORTA)
    entries = [structure("total:liver"), structure("total:aorta")]
    legacy = legacy_display_model(list(zip(entries, (liver, aorta))), tmp_path / "legacy.glb")
    document, binary = surfaces._read_glb(legacy)
    assert "extensionsUsed" not in document and all("scale" not in node for node in document["nodes"])
    legacy_arrays = [surfaces._primitive_arrays(document, binary, document["meshes"][node["mesh"]]["primitives"][0], node)
                     for node in document["nodes"]]
    for arrays, expected in zip(legacy_arrays, (liver, aorta), strict=True):
        assert not isinstance(arrays, surfaces._Quantized)
        for actual, original in zip(arrays, expected, strict=True):
            np.testing.assert_array_equal(actual, original)
    fresh = surfaces._write_model(list(zip(entries, (liver, aorta))), tmp_path / "fresh.glb")
    # Aggregation may mix an old float asset with a current quantized one.
    surfaces._write_model([(entries[1], aorta)], tmp_path / "aorta.glb")
    current = surfaces._asset_arrays(tmp_path / "aorta.glb")
    mixed = surfaces._write_model([(entries[0], legacy_arrays[0]), (entries[1], current)], tmp_path / "mixed.glb")
    assert (tmp_path / "mixed.glb").read_bytes() == (tmp_path / "fresh.glb").read_bytes() and mixed == fresh
    with pytest.raises(ValueError, match="float geometry"):
        surfaces._write_model([(entries[1], current)], tmp_path / "detailed.glb", detailed=True)


def test_import_quantizes_legacy_display_models_without_mask_decode(tmp_path, monkeypatch):
    entries = [structure("total:liver", mask_file(tmp_path / "first.nii.gz")),
               structure("total:kidney_left", mask_file(tmp_path / "second.nii.gz", offset=1))]
    arrays = [surfaces._mesh(item["mask_file"], detailed=False) for item in entries]
    legacy = legacy_display_model(list(zip(entries, arrays)), tmp_path / "legacy.glb")
    before = legacy.read_bytes()
    fresh = surfaces.build_model(entries, tmp_path / "fresh.glb")

    def forbidden(*_):
        raise AssertionError("Display migration must not decompress masks")

    monkeypatch.setattr(surfaces, "_binary", forbidden)
    cache = tmp_path / "cache"
    imported = surfaces.import_display_assets(entries, legacy, cache, fresh["display_simplification"])
    assert imported["cache"]["surface_misses"] == 2 and legacy.read_bytes() == before
    for asset, item, expected in zip(imported["surface_assets"], entries, arrays, strict=True):
        assert asset["key"] == surfaces._surface_identity(item, detailed=False)[1]
        quantized = surfaces._asset_arrays(asset["path"])
        assert isinstance(quantized, surfaces._Quantized)
        assert np.abs(surfaces._float_arrays(quantized)[0] - expected[0]).max() <= STEP / 2
    combined = surfaces.build_model(entries, tmp_path / "combined.glb", cache_dir=cache)
    assert combined["cache"]["surface_hits"] == 2
    assert (tmp_path / "combined.glb").read_bytes() == (tmp_path / "fresh.glb").read_bytes()


def test_optimize_migrates_a_legacy_revision_to_the_assets_a_fresh_build_writes(tmp_path):
    from scan3d.config import Settings
    from scan3d.demo import create_demo
    from scan3d.service import Service

    service = Service(Settings(tmp_path / "data", (tmp_path,)))
    case = create_demo(service)
    revision = service.store.revision(case["id"], case["active_revision_id"])
    fresh = {asset["id"]: surfaces.surface_asset_path(service.surface_cache, asset["key"]).read_bytes()
             for asset in revision["surface_assets"]}
    # A revision from the previous release: float32 model.glb, no current-key assets.
    legacy_display_model([(item, surfaces._mesh(item["mask_file"], detailed=False)) for item in revision["structures"]],
                         Path(revision["directory"]) / "model.glb")
    for asset in revision["surface_assets"]:
        path = surfaces.surface_asset_path(service.surface_cache, asset["key"])
        path.unlink()
        surfaces._metadata_path(path).unlink()
    report = service.optimize_assets(case["id"])
    assert report["cache"]["surface_misses"] == len(fresh) == len(revision["structures"])
    migrated = service.store.revision(case["id"], case["active_revision_id"])
    structures = {item["id"]: item for item in migrated["structures"]}
    for asset in migrated["surface_assets"]:
        assert asset["key"] == surfaces._surface_identity(structures[asset["id"]], detailed=False)[1]
        assert surfaces.surface_asset_path(service.surface_cache, asset["key"]).read_bytes() == fresh[asset["id"]]


def test_build_model_packages_a_float_asset_like_a_quantized_one(tmp_path):
    path = mask_file(tmp_path / "mask.nii.gz")
    entry = structure("total:liver", path)
    cache = tmp_path / "cache"
    prepared = surfaces.prepare_surface(entry, cache)
    surfaces.build_model([entry], tmp_path / "expected.glb", cache_dir=cache)
    asset = Path(prepared["path"])
    legacy_display_model([(entry, surfaces._mesh(path, detailed=False))], asset)
    surfaces._record_cache(asset, triangles=prepared["triangles"])
    assert not isinstance(surfaces._asset_arrays(asset), surfaces._Quantized)
    report = surfaces.build_model([entry], tmp_path / "repackaged.glb", cache_dir=cache)
    assert report["cache"]["surface_hits"] == 1
    assert (tmp_path / "repackaged.glb").read_bytes() == (tmp_path / "expected.glb").read_bytes()


def test_display_keys_are_versioned_and_legacy_assets_never_collide(tmp_path, monkeypatch):
    path = mask_file(tmp_path / "mask.nii.gz")
    entry = structure("total:liver", path)
    base, display = surfaces._surface_identity(entry, detailed=False)
    detailed = surfaces._surface_identity(entry, detailed=True)[1]

    def legacy(flag, parameters):
        return surfaces._key({"base": base, "implementation": "physical-glb-v2", "detailed": flag, "parameters": parameters,
                              "id": entry["id"], "name": entry["name"], "color": entry["color"]})

    legacy_display = legacy(False, {"target": 12000, "local_error_mm": 0.25, "preserve_topology": True})
    assert detailed == legacy(True, None)  # float32 detailed exports keep their cache entries
    assert display != legacy_display
    # Each implementation version owns exactly one encoding.
    monkeypatch.setattr(surfaces, "_DISPLAY_ASSET_IMPLEMENTATION", "future-display")
    assert surfaces._surface_identity(entry, detailed=False)[1] not in (display, legacy_display)
    assert surfaces._surface_identity(entry, detailed=True)[1] == detailed
    monkeypatch.undo()
    monkeypatch.setattr(surfaces, "_ASSET_IMPLEMENTATION", "future-detailed")
    assert surfaces._surface_identity(entry, detailed=True)[1] != detailed
    assert surfaces._surface_identity(entry, detailed=False)[1] == display
    monkeypatch.undo()
    # A float asset cached by the previous release remains valid for old manifests.
    cache = tmp_path / "cache"
    old = surfaces.surface_asset_path(cache, legacy_display)
    legacy_display_model([(entry, surfaces._mesh(path, detailed=False))], old)
    surfaces._record_cache(old, triangles=len(surfaces._asset_arrays(old)[2]))
    before = old.read_bytes()
    current = surfaces.prepare_surface(entry, cache)
    assert current["key"] == display and current["cache"]["surface_misses"] == 1
    assert isinstance(surfaces._asset_arrays(current["path"]), surfaces._Quantized)
    assert old.read_bytes() == before and surfaces._valid_cache(old) is not None
    assert not isinstance(surfaces._asset_arrays(old), surfaces._Quantized)


def test_display_meshes_of_oblique_reflected_masks_stay_within_the_grid_bound(tmp_path):
    path = mask_file(tmp_path / "mask.nii.gz")
    model = tmp_path / "model.glb"
    report = surfaces.build_model([structure("total:liver", path)], model)
    expected = surfaces._mesh(path, detailed=False)
    document, binary = surfaces._read_glb(model)
    positions, normals, faces = decode(document, binary, document["nodes"][0])
    assert report["triangles"] == len(faces) == len(expected[2])
    np.testing.assert_array_equal(faces, expected[2])
    assert np.abs(positions - expected[0]).max() * 1000 <= STEP * 1000 / 2
    assert np.abs(normals - expected[1]).max() <= .5 / 127 + 1e-6
    # Only display GLBs change: detailed exports remain exact float32.
    surfaces.build_model([structure("total:liver", path)], tmp_path / "detailed.glb", detailed=True)
    detailed, detailed_binary = surfaces._read_glb(tmp_path / "detailed.glb")
    assert "extensionsUsed" not in detailed
    np.testing.assert_array_equal(decode(detailed, detailed_binary, detailed["nodes"][0])[0],
                                  surfaces._mesh(path, detailed=True)[0])


def test_import_rejects_inconsistent_quantized_models(tmp_path):
    entry = structure("total:liver", mask_file(tmp_path / "mask.nii.gz"))
    model = tmp_path / "model.glb"
    parameters = surfaces.build_model([entry], model)["display_simplification"]
    document, binary = surfaces._read_glb(model)
    normal = document["meshes"][0]["primitives"][0]["attributes"]["NORMAL"]
    for change in (lambda value: value["nodes"][0].update(scale=[STEP, STEP, 2 * STEP]),
                   lambda value: value["nodes"][0].pop("translation"),
                   lambda value: value["nodes"][0].update(rotation=[0, 0, 0, 1]),
                   lambda value: value.pop("extensionsRequired"),
                   lambda value: value.update(extensionsUsed=["KHR_mesh_quantization", "EXT_meshopt_compression"]),
                   lambda value: value["accessors"][normal].pop("normalized")):
        broken = json.loads(json.dumps(document))
        change(broken)
        with pytest.raises(ValueError):
            surfaces.import_display_assets([entry], write_glb(tmp_path / "broken.glb", broken, binary),
                                           tmp_path / "cache", parameters)
    assert not list((tmp_path / "cache").rglob("*.glb"))
    assert surfaces.import_display_assets([entry], model, tmp_path / "cache", parameters)["cache"]["surface_misses"] == 1


def test_browser_fixtures_match_the_current_writer(tmp_path):
    write_browser_fixtures(tmp_path)
    for name in ("quantized-model.glb", "legacy-model.glb", "quantized-surface.glb"):
        assert (tmp_path / name).read_bytes() == (FIXTURES / name).read_bytes(), f"Regenerate {name}"
