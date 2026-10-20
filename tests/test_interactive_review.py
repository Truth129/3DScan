"""Review-state, geometry and HTTP tests with a deterministic inference boundary.

The fake engine exercises publication and acceptance, not model accuracy. Native
nnInteractive behavior is covered separately by adapter and workstation checks.
"""

import json
from pathlib import Path
import time
import zipfile

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from scan3d import imaging
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.review import public_review
from scan3d.service import Service
from scan3d.store import Store, identifier


class FakeRuntime:
    def __init__(self, mode="change", during=None):
        self.mode, self.during = mode, during
        self.calls, self.released = [], False

    def predict(self, ct_path, mask_path, prompts, output_path, progress, cancelled, session_key, **kwargs):
        self.calls.append((ct_path, mask_path, prompts, session_key, kwargs))
        image, affine = imaging._image(ct_path)
        mask, _, _ = imaging._binary(mask_path, image, affine)
        if self.mode == "change":
            mask[2, 2, 2] = False
            mask[1, 1, 1] = True
        elif self.mode == "empty":
            mask[:] = False
        imaging._save_mask(mask, image, output_path)
        if self.during:
            self.during()
        return {"engine": "fake-nninteractive", "version": "test", "checkpoint_sha256": "a" * 64,
                "device": "test-boundary"}

    def release(self):
        self.released = True


@pytest.fixture
def example(tmp_path):
    service = Service(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    case = service.new_case("Oblique reflected correction fixture")
    folder = service.store.case_dir(case["id"]) / "fixture"
    folder.mkdir()
    angle = .24
    rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    affine = np.eye(4)
    affine[:3, :3] = rotation @ np.diag([-1.5, 2., 3.])
    affine[:3, 3] = [20, -10, 40]
    shape = (12, 10, 8)
    ct = nib.Nifti1Image(np.full(shape, 45, dtype=np.int16), affine)
    ct.header.set_xyzt_units("mm")
    ct_file = folder / "reference.nii.gz"
    nib.save(ct, ct_file)
    structures = []
    for name, bounds in (("liver", (slice(2, 7), slice(2, 7), slice(2, 7))),
                         ("kidney_left", (slice(8, 11), slice(2, 5), slice(2, 6)))):
        mask = np.zeros(shape, dtype=np.uint8)
        mask[bounds] = 1
        target = folder / f"{name}.nii.gz"
        imaging._save_mask(mask, ct, target)
        structures.append(dict(id=f"total:{name}", name=name, task="total", group="Abdominal organs",
                               color="#c08090", mask_file=str(target)))
    revision = service.build_revision(case["id"], ct_file, structures, source="synthetic_fixture",
        acquisition={"series_uid": "fixture", "phase": "venous"},
        inference_manifest={"engine": "fixture", "engine_version": "test", "qc": []})
    service.store.commit_revision(revision)
    service.interactive_runtime = FakeRuntime()
    prompt = dict(id="point-1", kind="point", ras=imaging._world(affine, [4, 4, 4]).tolist(), include=False)
    return service, case, revision, prompt


def execute_next(service, job):
    claimed = service.store.claim()
    assert claimed["id"] == job["id"]
    service.execute(claimed)
    return service.store.get("jobs", job["id"])


def candidate(example):
    service, case, base, prompt = example
    draft = service.reviews.create(case["id"], base["id"], "total:liver")
    draft = service.reviews.update(case["id"], draft["id"], 0, [prompt])
    result = execute_next(service, service.reviews.predict(case["id"], draft["id"], draft["generation"]))
    assert result["status"] == "completed", result
    return service.reviews.get(case["id"], draft["id"])


def test_prediction_is_a_draft_and_accept_changes_only_selected_structure(example):
    service, case, base, _ = example
    original = json.loads(json.dumps(base))
    original_bytes = {s["id"]: Path(s["mask_file"]).read_bytes() for s in base["structures"]}
    draft = candidate(example)
    assert draft["status"] == "ready" and draft["generation"] == 1
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]
    assert len(service.store.list("revisions", case["id"])) == 1
    summary = draft["candidate"]["summary"]
    assert summary["changed_voxels"] == 2 and summary["added_voxels"] == summary["removed_voxels"] == 1
    assert summary["before_voxels"] == summary["after_voxels"] == 125
    assert summary["before_volume_ml"] == pytest.approx(125 * 9 / 1000)
    virtual = service.reviews.candidate_revision(case["id"], draft["candidate"]["revision_id"])
    assert virtual["parent_id"] == base["id"] and virtual["ct_file"] == base["ct_file"]
    np.testing.assert_allclose(virtual["geometry"]["affine_ras"], base["geometry"]["affine_ras"])
    published = public_review(draft)
    assert "candidates" not in published and "mask_file" not in json.dumps(published)
    assert str(service.settings.data_dir) not in json.dumps(published)
    accepted_job = execute_next(service, service.reviews.accept(case["id"], draft["id"], 1))
    assert accepted_job["status"] == "completed", accepted_job
    corrected = service.store.revision(case["id"], accepted_job["revision_id"])
    assert corrected["source"] == "nninteractive_correction"
    assert corrected["review_status"] == "corrected" and corrected["parent_id"] == base["id"]
    assert corrected["inference_manifest"] == base["inference_manifest"]
    assert corrected["acquisition"] == base["acquisition"]
    assert corrected["operation"]["summary"] == summary
    assert corrected["operation"]["prompts"] == draft["prompts"]
    assert corrected["structures"][1] == base["structures"][1]
    assert corrected["surface_assets"][1] == base["surface_assets"][1]
    assert corrected["structures"][0]["mask_file"] != base["structures"][0]["mask_file"]
    assert corrected["surface_assets"][0]["key"] != base["surface_assets"][0]["key"]
    assert service.store.revision(case["id"], base["id"]) == original
    assert all(Path(s["mask_file"]).read_bytes() == original_bytes[s["id"]] for s in base["structures"])
    stored = service.reviews.get(case["id"], draft["id"])
    assert stored["status"] == "accepted" and stored["accepted_revision_id"] == corrected["id"]
    service.store.activate(case["id"], base["id"])
    restored = service.store.revision(case["id"], service.store.get("cases", case["id"])["active_revision_id"])
    assert restored == original
    with pytest.raises(ValueError, match="closed"):
        service.reviews.update(case["id"], draft["id"], 1, [])


def test_prompt_undo_invalidates_current_candidate_but_retains_immutable_preview(example):
    service, case, base, _ = example
    draft = candidate(example)
    alias = draft["candidate"]["revision_id"]
    previous_path = service.reviews.candidate_revision(case["id"], alias)["structures"][0]["mask_file"]
    updated = service.reviews.update(case["id"], draft["id"], 1, [])
    assert updated["generation"] == 2 and updated["candidate"] is None and updated["prompts"] == []
    assert service.reviews.candidate_revision(case["id"], alias)["structures"][0]["mask_file"] == previous_path
    with pytest.raises(ValueError, match="changed"):
        service.reviews.update(case["id"], draft["id"], 1, [])
    with pytest.raises(ValueError, match="changed"):
        service.reviews.accept(case["id"], draft["id"], 1)
    with pytest.raises(ValueError, match="prompt"):
        service.reviews.predict(case["id"], draft["id"], 2)
    service.reviews.discard(case["id"], draft["id"], 2)
    with pytest.raises(KeyError, match="discarded"):
        service.reviews.candidate_revision(case["id"], alias)
    assert len(service.store.list("revisions", case["id"])) == 1


@pytest.mark.parametrize("mode,reason", [("same", "unchanged"), ("empty", "empty")])
def test_no_change_and_empty_candidates_can_be_seen_but_not_accepted(example, mode, reason):
    service, case, base, _ = example
    service.interactive_runtime = FakeRuntime(mode)
    draft = candidate(example)
    assert draft["candidate"] is not None
    with pytest.raises(ValueError, match=reason):
        service.reviews.accept(case["id"], draft["id"], 1)
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]


def test_point_validation_uses_oblique_reflected_grid_and_rejects_bad_prompts(example):
    service, case, base, prompt = example
    draft = service.reviews.create(case["id"], base["id"], "total:liver")
    bad = dict(prompt, ras=[1e6, 0, 0])
    with pytest.raises(ValueError, match="outside"):
        service.reviews.update(case["id"], draft["id"], 0, [bad])
    with pytest.raises(ValueError, match="finite"):
        service.reviews.update(case["id"], draft["id"], 0, [dict(prompt, ras=[0, float("nan"), 0])])
    with pytest.raises(ValueError, match="unique"):
        service.reviews.update(case["id"], draft["id"], 0, [prompt, prompt])
    with pytest.raises(ValueError, match="128"):
        service.reviews.update(case["id"], draft["id"], 0, [dict(prompt, id=str(i)) for i in range(129)])
    assert service.reviews.get(case["id"], draft["id"])["generation"] == 0
    valid = service.reviews.update(case["id"], draft["id"], 0, [prompt])
    assert valid["prompts"] == [prompt]
    other = service.new_case("Other case")
    with pytest.raises(KeyError, match="belong"):
        service.reviews.get(other["id"], draft["id"])


def test_new_prompts_cancel_and_supersede_an_inflight_prediction(example):
    service, case, base, prompt = example
    draft = service.reviews.create(case["id"], base["id"], "total:liver")
    service.reviews.update(case["id"], draft["id"], 0, [prompt])
    service.interactive_runtime = FakeRuntime(during=lambda: service.reviews.update(case["id"], draft["id"], 1, []))
    job = execute_next(service, service.reviews.predict(case["id"], draft["id"], 1))
    assert job["status"] == "cancelled"
    current = service.reviews.get(case["id"], draft["id"])
    assert current["generation"] == 2 and current["status"] == "draft" and current["candidate"] is None
    assert current["candidates"] == {}
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]


def test_cancelled_acceptance_cannot_activate_or_close_draft(example, monkeypatch):
    service, case, base, _ = example
    draft = candidate(example)
    job = service.reviews.accept(case["id"], draft["id"], 1)
    commit = service.store.commit_revision

    def cancelled_commit(revision, queued, **kwargs):
        service.store.update_job(queued["id"], cancel_requested=True)
        return commit(revision, queued, **kwargs)

    monkeypatch.setattr(service.store, "commit_revision", cancelled_commit)
    result = execute_next(service, job)
    assert result["status"] == "cancelled"
    current = service.reviews.get(case["id"], draft["id"])
    assert current["status"] == "ready" and current["accepted_revision_id"] is None
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]
    assert len(service.store.list("revisions", case["id"])) == 1


def test_active_revision_cas_protects_acceptance_after_surface_preparation(example, monkeypatch):
    service, case, base, _ = example
    alternative = {**base, "id": identifier()}
    service.store.save("revisions", alternative)
    draft = candidate(example)
    job = service.reviews.accept(case["id"], draft["id"], 1)
    build = service.build_revision

    def concurrent_activation(*args, **kwargs):
        result = build(*args, **kwargs)
        service.store.activate(case["id"], alternative["id"])
        return result

    monkeypatch.setattr(service, "build_revision", concurrent_activation)
    result = execute_next(service, job)
    assert result["status"] == "failed" and "active revision changed" in result["error"]
    assert service.store.get("cases", case["id"])["active_revision_id"] == alternative["id"]
    assert len(service.store.list("revisions", case["id"])) == 2
    assert service.reviews.get(case["id"], draft["id"])["status"] == "ready"


def test_restart_preserves_prompts_and_completed_candidates(example):
    service, case, base, prompt = example
    draft = service.reviews.create(case["id"], base["id"], "total:liver")
    service.reviews.update(case["id"], draft["id"], 0, [prompt])
    queued = service.reviews.predict(case["id"], draft["id"], 1)
    service.store.claim()
    reopened = Store(service.settings.data_dir)
    reopened.recover()
    recovered = service.reviews.get(case["id"], draft["id"])
    assert recovered["status"] == "draft" and recovered["prompts"] == [prompt]
    assert "restarted" in recovered["error"]
    assert reopened.get("jobs", queued["id"])["status"] == "failed"
    retry = execute_next(service, service.reviews.predict(case["id"], draft["id"], 1))
    assert retry["status"] == "completed"
    ready = service.reviews.get(case["id"], draft["id"])
    service.reviews.accept(case["id"], draft["id"], 1)
    service.store.claim()
    reopened.recover()
    recovered = service.reviews.get(case["id"], draft["id"])
    assert recovered["status"] == "ready" and recovered["candidate"] == ready["candidate"]
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]


def test_interactive_runtime_released_before_totalsegmentator_job(example, monkeypatch):
    service, case, base, _ = example
    runtime = service.interactive_runtime

    def process(job):
        assert runtime.released and service.interactive_runtime is None
        return {**base, "id": identifier()}

    monkeypatch.setattr(service, "_process", process)
    result = execute_next(service, service.store.enqueue(case["id"], "process", {}))
    assert result["status"] == "completed"


def test_review_http_candidates_reuse_ct_and_authorize_masks_frames_and_history(example):
    service, case, base, prompt = example
    # Exercise the normal API worker without loading a model in this fixture.
    app = create_app(service.settings)
    app.state.service.interactive_runtime = service.interactive_runtime
    api_service = app.state.service
    prefix = f"/api/cases/{case['id']}"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        response = client.post(f"{prefix}/reviews", json={"revision_id": base["id"], "structure_id": "total:liver"})
        assert response.status_code == 200, response.text
        draft = response.json()
        review_url = f"{prefix}/reviews/{draft['id']}"
        response = client.patch(review_url, json={"expected_generation": 0, "prompts": [prompt]})
        assert response.status_code == 200, response.text
        stale = client.patch(review_url, json={"expected_generation": 0, "prompts": []})
        assert stale.status_code == 409, stale.text
        assert client.get(review_url).json()["generation"] == 1
        prediction = client.post(review_url + "/predict", json={"generation": 1})
        assert prediction.status_code == 200, prediction.text
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = client.get(f"/api/jobs/{prediction.json()['id']}").json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(.02)
        assert job["status"] == "completed", job
        current = client.get(review_url).json()
        assert current["status"] == "ready" and current["candidate"]["generation"] == 1
        assert str(service.settings.data_dir) not in json.dumps(current)
        alias = current["candidate"]["revision_id"]
        original_url, candidate_url = f"{prefix}/revisions/{base['id']}", f"{prefix}/revisions/{alias}"
        original_ct = client.get(original_url + "/ct-volume").json()
        candidate_ct = client.get(candidate_url + "/ct-volume").json()
        assert original_ct["key"] == candidate_ct["key"]
        query = {"structure_id": "total:liver", "compare": "true"}
        masks = client.get(candidate_url + "/mask-volume", params=query)
        assert masks.status_code == 200, masks.text
        original_mask = client.get(original_url + "/mask-volume", params={"structure_id": "total:liver"}).json()["current"]
        assert masks.json()["parent"]["key"] == original_mask["key"]
        assert masks.json()["current"]["key"] != original_mask["key"]
        assert client.get(masks.json()["current"]["chunks"][0]["url"]).status_code == 200
        frame_query = {**query, "plane": "axial", "position_mm": base["geometry"]["center_ras"][2]}
        frame = client.get(candidate_url + "/review-frame", params=frame_query)
        assert frame.status_code == 200, frame.text
        assert "X-CT-Frame" in frame.headers
        png = client.get(candidate_url + "/slice", params=frame_query)
        assert png.status_code == 200 and png.content.startswith(b"\x89PNG")
        assert client.get(candidate_url).status_code in (400, 404)
        assert client.get(candidate_url + "/model.glb").status_code in (400, 404)
        assert len(client.get(prefix + "/revisions").json()) == 1
        other = api_service.new_case("Unauthorized candidate")
        denied = f"/api/cases/{other['id']}/revisions/{alias}"
        assert client.get(denied + "/ct-volume").status_code == 404
        assert client.get(denied + "/mask-volume", params=query).status_code == 404
        assert client.get(f"/api/cases/{other['id']}/reviews/{draft['id']}").status_code == 404
        assert client.post(review_url + "/discard", json={"expected_generation": 1}).status_code == 200
        assert client.get(candidate_url + "/ct-volume").status_code == 404


def test_apply_boundaries_persist_only_after_publication_and_pending_undo_is_exact(example):
    service, case, base, prompt = example
    draft = candidate(example)
    assert draft["batches"] == [{"end": 1, "autozoom": True, "full_refine": False}]
    extra = dict(prompt, id="pending-2", include=True)
    edited = service.reviews.update(case["id"], draft["id"], 1, [prompt, extra])
    assert edited["candidate"]["revision_id"] == draft["candidate"]["revision_id"]
    public = public_review(edited)
    assert public["dirty"] and public["pending_count"] == 1 and public["applied_prompt_count"] == 1
    with pytest.raises(ValueError, match="candidate"):
        service.reviews.accept(case["id"], draft["id"], 2)
    restored = service.reviews.update(case["id"], draft["id"], 2, [prompt])
    assert not public_review(restored)["dirty"] and restored["status"] == "ready"
    assert restored["candidate"]["generation"] == 3
    assert restored["candidate"]["mask_file"] == draft["candidate"]["mask_file"]
    assert restored["candidate"]["revision_id"] != draft["candidate"]["revision_id"]
    assert len(service.interactive_runtime.calls) == 1
    next_state = service.reviews.update(case["id"], draft["id"], 3, [prompt, extra])
    job = service.reviews.predict(case["id"], draft["id"], 4)
    assert len(service.reviews.get(case["id"], draft["id"])["batches"]) == 1
    assert job["payload"]["batches"][-1]["end"] == 2
    assert execute_next(service, job)["status"] == "completed"
    assert len(service.reviews.get(case["id"], draft["id"])["batches"]) == 2
    assert service.interactive_runtime.calls[-1][-1]["options"] == {"autozoom": True, "start_empty": False}


def test_whole_refine_gets_fresh_alias_and_options_changes_reset_replay(example):
    service, case, base, prompt = example
    draft = candidate(example)
    full = service.reviews.predict(case["id"], draft["id"], 1, full_refine=True)
    assert full["payload"]["generation"] == 2
    assert full["payload"]["batches"][-1] == {"end": 1, "autozoom": True, "full_refine": True}
    assert execute_next(service, full)["status"] == "completed"
    state = service.reviews.get(case["id"], draft["id"])
    assert state["candidate"]["revision_id"] != draft["candidate"]["revision_id"]
    changed = service.reviews.update(case["id"], draft["id"], 2, [prompt], options={"autozoom": False, "start_empty": True})
    assert changed["candidate"] is None and changed["batches"] == []
    apply = service.reviews.predict(case["id"], draft["id"], 3)
    assert apply["payload"]["batches"] == [{"end": 1, "autozoom": False, "full_refine": False}]


def test_legacy_sessions_keep_pointwise_prediction_boundaries(example):
    service, case, base, prompt = example
    draft = candidate(example)
    legacy = {k: v for k, v in draft.items() if k not in {"options", "batches"}}
    legacy["schema_version"] = 1
    legacy["prompts"] = [{k: v for k, v in prompt.items() if k != "kind"}]
    legacy["candidate"] = {k: v for k, v in legacy["candidate"].items() if k not in {"options", "batches_snapshot", "prompts_snapshot"}}
    service.store.save("review_sessions", legacy)
    reopened = service.reviews.get(case["id"], draft["id"])
    assert reopened["prompts"][0]["kind"] == "point"
    assert reopened["batches"] == [{"end": 1, "autozoom": True, "full_refine": False, "legacy": True}]


def test_http_mixed_tools_apply_once_full_refine_and_stale_generation(example):
    service, case, base, point = example
    app = create_app(service.settings)
    runtime = FakeRuntime()
    app.state.service.interactive_runtime = runtime
    prefix = f"/api/cases/{case['id']}"
    prompts = [{k: v for k, v in point.items() if k != "kind"},
               dict(id="box", kind="bbox", include=True, axis=2, index=4, vertices=[[2, 2], [6, 6]]),
               dict(id="stroke", kind="scribble", include=False, axis=1, index=3, vertices=[[2, 2], [5, 5]]),
               dict(id="loop", kind="lasso", include=True, axis=0, index=4, vertices=[[2, 2], [6, 2], [6, 6], [2, 6]])]

    def wait(client, job):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get(f"/api/jobs/{job['id']}").json()
            if result["status"] not in ("queued", "running"):
                assert result["status"] == "completed", result
                return result
            time.sleep(.02)
        pytest.fail("Mixed interaction job did not finish")

    with TestClient(app, base_url="http://127.0.0.1") as http:
        status = http.get("/api/interactive/status").json()
        assert status["capabilities"]["bbox2d"] and not status["capabilities"]["bbox3d"]
        created = http.post(prefix + "/reviews", json={"revision_id": base["id"], "structure_id": "total:liver"}).json()
        url = prefix + "/reviews/" + created["id"]
        update = http.patch(url, json={"expected_generation": 0, "prompts": prompts,
                                      "options": {"autozoom": False, "start_empty": True}})
        assert update.status_code == 200, update.text
        assert update.json()["prompts"][0]["kind"] == "point"
        assert update.json()["prompts"][2]["radius_mm"] == 3
        assert update.json()["pending_count"] == 4 and update.json()["batches"] == []
        assert runtime.calls == []
        invalid = http.patch(url, json={"expected_generation": 1, "prompts": [{**prompts[1], "axis": 3}]})
        assert invalid.status_code == 422
        prediction = http.post(url + "/predict", json={"generation": 1})
        assert prediction.status_code == 200, prediction.text
        wait(http, prediction.json())
        assert runtime.calls[0][-1]["batches"] == [{"end": 4, "autozoom": False, "full_refine": False}]
        ready = http.get(url).json()
        assert not ready["dirty"] and ready["applied_prompt_count"] == 4
        whole = http.post(url + "/predict", json={"generation": 1, "full_refine": True})
        assert whole.status_code == 200, whole.text
        stale = http.post(url + "/predict", json={"generation": 1, "full_refine": True})
        assert stale.status_code == 409, stale.text
        wait(http, whole.json())
        final = http.get(url).json()
        assert final["generation"] == 2 and final["candidate"]["generation"] == 2
        assert final["batches"][-1] == {"end": 4, "autozoom": False, "full_refine": True}
        assert http.get(prefix).json()["active_revision_id"] == base["id"]
        assert len(http.get(prefix + "/revisions").json()) == 1


def test_new_named_structure_stays_empty_draft_until_explicit_acceptance(example, tmp_path):
    from scan3d import slicer_io
    service, case, base, prompt = example
    before = json.loads(json.dumps(base))
    draft = service.reviews.create_structure(case["id"], base["id"], "  Accessory finding  ", "Teaching", "#AB12Ef")
    target = draft["target"]
    assert target["id"].startswith("custom:") and target["id"] == draft["structure_id"]
    assert target["name"] == "Accessory finding" and target["color"] == "#ab12ef" and target["task"] == "custom"
    assert target["volume_ml"] == 0 and draft["options"]["start_empty"]
    original_mask, _, original_affine = imaging._binary(Path(target["mask_file"]))
    assert not original_mask.any()
    np.testing.assert_allclose(original_affine, base["geometry"]["affine_ras"])
    public = public_review(draft)
    assert public["new_structure"] and public["target"]["voxel_count"] == 0
    assert "mask_file" not in json.dumps(public) and str(service.settings.data_dir) not in json.dumps(public)
    alias0 = public["preview_revision_id"]
    preview = service.reviews.candidate_revision(case["id"], alias0)
    assert preview["structures"][:-1] == base["structures"]
    assert preview["structures"][-1]["id"] == target["id"] and preview["structures"][-1]["voxel_count"] == 0
    assert len(service.store.list("revisions", case["id"])) == 1
    service.reviews.update(case["id"], draft["id"], 0, [dict(prompt, include=True)])
    result = execute_next(service, service.reviews.predict(case["id"], draft["id"], 1))
    assert result["status"] == "completed", result
    ready = service.reviews.get(case["id"], draft["id"])
    assert ready["candidate"]["summary"]["before_voxels"] == 0
    assert ready["candidate"]["summary"]["added_voxels"] == ready["candidate"]["summary"]["after_voxels"] == 1
    candidate = service.reviews.candidate_revision(case["id"], ready["candidate"]["revision_id"])
    assert candidate["parent_id"] == alias0 and len(candidate["structures"]) == len(base["structures"]) + 1
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]
    accepted_job = execute_next(service, service.reviews.accept(case["id"], draft["id"], 1))
    assert accepted_job["status"] == "completed", accepted_job
    accepted = service.store.revision(case["id"], accepted_job["revision_id"])
    assert accepted["source"] == "nninteractive_new_structure" and accepted["parent_id"] == base["id"]
    assert accepted["structures"][:-1] == base["structures"]
    assert accepted["surface_assets"][:-1] == base["surface_assets"]
    assert accepted["structures"][-1]["id"] == target["id"]
    assert accepted["structures"][-1]["volume_ml"] == pytest.approx(9 / 1000)
    assert accepted["structures"][-1]["name"] == target["name"]
    assert accepted["structures"][-1]["color"] == target["color"]
    assert accepted["inference_manifest"] == base["inference_manifest"]
    assert accepted["operation"]["new_structure"]["id"] == target["id"]
    assert "mask_file" not in accepted["operation"]["new_structure"]
    assert service.store.revision(case["id"], base["id"]) == before
    assert not imaging._binary(Path(target["mask_file"]))[0].any()
    again = service.reviews.create(case["id"], accepted["id"], target["id"])
    assert not again.get("new_structure") and service.reviews._base(again)[1]["mask_file"] == accepted["structures"][-1]["mask_file"]

    # Custom IDs and names survive the existing overlap-preserving Slicer path.
    bundle = slicer_io.export_bundle(Path(base["ct_file"]), accepted["structures"], tmp_path / "custom-slicer.zip")
    with zipfile.ZipFile(bundle) as archive:
        mapping = json.loads(archive.read("mapping.json"))
        assert mapping["structures"][-1]["id"] == target["id"]
        segmentation = tmp_path / "custom.seg.nrrd"
        segmentation.write_bytes(archive.read("segmentation.seg.nrrd"))
    imported = slicer_io.import_segmentation(Path(base["ct_file"]), segmentation, accepted["structures"],
                                            tmp_path / "reimported", require_source_hash=True)
    assert imported[-1]["id"] == target["id"] and imported[-1]["name"] == target["name"]
    for first, second in zip(accepted["structures"], imported):
        np.testing.assert_array_equal(imaging._binary(Path(first["mask_file"]))[0], imaging._binary(Path(second["mask_file"]))[0])
    service.store.activate(case["id"], base["id"])
    assert service.store.revision(case["id"], base["id"]) == before


def test_new_structure_discard_and_empty_result_never_add_anatomy(example):
    service, case, base, prompt = example
    draft = service.reviews.create_structure(case["id"], base["id"], "Discard me")
    alias = public_review(draft)["preview_revision_id"]
    service.reviews.discard(case["id"], draft["id"], 0)
    with pytest.raises(KeyError, match="discarded"):
        service.reviews.candidate_revision(case["id"], alias)
    empty = service.reviews.create_structure(case["id"], base["id"], "Empty proposal")
    service.interactive_runtime = FakeRuntime("empty")
    service.reviews.update(case["id"], empty["id"], 0, [prompt])
    assert execute_next(service, service.reviews.predict(case["id"], empty["id"], 1))["status"] == "completed"
    with pytest.raises(ValueError, match="empty"):
        service.reviews.accept(case["id"], empty["id"], 1)
    assert len(service.store.list("revisions", case["id"])) == 1
    assert service.store.revision(case["id"], base["id"])["structures"] == base["structures"]


@pytest.mark.parametrize("values", [{"name": "  "}, {"name": "bad\nname"}, {"name": "a" * 129},
                                    {"name": "Valid", "group": ""}, {"name": "Valid", "color": "#f00"}])
def test_new_structure_metadata_is_validated_before_creation(example, values):
    service, case, base, _ = example
    with pytest.raises(ValueError):
        service.reviews.create_structure(case["id"], base["id"], **values)
    assert service.store.list("review_sessions", case["id"]) == []


def test_http_new_structure_empty_transport_candidate_comparison_and_atomic_save(example):
    service, case, base, point = example
    app = create_app(service.settings)
    app.state.service.interactive_runtime = FakeRuntime()
    prefix = f"/api/cases/{case['id']}"
    with TestClient(app, base_url="http://127.0.0.1") as http:
        response = http.post(prefix + "/reviews/new-structure", json={"revision_id": base["id"],
                             "name": "New browser label", "group": "Custom", "color": "#d977aa"})
        assert response.status_code == 200, response.text
        draft = response.json()
        assert draft["new_structure"] and draft["options"]["start_empty"]
        target_id, alias0 = draft["target"]["id"], draft["preview_revision_id"]
        assert "mask_file" not in json.dumps(draft)
        review_url = prefix + "/reviews/" + draft["id"]
        preview_url = prefix + "/revisions/" + alias0
        original_url = prefix + "/revisions/" + base["id"]
        assert http.get(preview_url + "/ct-volume").json()["key"] == http.get(original_url + "/ct-volume").json()["key"]
        zero = http.get(preview_url + "/mask-volume", params={"structure_id": target_id})
        assert zero.status_code == 200 and zero.json()["current"]["empty"]
        assert http.get(original_url + "/mask-volume", params={"structure_id": target_id}).status_code == 400
        other = app.state.service.new_case("Another case")
        denied_url = f"/api/cases/{other['id']}/revisions/{alias0}"
        assert http.get(denied_url + "/ct-volume").status_code == 404
        update = http.patch(review_url, json={"expected_generation": 0, "prompts": [{**point, "include": True}]})
        assert update.status_code == 200
        queued = http.post(review_url + "/predict", json={"generation": 1}).json()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = http.get("/api/jobs/" + queued["id"]).json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(.02)
        assert job["status"] == "completed", job
        ready = http.get(review_url).json()
        candidate_url = prefix + "/revisions/" + ready["candidate"]["revision_id"]
        masks = http.get(candidate_url + "/mask-volume", params={"structure_id": target_id, "compare": True}).json()
        assert not masks["current"]["empty"] and masks["parent"]["empty"]
        frame = http.get(candidate_url + "/review-frame", params={"structure_id": target_id, "compare": True,
                       "plane": "axial", "native_axis": 2, "native_index": 1})
        assert frame.status_code == 200, frame.text
        assert json.loads(frame.headers["X-CT-Frame"])["parent_offset"] is not None
        assert len(http.get(prefix + "/revisions").json()) == 1
        save = http.post(review_url + "/accept", json={"generation": 1}).json()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = http.get("/api/jobs/" + save["id"]).json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(.02)
        assert job["status"] == "completed", job
        accepted = http.get(prefix + "/revisions/" + job["revision_id"]).json()
        assert accepted["structures"][-1]["id"] == target_id and len(accepted["structures"]) == len(base["structures"]) + 1


def test_concurrent_new_targets_bound_dense_baseline_generation(example, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    service, case, base, _ = example
    save = imaging._save_mask
    guard = threading.Lock()
    active = peak = 0

    def measured_save(*args, **kwargs):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        try:
            time.sleep(.02)
            return save(*args, **kwargs)
        finally:
            with guard:
                active -= 1

    monkeypatch.setattr(imaging, "_save_mask", measured_save)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(service.reviews.create_structure, case["id"], base["id"], f"Concurrent target {i}") for i in range(2)]
        drafts = [future.result() for future in futures]
    assert peak == 1 and drafts[0]["structure_id"] != drafts[1]["structure_id"]
    assert all(not imaging._binary(Path(draft["target"]["mask_file"]))[0].any() for draft in drafts)
