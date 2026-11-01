"""Rendered view exports through the real manager, routes and encoder, on a fake Orthanc."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
from io import BytesIO
import json
from pathlib import Path
import stat
from types import SimpleNamespace

import numpy as np
from PIL import Image
import pydicom
import pytest

from scan3d import dicom_view
from scan3d import orthanc_view_exports as view_exports
from scan3d.errors import NotFound
from scan3d.orthanc import OrthancDeliveryUnknown, OrthancError
from scan3d.orthanc_exports import ExportUnavailable
from scan3d.store import identifier
from test_dicom_view import write_sources
from test_orthanc import A, Response, export_archive
from test_orthanc_exports import execute, rig
from test_orthanc_imports import SERIES_UID, STUDY, STUDY_UID, oid


def capture(seed=1, rows=24, columns=32):
    stream = BytesIO()
    Image.fromarray(np.random.default_rng(seed).integers(0, 256, (rows, columns, 3), dtype=np.uint8)).save(stream, "PNG")
    return stream.getvalue()


PNG = capture()
PNG_SHA256 = hashlib.sha256(PNG).hexdigest()


@pytest.fixture
def view(rig, monkeypatch):
    """The SEG rig's archive, with the study lookup and parent a view export relies on."""
    archive = rig.archive
    archive.patient_id, archive.parent_study, archive.study_error = "SYNTHETIC-ORTHANC", STUDY, None
    upload = archive.upload_instance

    def upload_instance(path, *, expected_sha256):
        return {**upload(path, expected_sha256=expected_sha256), "parent_study": archive.parent_study}

    def study(study_id):
        assert study_id == STUDY
        if archive.study_error:
            raise archive.study_error
        return {"id": STUDY, "study_uid": STUDY_UID, "patient_id": archive.patient_id}

    archive.upload_instance, archive.study = upload_instance, study
    encode, rig.encoded = dicom_view.export_view, []

    def counted(*args, **kwargs):
        result = encode(*args, **kwargs)
        rig.encoded.append(result)
        return result

    monkeypatch.setattr(dicom_view, "export_view", counted)
    rig.manager = rig.service.orthanc_view_exports
    rig.reference_id = rig.revision["reference_image_id"]
    rig.url = f"/api/cases/{rig.case['id']}/revisions/{rig.revision['id']}/dicom-view"
    rig.base_url = f"/api/cases/{rig.case['id']}/revisions/{rig.base['id']}/dicom-view"
    rig.captures = rig.service.store.case_dir(rig.case["id"]) / "dicom-view-captures"
    return rig


def request(rig, image=PNG, *, url=None, destination=None, resend=None, reference=None, headers=None):
    data = {"reference_image_id": rig.reference_id if reference is None else reference}
    if destination is not None:
        data["destination"] = destination
    if resend is not None:
        data["resend"] = "true" if resend else "false"
    return rig.http.post(url or rig.url, files={"image": ("view.png", image, "image/png")}, data=data, headers=headers)


def post(rig, image=PNG, **kwargs):
    response = request(rig, image, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()


def private_job(rig, job):
    return rig.service.store.get("jobs", job["id"])


def export_directory(rig, job):
    return rig.manager._directory(rig.case["id"], private_job(rig, job)["payload"]["export_id"])


def manifest(rig, job):
    return json.loads((export_directory(rig, job) / "export.json").read_text())


def stored(rig):
    return sorted(rig.captures.iterdir()) if rig.captures.exists() else []


def stored_names(rig):
    return [path.name for path in stored(rig)]


def stored_blob(rig):
    assert len(rig.archive.blobs) == 1
    return next(iter(rig.archive.blobs.values()))[1]


def test_archive_files_one_exact_secondary_capture_in_the_source_study(view):
    revisions = view.service.store.list("revisions", view.case["id"])
    queued = post(view)
    assert queued["kind"] == "orthanc_view_export" and queued["status"] == "queued" and queued["revision_id"] == view.revision["id"]
    assert "payload" not in queued
    finished = execute(view, queued)
    assert finished["status"] == "completed", finished["error"]
    assert finished["stage"] == "DICOM view archived"
    assert finished["result"] == {
        "dicom_view": {"export_id": finished["payload"]["export_id"], "encoding_version": 1, "archived": True, "sent": False, "instance_count": 1}
    }
    assert len(view.archive.uploads) == 1 and not view.archive.sends
    ds = pydicom.dcmread(BytesIO(stored_blob(view)))
    with Image.open(BytesIO(PNG)) as image:
        np.testing.assert_array_equal(ds.pixel_array, np.asarray(image))
    assert ds.StudyInstanceUID == STUDY_UID and ds.SeriesInstanceUID != SERIES_UID
    assert ds.PatientID == "SYNTHETIC-ORTHANC" and str(ds.PatientName) == "Synthetic^Orthanc"
    assert ds.Modality == "OT" and SERIES_UID in ds.DerivationDescription and view.revision["id"] in ds.DerivationDescription
    written = manifest(view, finished)
    assert written["png_sha256"] == PNG_SHA256 and (written["rows"], written["columns"]) == (24, 32)
    assert written["files"][0]["path"] == "view-0001.dcm" and written["files"][0]["sop_instance_uid"] == ds.SOPInstanceUID
    assert written["series_instance_uid"] == ds.SeriesInstanceUID and "frame_of_reference_uid" not in written
    assert hashlib.sha256((export_directory(view, finished) / "export.json").read_bytes()).hexdigest() == finished["payload"]["artifact_manifest_sha256"]
    assert view.service.store.list("revisions", view.case["id"]) == revisions


def test_send_reaches_the_configured_destination(view):
    finished = execute(view, post(view, destination="osirix"))
    assert finished["status"] == "completed", finished["error"]
    assert finished["stage"] == "DICOM view sent"
    assert finished["result"]["dicom_view"] == {
        "export_id": finished["payload"]["export_id"],
        "encoding_version": 1,
        "archived": True,
        "sent": True,
        "instance_count": 1,
        "destination": "OsiriX",
        "destination_id": "osirix",
    }
    assert len(view.archive.sends) == 1 and view.archive.sends[0][2] == "osirix"


def test_a_ct_only_revision_exports_though_the_seg_path_refuses_it(view):
    assert not view.base["structures"]
    available = view.http.get(view.base_url).json()
    assert available["available"] is True and available["destinations"][0]["id"] == "osirix"
    assert view.service.orthanc_exports.availability(view.case["id"], view.base["id"])["available"] is False
    finished = execute(view, post(view, url=view.base_url))
    assert finished["status"] == "completed", finished["error"]
    assert finished["result"]["dicom_view"]["archived"] is True


def test_availability_has_the_dicom_seg_shape(view):
    available = view.http.get(view.url)
    assert available.status_code == 200 and available.headers["cache-control"] == "no-store"
    assert available.json() == {
        "available": True,
        "destinations": [{"id": "osirix", "name": "OsiriX", "aet": "SYNTHETIC_OSIRIX"}],
        "exports": [],
        "encoding_version": 1,
    }
    assert set(available.json()) == set(view.http.get(view.url.replace("/dicom-view", "/dicom-seg")).json())
    case = view.service.store.get("cases", view.case["id"])
    case["references"] = {}
    view.service.store.save("cases", case)
    refused = view.http.get(view.url).json()
    assert refused["available"] is False and "original Orthanc CT" in refused["reason"] and refused["destinations"] == []
    assert request(view).status_code == 400 and not stored(view)


def test_an_unconfigured_archive_is_named_for_a_view(view, monkeypatch):
    monkeypatch.setattr(view.service, "settings", replace(view.settings, orthanc_url=""))
    assert view.http.get(view.url).json()["reason"] == "Configure Orthanc before exporting DICOM view."


def test_same_bytes_dedupe_to_one_job_and_a_completed_job_is_returned(view):
    queued = post(view)
    assert post(view)["id"] == queued["id"]
    finished = execute(view, queued)
    assert post(view)["id"] == finished["id"]
    assert len(view.encoded) == 1 and view.service.store.claim("cpu") is None


def test_sixteen_concurrent_identical_posts_make_one_job_and_one_retained_png(view):
    with ThreadPoolExecutor(max_workers=16) as pool:
        jobs = list(pool.map(lambda _: post(view, destination="osirix"), range(16)))
    assert len({job["id"] for job in jobs}) == 1
    assert stored_names(view) == [f"{PNG_SHA256}.png"]
    assert len(view.http.get(view.url).json()["exports"]) == 1


def test_concurrent_mixed_destinations_share_one_sop(view):
    with ThreadPoolExecutor(max_workers=8) as pool:
        jobs = list(pool.map(lambda index: post(view, destination="osirix" if index % 2 else None), range(16)))
    unique = {job["id"]: job for job in jobs}
    assert len(unique) == 2 and stored_names(view) == [f"{PNG_SHA256}.png"]
    assert len({private_job(view, job)["payload"]["export_id"] for job in unique.values()}) == 1
    while claimed := view.service.store.claim("cpu"):
        view.service.execute(claimed)
    assert {private_job(view, job)["status"] for job in unique.values()} == {"completed"}
    assert len(view.encoded) == 1 and len(view.archive.blobs) == 1 and len(view.archive.sends) == 1


def test_mixed_destinations_share_one_sop_in_sequence(view):
    archived = execute(view, post(view))
    sent = execute(view, post(view, destination="osirix"))
    assert sent["result"]["dicom_view"]["export_id"] == archived["result"]["dicom_view"]["export_id"]
    assert sent["result"]["dicom_view"]["sent"] is True
    assert len(view.encoded) == 1 and len(view.archive.blobs) == 1 and len(view.archive.sends) == 1
    assert manifest(view, sent) == manifest(view, archived)


def test_a_new_capture_is_a_new_series(view):
    first = execute(view, post(view, destination="osirix"))
    second = execute(view, post(view, capture(2), destination="osirix"))
    assert second["id"] != first["id"] and second["payload"]["export_id"] != first["payload"]["export_id"]
    one, two = manifest(view, first), manifest(view, second)
    assert one["series_instance_uid"] != two["series_instance_uid"] and one["files"][0]["sop_instance_uid"] != two["files"][0]["sop_instance_uid"]
    assert len(view.encoded) == 2 and len(view.archive.sends) == 2 and len(stored(view)) == 2


def test_retry_after_a_failed_upload_reuses_the_exact_sop(view):
    view.archive.upload_error = OrthancError("private filename /patient/DICOM.dcm")
    failed = execute(view, post(view, destination="osirix"))
    assert failed["status"] == "failed" and failed["stage"] == "Export failed" and "/patient" not in failed["error"]
    assert failed["error"] == "DICOM view export failed. Check the original CT, then retry."
    assert not view.archive.sends and len(view.archive.blobs) == 1
    original = (export_directory(view, failed) / "export.json").read_bytes()
    retried = post(view, destination="osirix")
    assert retried["id"] != failed["id"]
    finished = execute(view, retried)
    assert finished["result"]["dicom_view"]["sent"] is True
    assert (export_directory(view, finished) / "export.json").read_bytes() == original
    assert len(view.encoded) == 1 and len(view.archive.blobs) == 1 and len(view.archive.sends) == 1


def test_unknown_delivery_never_resends_itself_and_retry_reuses_the_sop(view):
    view.archive.start_error = OrthancDeliveryUnknown("Synthetic lost start response")
    failed = execute(view, post(view, destination="osirix"))
    assert failed["status"] == "failed" and failed["stage"] == "Delivery not confirmed"
    assert "exact view instances" in failed["error"] and failed["result"]["dicom_view"]["archived"] is True
    receipt = json.loads((export_directory(view, failed) / "delivery.json").read_text())
    assert receipt["destinations"]["osirix"]["state"] == "unknown"
    view.service.store.recover()
    view.manager.recover()
    assert len(view.archive.sends) == 1 and view.service.store.claim("cpu") is None
    finished = execute(view, post(view, destination="osirix"))
    assert finished["status"] == "completed", finished["error"]
    assert finished["payload"]["delivery_attempt_id"] == failed["payload"]["delivery_attempt_id"]
    assert len(view.encoded) == 1 and len(view.archive.sends) == 2 and view.archive.sends[0][1:] == view.archive.sends[1][1:]


def test_a_known_remote_job_is_polled_by_retry_without_a_second_send(view):
    view.archive.poll_error = OrthancError("Synthetic lost poll")
    failed = execute(view, post(view, destination="osirix"))
    assert failed["status"] == "failed" and "not confirmed" in failed["error"]
    view.service.store.update_job(failed["id"], status="running")
    view.service.store.recover()
    view.manager.recover()
    recovered = private_job(view, failed)
    assert recovered["stage"] == "Interrupted" and recovered["error"] == view.manager.interrupted_error and "view image" in recovered["error"]
    finished = execute(view, post(view, destination="osirix"))
    assert finished["status"] == "completed" and len(view.archive.sends) == 1


def test_resend_requires_a_confirmed_send_of_this_capture_to_this_destination(view):
    assert request(view, resend=True).status_code == 400
    refused = request(view, destination="osirix", resend=True)
    assert refused.status_code == 400 and "Send again requires" in refused.json()["detail"]
    assert not stored(view)
    first = execute(view, post(view, destination="osirix"))
    assert request(view, capture(2), destination="osirix", resend=True).status_code == 400
    again = post(view, destination="osirix", resend=True)
    assert again["id"] != first["id"]
    assert post(view, destination="osirix", resend=True)["id"] == again["id"]
    finished = execute(view, again)
    assert finished["payload"]["delivery_attempt_id"] != first["payload"]["delivery_attempt_id"]
    assert len(view.encoded) == 1 and len(view.archive.sends) == 2 and view.archive.sends[0][1:] == view.archive.sends[1][1:]
    assert post(view, destination="osirix")["id"] == finished["id"]


def test_cancelling_a_queued_retry_never_claims_an_active_delivery_was_cancelled(view):
    view.archive.poll_error = OrthancError("Synthetic poll interruption")
    execute(view, post(view, destination="osirix"))
    retry = post(view, destination="osirix")
    response = view.http.post(f"/api/jobs/{retry['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "failed" and "delivery may continue" in response.json()["stage"]
    assert view.service.store.claim("cpu") is None and len(view.archive.sends) == 1


def test_cancelling_a_queued_export_with_no_delivery_is_a_plain_cancel(view):
    queued = post(view)
    response = view.http.post(f"/api/jobs/{queued['id']}/cancel").json()
    assert response["status"] == "cancelled" and response["stage"] == "Cancelled before delivery"
    assert view.service.store.claim("cpu") is None and not view.archive.uploads


def test_cancel_after_upload_never_sends_and_retry_reuses_the_artifact(view):
    job = post(view, destination="osirix")
    view.archive.on_upload = lambda: view.service.store.update_job(job["id"], cancel_requested=True)
    cancelled = execute(view, job)
    assert cancelled["status"] == "cancelled" and not view.archive.sends
    view.archive.on_upload = None
    finished = execute(view, post(view, destination="osirix"))
    assert finished["result"]["dicom_view"]["sent"] is True and len(view.encoded) == 1


def test_cancel_during_the_retained_hash_is_a_cancellation_not_a_missing_file(view):
    job = post(view)
    claimed = view.service.store.claim("cpu")
    view.service.store.update_job(job["id"], cancel_requested=True)
    view.service.execute(claimed)
    cancelled = private_job(view, job)
    assert cancelled["status"] == "cancelled" and cancelled["stage"] == "Cancelled before delivery" and cancelled["error"] is None


def test_restart_interrupts_queued_view_jobs_and_never_sends_them(view):
    queued = post(view, destination="osirix")
    view.service.start(lanes=())
    view.service.worker_lock.close()
    recovered = private_job(view, queued)
    assert recovered["status"] == "failed" and recovered["stage"] == "Interrupted" and "view image" in recovered["error"]
    assert view.service.store.claim("cpu") is None and not view.archive.uploads and not view.archive.sends
    assert execute(view, post(view, destination="osirix"))["status"] == "completed"


def test_seg_recovery_leaves_view_jobs_alone_and_the_reverse(view):
    queued = post(view)
    seg = view.service.orthanc_exports.start(view.case["id"], view.revision["id"], ["test:0"])
    view.service.orthanc_exports.recover()
    assert private_job(view, queued)["status"] == "queued" and private_job(view, seg)["status"] == "failed"
    view.manager.recover()
    assert private_job(view, queued)["status"] == "failed" and private_job(view, seg)["error"] == view.service.orthanc_exports.interrupted_error


def test_an_orphan_encode_moves_the_export_but_the_retained_png_stays_reachable(view, monkeypatch):
    first = post(view)
    second = post(view, destination="osirix")
    write = view_exports.atomic_json

    def fail_manifest(path, value):
        if path.name == "export.json":
            raise OSError("Synthetic manifest write failure")
        return write(path, value)

    monkeypatch.setattr(view_exports, "atomic_json", fail_manifest)
    failed = execute(view, first)
    assert failed["status"] == "failed" and not view.archive.uploads
    old = view.manager._directory(view.case["id"], failed["payload"]["export_id"])
    orphan = (old / "dicom" / "view-0001.dcm").read_bytes()
    monkeypatch.setattr(view_exports, "atomic_json", write)
    finished = execute(view, second)
    assert finished["status"] == "completed", finished["error"]
    assert finished["stage"] == "DICOM view sent" and finished["payload"]["export_id"] != failed["payload"]["export_id"]
    assert (old / "dicom" / "view-0001.dcm").read_bytes() == orphan
    assert len(view.encoded) == 2 and len(view.archive.sends) == 1 and stored_names(view) == [f"{PNG_SHA256}.png"]


def test_a_capture_of_another_image_is_refused_before_anything_is_stored(view):
    refused = request(view, reference="0" * 64)
    assert refused.status_code == 400 and "different image" in refused.json()["detail"]
    assert not view.captures.exists() and not view.http.get(view.url).json()["exports"]


def test_another_cases_revision_is_not_found_and_touches_no_directory(view):
    other = view.service.new_case("Synthetic other")
    url = view.url.replace(view.case["id"], other["id"])
    assert view.http.get(url).status_code == 404
    assert request(view, url=url).status_code == 404
    assert not (view.service.store.case_dir(other["id"]) / "dicom-view-captures").exists()
    made_up = identifier()
    assert request(view, url=view.url.replace(view.case["id"], made_up)).status_code == 404
    assert not (view.settings.data_dir / "cases" / made_up).exists()


def test_a_review_candidate_is_never_a_saved_export(view):
    candidate = view.url.replace(view.revision["id"], "review-" + identifier())
    assert request(view, url=candidate).status_code == 400 and not stored(view)


@pytest.mark.parametrize("change", ["removed", "rewritten", "linked"])
def test_a_changed_retained_png_blocks_before_upload_and_an_identical_post_heals_it(view, tmp_path, change):
    job = post(view, destination="osirix")
    path = stored(view)[0]
    if change == "removed":
        path.unlink()
    elif change == "rewritten":
        path.write_bytes(b"changed capture")
    else:
        elsewhere = tmp_path / "elsewhere.png"
        elsewhere.write_bytes(PNG)
        path.unlink()
        path.symlink_to(elsewhere)
    failed = execute(view, job)
    assert failed["status"] == "failed" and "retained view image" in failed["error"]
    assert not view.encoded and not view.archive.uploads and not view.archive.sends
    finished = execute(view, post(view, destination="osirix"))
    assert finished["status"] == "completed", finished["error"]
    assert stored(view)[0].read_bytes() == PNG and not stored(view)[0].is_symlink()


def test_a_retained_png_changed_after_encoding_blocks_the_cached_artifact(view):
    view.archive.upload_error = OrthancError("Synthetic interruption")
    execute(view, post(view))
    retry = post(view, destination="osirix")
    stored(view)[0].write_bytes(b"changed capture")
    uploads = len(view.archive.uploads)
    failed = execute(view, retry)
    assert failed["status"] == "failed" and "retained view image" in failed["error"]
    assert len(view.archive.uploads) == uploads and not view.archive.sends and len(view.encoded) == 1


@pytest.mark.parametrize("changed", ["original", "manifest", "reference", "encoder"])
def test_queued_view_rechecks_its_sources_before_encoding(view, changed):
    job = post(view, destination="osirix")
    source_dir = Path(view.case["source_dir"])
    if changed == "original":
        path = next(source_dir.glob("*.dcm"))
        path.write_bytes(path.read_bytes() + b"changed source")
    elif changed == "manifest":
        path = source_dir.parent / "source-import.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif changed == "reference":
        Path(view.revision["ct_file"]).write_bytes(b"changed reference")
    else:
        payload = private_job(view, job)["payload"]
        payload["encoding_version"] = 99
        view.service.store.update_job(job["id"], payload=payload)
    failed = execute(view, job)
    assert failed["status"] == "failed" and str(view.root) not in failed["error"]
    assert not view.encoded and not view.archive.uploads and not view.archive.sends


@pytest.mark.parametrize("change", ["dicom", "manifest", "manifest-field"])
def test_a_changed_cached_artifact_blocks_a_retry(view, change):
    view.archive.upload_error = OrthancError("Synthetic interruption")
    first = execute(view, post(view, destination="osirix"))
    directory = export_directory(view, first)
    if change == "dicom":
        (directory / "dicom" / "view-0001.dcm").write_bytes(b"changed DICOM")
    elif change == "manifest":
        (directory / "export.json").write_bytes(b"{}")
    else:
        written = json.loads((directory / "export.json").read_text())
        written["png_sha256"] = "0" * 64
        (directory / "export.json").write_text(json.dumps(written))
        private = private_job(view, first)
        del private["payload"]["artifact_manifest_sha256"]
        view.service.store.update_job(first["id"], payload=private["payload"])
    uploads = len(view.archive.uploads)
    failed = execute(view, post(view, destination="osirix"))
    assert failed["status"] == "failed" and len(view.archive.uploads) == uploads and not view.archive.sends and len(view.encoded) == 1


@pytest.mark.parametrize(
    "study_patient, study_uid",
    [("Mller12", STUDY_UID), ("OTHER-PATIENT", STUDY_UID), ("SYNTHETIC-ORTHANC", "1.2.3")],
)
def test_a_study_filed_under_another_identity_uploads_nothing(view, study_patient, study_uid):
    view.archive.patient_id = study_patient
    original = view.archive.study
    view.archive.study = lambda study_id: {**original(study_id), "study_uid": study_uid}
    failed = execute(view, post(view, destination="osirix"))
    assert failed["status"] == "failed" and failed["stage"] == "Export failed"
    assert "different patient ID" in failed["error"] and "Nothing was stored" in failed["error"] and study_patient not in failed["error"]
    assert not view.archive.uploads and not view.archive.sends


def test_a_study_gone_from_the_archive_uploads_nothing(view):
    view.archive.study_error = NotFound("The selected Orthanc resource is no longer available")
    failed = execute(view, post(view, destination="osirix"))
    assert failed["status"] == "failed" and "no longer in Orthanc" in failed["error"]
    assert not view.archive.uploads and not view.archive.sends


def test_an_unreachable_archive_during_the_study_check_uploads_nothing(view):
    view.archive.study_error = OrthancError("Synthetic outage")
    failed = execute(view, post(view))
    assert failed["status"] == "failed" and failed["error"] == view.manager.failed_error and not view.archive.uploads


def test_trailing_spaces_do_not_refuse_a_matching_study(view):
    view.archive.patient_id = "SYNTHETIC-ORTHANC  "
    assert execute(view, post(view))["status"] == "completed"


def test_the_study_check_reads_the_instance_as_utf8(tmp_path):
    """The same comparison on a real non-ASCII instance, as the source would give it."""
    sources = write_sources(tmp_path / "sources", 1, SpecificCharacterSet="ISO_IR 100", PatientID="MÜ12")
    png = tmp_path / "capture.png"
    png.write_bytes(PNG)
    instance = dicom_view.export_view(sources, png, tmp_path / "export", revision_id="a" * 32)
    study_uid = str(pydicom.dcmread(sources[0], stop_before_pixels=True).StudyInstanceUID)
    archived = ["MÜ12"]

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def study(self, study_id):
            return {"study_uid": study_uid, "patient_id": archived[0]}

    service = SimpleNamespace(orthanc=SimpleNamespace(client=Client))
    inputs = SimpleNamespace(source=SimpleNamespace(snapshot=SimpleNamespace(study_id=STUDY, study_uid=study_uid)))
    manager = view_exports.OrthancViewExports(service)
    manager._check_study(inputs, instance.path)
    archived[0] = "M12"
    with pytest.raises(ExportUnavailable, match="different patient ID"):
        manager._check_study(inputs, instance.path)


def test_the_client_reports_the_study_orthanc_chose(export_archive, tmp_path, monkeypatch):
    client, upstream = export_archive
    source = tmp_path / "synthetic-view.dcm"
    source.write_bytes(b"synthetic view")
    digest = hashlib.sha256(b"synthetic view").hexdigest()
    forward = upstream.request

    def with_parent(method, url, **kwargs):
        response = forward(method, url, **kwargs)
        return Response(json.dumps({"ID": A, "Status": "Success", "ParentStudy": STUDY}).encode()) if method == "POST" else response

    monkeypatch.setattr(upstream, "request", with_parent)
    result = client.upload_instance(source, expected_sha256=digest)
    assert result == {"id": A, "status": "Success", "sha256": digest, "size": source.stat().st_size, "parent_study": STUDY}


@pytest.mark.parametrize("parent", [oid(77), None])
def test_a_misfiled_upload_stops_before_its_receipt_and_any_send(view, parent):
    view.archive.parent_study = parent
    failed = execute(view, post(view, destination="osirix"))
    assert failed["status"] == "failed" and failed["stage"] == "Export failed"
    assert failed["error"] == "Orthanc stored this view outside the original study, so it was not sent. Check the study in Orthanc."
    assert len(view.archive.uploads) == 1 and not view.archive.sends
    assert not (export_directory(view, failed) / "delivery.json").exists()
    assert failed["result"]["dicom_view"]["archived"] is False


def test_the_public_job_carries_no_patient_field_path_or_capture_hash(view):
    queued = post(view, destination="osirix")
    finished = execute(view, queued)
    for record in (
        queued,
        view.http.get(f"/api/jobs/{queued['id']}").json(),
        view.http.get(view.url).json(),
        view.http.get(f"/api/cases/{view.case['id']}/jobs").json(),
    ):
        encoded = json.dumps(record)
        for private in ("SYNTHETIC-ORTHANC", "Synthetic^Orthanc", str(view.root), PNG_SHA256, "private-user", "private-password", "artifact_key", STUDY_UID):
            assert private not in encoded, private
    assert finished["result"]["dicom_view"].keys() == {"export_id", "encoding_version", "archived", "sent", "instance_count", "destination", "destination_id"}


def spy_on_start(view, monkeypatch):
    calls = []

    def start(*args, **kwargs):
        calls.append(args)
        raise AssertionError("the handler must not run")

    monkeypatch.setattr(view.manager, "start", start)
    return calls


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"content-length": "abc"}, 411),
        ({"content-length": "-1"}, 411),
        ({"content-length": "+5"}, 411),
        ({"content-length": "1e3"}, 411),
        ({"content-length": ""}, 411),
        ({"transfer-encoding": "chunked"}, 411),
        ({"content-length": "10", "transfer-encoding": "chunked"}, 411),
        ({"content-length": str(128 * 1024**2 + 1024**2 + 1)}, 413),
        ({"content-length": str(300 * 1024**2)}, 413),
        ({"content-length": "9" * 5000}, 413),
    ],
)
def test_a_body_that_cannot_be_bounded_is_refused_before_the_handler(view, monkeypatch, headers, status):
    calls = spy_on_start(view, monkeypatch)
    response = request(view, headers=headers)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert not calls and not stored(view)


def test_a_chunked_body_is_refused_before_the_handler(view, monkeypatch):
    calls = spy_on_start(view, monkeypatch)

    def body():
        yield b"--x\r\n"

    response = view.http.post(view.url, content=body(), headers={"content-type": "multipart/form-data; boundary=x"})
    assert response.status_code == 411 and response.headers["cache-control"] == "no-store" and not calls


def test_an_encoded_question_mark_does_not_slip_past_the_size_check(view, monkeypatch):
    calls = spy_on_start(view, monkeypatch)
    response = view.http.post(f"/api/cases/abc%3Fx/revisions/{view.revision['id']}/dicom-view", content=b"x", headers={"content-length": str(300 * 1024**2)})
    assert response.status_code == 413 and not calls


def test_the_limit_follows_a_smaller_configured_upload_limit(view, monkeypatch):
    monkeypatch.setattr(view.service, "settings", replace(view.settings, upload_limit=1024))
    calls = spy_on_start(view, monkeypatch)
    assert request(view, headers={"content-length": str(1024 + 1024**2 + 1)}).status_code == 413
    assert not calls


def test_staging_counts_bytes_as_a_second_check(view, monkeypatch):
    monkeypatch.setattr(view.service, "settings", replace(view.settings, upload_limit=64))
    refused = request(view)
    assert refused.status_code == 400 and "upload limit" in refused.json()["detail"]
    assert stored(view) == [] and not view.http.get(view.url).json()["exports"]


def test_get_never_needs_a_body_declaration(view):
    assert view.http.get(view.url, headers={"transfer-encoding": "chunked"}).status_code == 200


def test_every_dicom_view_response_is_no_store(view):
    responses = [
        view.http.get(view.url),
        view.http.get(view.url.replace(view.case["id"], identifier())),
        request(view),
        request(view, b"not a png"),
        request(view, reference=""),
        request(view, url=view.url.replace(view.case["id"], identifier())),
        view.http.post(view.url, json={}),
        request(view, headers={"origin": "https://elsewhere.test"}),
        request(view, headers={"content-length": "abc"}),
        request(view, headers={"content-length": str(300 * 1024**2)}),
    ]
    assert [item.status_code for item in responses] == [200, 404, 200, 400, 422, 404, 422, 403, 411, 413]
    assert all(item.headers["cache-control"] == "no-store" for item in responses)


@pytest.mark.parametrize(
    "image",
    [
        b"",
        b"not a png",
        PNG[:40],
        PNG[:-12],
        PNG[:8] + b"\0" * 64,
    ],
)
def test_a_corrupt_png_is_a_400_and_leaves_nothing_behind(view, image):
    response = request(view, image)
    assert response.status_code == 400, response.text
    assert "capture" in response.json()["detail"].lower()
    assert stored(view) == [] and not view.http.get(view.url).json()["exports"]


def test_a_blank_capture_is_a_400(view):
    stream = BytesIO()
    Image.fromarray(np.zeros((8, 8, 3), dtype=np.uint8)).save(stream, "PNG")
    response = request(view, stream.getvalue())
    assert response.status_code == 400 and "blank" in response.json()["detail"]


@pytest.mark.parametrize(
    "fields,status",
    [
        ({"destination": ""}, 422),
        ({"destination": "http://untrusted/"}, 422),
        ({"destination": "unlisted"}, 400),
        ({"destination": "null"}, 400),
    ],
)
def test_destination_fields_are_validated_before_any_bytes_are_kept(view, fields, status):
    assert request(view, **fields).status_code == status
    assert stored(view) == [] and not view.archive.uploads


def test_required_multipart_fields(view):
    files = {"image": ("view.png", PNG, "image/png")}
    assert view.http.post(view.url, files=files).status_code == 422
    assert view.http.post(view.url, data={"reference_image_id": view.reference_id}).status_code == 422
    assert view.http.post(view.url, data={"reference_image_id": view.reference_id, "image": "text"}).status_code == 422


def test_retained_captures_are_private_and_named_by_content(view):
    post(view)
    assert stored_names(view) == [f"{PNG_SHA256}.png"]
    assert stat.S_IMODE(view.captures.stat().st_mode) == 0o700
    assert stat.S_IMODE(stored(view)[0].stat().st_mode) == 0o600
    assert stored(view)[0].read_bytes() == PNG


def test_a_failed_commit_leaves_only_a_reusable_retained_file(view, monkeypatch):
    real = view.manager._enqueue

    def crash(*args, **kwargs):
        publish = kwargs["publish"]

        def explode(payload):
            publish(payload)
            raise OSError("Synthetic commit failure")

        return real(*args, **{**kwargs, "publish": explode})

    monkeypatch.setattr(view.manager, "_enqueue", crash)
    with pytest.raises(OSError, match="Synthetic commit failure"):
        view.manager.start(view.case["id"], view.revision["id"], BytesIO(PNG), view.reference_id)
    monkeypatch.setattr(view.manager, "_enqueue", real)
    assert stored_names(view) == [f"{PNG_SHA256}.png"] and not view.http.get(view.url).json()["exports"]
    assert execute(view, post(view))["status"] == "completed" and stored_names(view) == [f"{PNG_SHA256}.png"]


def test_a_view_of_a_destination_disabled_after_queueing_stays_archived(view, monkeypatch):
    job = post(view, destination="osirix")
    monkeypatch.setattr(view.service, "settings", replace(view.settings, orthanc_export_destinations=()))
    failed = execute(view, job)
    assert failed["status"] == "failed" and failed["error"] == "The selected destination is no longer enabled. DICOM view remains archived."
    assert failed["result"]["dicom_view"]["archived"] is True and not view.archive.sends


@pytest.mark.parametrize("safe", [True, False])
def test_only_deliberately_safe_encoder_errors_reach_the_public_job(view, monkeypatch, safe):
    def fail(*args, **kwargs):
        raise dicom_view.DicomViewError("Original CT images do not belong to one patient, study and series") if safe else TypeError("private/patient/file")

    monkeypatch.setattr(dicom_view, "export_view", fail)
    failed = execute(view, post(view))
    assert failed["status"] == "failed" and "private/patient" not in failed["error"]
    assert ("one patient" in failed["error"]) is safe
    assert not view.archive.uploads


def test_seg_and_view_availability_never_list_each_others_jobs(view):
    finished_view = execute(view, post(view, destination="osirix"))
    seg = view.service.orthanc_exports.start(view.case["id"], view.revision["id"], ["test:0"], "osirix")
    assert execute(view, seg)["status"] == "completed"
    seg_exports = view.http.get(view.url.replace("/dicom-view", "/dicom-seg")).json()["exports"]
    view_exports_listed = view.http.get(view.url).json()["exports"]
    assert [item["id"] for item in seg_exports] == [seg["id"]] and all(item["kind"] == "orthanc_export" for item in seg_exports)
    assert [item["id"] for item in view_exports_listed] == [finished_view["id"]]
    assert all(item["kind"] == "orthanc_view_export" and "dicom_view" in item["result"] for item in view_exports_listed)


def test_availability_only_lists_exports_of_the_requested_revision(view):
    execute(view, post(view))
    assert view.http.get(view.url).json()["exports"]
    assert not view.http.get(view.base_url).json()["exports"]


def test_a_send_posted_during_an_encode_does_not_disturb_it(view, monkeypatch):
    encode, queued = dicom_view._encode, []

    def during(*args, **kwargs):
        queued.append(post(view, destination="osirix"))
        return encode(*args, **kwargs)

    monkeypatch.setattr(dicom_view, "_encode", during)
    first = execute(view, post(view))
    assert first["status"] == "completed", first["error"]
    assert private_job(view, queued[0])["payload"]["export_id"] == first["payload"]["export_id"]
    finished = execute(view, queued[0])
    assert finished["status"] == "completed", finished["error"]
    assert len(view.encoded) == 1 and len(view.archive.blobs) == 1 and len(view.archive.sends) == 1
