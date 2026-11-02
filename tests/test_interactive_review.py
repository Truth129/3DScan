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

from inference_path_stub import GPU_NAME, activate, stand_in
from scan3d import imaging
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.inference_paths import binding, replaced
from scan3d.interactive import _batches
from scan3d.review import public_review
from scan3d.service import Service
from scan3d.store import ReviewConflict, Store, identifier, public_job, public_revision


DEFAULT_GPU = "Test Default GPU"
SECOND_GPU = "Test Second GPU"


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
        return {"engine": "fake-nninteractive", "version": "test", "checkpoint_sha256": "a" * 64, "device": "test-boundary"}

    def release(self):
        self.released = True


class MeasuringRuntime(FakeRuntime):
    """Reports the card a real child would measure on the path it was sent."""

    measures = None  # Set to a name to report a card the path's proof never saw.

    def predict(self, *args, **kwargs):
        provenance = super().predict(*args, **kwargs)
        chosen = kwargs.get("inference_path")
        if chosen is None:
            return {**provenance, "device": "gpu:0", "gpu_name": DEFAULT_GPU, "cuda_version": None}
        card = self.measures or chosen.gpu_name
        return {**provenance, "device": "gpu:0", "gpu_name": card, "cuda_version": "13.0.fake" if chosen.kind == "cuda" else None}


@pytest.fixture
def example(tmp_path):
    service = Service(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    case = service.new_case("Oblique reflected correction fixture")
    folder = service.store.case_dir(case["id"]) / "fixture"
    folder.mkdir()
    angle = 0.24
    rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    affine = np.eye(4)
    affine[:3, :3] = rotation @ np.diag([-1.5, 2.0, 3.0])
    affine[:3, 3] = [20, -10, 40]
    shape = (12, 10, 8)
    ct = nib.Nifti1Image(np.full(shape, 45, dtype=np.int16), affine)
    ct.header.set_xyzt_units("mm")
    ct_file = folder / "reference.nii.gz"
    nib.save(ct, ct_file)
    structures = []
    for name, bounds in (("liver", (slice(2, 7), slice(2, 7), slice(2, 7))), ("kidney_left", (slice(8, 11), slice(2, 5), slice(2, 6)))):
        mask = np.zeros(shape, dtype=np.uint8)
        mask[bounds] = 1
        target = folder / f"{name}.nii.gz"
        imaging._save_mask(mask, ct, target)
        structures.append(dict(id=f"total:{name}", name=name, task="total", group="Abdominal organs", color="#c08090", mask_file=str(target)))
    revision = service.build_revision(
        case["id"],
        ct_file,
        structures,
        source="synthetic_fixture",
        acquisition={"series_uid": "fixture", "phase": "venous"},
        inference_manifest={"engine": "fixture", "engine_version": "test", "qc": []},
    )
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
    assert draft["status"] == "ready" and draft["generation"] == 2
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
    accepted_job = execute_next(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
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
        service.reviews.update(case["id"], draft["id"], draft["generation"], [])


def test_prompt_undo_invalidates_current_candidate_but_retains_immutable_preview(example):
    service, case, base, _ = example
    draft = candidate(example)
    alias = draft["candidate"]["revision_id"]
    previous_path = service.reviews.candidate_revision(case["id"], alias)["structures"][0]["mask_file"]
    updated = service.reviews.update(case["id"], draft["id"], 2, [])
    assert updated["generation"] == 3 and updated["candidate"] is None and updated["prompts"] == []
    assert service.reviews.candidate_revision(case["id"], alias)["structures"][0]["mask_file"] == previous_path
    with pytest.raises(ValueError, match="changed"):
        service.reviews.update(case["id"], draft["id"], 2, [])
    with pytest.raises(ValueError, match="changed"):
        service.reviews.accept(case["id"], draft["id"], 2)
    with pytest.raises(ValueError, match="prompt"):
        service.reviews.predict(case["id"], draft["id"], 3)
    service.reviews.discard(case["id"], draft["id"], 3)
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
        service.reviews.accept(case["id"], draft["id"], draft["generation"])
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
    service.interactive_runtime = FakeRuntime(during=lambda: service.reviews.update(case["id"], draft["id"], 2, []))
    job = execute_next(service, service.reviews.predict(case["id"], draft["id"], 1))
    assert job["status"] == "cancelled"
    current = service.reviews.get(case["id"], draft["id"])
    assert current["generation"] == 3 and current["status"] == "draft" and current["candidate"] is None
    assert current["candidates"] == {}
    assert service.store.get("cases", case["id"])["active_revision_id"] == base["id"]


def test_cancelled_acceptance_cannot_activate_or_close_draft(example, monkeypatch):
    service, case, base, _ = example
    draft = candidate(example)
    job = service.reviews.accept(case["id"], draft["id"], draft["generation"])
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
    job = service.reviews.accept(case["id"], draft["id"], draft["generation"])
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
    retry = execute_next(service, service.reviews.predict(case["id"], draft["id"], recovered["generation"]))
    assert retry["status"] == "completed"
    ready = service.reviews.get(case["id"], draft["id"])
    service.reviews.accept(case["id"], draft["id"], ready["generation"])
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

    monkeypatch.setattr(service, "_segment", process)
    result = execute_next(service, service.store.enqueue(case["id"], "segment", {"revision_id": base["id"]}))
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
            time.sleep(0.02)
        assert job["status"] == "completed", job
        current = client.get(review_url).json()
        assert current["status"] == "ready" and current["candidate"]["generation"] == 2
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
        candidate_metadata = client.get(candidate_url)
        assert candidate_metadata.status_code == 200, candidate_metadata.text
        metadata = candidate_metadata.json()
        assert metadata["id"] == alias and metadata["parent_id"] == base["id"]
        assert metadata["source"] == "nninteractive_candidate" and metadata["review_status"] == "unreviewed"
        assert metadata["geometry"] == base["geometry"]
        assert metadata["model_url"] is metadata["model_manifest_url"] is None
        assert metadata["preview_3d_ready"] is False and metadata["surface_assets"] == []
        assert str(service.settings.data_dir) not in candidate_metadata.text
        assert "ct_file" not in candidate_metadata.text and "mask_file" not in candidate_metadata.text
        assert candidate_metadata.headers["cache-control"] == "private, no-store"
        assert client.post(prefix + "/activate", json={"revision_id": alias}).status_code in (400, 404)
        assert client.patch(candidate_url, json={"review_status": "corrected"}).status_code in (400, 404)
        assert client.get(candidate_url + "/export.html").status_code in (400, 404)
        assert client.post(candidate_url + "/export.html", json={}).status_code in (400, 404)
        assert client.get(candidate_url + "/model.glb").status_code in (400, 404)
        assert client.get(candidate_url + "/model-manifest").status_code == 404
        assert len(client.get(prefix + "/revisions").json()) == 1
        assert api_service.store.get("cases", case["id"])["active_revision_id"] == base["id"]
        other = api_service.new_case("Unauthorized candidate")
        denied = f"/api/cases/{other['id']}/revisions/{alias}"
        assert client.get(denied).status_code == 404
        assert client.get(denied + "/ct-volume").status_code == 404
        assert client.get(denied + "/mask-volume", params=query).status_code == 404
        assert client.get(f"/api/cases/{other['id']}/reviews/{draft['id']}").status_code == 404
        assert client.post(review_url + "/discard", json={"expected_generation": 2}).status_code == 200
        assert client.get(candidate_url).status_code == 404
        assert client.get(candidate_url + "/ct-volume").status_code == 404


def test_apply_boundaries_persist_only_after_publication_and_pending_undo_is_exact(example):
    service, case, base, prompt = example
    draft = candidate(example)
    assert draft["batches"] == [{"end": 1, "autozoom": True, "full_refine": False}]
    extra = dict(prompt, id="pending-2", include=True)
    edited = service.reviews.update(case["id"], draft["id"], 2, [prompt, extra])
    assert edited["candidate"]["revision_id"] == draft["candidate"]["revision_id"]
    public = public_review(edited)
    assert public["dirty"] and public["pending_count"] == 1 and public["applied_prompt_count"] == 1
    with pytest.raises(ValueError, match="candidate"):
        service.reviews.accept(case["id"], draft["id"], 3)
    restored = service.reviews.update(case["id"], draft["id"], 3, [prompt])
    assert not public_review(restored)["dirty"] and restored["status"] == "ready"
    assert restored["candidate"]["generation"] == 4
    assert restored["candidate"]["mask_file"] == draft["candidate"]["mask_file"]
    assert restored["candidate"]["revision_id"] != draft["candidate"]["revision_id"]
    assert len(service.interactive_runtime.calls) == 1
    service.reviews.update(case["id"], draft["id"], 4, [prompt, extra])
    job = service.reviews.predict(case["id"], draft["id"], 5)
    assert len(service.reviews.get(case["id"], draft["id"])["batches"]) == 1
    assert job["payload"]["batches"][-1]["end"] == 2
    assert execute_next(service, job)["status"] == "completed"
    assert len(service.reviews.get(case["id"], draft["id"])["batches"]) == 2
    assert service.interactive_runtime.calls[-1][-1]["options"] == {"autozoom": True, "start_empty": False}


def test_whole_refine_gets_fresh_alias_and_options_changes_reset_replay(example):
    service, case, base, prompt = example
    draft = candidate(example)
    full = service.reviews.predict(case["id"], draft["id"], 2, full_refine=True)
    assert full["payload"]["generation"] == 3
    assert full["payload"]["batches"][-1] == {"end": 1, "autozoom": True, "full_refine": True}
    assert execute_next(service, full)["status"] == "completed"
    state = service.reviews.get(case["id"], draft["id"])
    assert state["candidate"]["revision_id"] != draft["candidate"]["revision_id"]
    changed = service.reviews.update(case["id"], draft["id"], 3, [prompt], options={"autozoom": False, "start_empty": True})
    assert changed["candidate"] is None and changed["batches"] == []
    apply = service.reviews.predict(case["id"], draft["id"], 4)
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
    prompts = [
        {k: v for k, v in point.items() if k != "kind"},
        dict(id="box", kind="bbox", include=True, axis=2, index=4, vertices=[[2, 2], [6, 6]]),
        dict(id="stroke", kind="scribble", include=False, axis=1, index=3, vertices=[[2, 2], [5, 5]]),
        dict(id="loop", kind="lasso", include=True, axis=0, index=4, vertices=[[2, 2], [6, 2], [6, 6], [2, 6]]),
    ]

    def wait(client, job):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            result = client.get(f"/api/jobs/{job['id']}").json()
            if result["status"] not in ("queued", "running"):
                assert result["status"] == "completed", result
                return result
            time.sleep(0.02)
        pytest.fail("Mixed interaction job did not finish")

    with TestClient(app, base_url="http://127.0.0.1") as http:
        status = http.get("/api/interactive/status").json()
        assert status["capabilities"]["bbox2d"] and not status["capabilities"]["bbox3d"]
        created = http.post(prefix + "/reviews", json={"revision_id": base["id"], "structure_id": "total:liver"}).json()
        url = prefix + "/reviews/" + created["id"]
        update = http.patch(url, json={"expected_generation": 0, "prompts": prompts, "options": {"autozoom": False, "start_empty": True}})
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
        whole = http.post(url + "/predict", json={"generation": 2, "full_refine": True})
        assert whole.status_code == 200, whole.text
        stale = http.post(url + "/predict", json={"generation": 2, "full_refine": True})
        assert stale.status_code == 409, stale.text
        wait(http, whole.json())
        final = http.get(url).json()
        assert final["generation"] == 3 and final["candidate"]["generation"] == 3
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
    accepted_job = execute_next(service, service.reviews.accept(case["id"], draft["id"], ready["generation"]))
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
    imported = slicer_io.import_segmentation(Path(base["ct_file"]), segmentation, accepted["structures"], tmp_path / "reimported", require_source_hash=True)
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
        service.reviews.accept(case["id"], empty["id"], 2)
    assert len(service.store.list("revisions", case["id"])) == 1
    assert service.store.revision(case["id"], base["id"])["structures"] == base["structures"]


@pytest.mark.parametrize(
    "values", [{"name": "  "}, {"name": "bad\nname"}, {"name": "a" * 129}, {"name": "Valid", "group": ""}, {"name": "Valid", "color": "#f00"}]
)
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
        response = http.post(
            prefix + "/reviews/new-structure", json={"revision_id": base["id"], "name": "New browser label", "group": "Custom", "color": "#d977aa"}
        )
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
            time.sleep(0.02)
        assert job["status"] == "completed", job
        ready = http.get(review_url).json()
        candidate_url = prefix + "/revisions/" + ready["candidate"]["revision_id"]
        masks = http.get(candidate_url + "/mask-volume", params={"structure_id": target_id, "compare": True}).json()
        assert not masks["current"]["empty"] and masks["parent"]["empty"]
        frame = http.get(
            candidate_url + "/review-frame", params={"structure_id": target_id, "compare": True, "plane": "axial", "native_axis": 2, "native_index": 1}
        )
        assert frame.status_code == 200, frame.text
        assert json.loads(frame.headers["X-CT-Frame"])["parent_offset"] is not None
        assert len(http.get(prefix + "/revisions").json()) == 1
        save = http.post(review_url + "/accept", json={"generation": ready["generation"]}).json()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            job = http.get("/api/jobs/" + save["id"]).json()
            if job["status"] not in ("queued", "running"):
                break
            time.sleep(0.02)
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
            time.sleep(0.02)
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


# A target keeps one open draft


def start(example, structure="total:liver", **choice):
    service, case, base, _ = example
    return service.reviews.create(case["id"], base["id"], structure, **choice)


def open_drafts(example):
    service, case, _, _ = example
    return [record["id"] for record in service.store.list("review_sessions", case["id"]) if record["status"] not in ("accepted", "discarded")]


def test_a_second_start_returns_the_open_draft_with_its_marks(example):
    service, case, _, prompt = example
    first = start(example)
    saved = service.reviews.update(case["id"], first["id"], 0, [prompt])
    second = start(example)
    assert second["id"] == first["id"] and second["generation"] == 1 and second["prompts"] == saved["prompts"]
    assert open_drafts(example) == [first["id"]]


def test_each_structure_and_method_keeps_its_own_draft(example):
    drafts = [start(example), start(example, "total:kidney_left"), start(example, method="direct")]
    assert len({draft["id"] for draft in drafts}) == len(open_drafts(example)) == 3


def test_http_starts_at_the_same_moment_make_one_draft(example, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import sqlite3
    import threading

    service, case, base, _ = example
    app = create_app(service.settings)
    store = app.state.service.store
    starts = 8
    together = threading.Barrier(starts)
    open_review, connect = store.open_review, sqlite3.connect

    def at_once(record):
        together.wait(timeout=30)
        return open_review(record)

    class SlowRead(sqlite3.Connection):
        """Holds each read past the moment the others make theirs, so only a write lock can pass."""

        def execute(self, sql, *values):
            cursor = super().execute(sql, *values)
            if sql.startswith("SELECT data FROM review_sessions WHERE case_id"):
                time.sleep(0.05)
            return cursor

    monkeypatch.setattr(store, "open_review", at_once)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **options: connect(*args, factory=SlowRead, **options))
    with TestClient(app, base_url="http://127.0.0.1") as client:

        def post(_):
            return client.post(f"/api/cases/{case['id']}/reviews", json={"revision_id": base["id"], "structure_id": "total:liver"})

        with ThreadPoolExecutor(max_workers=starts) as pool:
            responses = list(pool.map(post, range(starts)))
    assert {response.status_code for response in responses} == {200}
    assert len({response.json()["id"] for response in responses}) == 1 and len(open_drafts(example)) == 1


def test_a_start_cannot_change_the_draft_it_returns(example):
    service, case, _, prompt = example
    first = start(example)
    service.reviews.update(case["id"], first["id"], 0, [prompt])
    second = start(example, relationship={"kind": "subregion", "parent_id": "total:kidney_left"})
    assert second["id"] == first["id"] and second["generation"] == 1 and second["relationship"] == first["relationship"]
    with pytest.raises(ValueError):
        start(example, relationship={"kind": "subregion", "parent_id": "total:liver"})
    assert current(example, first)["generation"] == 1


def test_a_start_leaves_a_running_job_alone_and_settles_one_that_ended(example):
    service, case, _, prompt = example
    draft = service.reviews.update(case["id"], start(example)["id"], 0, [prompt])
    queued = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    running = start(example)
    assert running["id"] == draft["id"] and running["status"] == "predicting" and running["job_id"] == queued["id"]
    assert service.store.get("jobs", queued["id"])["status"] == "queued"
    service.store.update_job(queued["id"], status="failed", error="Synthetic failure")
    settled = start(example)
    assert settled["id"] == draft["id"] and settled["status"] == "draft" and settled["error"] == "Synthetic failure"


def test_a_tab_that_has_not_seen_a_run_can_neither_cancel_it_nor_clear_its_result(example):
    service, case, _, prompt = example
    draft = start(example)
    behind = [
        lambda: service.reviews.update(case["id"], draft["id"], 0, [prompt]),
        lambda: service.reviews.update(case["id"], draft["id"], 0, [], options={"autozoom": False, "start_empty": False}),
        lambda: service.reviews.discard(case["id"], draft["id"], 0),
    ]
    queued = service.reviews.predict(case["id"], draft["id"], 0, full_refine=True)
    for change in behind:
        with pytest.raises(ReviewConflict, match="draft changed"):
            change()
    assert not service.store.get("jobs", queued["id"])["cancel_requested"]
    assert execute_next(service, queued)["status"] == "completed"
    published = current(example, draft)
    assert published["candidate"]["generation"] == published["generation"] == 1
    for change in behind:
        with pytest.raises(ReviewConflict, match="draft changed"):
            change()
    assert current(example, draft) == published


def test_a_tab_that_has_not_seen_a_save_cannot_discard_the_draft_under_it(example):
    service, case, _, _ = example
    draft = candidate(example)
    saving = service.reviews.accept(case["id"], draft["id"], draft["generation"])
    with pytest.raises(ReviewConflict, match="Wait for acceptance"):
        service.reviews.discard(case["id"], draft["id"], draft["generation"])
    assert execute_next(service, saving)["status"] == "completed"
    assert current(example, draft)["status"] == "accepted"


def test_a_discarded_draft_is_never_returned(example):
    service, case, _, _ = example
    draft = candidate(example)
    service.reviews.discard(case["id"], draft["id"])
    fresh = start(example)
    assert fresh["id"] != draft["id"] and fresh["generation"] == 0 and not fresh["prompts"] and fresh["candidate"] is None


def test_a_saved_draft_is_never_returned_and_a_start_stays_on_the_active_revision(example):
    service, case, base, _ = example
    waiting = start(example, "total:kidney_left")
    draft = candidate(example)
    saved = execute_next(service, service.reviews.accept(case["id"], draft["id"], draft["generation"]))
    assert saved["status"] == "completed", saved
    # A draft left open on the old revision does not make that revision startable again.
    with pytest.raises(ReviewConflict, match="active revision"):
        start(example, "total:kidney_left")
    for structure, closed in (("total:liver", draft), ("total:kidney_left", waiting)):
        fresh = service.reviews.create(case["id"], saved["revision_id"], structure)
        assert fresh["id"] != closed["id"] and fresh["revision_id"] == saved["revision_id"] and not fresh["prompts"]


def test_two_drafts_from_before_answer_with_the_one_a_client_shows(example):
    service, case, _, _ = example
    older = start(example)
    newer = service.store.save("review_sessions", {**older, "id": identifier()})
    assert start(example)["id"] == newer["id"] == service.reviews.list(case["id"])[0]["id"]
    assert open_drafts(example) == [newer["id"], older["id"]]


def test_a_draft_saved_before_methods_existed_counts_as_ai_assist(example):
    service, case, _, _ = example
    draft = start(example)
    service.store.save("review_sessions", {key: value for key, value in draft.items() if key != "method"})
    assert start(example)["id"] == draft["id"]
    assert start(example, method="direct")["id"] != draft["id"]


# Inference paths: a draft keeps the card of its first result


@pytest.fixture
def paths(example, tmp_path, monkeypatch):
    """Two added paths beside the default, and a runtime that can answer for all three."""
    first = stand_in(tmp_path)
    second = stand_in(tmp_path, "second", proofs=first.proof.parent, kind="rocm", gpu_name=SECOND_GPU)
    activate(monkeypatch, first)
    monkeypatch.setenv("SCAN3D_GPU_PROOF", str(tmp_path / "no-proof.json"))
    monkeypatch.delenv("SCAN3D_DEVICE", raising=False)
    example[0].interactive_runtime = MeasuringRuntime()
    return first, second


def unmarked(example):
    service, case, base, _ = example
    return service.reviews.create(case["id"], base["id"], "total:liver")


def marked(example):
    """A new draft with one mark waiting for its first Run AI."""
    service, case, _, prompt = example
    return service.reviews.update(case["id"], unmarked(example)["id"], 0, [prompt])


def current(example, draft):
    return example[0].reviews.get(example[1]["id"], draft["id"])


def queue(example, draft, **choice):
    return example[0].reviews.predict(example[1]["id"], draft["id"], current(example, draft)["generation"], **choice)


def run(example, draft, **choice):
    job = execute_next(example[0], queue(example, draft, **choice))
    return job, current(example, draft)


def ran(example, draft, **choice):
    job, state = run(example, draft, **choice)
    assert job["status"] == "completed", job
    return state


def add_mark(example, draft, identity="point-2"):
    state = current(example, draft)
    mark = dict(example[3], id=identity, include=True)
    return example[0].reviews.update(example[1]["id"], draft["id"], state["generation"], [*state["prompts"], mark])


def sent(example):
    """What the engine was asked to do last."""
    (_, _, prompts, _, kwargs) = example[0].interactive_runtime.calls[-1]
    return prompts, kwargs


def fail_next_run(example):
    def failure():
        example[0].interactive_runtime.during = None
        raise RuntimeError("Synthetic GPU failure")

    example[0].interactive_runtime.during = failure


def test_only_a_published_result_binds_a_draft_to_its_card(example, paths):
    service, case, _, _ = example
    draft = marked(example)
    assert public_review(draft)["inference_path"] is None
    queued = queue(example, draft, inference_path="nvidia")
    assert queued["inference_path"] == binding("nvidia") == public_job(queued)["inference_path"]
    assert "inference_path" not in queued["payload"]
    assert public_review(current(example, draft))["inference_path"] is None
    assert execute_next(service, queued)["status"] == "completed"
    view = public_review(current(example, draft))
    assert view["inference_path"] == binding("nvidia") and view["candidate"]["gpu_name"] == GPU_NAME
    assert sent(example)[1]["inference_path"].id == "nvidia"
    assert current(example, draft)["candidate"]["provenance"]["inference_path"] == "nvidia"


def test_a_saved_correction_records_its_card_publicly(example, paths):
    service, case, base, _ = example
    ready = ran(example, marked(example), inference_path="nvidia")
    accepted = execute_next(service, service.reviews.accept(case["id"], ready["id"], ready["generation"]))
    assert accepted["status"] == "completed", accepted
    assert "inference_path" not in accepted
    runtime = public_revision(service.store.revision(case["id"], accepted["revision_id"]))["operation"]["runtime"]
    assert (runtime["inference_path"], runtime["gpu_name"]) == ("nvidia", GPU_NAME)


@pytest.mark.parametrize("choice", [{}, {"inference_path": "default"}], ids=["unnamed", "named"])
def test_the_default_path_sends_the_engine_todays_request(example, paths, choice):
    ready = ran(example, marked(example), **choice)
    assert "inference_path" not in sent(example)[1]
    assert ("inference_path" in ready["candidate"]["provenance"]) == bool(choice)


def test_one_path_keeps_todays_records(example, paths):
    draft = marked(example)
    queued = queue(example, draft)
    assert "inference_path" not in queued and "inference_path" not in public_job(queued)
    assert execute_next(example[0], queued)["status"] == "completed"
    state = current(example, draft)
    assert "inference_path" not in state["candidate"]["provenance"]
    view = public_review(state)
    assert view["inference_path"] is None and "gpu_name" not in view["candidate"]


def test_naming_the_default_gives_the_draft_a_card_to_show(example, paths):
    state = ran(example, marked(example), inference_path="default")
    view = public_review(state)
    assert view["inference_path"] == binding("default") and view["candidate"]["gpu_name"] == DEFAULT_GPU


def test_another_card_is_refused_once_a_draft_has_a_result(example, paths):
    service, case, _, _ = example
    draft = ran(example, marked(example), inference_path="nvidia")
    add_mark(example, draft)
    before, jobs = current(example, draft), service.store.list("jobs", case["id"])
    conflict = f"This draft's AI results came from {GPU_NAME}. Re-run its steps to use another GPU."
    for other in ("second", "default"):
        with pytest.raises(ReviewConflict) as refused:
            queue(example, draft, inference_path=other)
        assert str(refused.value) == conflict
    assert current(example, draft) == before and service.store.list("jobs", case["id"]) == jobs


def test_an_unnamed_default_draft_is_locked_too(example, paths):
    draft = ran(example, marked(example))
    add_mark(example, draft)
    with pytest.raises(ReviewConflict, match=f"came from {binding('default')['label']}\\."):
        queue(example, draft, inference_path="nvidia")
    assert ran(example, draft, inference_path="default")["inference_path"] == binding("default")


@pytest.mark.parametrize("choice", [{}, {"inference_path": "nvidia"}], ids=["old-tab", "matching"])
def test_a_locked_draft_stays_on_its_card_with_no_path_or_the_same_one(example, paths, choice):
    draft = ran(example, marked(example), inference_path="nvidia")
    add_mark(example, draft)
    queued = queue(example, draft, **choice)
    assert queued["inference_path"] == binding("nvidia")
    assert execute_next(example[0], queued)["status"] == "completed"
    assert sent(example)[1]["inference_path"].id == "nvidia"
    assert current(example, draft)["inference_path"] == binding("nvidia")
    assert current(example, draft)["candidate"]["provenance"]["inference_path"] == "nvidia"


def test_a_failed_first_run_leaves_the_draft_free(example, paths):
    draft = marked(example)
    fail_next_run(example)
    job, state = run(example, draft, inference_path="nvidia")
    assert job["status"] == "failed" and job["error"] == "Synthetic GPU failure" == state["error"]
    assert job["inference_path"] == binding("nvidia")
    assert state["batches"] == [] and state["candidate"] is None and public_review(state)["inference_path"] is None
    assert ran(example, draft, inference_path="second")["inference_path"] == binding("second")


def test_a_cancelled_first_run_leaves_the_draft_free(example, paths):
    service, case, _, _ = example
    draft = marked(example)
    queued = queue(example, draft, inference_path="nvidia")
    service.interactive_runtime.during = lambda: service.store.update_job(queued["id"], cancel_requested=True)
    assert execute_next(service, queued)["status"] == "cancelled"
    service.interactive_runtime.during = None
    state = current(example, draft)
    assert state["batches"] == [] and public_review(state)["inference_path"] is None
    assert ran(example, draft, inference_path="second")["inference_path"] == binding("second")


def test_undoing_every_step_frees_the_draft(example, paths):
    service, case, _, _ = example
    draft = ran(example, marked(example), inference_path="nvidia")
    undone = service.reviews.update(case["id"], draft["id"], draft["generation"], [])
    assert undone["batches"] == [] and public_review(undone)["inference_path"] is None
    redone = service.reviews.update(case["id"], draft["id"], undone["generation"], draft["prompts"])
    assert ran(example, redone, inference_path="second")["inference_path"] == binding("second")


def stored_card(example, draft):
    """The card in the row itself. `public_review` hides it on an empty draft, and older code reads the row."""
    with example[0].store.connection() as connection:
        row = connection.execute("SELECT data FROM review_sessions WHERE id=?", (draft["id"],)).fetchone()
    return json.loads(row["data"])["inference_path"]


@pytest.mark.parametrize(
    "change",
    [
        lambda service, case, draft: service.reviews.update(case["id"], draft["id"], draft["generation"], []),
        lambda service, case, draft: service.reviews.update(
            case["id"], draft["id"], draft["generation"], draft["prompts"], options={"autozoom": False, "start_empty": False}
        ),
        lambda service, case, draft: service.reviews.discard(case["id"], draft["id"], draft["generation"]),
    ],
    ids=["undone", "options-changed", "discarded"],
)
def test_a_draft_left_with_no_steps_keeps_no_card_in_its_row(example, paths, change):
    service, case, _, _ = example
    draft = ran(example, marked(example), inference_path="nvidia")
    assert stored_card(example, draft) == binding("nvidia")
    change(service, case, draft)
    assert stored_card(example, draft) is None


def test_a_draft_with_steps_left_keeps_its_card_in_its_row(example, paths):
    service, case, _, _ = example
    draft = ran(example, marked(example), inference_path="nvidia")
    add_mark(example, draft)
    draft = ran(example, draft)
    service.reviews.update(case["id"], draft["id"], draft["generation"], draft["prompts"][:1])
    assert stored_card(example, draft) == binding("nvidia")


def test_changing_an_option_frees_the_draft(example, paths):
    service, case, _, _ = example
    draft = ran(example, marked(example), inference_path="nvidia")
    changed = service.reviews.update(case["id"], draft["id"], draft["generation"], draft["prompts"], options={"autozoom": False, "start_empty": False})
    assert changed["batches"] == [] and public_review(changed)["inference_path"] is None
    assert ran(example, draft, inference_path="second")["inference_path"] == binding("second")


def test_a_freed_draft_with_no_path_is_the_default_and_not_its_old_card(example, paths):
    service, case, _, _ = example
    draft = ran(example, marked(example), inference_path="nvidia")
    undone = service.reviews.update(case["id"], draft["id"], draft["generation"], [])
    redone = service.reviews.update(case["id"], draft["id"], undone["generation"], draft["prompts"])
    queued = queue(example, redone)
    assert "inference_path" not in queued
    assert execute_next(service, queued)["status"] == "completed"
    assert "inference_path" not in sent(example)[1] and current(example, draft)["inference_path"] is None


def test_undoing_only_some_steps_keeps_the_lock(example, paths):
    service, case, _, _ = example
    draft = ran(example, marked(example), inference_path="nvidia")
    add_mark(example, draft)
    draft = ran(example, draft)
    assert len(draft["batches"]) == 2
    undone = service.reviews.update(case["id"], draft["id"], draft["generation"], draft["prompts"][:1])
    assert len(undone["batches"]) == 1 and public_review(undone)["inference_path"] == binding("nvidia")
    add_mark(example, draft, "point-3")
    with pytest.raises(ReviewConflict, match="came from"):
        queue(example, draft, inference_path="second")


def test_a_full_refine_with_no_marks_locks_though_it_applied_no_prompts(example, paths):
    service, case, _, _ = example
    draft = unmarked(example)
    ready = ran(example, draft, full_refine=True, inference_path="nvidia")
    view = public_review(ready)
    assert view["applied_prompt_count"] == 0 and ready["batches"] == [{"end": 0, "autozoom": True, "full_refine": True}]
    assert view["inference_path"] == binding("nvidia")
    undone = service.reviews.update(case["id"], draft["id"], ready["generation"], [])
    assert public_review(undone)["inference_path"] == binding("nvidia")  # It has a result, so undo cannot free it.
    with pytest.raises(ReviewConflict, match="came from"):
        queue(example, draft, full_refine=True, inference_path="second")


def test_a_path_that_cannot_run_is_refused_with_no_job(example, paths):
    service, case, _, _ = example
    first, _ = paths
    draft = marked(example)
    with pytest.raises(ValueError, match="This GPU is not set up on this workstation."):
        queue(example, draft, inference_path="absent")
    first.change(lambda record: record.update(gpu_ready=False))
    with pytest.raises(ValueError, match=f"{GPU_NAME} is unavailable: Its last GPU check failed."):
        queue(example, draft, inference_path="nvidia")
    state = current(example, draft)
    assert service.store.list("jobs", case["id"]) == [] and (state["status"], state["job_id"]) == ("draft", None)


def test_a_locked_draft_whose_card_went_away_fails_by_name_and_never_moves(example, paths):
    first, _ = paths
    draft = ran(example, marked(example), inference_path="nvidia")
    add_mark(example, draft)
    first.proof.unlink()
    job, state = run(example, draft)  # An old tab names no card, so this reaches the job's own check.
    assert job["status"] == "failed"
    assert job["error"] == f"{GPU_NAME} is no longer available: This GPU is not set up on this workstation. Nothing ran and no other GPU was tried."
    assert state["inference_path"] == job["inference_path"] and len(example[0].interactive_runtime.calls) == 1


def test_a_card_swapped_under_a_locked_draft_is_not_treated_as_the_same_card(example, paths):
    first, _ = paths
    draft = ran(example, marked(example), inference_path="nvidia")
    add_mark(example, draft)
    first.change(lambda record: record["path"].update(gpu_name="Test Replacement GPU"))
    # The second is an old tab, and a job for it could only fail on every attempt.
    for choice in ({"inference_path": "nvidia"}, {}):
        with pytest.raises(ReviewConflict) as refused:
            queue(example, draft, **choice)
        assert str(refused.value) == f"This draft's AI results came from {GPU_NAME}. Re-run its steps to use another GPU."
    assert len(example[0].interactive_runtime.calls) == 1


def test_a_card_swapped_after_a_job_was_queued_fails_that_job_by_name(example, paths):
    first, _ = paths
    draft = ran(example, marked(example), inference_path="nvidia")
    add_mark(example, draft)
    queued = queue(example, draft)
    first.change(lambda record: record["path"].update(gpu_name="Test Replacement GPU"))
    job = execute_next(example[0], queued)
    assert (
        job["status"] == "failed" and job["error"] == f"{GPU_NAME} changed after this job was queued. Nothing ran and no other GPU was tried. Start it again."
    )


def test_a_card_swapped_under_a_locked_draft_leaves_a_named_re_run_as_the_way_out(example, paths):
    first, _ = paths
    draft = ran(example, marked(example), inference_path="nvidia")
    first.change(lambda record: record["path"].update(gpu_name="Test Replacement GPU"))
    with pytest.raises(ReviewConflict, match=f"came from {GPU_NAME}"):
        queue(example, draft, replay=True)  # It names no card to move to.
    state = ran(example, draft, inference_path="nvidia", replay=True)
    assert state["inference_path"] == binding("nvidia") and state["inference_path"]["label"] == "Test Replacement GPU"


def test_an_unchanged_card_keeps_working_for_a_tab_that_names_nothing(example, paths):
    first, _ = paths
    draft = ran(example, marked(example), inference_path="nvidia")
    add_mark(example, draft)
    first.change(lambda record: record.update(checked_at="a later check of the same card"))
    assert ran(example, draft)["inference_path"] == binding("nvidia")


def test_only_a_card_that_passed_its_own_check_counts_as_replaced(paths):
    first, _ = paths
    bound = binding("nvidia")
    assert not replaced(None) and not replaced(binding("default")) and not replaced(bound)
    first.change(lambda record: record["path"].update(gpu_name="Test Replacement GPU"))
    assert replaced(bound)
    first.change(lambda record: record.update(gpu_ready=False))
    assert not replaced(bound)  # Unavailable: its job says so by name.
    first.proof.unlink()
    assert not replaced(bound)


@pytest.mark.parametrize(
    "stored",
    [lambda state: {key: value for key, value in state.items() if key != "inference_path"}, lambda state: {**state, "inference_path": None}],
    ids=["no-key", "null"],
)
def test_a_session_and_a_job_saved_before_paths_existed_run_as_the_default(example, paths, stored):
    # A master-made draft has no key, and a null one is what clearing a card leaves.
    service, case, _, _ = example
    draft = ran(example, marked(example))
    add_mark(example, draft)
    service.store.save("review_sessions", stored(current(example, draft)))
    assert public_review(current(example, draft))["inference_path"] is None  # Its batches lock it, to no named card.
    with pytest.raises(ReviewConflict, match="came from"):
        queue(example, draft, inference_path="nvidia")
    queued = queue(example, draft)
    service.store.save("jobs", {key: value for key, value in queued.items() if key != "inference_path"})
    assert execute_next(service, queued)["status"] == "completed"
    assert "inference_path" not in sent(example)[1]


def test_http_predict_takes_a_path_and_says_why_it_will_not(example, paths):
    service, case, _, _ = example
    http = TestClient(create_app(service.settings), base_url="http://127.0.0.1")
    url = f"/api/cases/{case['id']}/reviews/{marked(example)['id']}"
    predict = lambda **body: http.post(url + "/predict", json={"generation": http.get(url).json()["generation"], **body})
    for malformed in ("Not-Lower", "-leading", "a" * 33, "../nvidia", 7):
        assert predict(inference_path=malformed).status_code == 422
    assert predict(unknown=True).status_code == 422
    assert predict(path="nvidia").status_code == 422
    refused = predict(inference_path="absent")
    assert (refused.status_code, refused.json()["detail"]) == (400, "This GPU is not set up on this workstation.")
    assert http.get(f"/api/cases/{case['id']}/jobs").json() == []
    accepted = predict(inference_path="nvidia")
    assert accepted.status_code == 200 and accepted.json()["inference_path"] == binding("nvidia"), accepted.text
    assert execute_next(service, accepted.json())["status"] == "completed"
    state = http.get(url).json()
    assert state["inference_path"] == binding("nvidia") and state["candidate"]["gpu_name"] == GPU_NAME
    patched = http.patch(url, json={"expected_generation": state["generation"], "prompts": [*state["prompts"], dict(example[3], id="point-2", include=True)]})
    assert patched.status_code == 200, patched.text
    conflict = predict(inference_path="second")
    assert (conflict.status_code, conflict.json()["detail"]) == (409, f"This draft's AI results came from {GPU_NAME}. Re-run its steps to use another GPU.")
    assert predict().json()["inference_path"] == binding("nvidia")


# Inference paths: re-running a draft on another card


def bound_draft(example, name="nvidia"):
    """A result on `name` from two Apply steps, with nothing pending."""
    draft = ran(example, marked(example), inference_path=name)
    add_mark(example, draft)
    return ran(example, draft)


def test_a_draft_returned_to_a_second_page_stays_on_its_card(example, paths):
    draft = bound_draft(example)
    returned = start(example)
    assert returned["id"] == draft["id"] and returned["batches"] == draft["batches"] and returned["candidate"] == draft["candidate"]
    assert public_review(returned)["inference_path"] == binding("nvidia")
    add_mark(example, returned, "point-3")
    with pytest.raises(ReviewConflict, match="Re-run its steps"):
        queue(example, returned, inference_path="second")
    assert ran(example, returned)["inference_path"] == binding("nvidia")
    assert sent(example)[1]["inference_path"].id == "nvidia"


def test_a_replay_repeats_the_exact_steps_on_the_new_card_and_moves_the_draft_at_publish(example, paths):
    service, case, _, _ = example
    draft = bound_draft(example)
    calls = len(service.interactive_runtime.calls)
    old = draft["candidate"]
    queued = queue(example, draft, inference_path="second", replay=True)
    assert queued["payload"]["batches"] == draft["batches"] and queued["inference_path"] == binding("second")
    assert queued["payload"]["generation"] == draft["generation"] + 1
    waiting = current(example, draft)
    assert waiting["inference_path"] == binding("nvidia") and waiting["candidate"] == old  # Nothing moved yet.
    assert public_review(waiting)["dirty"]
    assert execute_next(service, queued)["status"] == "completed"
    prompts, request = sent(example)
    assert len(service.interactive_runtime.calls) == calls + 1
    assert prompts == draft["prompts"] and request["batches"] == draft["batches"] and request["inference_path"].id == "second"
    state = current(example, draft)
    assert state["batches"] == draft["batches"] and state["prompts"] == draft["prompts"]
    assert state["inference_path"] == binding("second") and state["candidate"]["gpu_name"] == SECOND_GPU
    assert state["candidate"]["provenance"]["inference_path"] == "second"


def test_a_replay_gets_its_own_alias_and_the_old_result_stays_readable(example, paths):
    service, case, _, _ = example
    draft = bound_draft(example)
    alias = draft["candidate"]["revision_id"]
    state = ran(example, draft, inference_path="second", replay=True)
    assert state["candidate"]["revision_id"] != alias and state["candidate"]["generation"] == draft["generation"] + 1
    assert service.reviews.candidate_revision(case["id"], alias)["id"] == alias
    assert not public_review(state)["dirty"] and state["status"] == "ready"
    accepted = execute_next(service, service.reviews.accept(case["id"], draft["id"], state["generation"]))
    assert accepted["status"] == "completed", accepted
    operation = service.store.revision(case["id"], accepted["revision_id"])["operation"]
    assert operation["runtime"]["inference_path"] == "second" and operation["batches"] == draft["batches"]


def test_pending_marks_become_one_more_step_after_the_replayed_ones(example, paths):
    draft = bound_draft(example)
    add_mark(example, draft, "point-3")
    add_mark(example, draft, "point-4")
    state = ran(example, draft, inference_path="second", replay=True)
    assert state["batches"] == [*draft["batches"], {"end": 4, "autozoom": True, "full_refine": False}]
    assert sent(example)[1]["batches"] == state["batches"] and state["inference_path"] == binding("second")


def test_a_full_refine_only_draft_replays_one_refine_and_not_two(example, paths):
    draft = unmarked(example)
    ready = ran(example, draft, full_refine=True, inference_path="nvidia")
    assert len(ready["batches"]) == 1
    state = ran(example, draft, inference_path="second", replay=True)
    assert state["batches"] == ready["batches"] and sent(example)[1]["batches"] == ready["batches"]
    assert state["inference_path"] == binding("second")


def test_the_replayed_steps_satisfy_the_engines_own_validation(example, paths):
    draft = bound_draft(example)
    state = ran(example, draft, inference_path="second", replay=True)
    assert _batches(sent(example)[1]["batches"], len(state["prompts"]))
    full = start(example, "total:kidney_left")
    ran(example, full, full_refine=True, inference_path="nvidia")
    ran(example, full, inference_path="second", replay=True)
    assert _batches(sent(example)[1]["batches"], 0) == [{"end": 0, "autozoom": True, "full_refine": True, "legacy": False}]


def test_a_replay_with_no_named_path_stays_on_the_draft_card(example, paths):
    draft = bound_draft(example)
    assert queue(example, draft, replay=True)["inference_path"] == binding("nvidia")


def test_a_replay_leaves_a_removed_card_for_the_default_and_every_step_repeats(example, paths, tmp_path):
    # The way out of a draft whose card was taken out of the workstation.
    service, case, _, _ = example
    first, _ = paths
    draft = bound_draft(example)
    calls = len(service.interactive_runtime.calls)
    first.proof.rename(tmp_path / "moved-out.json")
    queued = queue(example, draft, inference_path="default", replay=True)
    assert queued["payload"]["batches"] == draft["batches"] and queued["inference_path"] == binding("default")
    assert execute_next(service, queued)["status"] == "completed"
    prompts, request = sent(example)
    assert len(service.interactive_runtime.calls) == calls + 1 and len(draft["batches"]) == 2
    assert prompts == draft["prompts"] and request["batches"] == draft["batches"] and "inference_path" not in request
    state = current(example, draft)
    assert state["inference_path"] == binding("default") and state["candidate"]["gpu_name"] == DEFAULT_GPU
    assert state["batches"] == draft["batches"] and not public_review(state)["dirty"]
    accepted = execute_next(service, service.reviews.accept(case["id"], draft["id"], state["generation"]))
    assert accepted["status"] == "completed", accepted
    published = service.store.revision(case["id"], accepted["revision_id"])["operation"]
    assert published["batches"] == draft["batches"] and published["runtime"]["inference_path"] == "default"


def test_a_replay_can_return_to_the_draft_card_after_a_failure_elsewhere(example, paths):
    draft = bound_draft(example)
    fail_next_run(example)
    assert run(example, draft, inference_path="second", replay=True)[0]["status"] == "failed"
    state = ran(example, draft, inference_path="nvidia", replay=True)
    assert state["inference_path"] == binding("nvidia") and not public_review(state)["dirty"]


def test_a_failed_replay_keeps_the_old_result_and_card_and_cannot_be_saved(example, paths):
    service, case, _, _ = example
    draft = bound_draft(example)
    fail_next_run(example)
    job, state = run(example, draft, inference_path="second", replay=True)
    assert job["status"] == "failed" and state["error"] == "Synthetic GPU failure"
    assert state["status"] == "ready" and state["candidate"] == draft["candidate"]
    assert state["inference_path"] == binding("nvidia") and state["batches"] == draft["batches"] and state["prompts"] == draft["prompts"]
    view = public_review(state)
    assert view["dirty"] and view["candidate"]["revision_id"] == draft["candidate"]["revision_id"]
    assert service.reviews.candidate_revision(case["id"], draft["candidate"]["revision_id"])
    with pytest.raises(ReviewConflict, match="Generate and inspect"):
        service.reviews.accept(case["id"], draft["id"], state["generation"])
    assert ran(example, draft, inference_path="second", replay=True)["inference_path"] == binding("second")


def test_a_cancelled_replay_keeps_the_old_result_and_card(example, paths):
    service, case, _, _ = example
    draft = bound_draft(example)
    queued = queue(example, draft, inference_path="second", replay=True)
    service.interactive_runtime.during = lambda: service.store.update_job(queued["id"], cancel_requested=True)
    assert execute_next(service, queued)["status"] == "cancelled"
    state = current(example, draft)
    assert state["status"] == "ready" and state["candidate"] == draft["candidate"] and state["inference_path"] == binding("nvidia")


def test_a_replay_on_a_card_that_went_away_keeps_the_old_result_and_card(example, paths):
    _, second = paths
    draft = bound_draft(example)
    queued = queue(example, draft, inference_path="second", replay=True)
    second.proof.unlink()
    job = execute_next(example[0], queued)
    assert job["error"] == f"{SECOND_GPU} is no longer available: This GPU is not set up on this workstation. Nothing ran and no other GPU was tried."
    state = current(example, draft)
    assert state["candidate"] == draft["candidate"] and state["inference_path"] == binding("nvidia") and state["error"] == job["error"]


@pytest.mark.parametrize("choice", [{}, {"inference_path": "second", "replay": True}], ids=["next-step", "re-run"])
def test_every_run_confirms_its_card_and_a_wrong_one_keeps_the_last_good_result(example, paths, choice):
    # A first result has nothing to lose. Here a wrong card would replace reviewed work.
    draft = bound_draft(example)
    add_mark(example, draft, "point-3")
    example[0].interactive_runtime.measures = "Test Other GPU"
    job, state = run(example, draft, **choice)
    assert job["status"] == "failed" and state["error"] == job["error"]
    assert "but the GPU check found Test Other GPU (gpu:0, " in job["error"] and job["error"].endswith("No result was kept and no other GPU was tried.")
    assert state["candidate"]["revision_id"] == draft["candidate"]["revision_id"] and set(state["candidates"]) == set(draft["candidates"])
    assert state["inference_path"] == binding("nvidia") and state["batches"] == draft["batches"]


def test_a_replay_runs_the_model_even_with_a_relationship_change_waiting(example, paths):
    service, case, _, _ = example
    draft = bound_draft(example)
    choice = {"kind": "subregion", "parent_id": "total:kidney_left", "exclusive_with": []}
    service.reviews.update(case["id"], draft["id"], draft["generation"], draft["prompts"], relationship=choice)
    queued = queue(example, draft, inference_path="second", replay=True)
    assert "relationship_only" not in queued["payload"]
    assert execute_next(service, queued)["status"] == "completed"
    state = current(example, draft)
    assert state["inference_path"] == binding("second") and state["candidate"]["relationship"]["parent_id"] == "total:kidney_left"
    assert not public_review(state)["relationship_dirty"]


def test_a_replay_with_nothing_to_repeat_is_an_ordinary_run(example, paths):
    draft = marked(example)
    assert ran(example, draft, inference_path="nvidia", replay=True)["batches"] == [{"end": 1, "autozoom": True, "full_refine": False}]
    empty = start(example, "total:kidney_left")
    with pytest.raises(ReviewConflict, match="Add or edit a prompt"):
        queue(example, empty, inference_path="nvidia", replay=True)


def test_without_replay_a_draft_with_nothing_pending_is_still_refused(example, paths):
    draft = bound_draft(example)
    with pytest.raises(ReviewConflict, match="Add or edit a prompt"):
        queue(example, draft, inference_path="nvidia")


def test_a_replay_cannot_also_refine_the_whole_mask(example, paths):
    service, case, _, _ = example
    draft = bound_draft(example)
    jobs = service.store.list("jobs", case["id"])
    with pytest.raises(ValueError, match="cannot add a whole-object refinement"):
        queue(example, draft, inference_path="second", replay=True, full_refine=True)
    http = TestClient(create_app(service.settings), base_url="http://127.0.0.1")
    url = f"/api/cases/{case['id']}/reviews/{draft['id']}/predict"
    refused = http.post(url, json={"generation": draft["generation"], "inference_path": "second", "replay": True, "full_refine": True})
    assert refused.status_code == 400, refused.text
    assert service.store.list("jobs", case["id"]) == jobs and current(example, draft)["generation"] == draft["generation"]


def test_http_replay_needs_a_boolean_and_runs_on_the_existing_route(example, paths):
    service, case, _, _ = example
    draft = bound_draft(example)
    http = TestClient(create_app(service.settings), base_url="http://127.0.0.1")
    url = f"/api/cases/{case['id']}/reviews/{draft['id']}"
    body = {"generation": draft["generation"], "inference_path": "second"}
    assert http.post(url + "/predict", json={**body, "replay": "soon"}).status_code == 422
    assert http.post(url + "/predict", json=body).status_code == 409
    queued = http.post(url + "/predict", json={**body, "replay": True})
    assert queued.status_code == 200, queued.text
    assert queued.json()["inference_path"] == binding("second") and "replay" not in queued.text
    assert execute_next(service, queued.json())["status"] == "completed"
    assert http.get(url).json()["inference_path"] == binding("second")
