"""Voxel-centre 3D scissors geometry, ordering and immutable surface previews."""

from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient
import numpy as np
import pytest

from scan3d import imaging, scissors, surfaces
from scan3d.api import create_app
from scan3d.direct_edit import apply_strokes
from scan3d.interactive_prompts import normalize_prompts
from scan3d.review import public_review
from scan3d.store import identifier
from test_direct_edit import direct_case, apply, execute, load, stroke


def projection(affine, shape, view="axial"):
    # Build an independently defined camera from patient-space corners.
    rows = {
        "axial": np.eye(3),
        "side": np.array([[0.0, 0, 1], [0, 1, 0], [-1, 0, 0]]),
        "oblique": np.array([[0.8, 0.6, 0], [-0.3, 0.4, 0.8660254037844386], [0.5196152422706631, -0.6928203230275509, 0.5]]),
    }[view]
    corners = imaging._world(affine, imaging._corners(np.zeros(3), np.asarray(shape) - 1))
    camera_corners = corners @ rows.T
    low, high = camera_corners.min(axis=0), camera_corners.max(axis=0)
    matrix = np.eye(4)
    matrix[:3, :3] = np.diag(2 / (high - low)) @ rows
    matrix[:3, 3] = -1 - 2 * low / (high - low)
    matrix[2, 3] += 1000  # Every voxel is outside clip-z; cuts still pass through.
    return matrix


def polygon(shape):
    if shape == "rectangle":
        return [[-0.5, -0.4], [0.5, -0.4], [0.5, 0.45], [-0.5, 0.45]]
    if shape == "circle":
        angle = np.arange(64) * 2 * np.pi / 64
        return np.column_stack((0.5 * np.cos(angle), 0.45 * np.sin(angle))).tolist()
    return [[-0.7, -0.6], [0.55, -0.6], [0.55, -0.15], [-0.05, -0.15], [-0.05, 0.65], [-0.7, 0.65]]


def cut(affine, shape, *, identity="cut", kind="rectangle", operation="erase_inside", view="axial", vertices=None):
    return dict(
        id=identity,
        kind="scissors",
        include=False,
        operation=operation,
        shape=kind,
        vertices=vertices or polygon(kind),
        ras_to_clip=projection(affine, shape, view).tolist(),
    )


def scalar_inside(point, vertices):
    # Scalar ray crossing/distance oracle, independent of the production vector
    # classifier, native flat-index traversal and rectangle fast path.
    x, y = point
    inside = False
    for first, second in zip(vertices, vertices[1:] + vertices[:1]):
        start, finish = np.asarray(first), np.asarray(second)
        delta = finish - start
        length2 = delta @ delta
        fraction = np.clip((np.asarray(point) - start) @ delta / length2, 0, 1) if length2 else 0
        if np.linalg.norm(np.asarray(point) - start - fraction * delta) <= 1e-10:
            return True
        if (first[1] > y) != (second[1] > y):
            crossing = first[0] + (y - first[1]) * (second[0] - first[0]) / (second[1] - first[1])
            if x < crossing:
                inside = not inside
    return inside


def oracle(before, affine, prompt):
    result = before.copy()
    matrix = np.asarray(prompt["ras_to_clip"])
    for voxel in np.ndindex(before.shape):
        if not before[voxel]:
            continue
        clip = matrix @ np.r_[imaging._world(affine, voxel), 1.0]
        inside = scalar_inside(clip[:2], prompt["vertices"])
        if inside == (prompt["operation"] == "erase_inside"):
            result[voxel] = False
    return result


@pytest.mark.parametrize("shape", ["rectangle", "circle", "freehand"])
@pytest.mark.parametrize("operation", ["erase_inside", "erase_outside"])
@pytest.mark.parametrize("view", ["axial", "side", "oblique"])
def test_projected_cut_matches_patient_space_oracle_and_ignores_clip_depth(shape, operation, view):
    affine = np.array([[-0.8, 0.2, 0, 31.0], [0.1, 1.4, 0.3, -52.0], [0, 0.1, 2.2, 87.0], [0, 0, 0, 1.0]])
    before = np.ones((7, 8, 9), dtype=bool)
    before[::3, ::2, ::4] = False
    prompt = cut(affine, before.shape, kind=shape, operation=operation, view=view)
    expected = oracle(before, affine, prompt)
    result = before.copy()
    removed = scissors.erase_projected(result, affine, prompt)
    np.testing.assert_array_equal(result, expected)
    assert removed == np.count_nonzero(before & ~expected) > 0
    assert not np.any(result & ~before)
    # A view-space polygon acts through multiple native slices.
    assert np.count_nonzero(np.any(before != result, axis=(0, 1))) > 1


@pytest.mark.parametrize("operation,remaining", [("erase_inside", 80), ("erase_outside", 45)])
def test_rectangle_boundary_centres_are_included_at_all_depths(operation, remaining):
    target = np.ones((5, 5, 5), dtype=bool)
    matrix = np.diag([0.5, 0.5, 0.01, 1.0])
    matrix[:3, 3] = [-1, -1, 1000]
    prompt = dict(
        id="boundary",
        kind="scissors",
        include=False,
        shape="rectangle",
        operation=operation,
        vertices=[[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]],
        ras_to_clip=matrix.tolist(),
    )
    scissors.erase_projected(target, np.eye(4), prompt)
    assert np.count_nonzero(target) == remaining
    assert target[1, 1, 0] == (operation == "erase_outside")
    assert target[3, 3, 4] == (operation == "erase_outside")


@pytest.mark.parametrize(
    "change",
    [
        {"include": True},
        {"operation": "paint"},
        {"shape": "triangle"},
        {"vertices": [[-2, 0], [0, 1], [1, 0]]},
        {"vertices": [[0, 0], [0.2, 0.2], [0.4, 0.4]]},
        {"vertices": [[0, 0], [float("nan"), 1], [1, 0]]},
        {"vertices": [[-0.5, -0.5], [0.5, 0.5], [0.5, -0.5], [-0.5, 0.5]]},
        {"ras_to_clip": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0.01, 1]]},
        {"ras_to_clip": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 2]]},
        {"ras_to_clip": [[1, 0, 0, 0], [0, 0, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]]},
        {"ras_to_clip": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1e-14, 0], [0, 0, 0, 1]]},
        {"ras_to_clip": [[1, 0], [0, 1]]},
        {"unexpected": "field"},
    ],
)
def test_invalid_or_perspective_cut_is_rejected_before_editing(change):
    prompt = {**cut(np.eye(4), (7, 8, 9)), **change}
    with pytest.raises(ValueError):
        normalize_prompts([prompt], np.eye(4), (7, 8, 9))


def test_projection_and_vertices_are_owned_and_total_vertex_budget_is_enforced():
    prompt = cut(np.eye(4), (7, 8, 9))
    normalized = normalize_prompts([prompt], np.eye(4), (7, 8, 9))[0]
    prompt["ras_to_clip"][0][0] = 44
    prompt["vertices"][0][0] = 0.9
    assert normalized["ras_to_clip"][0][0] != 44 and normalized["vertices"][0][0] == -0.5
    long = cut(np.eye(4), (7, 8, 9), kind="circle")
    long["vertices"] = long["vertices"] * 32
    with pytest.raises(ValueError, match="8192"):
        normalize_prompts([{**long, "id": str(i)} for i in range(5)], np.eye(4), (7, 8, 9))


def test_foreground_coordinates_are_chunk_bounded_and_no_dense_grid_is_allocated(monkeypatch):
    target = np.ones((19, 20, 21), dtype=bool)
    prompt = cut(np.eye(4), target.shape, view="oblique")
    expected = oracle(target, np.eye(4), prompt)
    monkeypatch.setattr(scissors, "SCISSORS_CHUNK_VOXELS", 37)
    original = scissors._polygon_contains
    counts = []

    def bounded(x, y, *args, **kwargs):
        counts.append(len(x))
        assert len(x) <= 37
        return original(x, y, *args, **kwargs)

    monkeypatch.setattr(scissors, "_polygon_contains", bounded)
    monkeypatch.setattr(scissors.np, "indices", lambda *args, **kwargs: pytest.fail("A cut must never allocate a dense coordinate grid"))
    scissors.erase_projected(target, np.eye(4), prompt)
    assert len(counts) > 100
    np.testing.assert_array_equal(target, expected)


def test_paint_and_cuts_interleave_exactly_and_ai_rejects_scissors(direct_case, tmp_path):
    service, case, base, affine = direct_case
    source = Path(base["structures"][0]["mask_file"])
    image, exact_affine = imaging._image(Path(base["ct_file"]))
    native_projection = np.eye(4)
    native_projection[:3, :3] = np.diag(2 / (np.asarray(image.shape) - 1))
    native_projection[:3, 3] = -1
    matrix = native_projection @ np.linalg.inv(exact_affine)
    matrix[3] = [0, 0, 0, 1]
    corners = [[1.75, 2.75, 0, 1], [2.25, 2.75, 0, 1], [2.25, 3.25, 0, 1], [1.75, 3.25, 0, 1]]
    projected = np.asarray(corners) @ native_projection.T
    scissors_prompt = cut(affine, image.shape, vertices=projected[:, :2].tolist())
    scissors_prompt["ras_to_clip"] = matrix.tolist()
    paint = stroke(vertices=[[2.0, 3.0], [2.0, 3.0]], radius=0.5)
    first = tmp_path / "paint-cut.nii.gz"
    second = tmp_path / "cut-paint.nii.gz"
    provenance, _ = apply_strokes(base["ct_file"], source, [paint, scissors_prompt], first)
    apply_strokes(base["ct_file"], source, [scissors_prompt, paint], second)
    np.testing.assert_array_equal(load(first), load(source))
    assert load(second)[2, 3, 5] == 1 and load(first)[2, 3, 5] == 0
    assert provenance["cut_count"] == 1 and provenance["stroke_count"] == 1
    assert provenance["operations"][0]["removed_voxels"] == 1
    draft = service.reviews.create(case["id"], base["id"], base["structures"][0]["id"])
    with pytest.raises(ValueError, match="only available in direct"):
        service.reviews.update(case["id"], draft["id"], 0, [scissors_prompt])


def test_cut_preview_only_rebuilds_target_reuses_decoded_mask_and_undo_surface_checkpoint(direct_case, monkeypatch):
    service, case, base, affine = direct_case
    source_bytes = {item["id"]: Path(item["mask_file"]).read_bytes() for item in base["structures"]}
    calls = []
    original = surfaces.prepare_surface

    def prepare(structure, *args, **kwargs):
        calls.append((structure["id"], kwargs.get("decoded_mask") is not None))
        return original(structure, *args, **kwargs)

    monkeypatch.setattr(surfaces, "prepare_surface", prepare)
    monkeypatch.setattr(surfaces, "build_model", lambda *args, **kwargs: pytest.fail("Apply must not rebuild a full model"))
    first_prompt = cut(affine, base["geometry"]["shape"], view="oblique")
    first = apply(service, case, base, [first_prompt])
    candidate = first["candidate"]
    assert candidate["preview_3d_ready"]
    assert calls == [(base["structures"][0]["id"], True)]
    assert candidate["surface_assets"][1:] == base["surface_assets"][1:]
    assert candidate["surface_assets"][0]["key"] != base["surface_assets"][0]["key"]
    expected = oracle(load(base["structures"][0]["mask_file"]).astype(bool), imaging._image(Path(base["ct_file"]))[1], first_prompt)
    np.testing.assert_array_equal(load(candidate["mask_file"]), expected)
    assert candidate["summary"]["after_voxels"] == np.count_nonzero(expected)
    alias = candidate["revision_id"]
    virtual = service.reviews.candidate_revision(case["id"], alias)
    assert virtual["model_url"] is None and virtual["model_manifest_url"].endswith(alias + "/model-manifest")
    assert virtual["surface_assets"] == candidate["surface_assets"]
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]
    assert len(service.store.list("revisions", case["id"])) == 1
    assert all(Path(item["mask_file"]).read_bytes() == source_bytes[item["id"]] for item in base["structures"])
    second_prompt = cut(affine, base["geometry"]["shape"], identity="second", kind="circle", operation="erase_outside")
    second = apply(service, case, base, [first_prompt, second_prompt], draft=first)
    calls.clear()
    undone = service.reviews.update(case["id"], first["id"], second["generation"], [first_prompt])
    assert undone["status"] == "ready" and not calls
    assert undone["candidate"]["surface_assets"] == candidate["surface_assets"]
    assert undone["candidate"]["model_manifest_url"].endswith(undone["candidate"]["revision_id"] + "/model-manifest")
    assert public_review(undone)["candidate"]["preview_3d_ready"] is True


def test_complete_cut_empty_target_has_no_preview_asset_and_can_save_revision(direct_case):
    service, case, base, affine = direct_case
    prompt = cut(affine, base["geometry"]["shape"], vertices=[[-1.0, -1.0], [1.0, -1.0], [1.0, 1.0], [-1.0, 1.0]])
    draft = apply(service, case, base, [prompt])
    candidate = draft["candidate"]
    assert candidate["summary"]["after_voxels"] == 0
    assert candidate["surface_assets"] == base["surface_assets"][1:]
    accepted = execute(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
    result = service.store.revision(case["id"], accepted["revision_id"])
    assert result["structures"][0]["voxel_count"] == result["structures"][0]["volume_ml"] == 0
    assert result["surface_assets"] == candidate["surface_assets"]
    assert result["operation"]["runtime"]["cut_count"] == 1
    service.store.activate(case["id"], base["id"])
    assert service.store.revision(case["id"], base["id"]) == base


def test_cancelled_or_superseded_cut_cannot_publish_surfaces_or_revision(direct_case, monkeypatch):
    service, case, base, affine = direct_case
    draft = service.reviews.create(case["id"], base["id"], base["structures"][0]["id"], method="direct")
    draft = service.reviews.update(case["id"], draft["id"], 0, [cut(affine, base["geometry"]["shape"])])
    queued = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    claimed = service.store.claim()
    original = surfaces.prepare_surface

    def supersede(structure, *args, **kwargs):
        result = original(structure, *args, **kwargs)
        service.reviews.update(case["id"], draft["id"], draft["generation"], [])
        return result

    monkeypatch.setattr(surfaces, "prepare_surface", supersede)
    service.execute(claimed)
    assert service.store.get("jobs", queued["id"])["status"] == "cancelled"
    assert service.reviews.get(case["id"], draft["id"])["candidate"] is None
    assert len(service.store.list("revisions", case["id"])) == 1
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]


def test_surface_api_owns_candidate_keys_and_discard_revokes_every_alias(direct_case):
    service, case, base, affine = direct_case
    draft = apply(service, case, base, [cut(affine, base["geometry"]["shape"], view="oblique")])
    candidate = draft["candidate"]
    app = create_app(service.settings)
    client = TestClient(app, base_url="http://127.0.0.1")
    prefix = f"/api/cases/{case['id']}/revisions/{candidate['revision_id']}"
    try:
        virtual = client.get(prefix)
        assert virtual.status_code == 200, virtual.text
        data = virtual.json()
        assert data["preview_3d_ready"] and data["model_url"] is None
        assert "mask_file" not in virtual.text and str(service.settings.data_dir) not in virtual.text
        manifest = client.get(prefix + "/model-manifest")
        assert manifest.status_code == 200, manifest.text
        assert manifest.headers["cache-control"] == "private, no-store"
        entries = manifest.json()["structures"]
        assert {item["key"] for item in entries} == {item["key"] for item in candidate["surface_assets"]}
        for entry in entries:
            response = client.get(entry["url"])
            assert response.status_code == 200 and response.content.startswith(b"glTF")
        old_target_key = base["surface_assets"][0]["key"]
        assert client.get(prefix + f"/surfaces/{old_target_key}.glb").status_code == 404
        assert client.get(prefix.replace(case["id"], identifier())).status_code == 404
        assert client.get(prefix + "/model.glb").status_code == 404
        service.reviews.discard(case["id"], draft["id"], draft["generation"])
        assert client.get(prefix).status_code == 404
        assert client.get(prefix + "/model-manifest").status_code == 404
        assert client.get(entries[0]["url"]).status_code == 404
        assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]
    finally:
        client.close()
