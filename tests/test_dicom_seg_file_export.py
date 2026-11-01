"""A saved revision of a folder import becomes a SEG file with no Orthanc in reach."""

import copy
import hashlib
from io import BytesIO
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
from scan3d.store import identifier, job_lane
from test_dicom_seg import fixture, independent_pixels


def import_folder(service, fixture, reference=None):
    """What a folder import and preparation leave behind, without dcm2niix."""
    case = service.new_case("Synthetic local SEG export")
    source = service.store.case_dir(case["id"]) / "dicom"
    source.mkdir()
    for path in fixture["paths"]:
        shutil.copy2(path, source / path.name)
    case = service.finish_import(case, source)
    series = case["series"][0]
    digest = hashlib.sha256(b"".join(Path(path).read_bytes() for path in sorted(series["files"]))).hexdigest()
    target = service.store.case_dir(case["id"]) / "converted" / digest / "reference.nii.gz"
    target.parent.mkdir(parents=True)
    shutil.copy2(reference or fixture["reference"], target)
    ct = service.build_ct_revision(
        case["id"],
        target,
        acquisition={"series_uid": series["uid"], "profile": "whole_body", "phase": "unknown"},
        image_source={key: series[key] for key in ("study_uid", "frame_of_reference_uid", "sop_class_uids")},
    )
    service.store.commit_revision(ct)
    return case, ct


def save_masks(service, ct, fixture):
    directory = service.store.case_dir(ct["case_id"]) / "saved-masks"
    directory.mkdir()
    saved = copy.deepcopy(ct)
    saved.update(id=identifier(), parent_id=ct["id"], source="totalsegmentator", kind="segmentation", structures=[])
    for segment in fixture["segments"]:
        path = directory / segment.mask_path.name
        shutil.copy2(segment.mask_path, path)
        count = int(np.count_nonzero(np.asanyarray(nib.load(path).dataobj)))
        saved["structures"].append(
            dict(id=segment.id, name=segment.name, color=segment.color, mask_file=str(path), voxel_count=count, reference_image_id=ct["reference_image_id"])
        )
    service.store.save("revisions", saved)
    return saved


@pytest.fixture
def rig(tmp_path, fixture):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", modalities=("CT",)))
    service = app.state.service
    case, ct = import_folder(service, fixture)
    saved = save_masks(service, ct, fixture)
    http = TestClient(app, base_url="http://127.0.0.1")
    yield SimpleNamespace(
        service=service,
        http=http,
        case=case,
        ct=ct,
        saved=saved,
        fixture=fixture,
        endpoint=f"/api/cases/{case['id']}/revisions/{saved['id']}/dicom-seg",
    )
    http.close()


def prepare(rig, **body):
    response = rig.http.post(rig.endpoint, json={"delivery": "file", **body})
    assert response.status_code == 200, response.text
    return response.json()


def execute(rig, job):
    claimed = rig.service.store.claim("cpu")
    assert claimed["id"] == job["id"] and job_lane(claimed) == "cpu"
    rig.service.execute(claimed)
    return rig.service.store.get("jobs", job["id"])


def file_url(rig, job):
    return f"{rig.endpoint}/{job['result']['dicom_seg']['export_id']}/segment.dcm"


def download(rig, job, path):
    response = rig.http.get(file_url(rig, job))
    assert response.status_code == 200, response.text
    path.write_bytes(response.content)
    return response


def test_folder_case_saves_exact_masks_as_a_file_without_orthanc(rig, tmp_path):
    available = rig.http.get(rig.endpoint).json()
    assert available["available"] is False and "Configure Orthanc" in available["reason"]
    assert available["file"] == {"available": True}
    before = rig.service.store.list("revisions", rig.case["id"])
    finished = execute(rig, prepare(rig))
    assert finished["status"] == "completed" and finished["stage"] == "DICOM SEG file ready"
    result = finished["result"]["dicom_seg"]
    assert result["file"] is True and result["archived"] is False and result["sent"] is False
    assert result["segments_count"] == 2 and result["instance_count"] == 1
    output = tmp_path / "saved.dcm"
    response = download(rig, finished, output)
    assert response.headers["content-type"] == "application/dicom"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"] == (f'attachment; filename="scan3d-seg-{rig.saved["id"][:8]}-{result["export_id"][-8:]}.dcm"')
    assert int(response.headers["content-length"]) == len(response.content)
    images = rig.fixture["images"]
    for number, index in ((1, 1), (2, 0)):  # Unranked segments follow the sorted IDs: block, ring.
        seg, pixels = independent_pixels(output, images, number)
        assert np.array_equal(pixels, rig.fixture["truth"][..., index])
    assert seg.SOPClassUID == "1.2.840.10008.5.1.4.1.1.66.4"
    assert seg.PatientID == images[0].PatientID and seg.StudyInstanceUID == images[0].StudyInstanceUID
    assert seg.FrameOfReferenceUID == images[0].FrameOfReferenceUID
    assert seg.SegmentsOverlap == "UNDEFINED"
    assert rig.service.store.list("revisions", rig.case["id"]) == before
    assert rig.service.store.get("cases", rig.case["id"])["active_revision_id"] == rig.ct["id"]
    history = rig.http.get(rig.endpoint).json()["exports"]
    assert history[0]["result"] == finished["result"]
    private = rig.service.store.get("jobs", finished["id"])
    assert rig.case["label"] not in history[0]["stage"] and "payload" not in history[0]
    assert private["payload"]["source_manifest_sha256"] == Path(rig.ct["ct_file"]).parent.name


def test_downloaded_file_imports_back_into_its_own_case(rig, tmp_path):
    output = tmp_path / "saved.dcm"
    download(rig, execute(rig, prepare(rig)), output)
    queued = rig.http.post(
        f"/api/cases/{rig.case['id']}/dicom-seg/imports",
        data={"revision_id": rig.ct["id"]},
        files=[("files", ("seg.dcm", output.read_bytes(), "application/dicom"))],
    )
    assert queued.status_code == 200, queued.text
    assert execute(rig, queued.json())["status"] == "completed"
    imported = rig.service.store.revision(rig.case["id"], rig.service.store.get("cases", rig.case["id"])["active_revision_id"])
    assert sorted(item["name"] for item in imported["structures"]) == ["Block", "Ring"]
    masks = {item["name"]: np.asanyarray(nib.load(item["mask_file"]).dataobj) for item in imported["structures"]}
    for segment in rig.fixture["segments"]:
        assert np.array_equal(masks[segment.name] > 0, np.asanyarray(nib.load(segment.mask_path).dataobj) > 0)


def test_ranked_file_resolves_overlaps_only_in_that_export(rig, tmp_path):
    plain = execute(rig, prepare(rig))
    ranked = execute(rig, prepare(rig, overlap_priority=["ring", "block"]))
    assert ranked["status"] == "completed"
    result = ranked["result"]["dicom_seg"]
    assert result["overlap_priority"] == ["ring", "block"] and result["removed_voxels"]["block"] > 0 and result["removed_voxels"]["ring"] == 0
    assert result["export_id"] != plain["result"]["dicom_seg"]["export_id"]
    output = tmp_path / "ranked.dcm"
    served = download(rig, ranked, output)
    # Two files of one revision must not overwrite each other in a downloads folder.
    assert served.headers["content-disposition"] != rig.http.get(file_url(rig, plain)).headers["content-disposition"]
    seg, ring = independent_pixels(output, rig.fixture["images"], 1)
    _, block = independent_pixels(output, rig.fixture["images"], 2)
    assert seg.SegmentsOverlap == "NO" and seg.SeriesDescription.endswith("overlaps resolved")
    assert np.array_equal(ring, rig.fixture["truth"][..., 0]) and not np.any(ring & block)
    for entry in rig.saved["structures"]:
        original = next(item for item in rig.fixture["segments"] if item.id == entry["id"])
        assert Path(entry["mask_file"]).read_bytes() == original.mask_path.read_bytes()


def test_repeated_requests_reuse_one_job_and_its_immutable_bytes(rig, tmp_path, monkeypatch):
    queued = prepare(rig)
    assert prepare(rig)["id"] == queued["id"]
    finished = execute(rig, queued)
    first = rig.http.get(file_url(rig, finished)).content
    assert prepare(rig)["id"] == queued["id"]
    assert rig.service.store.claim("cpu") is None
    assert rig.http.get(file_url(rig, finished)).content == first


def test_retry_after_a_failed_job_reuses_the_same_sop(rig, monkeypatch):
    finished = execute(rig, prepare(rig))
    sop = pydicom.dcmread(BytesIO(rig.http.get(file_url(rig, finished)).content), stop_before_pixels=True).SOPInstanceUID
    rig.service.store.update_job(finished["id"], status="failed", stage="Export failed", error="Synthetic failure")
    retried = execute(rig, prepare(rig))
    assert retried["id"] != finished["id"] and retried["payload"]["export_id"] == finished["payload"]["export_id"]
    seg = pydicom.dcmread(BytesIO(rig.http.get(file_url(rig, retried)).content), stop_before_pixels=True)
    assert seg.SOPInstanceUID == sop


def test_changed_masks_are_refused_after_an_export_of_that_selection(rig):
    execute(rig, prepare(rig))
    path = Path(rig.saved["structures"][0]["mask_file"])
    image = nib.load(path)
    nib.save(nib.Nifti1Image(np.asanyarray(image.dataobj)[::-1].copy(), image.affine), path)
    refused = rig.http.post(rig.endpoint, json={"delivery": "file"})
    assert refused.status_code == 400 and "Saved masks changed" in refused.json()["detail"]


def test_source_bytes_that_no_longer_match_the_reference_stop_the_job(rig):
    queued = prepare(rig)
    path = Path(rig.case["series"][0]["files"][3])
    path.write_bytes(path.read_bytes() + b"\0\0")
    failed = execute(rig, queued)
    assert failed["status"] == "failed" and "no longer match the saved reference image" in failed["error"]
    missing = rig.http.get(f"{rig.endpoint}/{failed['payload']['export_id']}/segment.dcm")
    assert missing.status_code == 404


def test_strict_grid_checks_apply_to_a_file(tmp_path, fixture):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", modalities=("CT",)))
    service = app.state.service
    original = nib.load(fixture["reference"])
    shifted = original.affine.copy()
    shifted[:3, 3] += 0.4 * shifted[:3, 0]
    moved = tmp_path / "shifted.nii.gz"
    nib.save(nib.Nifti1Image(np.asanyarray(original.dataobj), shifted), moved)
    masks = []
    for segment in fixture["segments"]:
        target = tmp_path / f"shifted-{segment.mask_path.name}"
        nib.save(nib.Nifti1Image(np.asanyarray(nib.load(segment.mask_path).dataobj), shifted), target)
        masks.append(SimpleNamespace(id=segment.id, name=segment.name, color=segment.color, mask_path=target))
    case, ct = import_folder(service, fixture, moved)
    saved = save_masks(service, ct, {"segments": masks})
    http = TestClient(app, base_url="http://127.0.0.1")
    queued = http.post(f"/api/cases/{case['id']}/revisions/{saved['id']}/dicom-seg", json={"delivery": "file"}).json()
    http.close()
    claimed = service.store.claim("cpu")
    service.execute(claimed)
    failed = service.store.get("jobs", queued["id"])
    assert failed["status"] == "failed" and "reference grid" in failed["error"]
    assert not (service.store.case_dir(case["id"]) / "dicom-exports" / failed["payload"]["export_id"] / "export.json").exists()


def test_changed_prepared_bytes_are_never_served_and_stay_in_place(rig):
    finished = execute(rig, prepare(rig))
    path = rig.service.orthanc_exports._directory(rig.case["id"], finished["payload"]["export_id"]) / "dicom" / "segment-0001.dcm"
    changed = path.read_bytes()[:-1] + b"\x01"
    path.write_bytes(changed)
    refused = rig.http.get(file_url(rig, finished))
    assert refused.status_code == 400 and refused.headers["cache-control"] == "no-store"
    assert "prepared DICOM SEG bytes changed" in refused.json()["detail"]
    assert path.read_bytes() == changed


def test_only_a_finished_file_of_this_revision_is_served(rig):
    queued = prepare(rig)
    export_id = queued["result"]["dicom_seg"]["export_id"]
    assert rig.http.get(f"{rig.endpoint}/{export_id}/segment.dcm").status_code == 404
    finished = execute(rig, queued)
    other = rig.endpoint.replace(rig.saved["id"], rig.ct["id"])
    assert rig.http.get(f"{other}/{export_id}/segment.dcm").status_code == 404
    unknown = rig.http.get(f"{rig.endpoint}/{identifier()}/segment.dcm")
    assert unknown.status_code == 404 and unknown.headers["cache-control"] == "no-store"
    assert rig.http.get(f"{rig.endpoint}/..%2F..%2Fsecret/segment.dcm").status_code == 404
    assert rig.http.get(file_url(rig, finished)).status_code == 200


def test_a_file_takes_no_destination_and_an_unknown_delivery_is_a_schema_error(rig):
    for body in ({"destination": "osirix"}, {"resend": True}):
        refused = rig.http.post(rig.endpoint, json={"delivery": "file", **body})
        assert refused.status_code == 400 and "no destination" in refused.json()["detail"]
    assert rig.http.post(rig.endpoint, json={"delivery": "email"}).status_code == 422
    refused = rig.http.post(rig.endpoint, json={})
    assert refused.status_code == 400 and "Configure Orthanc" in refused.json()["detail"]


def test_a_reference_not_named_by_its_source_bytes_has_no_file_export(rig):
    revision = rig.service.store.revision(rig.case["id"], rig.saved["id"])
    moved = Path(revision["ct_file"]).parent.parent / "reference.nii.gz"
    shutil.copy2(revision["ct_file"], moved)
    revision["ct_file"] = str(moved)
    rig.service.store.save("revisions", revision)
    available = rig.http.get(rig.endpoint).json()["file"]
    assert available["available"] is False and "no verified original DICOM CT series" in available["reason"]
    assert rig.http.post(rig.endpoint, json={"delivery": "file"}).status_code == 400


def test_demo_masks_have_no_source_series_to_reference(tmp_path):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", modalities=("CT",)))
    http = TestClient(app, base_url="http://127.0.0.1")
    case = http.post("/api/cases/demo").json()
    available = http.get(f"/api/cases/{case['id']}/revisions/{case['active_revision_id']}/dicom-seg").json()
    http.close()
    assert available["file"]["available"] is False and "no verified original DICOM CT series" in available["file"]["reason"]


def test_cancelling_a_queued_file_stops_it_and_a_new_request_can_finish(rig):
    queued = prepare(rig)
    cancelled = rig.http.post(f"/api/jobs/{queued['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["error"] is None
    assert rig.service.store.claim("cpu") is None
    assert rig.http.get(file_url(rig, queued)).status_code == 404
    again = prepare(rig)
    assert again["id"] != queued["id"]
    finished = execute(rig, again)
    assert finished["status"] == "completed" and rig.http.get(file_url(rig, finished)).status_code == 200


def test_cancelling_while_encoding_publishes_nothing_and_a_new_request_can_finish(rig, monkeypatch):
    job = prepare(rig)
    update = rig.service.store.update_job

    def cancel_at_first_progress(job_id, **changes):
        if "progress" in changes:
            assert rig.http.post(f"/api/jobs/{job_id}/cancel").json()["status"] == "running"
        return update(job_id, **changes)

    monkeypatch.setattr(rig.service.store, "update_job", cancel_at_first_progress)
    cancelled = execute(rig, job)
    monkeypatch.undo()
    assert cancelled["status"] == "cancelled" and cancelled["stage"] == "Cancelled before delivery" and cancelled["error"] is None
    assert "completed" not in {item["status"] for item in rig.http.get(rig.endpoint).json()["exports"]}
    assert rig.http.get(file_url(rig, job)).status_code == 404
    export = rig.service.orthanc_exports._directory(rig.case["id"], job["result"]["dicom_seg"]["export_id"])
    assert not (export / "export.json").exists()
    finished = execute(rig, prepare(rig))
    assert finished["status"] == "completed" and rig.http.get(file_url(rig, finished)).status_code == 200


def test_an_interrupted_file_job_does_not_mention_orthanc(rig):
    queued = prepare(rig)
    rig.service.orthanc_exports.recover()
    failed = rig.service.store.get("jobs", queued["id"])
    assert failed["status"] == "failed" and failed["error"] == "Export interrupted. Retry to reuse its exact SEG bytes."
