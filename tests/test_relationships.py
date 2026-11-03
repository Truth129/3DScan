"""Explicit multi-mask overlap previews, reciprocal metadata and atomic acceptance."""

import copy
import hashlib
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from inference_path_stub import GPU_NAME
from scan3d import imaging
from scan3d.api import create_app
from scan3d.inference_paths import binding
from scan3d.relationships import INDEPENDENT, effective_relationship, normalize_relationship, reciprocal_updates, relationship_suggestions
from scan3d.review import public_review
from scan3d.store import job_lane
from test_interactive_review import example, execute_next, paths


class InsideLiver:
    def __init__(self):
        self.calls = 0

    def predict(self, ct, mask, prompts, output, **kwargs):
        self.calls += 1
        image, affine = imaging._image(ct)
        values, _, _ = imaging._binary(mask, image, affine)
        values[4, 4, 4] = True
        imaging._save_mask(values, image, output)
        return {"engine": "synthetic-one-voxel", "version": "test"}

    def release(self):
        pass


def new_candidate(example):
    service, case, base, prompt = example
    service.interactive_runtime = InsideLiver()
    draft = service.reviews.create_structure(case["id"], base["id"], "Accessory region")
    draft = service.reviews.update(case["id"], draft["id"], 0, [{**prompt, "include": True}])
    job = execute_next(service, service.reviews.predict(case["id"], draft["id"], draft["generation"]))
    assert job["status"] == "completed", job
    return service.reviews.get(case["id"], draft["id"])


def apply_relationship(service, case, draft, relationship):
    draft = service.reviews.update(case["id"], draft["id"], draft["generation"], draft["prompts"], relationship=relationship)
    assert public_review(draft)["relationship_dirty"]
    job = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    assert job["payload"]["relationship_only"]
    result = execute_next(service, job)
    assert result["status"] == "completed", result
    return service.reviews.get(case["id"], draft["id"])


def add_peer(example, relationship=None):
    service, case, base, _ = example
    image, _ = imaging._image(Path(base["ct_file"]))
    values = np.zeros(image.shape, dtype=np.uint8)
    values[4, 4, 4] = 1
    path = Path(base["directory"]) / "overlapping-peer.nii.gz"
    imaging._save_mask(values, image, path)
    entry = dict(id="custom:peer", name="Sibling region", task="custom", group="Same display group", color="#ab12ef", mask_file=str(path))
    if relationship:
        entry["relationship"] = relationship
    revision = service.build_revision(case["id"], Path(base["ct_file"]), [*base["structures"], entry], source="synthetic_overlap", parent=base["id"])
    service.store.commit_revision(revision)
    return service, case, revision, example[3]


def test_independent_parent_and_subdivision_use_one_prediction_and_preview_every_change(example, monkeypatch):
    service, case, base, _ = example
    original = copy.deepcopy(base)
    draft = new_candidate(example)
    first = draft["candidate"]
    assert first["overlaps"] == [dict(id="total:liver", name="liver", voxel_count=1, volume_ml=pytest.approx(0.009), policy="preserved", reason="independent")]
    assert len(first["warnings"]) == 1
    kidney = Path(base["structures"][1]["mask_file"])
    binary, decoded = imaging._binary, []

    def watched(path, *args, **kwargs):
        decoded.append(Path(path))
        return binary(path, *args, **kwargs)

    monkeypatch.setattr(imaging, "_binary", watched)
    draft = apply_relationship(service, case, draft, {"kind": "subregion", "parent_id": "total:liver", "exclusive_with": []})
    assert not draft["candidate"]["warnings"]
    assert draft["candidate"]["overlaps"][0]["reason"] == "parent_or_child"
    assert kidney not in decoded, "Spatially disjoint masks must not be decoded for overlap scanning"
    parent_alias = draft["candidate"]["revision_id"]
    draft = apply_relationship(service, case, draft, {"kind": "subdivision", "exclusive_with": ["total:liver"]})
    assert service.interactive_runtime.calls == 1
    candidate = draft["candidate"]
    assert candidate["overlaps"][0]["policy"] == "removed"
    assert candidate["total_changed_voxels"] == 2
    assert [entry["id"] for entry in candidate["affected_structures"]] == [draft["structure_id"], "total:liver"]
    virtual = service.reviews.candidate_revision(case["id"], candidate["revision_id"])
    entries = {entry["id"]: entry for entry in virtual["structures"]}
    source_liver = binary(Path(base["structures"][0]["mask_file"]))[0]
    target = binary(Path(entries[draft["structure_id"]]["mask_file"]))[0]
    np.testing.assert_array_equal(binary(Path(entries["total:liver"]["mask_file"]))[0], source_liver & ~target)
    assert entries["total:liver"]["voxel_count"] == 124
    old_preview = service.reviews.candidate_revision(case["id"], parent_alias)
    np.testing.assert_array_equal(binary(Path(old_preview["structures"][0]["mask_file"]))[0], source_liver)
    published = public_review(draft)
    assert "mask_file" not in str(published) and str(service.settings.data_dir) not in str(published)
    accepted_job = execute_next(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
    assert accepted_job["status"] == "completed", accepted_job
    saved = service.store.revision(case["id"], accepted_job["revision_id"])
    assert saved["structures"][1] == base["structures"][1]
    assert saved["surface_assets"][1] == base["surface_assets"][1]
    assert saved["structures"][0]["relationship"]["exclusive_with"] == [draft["structure_id"]]
    ledger = {entry["id"]: entry for entry in saved["operation"]["affected_structures"]}
    saved_structures = {entry["id"]: entry for entry in saved["structures"]}
    for structure_id, source in (("total:liver", base["structures"][0]["mask_file"]), (draft["structure_id"], draft["target"]["mask_file"])):
        assert ledger[structure_id]["before_mask_sha256"] == hashlib.sha256(Path(source).read_bytes()).hexdigest()
        assert ledger[structure_id]["after_mask_sha256"] == hashlib.sha256(Path(saved_structures[structure_id]["mask_file"]).read_bytes()).hexdigest()
    assert str(service.settings.data_dir) not in str(saved["operation"])
    later = service.reviews.create(case["id"], saved["id"], "total:liver")
    assert later["relationship"] == {"kind": "subdivision", "exclusive_with": [draft["structure_id"]]}
    service.store.activate(case["id"], base["id"])
    assert service.store.revision(case["id"], base["id"]) == original


def test_sibling_only_change_can_empty_peer_and_metadata_only_choice_removes_reverse_link(example, monkeypatch):
    service, case, base, _ = add_peer(example)
    original = copy.deepcopy(base)

    def no_gpu():
        raise AssertionError("Relationship-only processing must not initialize the neural runtime")

    monkeypatch.setattr(service, "_interactive", no_gpu)
    draft = service.reviews.create(case["id"], base["id"], "total:liver")
    draft = apply_relationship(service, case, draft, {"kind": "subdivision", "exclusive_with": ["custom:peer"]})
    candidate = draft["candidate"]
    assert candidate["summary"]["changed_voxels"] == 0 and candidate["total_changed_voxels"] == 1
    assert candidate["affected_structures"][1]["summary"]["after_voxels"] == 0
    assert "empty" in candidate["warnings"][0]
    result = execute_next(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
    assert result["status"] == "completed", result
    saved = service.store.revision(case["id"], result["revision_id"])
    assert saved["structures"][2]["voxel_count"] == 0 and saved["structures"][2]["volume_ml"] == 0
    assert saved["structures"][2]["relationship"]["exclusive_with"] == ["total:liver"]
    assert saved["surface_assets"][0] == base["surface_assets"][0]
    assert saved["structures"][0]["mask_file"] == base["structures"][0]["mask_file"]
    assert saved["structures"][1] == base["structures"][1]
    assert (Path(saved["directory"]) / "model.glb").stat().st_size > 0
    independent = service.reviews.create(case["id"], saved["id"], "total:liver")
    independent = apply_relationship(service, case, independent, INDEPENDENT)
    assert independent["candidate"]["total_changed_voxels"] == 0 and independent["candidate"]["relationship_changed"]
    assert all(entry["metadata_only"] for entry in independent["candidate"]["affected_structures"])
    result = execute_next(service, service.reviews.accept(case["id"], independent["id"], independent["generation"]))
    assert result["status"] == "completed", result
    final = service.store.revision(case["id"], result["revision_id"])
    assert final["structures"][0]["relationship"] == final["structures"][2]["relationship"] == INDEPENDENT
    assert final["surface_assets"] == saved["surface_assets"]
    assert service.reviews.create(case["id"], final["id"], "custom:peer")["relationship"] == INDEPENDENT
    service.store.activate(case["id"], base["id"])
    assert service.store.revision(case["id"], base["id"]) == original


def test_undoing_unapplied_relationship_restores_exact_immutable_candidate_without_inference(example):
    service, case, _, _ = example
    draft = new_candidate(example)
    original = draft["candidate"]
    changed = service.reviews.update(
        case["id"], draft["id"], draft["generation"], draft["prompts"], relationship={"kind": "subdivision", "exclusive_with": ["total:liver"]}
    )
    assert changed["candidate"]["generation"] < changed["generation"] and public_review(changed)["relationship_dirty"]
    restored = service.reviews.update(case["id"], draft["id"], changed["generation"], draft["prompts"], relationship=INDEPENDENT)
    assert restored["status"] == "ready" and restored["candidate"]["generation"] == restored["generation"]
    assert restored["candidate"]["revision_id"] != original["revision_id"]
    assert restored["candidate"]["mask_file"] == original["mask_file"]
    assert restored["candidate"]["affected_structures"] == original["affected_structures"]
    assert not public_review(restored)["relationship_dirty"] and service.interactive_runtime.calls == 1
    assert (
        service.reviews.candidate_revision(case["id"], original["revision_id"])["structures"]
        == service.reviews.candidate_revision(case["id"], restored["candidate"]["revision_id"])["structures"]
    )


@pytest.mark.parametrize(
    "relationship,reason",
    [
        ({"kind": "independent", "exclusive_with": ["b"]}, "Independent"),
        ({"kind": "subregion", "exclusive_with": []}, "parent"),
        ({"kind": "subdivision", "exclusive_with": []}, "at least one"),
        ({"kind": "subdivision", "exclusive_with": ["a"]}, "excluding"),
        ({"kind": "subdivision", "exclusive_with": ["foreign"]}, "existing"),
        ({"kind": "subdivision", "exclusive_with": ["b", "b"]}, "unique"),
        ({"kind": "subregion", "parent_id": "child"}, "cycle"),
        ({"kind": "subdivision", "exclusive_with": ["child"]}, "Parent and child"),
        ({"kind": "subdivision", "parent_id": "b", "exclusive_with": ["b"]}, "Parent and child"),
    ],
)
def test_relationship_validation_protects_identity_and_parent_graph(relationship, reason):
    structures = [{"id": "a"}, {"id": "b"}, {"id": "child", "relationship": {"kind": "subregion", "parent_id": "a", "exclusive_with": []}}]
    with pytest.raises(ValueError, match=reason):
        normalize_relationship(relationship, structures, "a")


def test_curated_exact_lobe_suggestions_are_opt_in_and_direct_links_never_expand_transitively():
    lobes = [{"id": value} for value in ["total:lung_upper_lobe_left", "total:lung_lower_lobe_left", "total:liver"]]
    assert relationship_suggestions(lobes, lobes[0]["id"])[0]["exclusive_with"] == [lobes[1]["id"]]
    assert effective_relationship(lobes, lobes[0]["id"]) == INDEPENDENT
    assert relationship_suggestions(lobes, "total:liver") == []
    assert relationship_suggestions([{"id": "custom:lung_upper_lobe_left", "group": "Lung lobes"}], "custom:lung_upper_lobe_left") == []
    structures = [
        {"id": "a", "relationship": {"kind": "subdivision", "exclusive_with": ["b"]}},
        {"id": "b", "relationship": {"kind": "subdivision", "exclusive_with": ["c"]}},
        {"id": "c"},
    ]
    assert effective_relationship(structures, "c")["exclusive_with"] == ["b"]
    updates = reciprocal_updates(structures, structures[0], INDEPENDENT)
    assert updates == {"a": INDEPENDENT}


@pytest.mark.parametrize("tamper", ["source", "candidate"])
def test_affected_peer_hashes_guard_atomic_acceptance(example, tamper):
    service, case, base, _ = example
    draft = new_candidate(example)
    draft = apply_relationship(service, case, draft, {"kind": "subdivision", "exclusive_with": ["total:liver"]})
    path = Path(base["structures"][0]["mask_file"] if tamper == "source" else draft["candidate"]["affected_structures"][1]["mask_file"])
    image, _ = imaging._image(Path(base["ct_file"]))
    imaging._save_mask(np.zeros(image.shape, dtype=np.uint8), image, path)
    result = execute_next(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
    assert result["status"] == "failed" and "changed" in result["error"]
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]
    assert len(service.store.list("revisions", case["id"])) == 1


def test_http_relationship_preview_recovery_stale_save_and_affected_mask_transport(example):
    service, case, base, _ = example
    draft = new_candidate(example)
    app = create_app(service.settings)
    # Claim the queued operation explicitly through the fixture's deterministic
    # worker. Both API and worker use the same persistent, synthetic store.
    app.state.service.start = lambda **_: None
    app.state.service.stop = lambda: None
    with TestClient(app, base_url="http://127.0.0.1") as http:
        prefix = f"/api/cases/{case['id']}/reviews/{draft['id']}"
        updated = http.patch(
            prefix,
            json={
                "expected_generation": draft["generation"],
                "prompts": draft["prompts"],
                "relationship": {"kind": "subdivision", "exclusive_with": ["total:liver"]},
            },
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["relationship_dirty"] and updated.json()["dirty"]
        assert http.get(prefix).json()["relationship"] == updated.json()["relationship"]
        assert http.post(prefix + "/accept", json={"generation": updated.json()["generation"]}).status_code == 409
        response = http.post(prefix + "/predict", json={"generation": updated.json()["generation"]})
        assert response.status_code == 200, response.text
        result = execute_next(service, service.store.get("jobs", response.json()["id"]))
        assert result["status"] == "completed", result
        candidate = http.get(prefix).json()["candidate"]
        assert candidate["affected_structures"][1]["summary"]["removed_voxels"] == 1
        assert candidate["relationship_changed"] and candidate["total_changed_voxels"] == 2
        alias = f"/api/cases/{case['id']}/revisions/{candidate['revision_id']}"
        current = http.get(alias + "/mask-volume", params={"structure_id": "total:liver", "compare": True})
        assert current.status_code == 200, current.text
        assert current.json()["current"]["key"] != current.json()["parent"]["key"]
        assert str(service.settings.data_dir) not in str(candidate) and "mask_file" not in str(candidate)


# Inference paths: changing a relationship runs no model on any card


def on_a_card(example, name="nvidia"):
    service, case, base, prompt = example
    draft = service.reviews.create_structure(case["id"], base["id"], "Accessory region")
    draft = service.reviews.update(case["id"], draft["id"], 0, [{**prompt, "include": True}])
    job = execute_next(service, service.reviews.predict(case["id"], draft["id"], draft["generation"], inference_path=name))
    assert job["status"] == "completed", job
    return service.reviews.get(case["id"], draft["id"])


def test_a_relationship_only_apply_leaves_the_draft_on_its_card(example, paths):
    service, case, _, _ = example
    draft = on_a_card(example)
    applied = apply_relationship(service, case, draft, {"kind": "subregion", "parent_id": "total:liver", "exclusive_with": []})
    assert applied["candidate"]["revision_id"] != draft["candidate"]["revision_id"]
    assert applied["inference_path"] == binding("nvidia") and public_review(applied)["inference_path"] == binding("nvidia")
    assert len(service.interactive_runtime.calls) == 1  # No model ran.
    # It reuses that card's mask, and the record says so.
    assert applied["candidate"]["gpu_name"] == GPU_NAME and applied["candidate"]["provenance"]["inference_path"] == "nvidia"
    assert applied["candidate"]["provenance"]["reused_prediction"]


@pytest.mark.parametrize("name", ["second", "default", "absent"])
def test_a_stale_client_cannot_break_a_relationship_only_apply(example, paths, name):
    # It runs no model, so a choice it ignores can neither conflict nor be refused.
    service, case, _, _ = example
    draft = on_a_card(example)
    choice = {"kind": "subregion", "parent_id": "total:liver", "exclusive_with": []}
    draft = service.reviews.update(case["id"], draft["id"], draft["generation"], draft["prompts"], relationship=choice)
    job = service.reviews.predict(case["id"], draft["id"], draft["generation"], inference_path=name)
    assert job["payload"]["relationship_only"] and "inference_path" not in job and job_lane(job) == "cpu"
    assert service.store.claim("gpu") is None
    assert execute_next(service, job)["status"] == "completed"
    assert service.reviews.get(case["id"], draft["id"])["inference_path"] == binding("nvidia")


def test_a_relationship_only_apply_on_a_free_draft_binds_nothing(example, paths):
    service, case, base, _ = example
    draft = service.reviews.create(case["id"], base["id"], "total:liver")
    draft = service.reviews.update(case["id"], draft["id"], 0, [], relationship={"kind": "subregion", "parent_id": "total:kidney_left", "exclusive_with": []})
    job = service.reviews.predict(case["id"], draft["id"], draft["generation"], inference_path="nvidia")
    assert job["payload"]["relationship_only"] and "inference_path" not in job
    assert execute_next(service, job)["status"] == "completed"
    ready = service.reviews.get(case["id"], draft["id"])
    assert ready["inference_path"] is None and "gpu_name" not in ready["candidate"] and service.interactive_runtime.calls == []


def test_a_relationship_change_needing_the_model_still_keeps_the_lock(example, paths):
    # Pending marks make this a model run, so here a choice counts.
    service, case, _, prompt = example
    draft = on_a_card(example)
    marks = [*draft["prompts"], {**prompt, "id": "point-2", "include": True}]
    choice = {"kind": "subregion", "parent_id": "total:liver", "exclusive_with": []}
    draft = service.reviews.update(case["id"], draft["id"], draft["generation"], marks, relationship=choice)
    with pytest.raises(ValueError, match="came from"):
        service.reviews.predict(case["id"], draft["id"], draft["generation"], inference_path="second")
    job = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    assert not job["payload"].get("relationship_only") and job["inference_path"] == binding("nvidia")
