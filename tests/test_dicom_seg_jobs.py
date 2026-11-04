"""Synthetic HTTP uploads become source-bound revisions without inference."""

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pydicom
import pytest

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.dicom_seg_jobs import stage_import
from scan3d.store import job_lane
from test_dicom_seg import fixture, export


@pytest.fixture
def rig(tmp_path, fixture):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", modalities=("CT",)))
    service = app.state.service
    case = service.new_case("Synthetic SEG import")
    source = service.store.case_dir(case["id"]) / "dicom"
    source.mkdir()
    for path in fixture["paths"]:
        shutil.copy2(path, source / path.name)
    case = service.finish_import(case, source)
    series = case["series"][0]
    reference = source.parent / "converted" / "reference.nii.gz"
    reference.parent.mkdir()
    shutil.copy2(fixture["reference"], reference)
    ct = service.build_ct_revision(
        case["id"],
        reference,
        acquisition={"series_uid": series["uid"], "profile": "whole_body", "phase": "unknown"},
        image_source={key: series[key] for key in ("study_uid", "frame_of_reference_uid", "sop_class_uids")},
    )
    service.store.commit_revision(ct)
    encoded = export(fixture).files[0].path.read_bytes()
    return SimpleNamespace(service=service, http=TestClient(app, base_url="http://127.0.0.1"), case=case, ct=ct, encoded=encoded, fixture=fixture)


def post(rig, revision_id=None, **kwargs):
    return rig.http.post(
        f"/api/cases/{rig.case['id']}/dicom-seg/imports",
        data={"revision_id": revision_id or rig.ct["id"]},
        files=[("files", ("seg.dcm", rig.encoded, "application/dicom"))],
        **kwargs,
    )


def parent_with_mask(rig):
    """A saved direct edit whose one mask shares the imported ring's name."""
    mask = str(rig.fixture["segments"][0].mask_path)
    parent = rig.service.build_revision(
        rig.case["id"],
        Path(rig.ct["ct_file"]),
        [{"id": "custom:existing", "name": "Ring", "task": "custom", "group": "Custom", "color": "#ff00ff", "mask_file": mask}],
        source="direct_edit",
        parent=rig.ct["id"],
        acquisition=rig.ct["acquisition"],
    )
    rig.service.store.commit_revision(parent)
    return parent


def run(rig, response):
    assert response.status_code == 200, response.text
    job = rig.service.store.claim("cpu")
    assert job and job["id"] == response.json()["id"] and job_lane(job) == "cpu"
    rig.service.execute(job)
    return rig.service.store.get("jobs", job["id"])


def test_ct_only_import_creates_editable_revision_and_preserves_overlap(rig):
    parent = deepcopy(rig.ct)
    response = post(rig)
    public = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert public["kind"] == "dicom_seg_import" and public["source_revision_id"] == rig.ct["id"]
    assert "payload" not in public and str(rig.service.settings.data_dir) not in json.dumps(public)
    assert rig.service.store.claim("gpu") is None
    job = run(rig, response)
    assert job["status"] == "completed", job.get("error")
    result = rig.service.store.revision(rig.case["id"], job["revision_id"])
    assert result["source"] == "dicom_seg_import" and result["parent_id"] == parent["id"]
    assert result["reference_image_id"] == parent["reference_image_id"] and result["review_status"] == "unreviewed"
    assert len(result["structures"]) == 2 and result["model_url"]
    masks = {item["name"].lower(): np.asanyarray(nib.load(item["mask_file"]).dataobj) for item in result["structures"]}
    np.testing.assert_array_equal(masks["ring"], nib.load(rig.fixture["segments"][0].mask_path).get_fdata())
    np.testing.assert_array_equal(masks["block"], nib.load(rig.fixture["segments"][1].mask_path).get_fdata())
    assert np.count_nonzero(masks["ring"] & masks["block"]) == 529
    assert rig.service.store.revision(rig.case["id"], parent["id"]) == parent
    assert rig.service.interactive_runtime is None
    rig.service.store.activate(rig.case["id"], parent["id"])
    assert rig.service.store.get("cases", rig.case["id"])["active_revision_id"] == parent["id"]


def test_append_keeps_existing_masks_and_duplicate_import_is_rejected(rig):
    parent = parent_with_mask(rig)
    job = run(rig, post(rig, parent["id"]))
    assert job["status"] == "completed", job.get("error")
    result = rig.service.store.revision(rig.case["id"], job["revision_id"])
    assert len(result["structures"]) == 3
    assert result["structures"][0] == parent["structures"][0]
    again = run(rig, post(rig, result["id"]))
    assert again["status"] == "failed" and "already present" in again["error"]
    assert rig.service.store.get("cases", rig.case["id"])["active_revision_id"] == result["id"]


def test_reimport_after_undo_queues_a_fresh_revision(rig):
    first = run(rig, post(rig))
    assert first["status"] == "completed"
    rig.service.store.activate(rig.case["id"], rig.ct["id"])
    response = post(rig)
    assert response.json()["id"] != first["id"]
    repeated = run(rig, response)
    assert repeated["status"] == "completed" and repeated["revision_id"] != first["revision_id"]


@pytest.mark.parametrize("changed", ["upload", "source", "reference", "active_revision", "cancel"])
def test_frozen_inputs_and_parent_guard_leave_failed_import_unpublished(rig, changed):
    response = post(rig)
    saved = rig.service.store.get("jobs", response.json()["id"])
    if changed in {"upload", "source"}:
        key = "files" if changed == "upload" else "sources"
        path = Path(saved["payload"][key][0]["path"])
        path.write_bytes(path.read_bytes() + b"changed")
    elif changed == "reference":
        path = Path(rig.ct["ct_file"])
        path.write_bytes(path.read_bytes() + b"changed")
    elif changed == "active_revision":
        other = rig.service.build_ct_revision(rig.case["id"], Path(rig.ct["ct_file"]), acquisition=rig.ct["acquisition"])
        rig.service.store.commit_revision(other)
    else:
        rig.service.store.update_job(saved["id"], cancel_requested=True)
    before = rig.service.store.get("cases", rig.case["id"])["active_revision_id"]
    job = rig.service.store.claim("cpu")
    if job:
        rig.service.execute(job)
    final = rig.service.store.get("jobs", saved["id"])
    assert final["status"] == ("cancelled" if changed == "cancel" else "failed")
    assert rig.service.store.get("cases", rig.case["id"])["active_revision_id"] == before
    assert not final.get("revision_id")


def test_http_rejects_stale_foreign_and_missing_parent_and_deduplicates_queue(rig):
    first, duplicate = post(rig), post(rig)
    assert first.status_code == duplicate.status_code == 200
    assert first.json()["id"] == duplicate.json()["id"]
    assert len(rig.service.store.list("jobs", rig.case["id"])) == 1
    assert post(rig, "0" * 32).status_code == 404
    other = rig.service.new_case("Other synthetic case")
    response = rig.http.post(
        f"/api/cases/{other['id']}/dicom-seg/imports",
        data={"revision_id": rig.ct["id"]},
        files={"files": ("s.dcm", rig.encoded)},
    )
    assert response.status_code == 404
    current = rig.service.build_ct_revision(rig.case["id"], Path(rig.ct["ct_file"]), acquisition=rig.ct["acquisition"])
    rig.service.store.commit_revision(current)
    assert post(rig).status_code == 409


def test_upload_limits_and_private_artifacts(rig, monkeypatch):
    monkeypatch.setattr("scan3d.dicom_seg_jobs.MAX_UPLOAD_BYTES", 10)
    before = rig.service.store.list("jobs", rig.case["id"])
    assert post(rig).status_code == 400
    assert rig.service.store.list("jobs", rig.case["id"]) == before
    with pytest.raises(ValueError, match="between 1 and 64"):
        stage_import(rig.service, rig.case["id"], rig.ct["id"], [SimpleNamespace(file=BytesIO(b"x"))] * 65)


def test_active_revision_change_during_surface_build_cannot_publish(rig, monkeypatch):
    original = rig.service.build_revision

    def build(*args, **kwargs):
        result = original(*args, **kwargs)
        other = rig.service.build_ct_revision(rig.case["id"], Path(rig.ct["ct_file"]), acquisition=rig.ct["acquisition"])
        rig.service.store.commit_revision(other)
        return result

    monkeypatch.setattr(rig.service, "build_revision", build)
    job = run(rig, post(rig))
    assert job["status"] == "failed" and not job.get("revision_id")
    assert all(revision["source"] != "dicom_seg_import" for revision in rig.service.store.list("revisions", rig.case["id"]))


@pytest.mark.parametrize("tag", ["SeriesInstanceUID", "FrameOfReferenceUID"])
def test_actual_source_headers_must_match_prepared_asset_before_enqueue(rig, tag):
    path = Path(rig.service.store.get("cases", rig.case["id"])["series"][0]["files"][0])
    image = pydicom.dcmread(path)
    setattr(image, tag, pydicom.uid.generate_uid())
    image.save_as(path, enforce_file_format=True)
    response = post(rig)
    assert response.status_code == 400 and "identities differ" in response.json()["detail"]
    assert rig.service.store.list("jobs", rig.case["id"]) == []


def test_existing_parent_mask_mutation_after_enqueue_is_rejected(rig):
    parent = parent_with_mask(rig)
    old = rig.fixture["segments"][0]
    response = post(rig, parent["id"])
    old.mask_path.write_bytes(old.mask_path.read_bytes() + b"changed")
    final = run(rig, response)
    assert final["status"] == "failed" and "existing segmentation mask changed" in final["error"]
    assert rig.service.store.get("cases", rig.case["id"])["active_revision_id"] == parent["id"]
