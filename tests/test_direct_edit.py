"""Exact native-plane editing and revision publication without an AI boundary."""

from pathlib import Path

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from scan3d import imaging
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.direct_edit import apply_strokes
from scan3d.review import public_review
from scan3d.service import Service
from scan3d.store import ReviewConflict


def stroke(identity="paint", *, axis=2, index=5, include=True, vertices=None, radius=1.5):
    return dict(id=identity, kind="scribble", include=include, axis=axis, index=index, vertices=vertices or [[2.0, 3.0], [7.0, 6.0]], radius_mm=radius)


@pytest.fixture
def direct_case(tmp_path, monkeypatch):
    service = Service(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    case = service.new_case("Synthetic direct editing")
    directory = service.store.case_dir(case["id"]) / "source"
    directory.mkdir()
    affine = np.array([[-1.4, 0.6, 0.1, 25.0], [0.2, 2.1, 0.3, -44.0], [0.0, 0.1, 2.8, 81.0], [0.0, 0.0, 0.0, 1.0]])
    shape = (13, 14, 15)
    values = np.arange(np.prod(shape), dtype=np.int16).reshape(shape) - 1000
    ct = nib.Nifti1Image(values, affine)
    ct.header.set_xyzt_units("mm")
    ct_path = directory / "ct.nii.gz"
    nib.save(ct, ct_path)
    structures = []
    for identity, bounds in (
        ("target", (slice(4, 9), slice(4, 9), slice(4, 9))),
        ("sibling", (slice(2, 6), slice(2, 6), slice(2, 7))),
        ("independent", (slice(2, 6), slice(2, 6), slice(2, 7))),
    ):
        mask = np.zeros(shape, np.uint8)
        mask[bounds] = 1
        path = directory / f"{identity}.nii.gz"
        imaging._save_mask(mask, ct, path)
        structures.append(dict(id=f"custom:{identity}", name=identity, task="fixture", group="Fixture", color="#ba9876", mask_file=str(path)))
    base = service.build_revision(case["id"], ct_path, structures, source="synthetic_fixture")
    service.store.commit_revision(base)

    def no_ai():
        pytest.fail("Direct edits must never construct, initialize or call an AI runtime")

    monkeypatch.setattr(service, "_interactive", no_ai)
    return service, case, base, affine


def execute(service, queued):
    claimed = service.store.claim()
    assert claimed and claimed["id"] == queued["id"]
    service.execute(claimed)
    result = service.store.get("jobs", queued["id"])
    assert result["status"] == "completed", result
    return result


def apply(service, case, base, prompts, *, draft=None, relationship=None):
    draft = draft or service.reviews.create(case["id"], base["id"], base["structures"][0]["id"], method="direct", relationship=relationship)
    draft = service.reviews.update(case["id"], draft["id"], draft["generation"], prompts)
    if (draft.get("candidate") or {}).get("generation") == draft["generation"]:
        return draft
    job = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    assert job["payload"]["method"] == "direct"
    execute(service, job)
    return service.reviews.get(case["id"], draft["id"])


def load(path):
    return np.asanyarray(nib.load(path).dataobj)


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_physical_brush_changes_only_exact_native_slice_with_shear_reflection(direct_case, tmp_path, axis):
    _, _, base, _ = direct_case
    image, affine = imaging._image(Path(base["ct_file"]))
    source = Path(base["structures"][0]["mask_file"])
    original = load(source)
    prompt = stroke(axis=axis, index=5, radius=2.4)
    output = tmp_path / "paint.nii.gz"
    provenance, (before, after, _, _) = apply_strokes(base["ct_file"], source, [prompt], output)
    voxel = np.moveaxis(np.indices(image.shape), 0, -1)
    uv = [i for i in range(3) if i != axis]
    endpoints = []
    for pair in prompt["vertices"]:
        native = np.zeros(3)
        native[axis] = prompt["index"]
        native[uv] = pair
        endpoints.append(imaging._world(affine, native))
    world = imaging._world(affine, voxel)
    direction = endpoints[1] - endpoints[0]
    fraction = np.clip(np.einsum("...i,i->...", world - endpoints[0], direction) / (direction @ direction), 0, 1)
    distance = np.linalg.norm(world - endpoints[0] - fraction[..., None] * direction, axis=-1)
    expected = original.astype(bool)
    expected[(voxel[..., axis] == prompt["index"]) & (distance <= prompt["radius_mm"] + 1e-9)] = True
    np.testing.assert_array_equal(before, original)
    np.testing.assert_array_equal(after, expected)
    np.testing.assert_array_equal(load(output), expected)
    np.testing.assert_allclose(imaging._image(output)[1], affine, atol=1e-6)
    np.testing.assert_array_equal(load(source), original)
    assert provenance["engine"] == "direct-paint" and provenance["device"] == "cpu"


def test_paint_then_erase_is_ordered_and_replay_undo_is_exact(direct_case):
    service, case, base, _ = direct_case
    source_bytes = {entry["id"]: Path(entry["mask_file"]).read_bytes() for entry in base["structures"]}
    paint = stroke(vertices=[[2.0, 3.0], [7.0, 6.0]])
    erase = stroke("erase", include=False, vertices=[[2.0, 3.0], [2.0, 3.0]], radius=0.5)
    first = apply(service, case, base, [paint])
    first_candidate = first["candidate"]
    first_values = load(first_candidate["mask_file"]).copy()
    second = apply(service, case, base, [paint, erase], draft=first)
    assert load(second["candidate"]["mask_file"])[2, 3, 5] == 0
    assert load(first_candidate["mask_file"])[2, 3, 5] == 1
    undone = apply(service, case, base, [paint], draft=second)
    np.testing.assert_array_equal(load(undone["candidate"]["mask_file"]), first_values)
    assert undone["candidate"]["summary"] == first_candidate["summary"]
    assert undone["candidate"]["revision_id"] != first_candidate["revision_id"]
    assert all(Path(entry["mask_file"]).read_bytes() == source_bytes[entry["id"]] for entry in base["structures"])
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]
    assert len(service.store.list("revisions", case["id"])) == 1


def test_accept_and_revision_undo_preserve_originals_measurements_and_other_segments(direct_case):
    service, case, base, _ = direct_case
    original = Path(base["structures"][0]["mask_file"]).read_bytes()
    draft = apply(service, case, base, [stroke()])
    public = public_review(draft)
    assert public["method"] == "direct" and "mask_file" not in str(public)
    assert service.reviews.candidate_revision(case["id"], public["candidate"]["revision_id"])["source"] == "direct_edit_candidate"
    accepted = execute(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
    revision = service.store.revision(case["id"], accepted["revision_id"])
    target = revision["structures"][0]
    assert revision["source"] == revision["operation"]["type"] == "direct_edit_correction"
    assert revision["operation"]["method"] == "direct"
    assert revision["operation"]["runtime"]["engine"] == "direct-paint"
    assert revision["parent_id"] == base["id"] and revision["review_status"] == "corrected"
    assert target["voxel_count"] == np.count_nonzero(load(target["mask_file"]))
    assert target["volume_ml"] == pytest.approx(target["voxel_count"] * abs(np.linalg.det(np.asarray(base["geometry"]["affine_ras"])[:3, :3])) / 1000)
    assert revision["structures"][1:] == base["structures"][1:]
    assert revision["surface_assets"][1:] == base["surface_assets"][1:]
    service.store.activate(case["id"], base["id"])
    assert service.store.revision(case["id"], base["id"]) == base
    assert Path(base["structures"][0]["mask_file"]).read_bytes() == original


def test_explicit_exclusive_siblings_remove_overlap_independent_segments_unchanged(direct_case):
    service, case, base, _ = direct_case
    relationship = {"kind": "subdivision", "exclusive_with": [base["structures"][1]["id"]]}
    independent = Path(base["structures"][2]["mask_file"]).read_bytes()
    draft = apply(service, case, base, [stroke()], relationship=relationship)
    candidate = draft["candidate"]
    changes = {entry["id"]: entry for entry in candidate["affected_structures"]}
    assert base["structures"][1]["id"] in changes
    target = load(candidate["mask_file"]).astype(bool)
    sibling = load(changes[base["structures"][1]["id"]]["mask_file"]).astype(bool)
    assert not np.any(target & sibling)
    assert Path(base["structures"][2]["mask_file"]).read_bytes() == independent
    assert any(entry["policy"] == "preserved" for entry in candidate["overlaps"])


def test_new_segment_can_be_painted_without_model_or_segmentation(direct_case):
    service, case, base, _ = direct_case
    draft = service.reviews.create_structure(case["id"], base["id"], "Hand painted", method="direct")
    assert draft["method"] == "direct" and draft["options"]["start_empty"]
    preview = public_review(draft)["preview_revision_id"]
    assert service.reviews.candidate_revision(case["id"], preview)["structures"][-1]["voxel_count"] == 0
    draft = apply(service, case, base, [stroke()], draft=draft)
    accepted = execute(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
    revision = service.store.revision(case["id"], accepted["revision_id"])
    assert revision["source"] == "direct_edit_new_structure"
    assert revision["structures"][-1]["id"] == draft["structure_id"]
    assert revision["structures"][-1]["voxel_count"] > 0
    assert revision["structures"][:-1] == base["structures"]


def test_complete_erase_existing_target_saves_named_empty_mask_and_exact_revision_undo(direct_case):
    service, case, base, _ = direct_case
    source_bytes = Path(base["structures"][0]["mask_file"]).read_bytes()
    strokes = [stroke(f"erase-{index}", index=index, include=False, vertices=[[6.0, 6.0], [6.0, 6.0]], radius=30) for index in range(4, 9)]
    draft = apply(service, case, base, strokes)
    summary = draft["candidate"]["summary"]
    assert summary["after_voxels"] == 0 and summary["changed_voxels"] == 125
    result = execute(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
    corrected = service.store.revision(case["id"], result["revision_id"])
    assert corrected["structures"][0]["id"] == base["structures"][0]["id"]
    assert corrected["structures"][0]["voxel_count"] == corrected["structures"][0]["volume_ml"] == 0
    assert not load(corrected["structures"][0]["mask_file"]).any()
    assert corrected["structures"][1:] == base["structures"][1:]
    assert all(asset["id"] != base["structures"][0]["id"] or asset["triangles"] == 0 for asset in corrected["surface_assets"])
    service.store.activate(case["id"], base["id"])
    assert Path(base["structures"][0]["mask_file"]).read_bytes() == source_bytes
    assert service.store.revision(case["id"], base["id"]) == base


def test_direct_rejects_ai_prompts_and_full_refine_and_method_is_immutable(direct_case):
    service, case, base, affine = direct_case
    draft = service.reviews.create(case["id"], base["id"], base["structures"][0]["id"], method="direct")
    point = dict(id="point", kind="point", include=True, ras=imaging._world(affine, [5, 5, 5]).tolist())
    with pytest.raises(ValueError, match="paint and erase"):
        service.reviews.update(case["id"], draft["id"], 0, [point])
    with pytest.raises(ValueError, match="AI refinement"):
        service.reviews.predict(case["id"], draft["id"], 0, full_refine=True)
    assert service.store.list("jobs") == []
    with pytest.raises(ValueError, match="Correction method"):
        service.reviews.create(case["id"], base["id"], base["structures"][0]["id"], method="other")


def test_a_stroke_that_covers_no_voxel_centre_is_refused_when_saved_and_named_when_an_older_draft_runs(direct_case):
    service, case, base, _ = direct_case
    # 0.5 mm around a point midway between centres reaches none of them on this grid.
    missed = stroke("missed", include=False, vertices=[[2.5, 3.5], [2.5, 3.5]], radius=0.5)
    draft = service.reviews.create(case["id"], base["id"], base["structures"][0]["id"], method="direct")
    with TestClient(create_app(service.settings), base_url="http://127.0.0.1") as client:
        refused = client.patch(f"/api/cases/{case['id']}/reviews/{draft['id']}", json={"expected_generation": 0, "prompts": [stroke(), missed]})
    assert refused.status_code == 400 and refused.json()["detail"] == "Erase stroke 2 covers no voxel centre on its slice. Draw it larger."
    assert service.reviews.get(case["id"], draft["id"])["prompts"] == []
    # A draft saved before the check still saves, and its Apply names the stroke.
    service.store.save("review_sessions", {**draft, "prompts": [missed]})
    draft = service.reviews.update(case["id"], draft["id"], 0, [missed, stroke()])
    queued = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    service.execute(service.store.claim())
    assert service.store.get("jobs", queued["id"])["status"] == "failed"
    assert service.reviews.get(case["id"], draft["id"])["error"] == "Erase stroke 1 covers no voxel centre on its slice"


def test_queued_and_running_direct_edits_cannot_publish_after_superseding_generation(direct_case, monkeypatch):
    service, case, base, _ = direct_case
    draft = service.reviews.create(case["id"], base["id"], base["structures"][0]["id"], method="direct")
    draft = service.reviews.update(case["id"], draft["id"], 0, [stroke()])
    queued = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    service.reviews.update(case["id"], draft["id"], queued["payload"]["generation"], [])
    assert service.store.get("jobs", queued["id"])["status"] == "cancelled"
    assert service.store.claim() is None
    draft = service.reviews.get(case["id"], draft["id"])
    draft = service.reviews.update(case["id"], draft["id"], draft["generation"], [stroke()])
    queued = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    claimed = service.store.claim()
    original = imaging._save_mask

    def save_then_supersede(mask, image, path):
        original(mask, image, path)
        service.reviews.update(case["id"], draft["id"], queued["payload"]["generation"], [])

    monkeypatch.setattr(imaging, "_save_mask", save_then_supersede)
    service.execute(claimed)
    assert service.store.get("jobs", queued["id"])["status"] == "cancelled"
    assert service.reviews.get(case["id"], draft["id"])["candidate"] is None
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]


def test_legacy_sessions_are_nninteractive_and_stale_generation_cannot_accept(direct_case):
    service, case, base, _ = direct_case
    old = service.reviews.create(case["id"], base["id"], base["structures"][0]["id"])
    old.pop("method")
    service.store.save("review_sessions", old)
    assert public_review(service.reviews.get(case["id"], old["id"]))["method"] == "nninteractive"
    draft = apply(service, case, base, [stroke()])
    with pytest.raises(ReviewConflict, match="changed"):
        service.reviews.accept(case["id"], draft["id"], 0)


def test_direct_api_contract_uses_existing_routes_and_forbids_method_patch(direct_case):
    service, case, base, _ = direct_case
    app = create_app(service.settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(
            f"/api/cases/{case['id']}/reviews", json={"revision_id": base["id"], "structure_id": base["structures"][0]["id"], "method": "direct"}
        )
        assert response.status_code == 200, response.text
        review = response.json()
        assert review["method"] == "direct"
        url = f"/api/cases/{case['id']}/reviews/{review['id']}"
        assert client.patch(url, json={"expected_generation": 0, "prompts": [], "method": "nninteractive"}).status_code == 422
        updated = client.patch(url, json={"expected_generation": 0, "prompts": [stroke()]})
        assert updated.status_code == 200, updated.text
        assert client.post(url + "/predict", json={"generation": updated.json()["generation"], "full_refine": True}).status_code == 400
        unknown = {"revision_id": base["id"], "structure_id": base["structures"][0]["id"], "method": "unknown"}
        assert client.post(f"/api/cases/{case['id']}/reviews", json=unknown).status_code == 422
