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
# What each recipe task marks on the fake engine: the aortic dissection model finds no lumens, and
# the pulmonary landmarks model finds nothing unless a test says otherwise.
OUTPUTS = {
    "total": {"aorta": (2, 9), "brachiocephalic_trunk": (9, 11), "subclavian_artery_left": (9, 12), "vertebrae_T12": (12, 14), "iliac_artery_right": (1, 3)},
    "aorta_annulus": {"annulus_proper": (4, 5), "sinotubular_junction": (6, 7)},
    "renal_arteries": {"celiac_trunk": (5, 6)},
    "aortic_dissection": {},
    "heartchambers_highres": {"pulmonary_artery": (2, 12)},
    "pulmonary_artery_landmarks": {},
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


def canned_pulmonary(request):
    """The pulmonary worker's output for one measured landmark, the bifurcation, and four it could not measure."""
    out = Path(request["output"])
    out.mkdir(parents=True)
    (out / "plane-3.png").write_bytes(PNG)
    measured = {
        "name": "bifurcation",
        "diameter": 2.6,
        "diameter_pd": 2.5,
        "area": 5.0,
        "start": [0, 0, 0],
        "end": [26, 0, 0],
        "start_pd": [13, -12.5, 0],
        "end_pd": [13, 12.5, 0],
    }
    empty = dict.fromkeys(("area", "diameter", "diameter_pd", "start", "end", "start_pd", "end_pd"))
    return {
        "engine_version": "2.18.0",
        "report_version": "0.2.1",
        "upstream": {"landmarks": {**{str(n): empty for n in (1, 2, 4, 5)}, "3": measured}},
        "geometry": {"3": {"centre": [13, 0, 0], "normal": [0, 0, 1], "outline": [[0, 0, 0], [26, 0, 0], [13, 13, 0]], "picture": "plane-3.png"}},
        "missing": {
            "1": ["the pulmonary valve annulus was not found"],
            "2": ["the pulmonary sinotubular junction was not found"],
            "4": ["no cross-section of the pulmonary artery was found there"],
            "5": ["the start of the right pulmonary artery was not found"],
        },
        "warnings": ["pul_annulus is empty!"],
        "files": ["plane-3.png"],
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
        return {"aorta": canned, "pulmonary": canned_pulmonary}[request["report"]](request)

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


def new_image(service, title):
    """Another image-only CT case on the rig's service."""
    case = service.new_case(title)
    source = service.store.case_dir(case["id"]) / "converted"
    source.mkdir()
    ct = nib.Nifti1Image(np.full(SHAPE, 40, np.int16), GRID)
    ct.header.set_xyzt_units("mm")
    nib.save(ct, source / "reference.nii.gz")
    image = service.build_ct_revision(case["id"], source / "reference.nii.gz")
    service.store.commit_revision(image)
    return case, image


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
    statuses = http.get(base).json()["reports"]
    assert [status["id"] for status in statuses] == ["aorta", "pulmonary"]
    status = statuses[0]
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
    revision = service.store.revision(case_id, revision_id)
    ids = {structure["id"] for structure in revision["structures"]}
    assert "total:aorta" not in ids and "heartchambers_highres:aorta" in ids and "spared" not in revision["operation"]
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


def test_the_pulmonary_report_needs_one_landmark(rig, monkeypatch):
    http, service, case_id, image_id, launched = rig
    base = f"/api/cases/{case_id}/revisions"
    status = http.get(f"{base}/{image_id}/reports/pulmonary").json()["report"]
    assert (status["available"], status["ready"], status["can_segment"], status["reason"]) == (True, False, True, None)
    # Until a landmark is present, each of the five is required.
    assert [(need["id"], need["required"], need["state"]) for need in status["needs"]] == [
        (need.id, True, "missing") for need in reports.definition("pulmonary").needs
    ]
    assert [(task["id"], task["license_required"]) for task in status["missing_tasks"]] == [
        ("heartchambers_highres", True),
        ("pulmonary_artery_landmarks", True),
    ]

    # The landmarks model ran and found none: the segmentation is saved, and no report follows.
    revision_id = run(service, http.post(f"{base}/{image_id}/reports/pulmonary/segment", json={}).json()["id"], "gpu")["revision_id"]
    assert not [job for job in service.store.list("jobs", case_id) if job["kind"] == "report"]
    status = http.get(f"{base}/{revision_id}/reports/pulmonary").json()["report"]
    assert not status["ready"] and not status["can_segment"] and status["reason"] == "None of the five pulmonary landmarks was found"
    refused = http.post(f"{base}/{revision_id}/reports/pulmonary", json={})
    assert refused.status_code == 400 and refused.json()["detail"] == "None of the five pulmonary landmarks was found"

    # One landmark is enough, and the rest are no longer required.
    monkeypatch.setitem(OUTPUTS, "pulmonary_artery_landmarks", {"pul_bifurcation": (6, 7)})
    revision_id = run(service, service.segment_case(case_id, revision_id, task="pulmonary_artery_landmarks")["id"], "gpu")["revision_id"]
    status = http.get(f"{base}/{revision_id}/reports/pulmonary").json()["report"]
    assert status["ready"] and status["reason"] is None
    assert {need["id"]: (need["required"], need["state"]) for need in status["needs"]} == {
        "pulmonary_artery": (True, "present"),
        "pul_annulus": (False, "not_found"),
        "pul_sinotubular_junction": (False, "not_found"),
        "pul_bifurcation": (False, "present"),
        "pul_left_start": (False, "not_found"),
        "pul_right_start": (False, "not_found"),
    }
    job = http.post(f"{base}/{revision_id}/reports/pulmonary", json={}).json()
    run(service, job["id"], "cpu")
    assert sorted(launched[0]["masks"]) == ["pul_bifurcation", "pulmonary_artery"]

    (row,) = http.get(f"{base}/{revision_id}/reports/pulmonary").json()["analyses"]
    assert row["summary"] == {"max_diameter_mm": 26.0, "max_landmark": 3, "landmarks_available": 1, "landmarks_total": 5}
    result = http.get(f"{base}/{revision_id}/reports/pulmonary/{job['id']}").json()
    assert (result["report"], result["files"], result["warnings"]) == ("pulmonary", ["plane-3.png"], ["pul_annulus is empty!"])
    assert not {"sections", "centreline", "contrast", "dissection"} & set(result)
    fields = ["number", "id", "name", "available", "unavailable_reason", "centre", "normal", "diameter_mm", "perpendicular_mm", "area_cm2"]
    fields += ["ends", "perpendicular_ends", "outline", "picture"]
    assert all(sorted(landmark) == sorted(fields) for landmark in result["landmarks"])
    assert [landmark["id"] for landmark in result["landmarks"]] == ["proximal_main", "sinotubular_junction", "bifurcation", "left", "right"]
    annulus, _, bifurcation, left, _ = result["landmarks"]
    assert (bifurcation["diameter_mm"], bifurcation["perpendicular_mm"], bifurcation["area_cm2"], bifurcation["picture"]) == (26.0, 25.0, 5.0, "plane-3.png")
    assert bifurcation["ends"] == [[0, 0, 0], [26, 0, 0]] and bifurcation["normal"] == [0, 0, 1]
    assert annulus["unavailable_reason"] == "Not measured: the pulmonary valve annulus was not found" and annulus["diameter_mm"] is None
    assert not left["available"] and left["unavailable_reason"] == "Not measured: no cross-section of the pulmonary artery was found there"
    assert http.get(f"{base}/{revision_id}/reports/pulmonary/{job['id']}/files/plane-3.png").content == PNG


def test_a_reports_segmentation_spares_segments_it_does_not_read(rig, monkeypatch):
    http, service, case_id, image_id, _ = rig
    # TotalSegmentator's own class names: both pulmonary tasks also mark an aorta structure.
    heart = ("heart_myocardium", "heart_atrium_left", "heart_ventricle_left", "heart_atrium_right", "heart_ventricle_right")
    monkeypatch.setitem(OUTPUTS, "heartchambers_highres", {**dict.fromkeys(heart, (4, 6)), "aorta": (3, 8), "pulmonary_artery": (2, 12)})
    landmarks = ("annulus", "sinotubular_junction", "pul_annulus", "pul_sinotubular_junction", "pul_bifurcation", "pul_left_start", "pul_right_start")
    monkeypatch.setitem(OUTPUTS, "pulmonary_artery_landmarks", dict.fromkeys(landmarks, (6, 7)))

    def segment_for(case, revision_id, report_id):
        job = http.post(f"/api/cases/{case}/revisions/{revision_id}/reports/{report_id}/segment", json={}).json()
        return service.store.revision(case, run(service, job["id"], "gpu")["revision_id"])

    def state(case, revision_id, report_id):
        return http.get(f"/api/cases/{case}/revisions/{revision_id}/reports/{report_id}").json()["report"]

    def masks(revision, *ids):
        structures = {structure["id"]: structure for structure in revision["structures"]}
        return [hashlib.sha256(Path(structures[sid]["mask_file"]).read_bytes()).hexdigest() for sid in ids]

    # The aorta's recipe, then the pulmonary arteries': the aorta report keeps its masks.
    first = segment_for(case_id, image_id, "aorta")
    second = segment_for(case_id, first["id"], "pulmonary")
    ids = {structure["id"] for structure in second["structures"]}
    assert "heartchambers_highres:aorta" not in ids and {f"heartchambers_highres:{label}" for label in heart} <= ids
    assert second["operation"]["spared"] == ["heartchambers_highres:aorta", "pulmonary_artery_landmarks:sinotubular_junction"]
    kept = ("total:aorta", "aorta_annulus:sinotubular_junction")
    assert masks(second, *kept) == masks(first, *kept)
    assert state(case_id, second["id"], "aorta")["ready"] and state(case_id, second["id"], "pulmonary")["ready"]

    # The other order: the aorta's recipe reads total:aorta, which replaces the heart chambers' aorta as before.
    case, image = new_image(service, "Synthetic reports in the other order")
    first = segment_for(case["id"], image["id"], "pulmonary")
    second = segment_for(case["id"], first["id"], "aorta")
    assert "heartchambers_highres:aorta" not in {structure["id"] for structure in second["structures"]} and "spared" not in second["operation"]
    assert state(case["id"], second["id"], "aorta")["ready"] and state(case["id"], second["id"], "pulmonary")["ready"]


def test_a_need_whose_task_found_nothing_is_offered_at_most_once(rig, monkeypatch):
    http, service, case_id, image_id, _ = rig
    monkeypatch.setitem(OUTPUTS, "aorta_annulus", {"annulus_proper": (4, 5)})
    monkeypatch.setitem(OUTPUTS, "pulmonary_artery_landmarks", {"sinotubular_junction": (6, 7)})

    def segment_for(case, revision_id, report_id):
        job = http.post(f"/api/cases/{case}/revisions/{revision_id}/reports/{report_id}/segment", json={}).json()
        return run(service, job["id"], "gpu")["revision_id"]

    def junction(case, revision_id):
        status = http.get(f"/api/cases/{case}/revisions/{revision_id}/reports/aorta").json()["report"]
        return {need["id"]: need["state"] for need in status["needs"]}["sinotubular_junction"], [task["id"] for task in status["missing_tasks"]]

    # The pulmonary landmarks' junction, made after Aortic annulus found none, may have taken its mask: offered once.
    revision_id = segment_for(case_id, segment_for(case_id, image_id, "aorta"), "pulmonary")
    assert junction(case_id, revision_id) == ("missing", ["aorta_annulus"])
    # Aortic annulus ran last and found none again.
    assert junction(case_id, segment_for(case_id, revision_id, "aorta")) == ("not_found", [])

    # The other order: Aortic annulus ran after the landmarks and found none.
    case, image = new_image(service, "Synthetic junction in the other order")
    assert junction(case["id"], segment_for(case["id"], segment_for(case["id"], image["id"], "pulmonary"), "aorta")) == ("not_found", [])


def test_a_reports_segmentation_keeps_a_task_output_beside_a_hand_drawn_namesake(rig, monkeypatch):
    http, service, case_id, image_id, _ = rig
    monkeypatch.setitem(OUTPUTS, "total", {**OUTPUTS["total"], "liver": (1, 3)})
    monkeypatch.setitem(OUTPUTS, "heartchambers_highres", {"aorta": (2, 9)})
    first = service.store.revision(case_id, run(service, service.segment_case(case_id, image_id, task="total")["id"], "gpu")["revision_id"])
    liver = next(structure for structure in first["structures"] if structure["id"] == "total:liver")
    drawn = service.build_revision(
        case_id,
        Path(first["ct_file"]),
        [*first["structures"], {**liver, "id": "custom:liver", "task": "custom", "name": "Liver"}],
        source="interactive_correction",
        parent=first["id"],
        acquisition=first["acquisition"],
        inference_manifest=first["inference_manifest"],
    )
    service.store.commit_revision(drawn)
    # An ordinary Heart chambers run takes total:aorta, so the aorta's recipe runs Whole body again.
    revision_id = run(service, service.segment_case(case_id, drawn["id"], task="heartchambers_highres")["id"], "gpu")["revision_id"]
    job = http.post(f"/api/cases/{case_id}/revisions/{revision_id}/reports/aorta/segment", json={}).json()
    revision = service.store.revision(case_id, run(service, job["id"], "gpu")["revision_id"])
    ids = {structure["id"] for structure in revision["structures"]}
    assert {"total:liver", "total:aorta"} <= ids and "total:liver" not in revision["operation"].get("spared", [])


def test_each_reports_key_covers_its_own_definition_module():
    def digests(*names):
        return [hashlib.sha256((Path(reports.__file__).parent / name).read_bytes()).hexdigest() for name in names]

    assert reports._code("aorta") == digests("reports.py", "report_worker.py", "aorta_report.py")
    assert reports._code("pulmonary") == digests("reports.py", "report_worker.py", "pulmonary_report.py")
