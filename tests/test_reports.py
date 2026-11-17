"""Measurement reports over HTTP: status, the recipe segmentation, runs and pictures, with a canned worker."""

import hashlib
import json
from pathlib import Path
import sys

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest

from scan3d import imaging, reports, segmentation, segmentation_options
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.store import identifier

SHAPE = (16, 16, 16)
GRID = np.diag([1.5, 1.5, 2.0, 1.0])
GRID[:3, 3] = (-10.0, 20.0, -30.0)
# What each recipe task marks on the fake engine: the aortic dissection model finds no lumens.
OUTPUTS = {
    "total": {"aorta": (2, 9), "brachiocephalic_trunk": (9, 11), "subclavian_artery_left": (9, 12), "vertebrae_T12": (12, 14), "iliac_artery_right": (1, 3)},
    "aorta_annulus": {"annulus_proper": (4, 5), "sinotubular_junction": (6, 7)},
    "renal_arteries": {"celiac_trunk": (5, 6)},
    "aortic_dissection": {},
}
PNG = b"\x89PNG\r\n\x1a\n synthetic"


def canned(request):
    """What the worker hands back for one measured landmark, the annulus, and ten it could not place."""
    out = Path(request["output"])
    out.mkdir(parents=True)
    for name in ("cpr.png", "plane-1.png"):
        (out / name).write_bytes(PNG)
    measured = {"diameter": 3.2, "diameter_pd": 3.1, "area": 8.0, "start": [0, 0, 0], "end": [32, 0, 0], "start_pd": [16, -15.5, 0], "end_pd": [16, 15.5, 0]}
    stats = ("length", "max_diameter", "volume", "volume_true_lumen", "volume_false_lumen")
    return {
        "engine_version": "2.18.0",
        "report_version": "0.1.2",
        "contrast": {"aorta_median_hu": 310.0, "native": False},
        "dissection": False,
        "upstream": {
            "landmarks": {"1": measured, **{str(n): dict.fromkeys(measured) for n in range(2, 12)}},
            "section_stats": {name: dict.fromkeys(stats) for name in ("aorta_ascending", "aorta_arch", "aorta_descending", "aorta_abdominal", "aorta_total")},
        },
        "geometry": {"1": {"centre": [16, 0, 0], "normal": [0, 0, 1], "outline": [[0, 0, 0], [32, 0, 0], [16, 16, 0]], "picture": "plane-1.png"}},
        "missing": {str(n): ["the sinotubular junction was not found"] for n in range(2, 12)},
        "centreline": [[16, 0, 0], [16, 0, -1]],
        "warnings": ["Sinotub. junc. is empty!"],
        "files": ["cpr.png", "plane-1.png"],
    }


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """An image-only CT case whose segment jobs use a fake engine, and a report worker that hands back `canned`."""
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", allowed_hosts=("127.0.0.1",)))
    service = app.state.service
    launched = []

    def fingerprint(profile, options=None, **source):
        digest = hashlib.sha256(json.dumps([profile, options], sort_keys=True).encode()).hexdigest()
        return {"versions": {"TotalSegmentator": "2.18.0"}, "device_request": "gpu", "wrapper_sha256": "f" * 64, "weights_complete": True, "digest": digest}

    def segment(ct_path, output_dir, profile, progress, cancelled, options=None, **source):
        run = Path(output_dir) / f"run-{identifier()}"
        reference = nib.load(ct_path)
        structures = []
        for task in options["tasks"]:
            for label, (low, high) in OUTPUTS[task].items():
                mask = np.zeros(SHAPE, dtype=np.uint8)
                mask[4:12, 4:12, low:high] = 1
                path = run / task / f"{label}.nii.gz"
                imaging._save_mask(mask, reference, path)
                structures.append(dict(id=f"{task}:{label}", name=label, task=task, group="Fixture", color="#a1b2c3", mask_file=str(path)))
        manifest = {"run_id": run.name, "options": options, "qc": [], "model_fingerprint": fingerprint(profile, options), "tasks": []}
        (Path(output_dir) / "inference.json").write_text(json.dumps(manifest))
        return structures

    def launch(service, job, request, work):
        launched.append(request)
        return canned(request)

    monkeypatch.setattr(segmentation, "model_fingerprint", fingerprint)
    monkeypatch.setattr(segmentation, "segment", segment)
    monkeypatch.setattr(segmentation_options, "license_configured", lambda: True)
    monkeypatch.setattr(reports, "_launch", launch)
    case = service.new_case("Synthetic aorta report")
    source = service.store.case_dir(case["id"]) / "converted"
    source.mkdir()
    ct = nib.Nifti1Image(np.full(SHAPE, 40, np.int16), GRID)
    ct.header.set_xyzt_units("mm")
    nib.save(ct, source / "reference.nii.gz")
    image = service.build_ct_revision(case["id"], source / "reference.nii.gz")
    service.store.commit_revision(image)
    return TestClient(app, base_url="http://127.0.0.1"), service, case["id"], image["id"], launched


def run(service, job_id, lane):
    claimed = service.store.claim(lane)
    assert claimed and claimed["id"] == job_id
    service.execute(claimed)
    job = service.store.get("jobs", job_id)
    assert job["status"] == "completed", job["error"]
    return job


def test_the_recipe_segments_then_measures_and_reuses_the_run(rig):
    http, service, case_id, image_id, launched = rig
    base = f"/api/cases/{case_id}/revisions/{image_id}/reports"
    (status,) = http.get(base).json()["reports"]
    assert (status["id"], status["available"], status["ready"], status["can_segment"]) == ("aorta", True, False, True)
    assert {need["state"] for need in status["needs"]} == {"missing"}
    assert [task["id"] for task in status["missing_tasks"]] == ["total", "aorta_annulus", "renal_arteries", "aortic_dissection"]
    assert http.post(f"{base}/aorta", json={}).status_code == 400

    queued = http.post(f"{base}/aorta/segment", json={"inference_path": None}).json()
    assert queued["kind"] == "segment" and "payload" not in queued
    assert service.store.get("jobs", queued["id"])["payload"]["options"]["tasks"] == [task["id"] for task in status["missing_tasks"]]
    revision_id = run(service, queued["id"], "gpu")["revision_id"]

    # The segmentation queued the report on the revision it saved.
    (job,) = [job for job in service.store.list("jobs", case_id) if job["kind"] == "report"]
    assert job["revision_id"] == revision_id and job["payload"]["report"] == "aorta"
    base = f"/api/cases/{case_id}/revisions/{revision_id}/reports"
    status = http.get(f"{base}/aorta").json()["report"]
    states = {need["id"]: need["state"] for need in status["needs"]}
    assert states["annulus"] == "present" and states["aorta_true_lumen"] == "not_found" and status["ready"]
    assert status["missing_tasks"] == [] and not status["can_segment"] and status["segment_reason"]
    assert http.post(f"{base}/aorta", json={}).json()["id"] == job["id"]
    revisions = service.store.list("revisions", case_id)
    run(service, job["id"], "cpu")
    assert {need: Path(mask["path"]).name for need, mask in launched[0]["masks"].items()}["annulus"] == "annulus_proper.nii.gz"
    assert service.store.list("revisions", case_id) == revisions

    (row,) = http.get(f"{base}/aorta").json()["analyses"]
    assert row["analysis_id"] == job["id"] and row["state"] == "completed"
    assert row["summary"] == {"max_diameter_mm": 32.0, "max_landmark": 1, "landmarks_available": 1, "landmarks_total": 11}
    result = http.get(f"{base}/aorta/{job['id']}").json()
    assert (result["format"], result["report"], result["revision_id"], result["files"]) == (
        "scan3d-report-v1",
        "aorta",
        revision_id,
        ["cpr.png", "plane-1.png"],
    )
    annulus, sinuses = result["landmarks"][:2]
    assert (annulus["diameter_mm"], annulus["perpendicular_mm"], annulus["area_cm2"], annulus["picture"]) == (32.0, 31.0, 8.0, "plane-1.png")
    assert annulus["ends"] == [[0, 0, 0], [32, 0, 0]] and len(result["landmarks"]) == 11
    assert not sinuses["available"] and sinuses["unavailable_reason"] == "Not measured: the sinotubular junction was not found"
    assert [section["name"] for section in result["sections"]][-1] == "Whole aorta" and result["warnings"] == ["Sinotub. junc. is empty!"]
    picture = http.get(f"{base}/aorta/{job['id']}/files/cpr.png")
    assert picture.status_code == 200 and picture.content == PNG and picture.headers["content-type"] == "image/png"
    for name in ("result.json", "plane-2.png", "..%2Fresult.json"):
        assert http.get(f"{base}/aorta/{job['id']}/files/{name}").status_code == 404
    # Stored in the case folder beside the coronary results, so Trash, Delete forever and backups treat both alike.
    folder = service.settings.data_dir / "cases" / case_id / "reports" / "aorta" / job["payload"]["key"]
    assert sorted(path.name for path in folder.iterdir()) == ["complete.json", "cpr.png", "plane-1.png", "result.json"]
    assert [path.name for path in folder.parent.iterdir()] == [folder.name]
    assert http.post(f"{base}/aorta", json={}).json()["id"] == job["id"] and len(launched) == 1
    # A picture changed on disk is refused rather than served.
    (folder / "cpr.png").write_bytes(b"changed")
    assert http.get(f"{base}/aorta/{job['id']}/files/cpr.png").status_code == 404
    # and the next start measures again instead of handing back the damaged run.
    assert http.post(f"{base}/aorta", json={}).json()["id"] != job["id"]


def test_refusals_and_a_required_structure_not_found(rig):
    http, service, case_id, image_id, _ = rig
    base = f"/api/cases/{case_id}/revisions"
    assert http.get(f"{base}/{image_id}/reports/spleen").status_code == 404
    assert http.post(f"{base}/review-{image_id}/reports/aorta", json={}).status_code == 400
    assert http.get(f"{base}/review-{image_id}/reports/aorta").json()["report"]["available"] is False
    assert http.get(f"{base}/{image_id}/reports/aorta/{identifier()}").status_code == 404
    assert http.post(f"{base}/{image_id}/reports/aorta", json={"aorta": "total:aorta"}).status_code == 422

    # The annulus model ran and found nothing: the segmentation is saved, and the report says why it did not follow.
    OUTPUTS["aorta_annulus"], kept = {}, OUTPUTS["aorta_annulus"]
    try:
        queued = http.post(f"{base}/{image_id}/reports/aorta/segment", json={}).json()
        revision_id = run(service, queued["id"], "gpu")["revision_id"]
    finally:
        OUTPUTS["aorta_annulus"] = kept
    assert not [job for job in service.store.list("jobs", case_id) if job["kind"] == "report"]
    status = http.get(f"{base}/{revision_id}/reports/aorta").json()["report"]
    assert status["available"] and not status["ready"] and status["reason"] == "The aortic annulus was looked for and not found"
    assert http.post(f"{base}/{revision_id}/reports/aorta", json={}).status_code == 400
    assert http.post(f"{base}/{revision_id}/reports/aorta/segment", json={}).status_code == 400
    # Only the current revision can be segmented.
    old = http.get(f"{base}/{image_id}/reports/aorta").json()["report"]
    assert not old["can_segment"] and old["segment_reason"] == "Segmentation runs only on the case's current revision"
    # A hard stop's scratch goes when the service starts.
    scratch = service.settings.data_dir / "cases" / case_id / "reports" / "aorta" / "work-stale"
    scratch.mkdir(parents=True)
    service.start(lanes=())
    service.stop()
    assert not scratch.exists()


def test_a_lost_aorta_offers_no_segmentation_and_a_replaced_one_is_segmented_again(rig, monkeypatch):
    http, service, case_id, image_id, _ = rig
    base = f"/api/cases/{case_id}/revisions"
    # Whole body finds no aorta: the tasks left could not give a measurement.
    whole = OUTPUTS["total"]
    monkeypatch.setitem(OUTPUTS, "total", {label: span for label, span in whole.items() if label != "aorta"})
    revision_id = run(service, service.segment_case(case_id, image_id, task="total")["id"], "gpu")["revision_id"]
    status = http.get(f"{base}/{revision_id}/reports/aorta").json()["report"]
    assert status["missing_tasks"] and not status["can_segment"]
    assert status["segment_reason"] == status["reason"] == "The aorta was looked for and not found"

    # A later run of another task takes the aorta's name (segmentation_merge): running Whole body again brings it back.
    monkeypatch.setitem(OUTPUTS, "total", whole)
    monkeypatch.setitem(OUTPUTS, "heartchambers_highres", {"aorta": (2, 9)})
    revision_id = run(service, service.segment_case(case_id, revision_id, tasks=list(reports.definition("aorta").recipe))["id"], "gpu")["revision_id"]
    revision_id = run(service, service.segment_case(case_id, revision_id, task="heartchambers_highres")["id"], "gpu")["revision_id"]
    status = http.get(f"{base}/{revision_id}/reports/aorta").json()["report"]
    assert {need["id"]: need["state"] for need in status["needs"]}["aorta"] == "missing"
    assert [task["id"] for task in status["missing_tasks"]] == ["total"] and status["can_segment"]


def test_without_totalsegmentator_the_status_says_so(rig, monkeypatch):
    http, _, case_id, image_id, _ = rig
    for name in ("totalsegmentator", "totalsegmentator.registry", "totalsegmentator.aorta_report"):
        monkeypatch.setitem(sys.modules, name, None)
    base = f"/api/cases/{case_id}/revisions/{image_id}/reports/aorta"
    status = http.get(base).json()["report"]
    assert not status["available"] and not status["can_segment"] and status["reason"] == "This server has no TotalSegmentator aorta report"
    assert http.post(f"{base}/segment", json={}).status_code == 400


def test_a_sheared_grid_is_not_measured(rig):
    _, service, _, _, _ = rig
    case = service.new_case("Synthetic sheared aorta report")
    source = service.store.case_dir(case["id"]) / "converted"
    source.mkdir()
    sheared = GRID.copy()
    sheared[0, 2] = 0.5
    ct = nib.Nifti1Image(np.full(SHAPE, 40, np.int16), sheared)
    ct.header.set_xyzt_units("mm")
    nib.save(ct, source / "reference.nii.gz")
    image = service.build_ct_revision(case["id"], source / "reference.nii.gz")
    service.store.commit_revision(image)
    status = reports.status(service, case["id"], image["id"], "aorta")
    assert not status["available"] and status["reason"] == "The aorta report cannot measure a scan with sheared slices"
    assert not status["can_segment"]
    with pytest.raises(ValueError, match="sheared"):
        reports.start(service, case["id"], image["id"], "aorta")
