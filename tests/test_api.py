import io
import json
from pathlib import Path
import time
import threading
import zipfile

import numpy as np
import pytest
from fastapi.testclient import TestClient

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.fileio import extract_zip
from scan3d.service import Service


@pytest.fixture
def client(tmp_path):
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", allowed_hosts=("127.0.0.1", "review.example"))
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, app.state.service


def completed(client, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            assert job["status"] == "completed", job
            return job
        time.sleep(0.05)
    pytest.fail("Worker did not finish")


def test_demo_review_refine_undo_frames_and_slicer_roundtrip(client):
    http, service = client
    response = http.post("/api/cases/demo")
    assert response.status_code == 200, response.text
    case = response.json()
    prefix = f"/api/cases/{case['id']}"
    rid = case["active_revision_id"]
    revision = http.get(f"{prefix}/revisions/{rid}").json()
    assert revision["source"] == "synthetic_demo"
    assert len(revision["structures"]) == 7
    for side, sign in (("left", -1), ("right", 1)):
        for organ in ("lung", "kidney"):
            structure = next(s for s in revision["structures"] if s["id"] == f"synthetic:{organ}_{side}")
            assert structure["centroid_ras"][0] * sign > 0
    assert "ct_file" not in revision
    model = http.get(revision["model_url"])
    assert model.content[:4] == b"glTF"
    liver = next(s for s in revision["structures"] if s["id"] == "synthetic:liver")
    query = {"plane": "axial", "position_mm": -50, "structure_id": liver["id"]}
    frame = http.get(f"{prefix}/revisions/{rid}/review-frame", params=query)
    assert frame.status_code == 200
    meta = json.loads(frame.headers["X-CT-Frame"])["meta"]
    assert np.asarray(meta["pixel_to_ras"]).shape == (4, 4)
    assert http.get(f"{prefix}/revisions/{rid}/probe", params=dict(x=37, y=5, z=-55)).json()["hu"] == 105
    job = http.post(
        f"{prefix}/refinements",
        json={"revision_id": rid, "structure_id": liver["id"], "operation": {"type": "erase_region", "bounds_ras": [[0, -80, -100], [37, 80, 0]]}},
    )
    assert job.status_code == 200, job.text
    result = completed(http, job.json()["id"])
    new_id = result["revision_id"]
    refined = http.get(f"{prefix}/revisions/{new_id}").json()
    assert refined["parent_id"] == rid
    new_liver = next(s for s in refined["structures"] if s["id"] == liver["id"])
    assert 0 < new_liver["volume_ml"] < liver["volume_ml"]
    for original in revision["structures"]:
        if original["id"] != liver["id"]:
            assert original["volume_ml"] == next(s for s in refined["structures"] if s["id"] == original["id"])["volume_ml"]
    assert http.post(f"{prefix}/activate", json={"revision_id": rid}).json()["active_revision_id"] == rid
    assert http.patch(f"{prefix}/revisions/{rid}", json={"review_status": "reviewed"}).json()["review_status"] == "reviewed"
    bundle = http.get(f"{prefix}/revisions/{rid}/slicer.zip")
    assert bundle.status_code == 200, bundle.text
    assert bundle.headers["cache-control"] == "private, no-cache"
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as archive:
        name = next(n for n in archive.namelist() if n.endswith(".seg.nrrd"))
        segmentation = archive.read(name)
    imported = http.post(f"{prefix}/corrections", data={"revision_id": rid}, files={"segmentation": ("segmentation.seg.nrrd", segmentation)})
    corrected_job = completed(http, imported.json()["id"])
    corrected = http.get(f"{prefix}/revisions/{corrected_job['revision_id']}").json()
    assert corrected["review_status"] == "corrected"
    assert [s["volume_ml"] for s in corrected["structures"]] == [s["volume_ml"] for s in revision["structures"]]
    # A fresh browser/device can discover jobs created by the CLI or another
    # browser without being given filesystem payloads from the worker record.
    jobs = http.get(f"{prefix}/jobs").json()
    assert {j["id"] for j in jobs} == {job.json()["id"], corrected_job["id"]}
    assert all(j["case_id"] == case["id"] and "payload" not in j for j in jobs)
    assert http.get("/api/cases/00000000000000000000000000000000/jobs").status_code == 404


def test_invalid_operations_and_cross_origin_fail_before_job(client):
    http, service = client
    case = http.post("/api/cases/demo").json()
    prefix = f"/api/cases/{case['id']}"
    invalid = {"revision_id": case["active_revision_id"], "structure_id": "synthetic:liver", "operation": {"type": "keep_component"}}
    assert http.post(f"{prefix}/refinements", json=invalid).status_code == 422
    assert service.store.list("jobs") == []
    assert http.post("/api/cases/import-local", json={"path": "/etc"}).status_code == 400
    data_dir = service.settings.data_dir
    for inside in (data_dir, data_dir / "cases" / case["id"], data_dir.parent):
        assert "outside the application's data directory" in http.post("/api/cases/import-local", json={"path": str(inside)}).json()["detail"]
    assert len(service.store.list("cases")) == 1
    assert http.post("/api/cases/demo", headers={"origin": "https://unrelated.example"}).status_code == 403
    assert http.get("/api/cases", headers={"host": "unrelated.example"}).status_code == 400
    assert http.get("/api/health", headers={"host": "review.example:8443"}).status_code == 200


def test_an_inference_path_is_checked_before_a_job_exists(client):
    http, service = client
    case = http.post("/api/cases/demo").json()
    route, body = f"/api/cases/{case['id']}/segment", {"revision_id": case["active_revision_id"], "task": "total"}
    assert http.post(route, json={**body, "inference_path": "Not A Path"}).status_code == 422
    refused = http.post(route, json={**body, "inference_path": "absent"})
    assert (refused.status_code, refused.json()["detail"]) == (400, "This GPU is not set up on this workstation.")
    assert service.store.list("jobs") == []


def test_png_slice_routes_are_retired(client):
    http, _ = client
    case = http.post("/api/cases/demo").json()
    prefix = f"/api/cases/{case['id']}/revisions/{case['active_revision_id']}"
    for endpoint in ("slice", "slice-meta"):
        assert http.get(f"{prefix}/{endpoint}").status_code == 404


def test_zip_traversal_and_symlinks_rejected(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("../../outside", b"bad")
    with pytest.raises(ValueError, match="Unsafe"):
        extract_zip(archive, tmp_path / "extract", 100)
    with zipfile.ZipFile(archive, "w") as z:
        entry = zipfile.ZipInfo("link")
        entry.external_attr = 0o120777 << 16
        z.writestr(entry, b"/etc/passwd")
    with pytest.raises(ValueError, match="symlink"):
        extract_zip(archive, tmp_path / "extract", 100)


def test_only_one_worker_can_claim_gpu_queue(tmp_path):
    settings = Settings(tmp_path, (tmp_path,))
    a, b = Service(settings), Service(settings)
    try:
        a.start()
        b.start()
        assert a.thread and a.thread.is_alive()
        assert b.thread is None
    finally:
        a.stop()
        b.stop()


def test_lazy_detailed_export_and_revision_scoped_surface_assets(client):
    http, service = client
    case = http.post("/api/cases/demo").json()
    rid = case["active_revision_id"]
    prefix = f"/api/cases/{case['id']}/revisions/{rid}"
    revision = http.get(prefix).json()
    original = service.store.revision(case["id"], rid)
    detailed = Path(original["directory"]) / "detailed.glb"
    assert revision["mesh_statistics"]["detailed"] is None
    assert not detailed.exists()
    manifest = http.get(revision["model_manifest_url"]).json()
    assert len(manifest["structures"]) == 7
    assert "path" not in json.dumps(manifest)
    asset = http.get(manifest["structures"][0]["url"])
    assert asset.content[:4] == b"glTF" and asset.headers["cache-control"] == "private, no-cache"
    assert http.get(f"{prefix}/surfaces/{'0' * 64}.glb").status_code == 404
    # Downloading an export must preserve both review state and active revision.
    http.patch(prefix, json={"review_status": "reviewed"})
    response = http.get(f"{prefix}/model.glb?detail=true")
    assert response.status_code == 200 and response.content[:4] == b"glTF"
    updated = http.get(prefix).json()
    assert updated["review_status"] == "reviewed"
    assert http.get(f"/api/cases/{case['id']}").json()["active_revision_id"] == rid
    assert updated["mesh_statistics"]["detailed"]["cache"]["base_hits"] == 7
    assert updated["processing_timings"]["detailed_export_seconds"] > 0
    assert detailed.exists()


def test_measurements_available_before_meshes_and_refinement_reuses_surfaces(client, monkeypatch):
    from scan3d import surfaces

    http, service = client
    case = http.post("/api/cases/demo").json()
    prefix = f"/api/cases/{case['id']}"
    rid = case["active_revision_id"]
    started, release = threading.Event(), threading.Event()
    original_build = surfaces.build_model

    def paused_build(structures, output_path, **kwargs):
        kwargs["progress"]("Generating display meshes: 1/7 (Liver)")
        started.set()
        assert release.wait(10)
        return original_build(structures, output_path, **kwargs)

    monkeypatch.setattr(surfaces, "build_model", paused_build)
    response = http.post(
        f"{prefix}/refinements",
        json={"revision_id": rid, "structure_id": "synthetic:liver", "operation": {"type": "erase_region", "bounds_ras": [[0, -80, -100], [37, 80, 0]]}},
    )
    job_id = response.json()["id"]
    try:
        assert started.wait(10)
        job = http.get(f"/api/jobs/{job_id}").json()
        assert job["progress"] == {"phase": "meshes", "completed": 0, "total": 7, "timings_seconds": job["progress"]["timings_seconds"]}
        assert "measurements" not in job and "payload" not in job
        measured = http.get(f"/api/jobs/{job_id}/measurements").json()
        assert measured["completed"] == measured["total"] == 7
        assert all(s["volume_ml"] > 0 and "mask_file" not in s for s in measured["structures"])
        assert http.get(prefix).json()["active_revision_id"] == rid
    finally:
        release.set()
    result = completed(http, job_id)
    assert result["progress"]["phase"] == "complete"
    refined = http.get(f"{prefix}/revisions/{result['revision_id']}").json()
    assert refined["processing_timings"]["measurement_cache_hits"] == 6
    assert refined["mesh_statistics"]["display"]["cache"]["surface_hits"] == 6
    assert refined["mesh_statistics"]["display"]["cache"]["surface_misses"] == 1
    assert refined["mesh_statistics"]["detailed"] is None
