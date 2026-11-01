"""Saved masks archived and sent as native OsiriX ROIs, with the real writer, on a fake Orthanc."""

from dataclasses import replace
import hashlib
from io import BytesIO
import json

import nibabel as nib
import numpy as np
import pydicom
import pytest

from scan3d import identity
from scan3d.orthanc import OrthancError

# Before the SEG rig swaps in its fake encoder, so the writer binds the real source geometry it shares.
from scan3d import osirix_roi
from test_orthanc_exports import execute, rig
from test_orthanc_imports import STUDY, STUDY_UID, oid


@pytest.fixture
def roi(rig, monkeypatch):
    """The SEG rig, with the study lookup and parent a study check relies on, and one mask over every slice."""
    archive = rig.archive
    archive.parent_study = STUDY
    upload = archive.upload_instance
    archive.upload_instance = lambda path, *, expected_sha256: {**upload(path, expected_sha256=expected_sha256), "parent_study": archive.parent_study}
    archive.study = lambda study_id: {"id": STUDY, "study_uid": STUDY_UID, "patient_id": "SYNTHETIC-ORTHANC"}
    structure = rig.revision["structures"][0]
    image = nib.load(structure["mask_file"])
    values = np.zeros(image.shape, dtype=np.uint8)
    values[1:4, 2:5, :] = 1
    nib.save(nib.Nifti1Image(values, image.affine), structure["mask_file"])
    structure["voxel_count"] = int(values.sum())
    rig.service.store.save("revisions", rig.revision)
    write, rig.encoded = osirix_roi.export_native_rois, []

    def counted(*args, **kwargs):
        rig.encoded.append(write(*args, **kwargs))
        return rig.encoded[-1]

    monkeypatch.setattr(osirix_roi, "export_native_rois", counted)
    return rig


def post(rig, **body):
    response = rig.http.post(rig.endpoint, json={"format": "osirix_roi", **body})
    assert response.status_code == 200, response.text
    return response.json()


def test_archive_then_send_stores_one_instance_per_slice_and_sends_them_in_one_orthanc_job(roi):
    queued = post(roi)
    assert queued["kind"] == "orthanc_roi_export" and "roi_count" not in queued["result"]["osirix_roi"]
    stages = []
    roi.archive.on_upload = lambda: stages.append(roi.service.store.get("jobs", queued["id"])["stage"])
    archived = execute(roi, queued)
    assert archived["status"] == "completed", archived["error"]
    assert archived["stage"] == "OsiriX ROIs archived" and stages == [f"Archiving OsiriX ROIs in Orthanc ({number} of 3)" for number in (1, 2, 3)]
    roi.archive.on_upload = None
    assert archived["result"] == {
        "osirix_roi": {
            "export_id": archived["payload"]["export_id"],
            "encoding_version": 1,
            "archived": True,
            "sent": False,
            "segments_count": 2,
            # The first mask covers all three slices and the second one of them.
            "roi_count": 4,
            "instance_count": 3,
            "structure_ids": ["test:0", "test:1"],
        }
    }
    assert len(roi.archive.uploads) == 3 and not roi.archive.sends and not roi.calls
    sent = execute(roi, post(roi, destination="osirix"))
    assert sent["status"] == "completed", sent["error"]
    assert sent["result"]["osirix_roi"]["sent"] is True and sent["result"]["osirix_roi"]["export_id"] == archived["result"]["osirix_roi"]["export_id"]
    assert len(roi.encoded) == 1 and len(roi.archive.sends) == 1
    assert sorted(roi.archive.sends[0][1]) == sorted({resource for resource, _ in roi.archive.uploads}) and roi.archive.sends[0][2] == "osirix"
    for _, data in roi.archive.blobs.values():
        ds = pydicom.dcmread(BytesIO(data))
        assert ds.Modality == "SR" and ds.StudyInstanceUID == STUDY_UID and ds.PatientID == "SYNTHETIC-ORTHANC"
    encoded = json.dumps(roi.http.get(roi.endpoint).json())
    assert str(roi.root) not in encoded and "SYNTHETIC-ORTHANC" not in encoded and "artifact_key" not in encoded


def test_a_repeat_dedupes_and_send_again_reuses_the_same_instances(roi):
    refused = roi.http.post(roi.endpoint, json={"format": "osirix_roi", "destination": "osirix", "resend": True})
    assert refused.status_code == 400 and "Send again requires" in refused.json()["detail"]
    queued = post(roi, destination="osirix")
    assert post(roi, destination="osirix")["id"] == queued["id"]
    first = execute(roi, queued)
    assert post(roi, destination="osirix")["id"] == first["id"]
    again = execute(roi, post(roi, destination="osirix", resend=True))
    assert again["id"] != first["id"] and again["result"] == first["result"]
    assert len(roi.encoded) == 1 and len(roi.archive.blobs) == 3
    assert len(roi.archive.sends) == 2 and roi.archive.sends[0][1:] == roi.archive.sends[1][1:]


@pytest.mark.parametrize("body", [{"delivery": "file"}, {"overlap_priority": ["test:1", "test:0"]}])
def test_a_file_or_a_ranking_is_refused_before_any_job(roi, body):
    response = roi.http.post(roi.endpoint, json={"format": "osirix_roi", **body})
    assert response.status_code == 400
    assert not roi.http.get(roi.endpoint).json()["exports"] and not roi.archive.uploads


def test_an_account_without_the_archive_is_refused(roi, monkeypatch):
    monkeypatch.setattr(identity, "current", lambda: replace(identity.LOCAL, archive_access=False))
    refused = roi.http.post(roi.endpoint, json={"format": "osirix_roi"})
    assert refused.status_code == 403 and refused.json() == {"detail": "This account cannot use the archive"}
    assert roi.http.post(roi.endpoint, json={"format": "osirix_roi", "delivery": "file"}).status_code == 400


def test_one_history_lists_both_formats_newest_first_and_keeps_seg_keys(roi):
    seg = execute(roi, roi.http.post(roi.endpoint, json={}).json())
    rois = execute(roi, post(roi))
    available = roi.http.get(roi.endpoint).json()
    assert available["available"] is True and available["encoding_version"] == 4 and available["osirix_roi"] == {"encoding_version": 1}
    assert [(item["id"], item["kind"]) for item in available["exports"]] == [(rois["id"], "orthanc_roi_export"), (seg["id"], "orthanc_export")]
    # A SEG keeps the key it had before ROIs existed, and the two formats share no artifact.
    payload = seg["payload"]
    identity_fields = {key: payload[key] for key in ("revision_id", "reference_image_id", "source_manifest_sha256", "structures")}
    key = hashlib.sha256(json.dumps({**identity_fields, "encoding_version": 4}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert payload["artifact_key"] == key and rois["payload"]["artifact_key"] != key
    assert rois["payload"]["input_key"] == payload["input_key"] and rois["payload"]["export_id"] != payload["export_id"]
    assert len(roi.calls) == len(roi.encoded) == 1


def test_a_misfiled_upload_stops_before_any_send(roi):
    roi.archive.parent_study = oid(77)
    failed = execute(roi, post(roi, destination="osirix"))
    assert failed["status"] == "failed" and failed["stage"] == "Export failed" and "outside the original study" in failed["error"]
    assert len(roi.archive.uploads) == 1 and not roi.archive.sends
    assert failed["result"]["osirix_roi"]["archived"] is False


def test_a_cancel_during_archiving_stores_every_report_and_sends_nothing(roi):
    queued = post(roi, destination="osirix")
    roi.archive.on_upload = lambda: roi.service.cancel_job(queued["id"])
    cancelled = execute(roi, queued)
    assert cancelled["status"] == "cancelled" and cancelled["result"]["osirix_roi"]["archived"] is True
    assert len(roi.archive.uploads) == 3 and not roi.archive.sends


def test_a_failure_partway_through_archiving_says_how_many_reports_were_stored(roi):
    roi.archive.on_upload = lambda: setattr(roi.archive, "upload_error", OrthancError("synthetic") if len(roi.archive.uploads) == 2 else None)
    failed = execute(roi, post(roi, destination="osirix"))
    assert failed["status"] == "failed" and failed["error"].startswith("Only 1 of 3 OsiriX ROI reports reached Orthanc")
    assert failed["result"]["osirix_roi"]["archived"] is False and not roi.archive.sends
    roi.archive.on_upload = None
    retried = execute(roi, post(roi, destination="osirix"))
    assert retried["status"] == "completed" and retried["result"]["osirix_roi"]["sent"] and len(roi.archive.blobs) == 3


def test_masks_that_share_a_name_are_refused_before_any_job(roi):
    second = roi.revision["structures"][1]
    second["name"] = roi.revision["structures"][0]["name"]
    roi.service.store.save("revisions", roi.revision)
    refused = roi.http.post(roi.endpoint, json={"format": "osirix_roi"})
    assert refused.status_code == 400 and "different names" in refused.json()["detail"]
    assert not roi.http.get(roi.endpoint).json()["exports"]


def test_restart_interrupts_a_queued_roi_export_and_seg_recovery_leaves_it_alone(roi):
    queued = post(roi, destination="osirix")
    roi.service.orthanc_exports.recover()
    assert roi.service.store.get("jobs", queued["id"])["status"] == "queued"
    roi.service.osirix_roi_exports.recover()
    recovered = roi.service.store.get("jobs", queued["id"])
    assert recovered["status"] == "failed" and recovered["stage"] == "Interrupted" and recovered["error"] == roi.service.osirix_roi_exports.interrupted_error
    assert roi.service.store.claim("cpu") is None and not roi.archive.uploads and not roi.archive.sends
