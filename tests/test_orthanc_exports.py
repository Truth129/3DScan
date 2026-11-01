"""Synthetic saved-mask exports, exact artifact reuse and delivery uncertainty."""

from concurrent.futures import ThreadPoolExecutor
import copy
from dataclasses import dataclass, replace
import hashlib
import importlib
from io import BytesIO
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest
import pydicom

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.orthanc import OrthancDeliveryUnknown, OrthancError
from scan3d.orthanc_exports import ExportUnavailable
from scan3d.service import Service
from scan3d.store import identifier, job_lane, public_job
from test_orthanc_imports import SyntheticArchive, SERIES, SERIES_UID, STUDY_UID, FRAME_UID, oid


@dataclass(frozen=True, slots=True)
class SegmentInput:
    id: str
    name: str
    color: str
    mask_path: Path


class DicomSegError(ValueError):
    pass


class ExportArchive(SyntheticArchive):
    def __init__(self):
        super().__init__()
        self.source_key = self.snapshot_doc["source_key"]
        self.uploads, self.blobs, self.sends, self.polls = [], {}, [], []
        self.upload_error = self.start_error = self.poll_error = None
        self.on_upload = self.on_start = self.on_poll = None
        self.remote_state, self.failed_count, self.delivered_count = "Success", 0, None

    def destinations(self, allowlist):
        return [{"id": "osirix", "name": "OsiriX", "aet": "SYNTHETIC_OSIRIX"}] if "osirix" in allowlist else []

    def upload_instance(self, path, *, expected_sha256):
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        assert digest == expected_sha256
        if digest not in self.blobs:
            self.blobs[digest] = (oid(len(self.blobs) + 100), data)
        resource = self.blobs[digest][0]
        self.uploads.append((resource, digest))
        if self.on_upload:
            self.on_upload()
        if self.upload_error:
            exc, self.upload_error = self.upload_error, None
            raise exc
        return {"id": resource, "status": "Success", "sha256": digest, "size": len(data)}

    def start_store(self, ids, destination):
        remote_id = str(uuid.uuid4())
        self.sends.append((remote_id, tuple(ids), destination))
        if self.on_start:
            self.on_start()
        if self.start_error:
            exc, self.start_error = self.start_error, None
            raise exc
        return remote_id

    def store_job(self, remote_id):
        self.polls.append(remote_id)
        if self.on_poll:
            self.on_poll()
        if self.poll_error:
            exc, self.poll_error = self.poll_error, None
            raise exc
        count = len(next(item[1] for item in self.sends if item[0] == remote_id))
        return {
            "id": remote_id,
            "state": self.remote_state,
            "instances_count": self.delivered_count if self.delivered_count is not None else count,
            "failed_instances_count": self.failed_count,
        }


@pytest.fixture
def rig(tmp_path, monkeypatch):
    archive = ExportArchive()
    settings = Settings(
        tmp_path / "data",
        (tmp_path,),
        web_dir=tmp_path / "dist",
        modalities=("CT",),
        orthanc_url="https://synthetic.example.test",
        orthanc_username="private-user",
        orthanc_password="private-password",
        orthanc_export_destinations=("osirix",),
    )
    app = create_app(settings)
    service = app.state.service
    monkeypatch.setattr(service.orthanc, "client", lambda: archive)
    imported = service.orthanc.start(SERIES, archive.snapshot_doc["snapshot"])
    service.execute(service.store.claim("cpu"))
    case = service.store.get("cases", imported["case_id"])
    directory = service.store.case_dir(case["id"])
    reference = directory / "reference.nii.gz"
    affine = np.diag([-0.7, -0.8, 1.2, 1])
    nib.save(nib.Nifti1Image(np.zeros((8, 6, 3), dtype=np.int16), affine), reference)
    source = case["import_source"]
    image_source = {key: source[key] for key in ("series_uid", "study_uid", "frame_of_reference_uid")}
    image_source.update(
        source_archive_key=source["source_key"], source_content_sha256=source["content_digest"], source_manifest_sha256=source["manifest_sha256"]
    )
    base = service.build_ct_revision(case["id"], reference, acquisition={"series_uid": SERIES_UID}, image_source=image_source)
    service.store.commit_revision(base)
    saved = copy.deepcopy(base)
    saved.update(id=identifier(), parent_id=base["id"], source="totalsegmentator", kind="segmentation", structures=[])
    for index in range(2):
        mask_path = directory / f"saved-mask-{index}.nii.gz"
        values = np.zeros((8, 6, 3), dtype=np.uint8)
        values[1 + index : 4, 2:5, 1] = 1
        nib.save(nib.Nifti1Image(values, affine), mask_path)
        saved["structures"].append(
            {
                "id": f"test:{index}",
                "name": f"Synthetic mask {index}",
                "color": ["#ff0000", "#00ffff"][index],
                "mask_file": str(mask_path),
                "voxel_count": int(values.sum()),
                "reference_image_id": base["reference_image_id"],
            }
        )
    service.store.save("revisions", saved)
    calls = []

    def encode(source_paths, reference_path, segments, output_dir, *, revision_id, source_kind, cancelled, progress, overlap_priority=None):
        assert all(Path(path).is_file() for path in source_paths)
        assert not output_dir.exists()
        output_dir.mkdir(parents=True)
        calls.append(tuple(segments))
        encoded_segments = []
        sop_uid = f"2.25.{uuid.uuid4().int}"
        chunks = [f"synthetic multi-segment SEG {sop_uid}"]
        ranks = {key: rank for rank, key in enumerate(overlap_priority or sorted(item.id for item in segments))}
        for index, segment in enumerate(sorted(segments, key=lambda item: ranks[item.id]), 1):
            if cancelled():
                raise InterruptedError("Synthetic encoding cancelled")
            mask_digest = hashlib.sha256(segment.mask_path.read_bytes()).hexdigest()
            chunks.append(f"{index} {segment.id} {mask_digest}")
            encoded_segments.append(
                SimpleNamespace(
                    structure_id=segment.id,
                    segment_number=index,
                    voxel_count=int(np.asanyarray(nib.load(segment.mask_path).dataobj).sum()),
                    mask_sha256=mask_digest,
                    removed_voxel_count=0,
                )
            )
            progress(index, len(segments))
        data = "\n".join(chunks).encode()
        path = output_dir / "segment-0001.dcm"
        path.write_bytes(data)
        instance = SimpleNamespace(
            path=path, sop_instance_uid=sop_uid, sha256=hashlib.sha256(data).hexdigest(), size=len(data), segments=tuple(encoded_segments)
        )
        return SimpleNamespace(
            revision_id=revision_id,
            series_instance_uid=f"2.25.{uuid.uuid4().int}",
            study_instance_uid=STUDY_UID,
            frame_of_reference_uid=FRAME_UID,
            reference_sha256=hashlib.sha256(reference_path.read_bytes()).hexdigest(),
            files=(instance,),
        )

    monkeypatch.setitem(sys.modules, "scan3d.dicom_seg", SimpleNamespace(SegmentInput=SegmentInput, export_segmentation=encode, DicomSegError=DicomSegError))
    http = TestClient(app, base_url="http://127.0.0.1")
    result = SimpleNamespace(http=http, service=service, archive=archive, case=case, revision=saved, base=base, settings=settings, calls=calls, root=tmp_path)
    result.endpoint = f"/api/cases/{case['id']}/revisions/{saved['id']}/dicom-seg"
    yield result
    http.close()


def execute(rig, job):
    assert job_lane(job) == "cpu"
    assert rig.service.store.claim("gpu") is None
    claimed = rig.service.store.claim("cpu")
    assert claimed["id"] == job["id"]
    rig.service.execute(claimed)
    return rig.service.store.get("jobs", job["id"])


def start(rig, *, destination=None, structures=None, resend=False, overlap_priority=None):
    body = {"destination": destination, "resend": resend}
    if structures is not None:
        body["structure_ids"] = structures
    if overlap_priority is not None:
        body["overlap_priority"] = overlap_priority
    response = rig.http.post(rig.endpoint, json=body)
    assert response.status_code == 200, response.text
    return response.json()


def artifact(rig, job):
    private = rig.service.store.get("jobs", job["id"])
    directory = rig.service.orthanc_exports._directory(rig.case["id"], private["payload"]["export_id"])
    return directory, json.loads((directory / "export.json").read_text())


def test_archive_only_freezes_saved_inactive_revision_without_creating_or_activating_one(rig):
    before = rig.service.store.list("revisions", rig.case["id"])
    available = rig.http.get(rig.endpoint)
    assert available.headers["cache-control"] == "no-store"
    assert available.json() == {
        "available": True,
        "destinations": [{"id": "osirix", "name": "OsiriX", "aet": "SYNTHETIC_OSIRIX"}],
        "exports": [],
        "encoding_version": 3,
        "file": {"available": True},
    }
    job = start(rig)
    assert job["kind"] == "orthanc_export" and job["revision_id"] == rig.revision["id"]
    assert "payload" not in job
    finished = execute(rig, job)
    assert finished["status"] == "completed"
    result = finished["result"]["dicom_seg"]
    assert result == {
        "export_id": finished["payload"]["export_id"],
        "encoding_version": 3,
        "archived": True,
        "sent": False,
        "segments_count": 2,
        "instance_count": 1,
        "structure_ids": ["test:0", "test:1"],
    }
    directory, manifest = artifact(rig, job)
    assert manifest["revision_id"] == rig.revision["id"]
    assert manifest["source_manifest_sha256"] == rig.case["import_source"]["manifest_sha256"]
    assert manifest["source_snapshot"] == rig.case["import_source"]["snapshot"]
    assert len({item["sop_instance_uid"] for item in manifest["files"]}) == 1
    assert [segment["segment_number"] for segment in manifest["files"][0]["segments"]] == [1, 2]
    assert hashlib.sha256((directory / "export.json").read_bytes()).hexdigest() == finished["payload"]["artifact_manifest_sha256"]
    assert len(rig.calls) == 1 and len(rig.archive.uploads) == 1 and not rig.archive.sends
    assert rig.service.store.list("revisions", rig.case["id"]) == before
    assert rig.service.store.get("cases", rig.case["id"])["active_revision_id"] == rig.base["id"]
    history = rig.http.get(rig.endpoint).json()["exports"]
    encoded = json.dumps(history)
    assert history[0]["result"] == finished["result"] and history[0]["revision_id"] == rig.revision["id"]
    assert str(rig.root) not in encoded and "private-password" not in encoded and "private-user" not in encoded


def test_file_delivery_of_an_orthanc_case_shares_its_artifact_and_touches_no_archive(rig):
    archived = execute(rig, start(rig))
    uploads = list(rig.archive.uploads)
    prepared = rig.http.post(rig.endpoint, json={"delivery": "file"}).json()
    assert prepared["id"] != archived["id"]
    finished = execute(rig, prepared)
    assert finished["status"] == "completed" and finished["payload"]["export_id"] == archived["payload"]["export_id"]
    assert len(rig.calls) == 1 and rig.archive.uploads == uploads and not rig.archive.sends
    assert finished["result"]["dicom_seg"]["file"] is True and finished["result"]["dicom_seg"]["archived"] is False
    served = rig.http.get(f"{rig.endpoint}/{finished['payload']['export_id']}/segment.dcm")
    directory, _ = artifact(rig, finished)
    assert served.status_code == 200 and served.content == (directory / "dicom" / "segment-0001.dcm").read_bytes()


def test_a_prepared_file_reuses_its_artifact_for_a_later_archive(rig):
    prepared = execute(rig, rig.http.post(rig.endpoint, json={"delivery": "file"}).json())
    assert not rig.archive.uploads
    archived = execute(rig, start(rig))
    assert archived["result"]["dicom_seg"]["archived"] is True
    assert archived["payload"]["export_id"] == prepared["payload"]["export_id"] and len(rig.calls) == 1


def test_an_archive_job_alone_serves_no_file(rig):
    archived = execute(rig, start(rig))
    assert rig.http.get(f"{rig.endpoint}/{archived['payload']['export_id']}/segment.dcm").status_code == 404


def test_an_orthanc_case_still_saves_a_file_when_its_archive_is_unreachable(rig, monkeypatch):
    def unreachable():
        raise OrthancError("Synthetic archive offline")

    monkeypatch.setattr(rig.service.orthanc, "client", unreachable)
    available = rig.http.get(rig.endpoint).json()
    assert available["available"] is False and available["file"] == {"available": True}
    finished = execute(rig, rig.http.post(rig.endpoint, json={"delivery": "file"}).json())
    assert finished["status"] == "completed" and finished["result"]["dicom_seg"]["file"] is True


def test_an_orthanc_case_still_saves_a_file_without_a_configured_archive(rig, monkeypatch):
    monkeypatch.setattr(rig.service, "settings", replace(rig.settings, orthanc_url=None))
    available = rig.http.get(rig.endpoint).json()
    assert available["available"] is False and "Configure Orthanc" in available["reason"] and available["file"] == {"available": True}
    finished = execute(rig, rig.http.post(rig.endpoint, json={"delivery": "file"}).json())
    assert finished["status"] == "completed" and not rig.archive.uploads


def test_changed_orthanc_source_bytes_stop_a_file_job(rig):
    prepared = rig.http.post(rig.endpoint, json={"delivery": "file"}).json()
    source = Path(rig.case["source_dir"])
    victim = next(iter(sorted(source.iterdir())))
    victim.write_bytes(victim.read_bytes() + b"\0")
    failed = execute(rig, prepared)
    assert failed["status"] == "failed" and not rig.calls


def test_configured_send_deduplicates_jobs_and_reuses_archive_artifact(rig):
    archived = execute(rig, start(rig, structures=["test:1"]))
    sending = start(rig, destination="osirix", structures=["test:1"])
    assert start(rig, destination="osirix", structures=["test:1"])["id"] == sending["id"]
    finished = execute(rig, sending)
    assert finished["status"] == "completed"
    assert finished["result"]["dicom_seg"]["sent"] is True
    assert finished["result"]["dicom_seg"]["destination"] == "OsiriX"
    assert finished["result"]["dicom_seg"]["export_id"] == archived["result"]["dicom_seg"]["export_id"]
    assert len(rig.calls) == 1 and len(rig.archive.sends) == 1
    assert start(rig, destination="osirix", structures=["test:1"])["id"] == finished["id"]


def eight_mask_revision(rig):
    template = rig.revision["structures"][0]
    image = nib.load(template["mask_file"])
    for index in range(2, 8):
        mask_path = Path(template["mask_file"]).with_name(f"saved-mask-{index}.nii.gz")
        values = np.zeros(image.shape, dtype=np.uint8)
        values[index, 1:4, 1] = 1
        nib.save(nib.Nifti1Image(values, image.affine), mask_path)
        rig.revision["structures"].append(
            {**template, "id": f"test:{index}", "name": f"Synthetic mask {index}", "mask_file": str(mask_path), "voxel_count": int(values.sum())}
        )
    rig.service.store.save("revisions", rig.revision)


def test_eight_selected_masks_archive_and_send_as_one_instance(rig):
    eight_mask_revision(rig)
    queued = start(rig, destination="osirix")
    assert queued["result"]["dicom_seg"]["segments_count"] == 8
    assert queued["result"]["dicom_seg"]["instance_count"] == 1
    finished = execute(rig, queued)
    assert finished["status"] == "completed", finished["error"]
    result = finished["result"]["dicom_seg"]
    assert result["sent"] and result["archived"] and result["segments_count"] == 8 and result["instance_count"] == 1
    assert result["structure_ids"] == [f"test:{index}" for index in range(8)]
    directory, manifest = artifact(rig, finished)
    assert len(manifest["files"]) == len(rig.archive.blobs) == len(rig.archive.uploads) == len(rig.archive.sends) == 1
    assert [item["segment_number"] for item in manifest["files"][0]["segments"]] == list(range(1, 9))
    receipt = json.loads((directory / "delivery.json").read_text())
    assert list(receipt["uploaded"]) == [manifest["files"][0]["path"]]
    assert len(rig.archive.sends[0][1]) == 1
    assert len(rig.calls) == 1


@pytest.mark.parametrize("problem", ["duplicate_segment_number", "missing_mask", "multiple_instances"])
def test_incomplete_or_ambiguous_combined_encoding_never_uploads(rig, monkeypatch, problem):
    module = sys.modules["scan3d.dicom_seg"]
    encode = module.export_segmentation

    def invalid(*args, **kwargs):
        result = encode(*args, **kwargs)
        if problem == "duplicate_segment_number":
            result.files[0].segments[1].segment_number = 1
        elif problem == "missing_mask":
            result.files[0].segments = result.files[0].segments[:1]
        else:
            result.files = result.files * 2
        return result

    monkeypatch.setattr(module, "export_segmentation", invalid)
    failed = execute(rig, start(rig, destination="osirix"))
    assert failed["status"] == "failed" and "frozen source" in failed["error"]
    assert not rig.archive.uploads and not rig.archive.sends


def test_combined_receipt_rejects_foreign_instance_keys_before_upload_or_delivery(rig):
    rig.archive.poll_error = OrthancError("Synthetic poll interruption")
    first = execute(rig, start(rig, destination="osirix"))
    directory, manifest = artifact(rig, first)
    receipt_path = directory / "delivery.json"
    receipt = json.loads(receipt_path.read_text())
    receipt["uploaded"]["unrelated-instance.dcm"] = oid(999)
    receipt_path.write_text(json.dumps(receipt))
    uploads, sends = len(rig.archive.uploads), len(rig.archive.sends)
    failed = execute(rig, start(rig, destination="osirix"))
    assert failed["status"] == "failed" and "receipt does not belong" in failed["error"]
    assert len(rig.archive.uploads) == uploads and len(rig.archive.sends) == sends


def legacy_export(rig, version=1):
    """Build historical per-structure artifacts without using live legacy code."""
    from scan3d.orthanc_exports import _ARTIFACT

    finished = execute(rig, start(rig, destination="osirix"))
    payload, result = finished["payload"], finished["result"]
    directory, manifest = artifact(rig, finished)
    identity = {key: payload[key] for key in ("revision_id", "reference_image_id", "source_manifest_sha256", "structures")}
    if version == 1:
        payload["artifact_key"] = payload.pop("input_key")
        payload.pop("encoding_version")
        manifest.pop("encoding_version")
        result["dicom_seg"].pop("encoding_version")
    else:
        payload["artifact_key"] = hashlib.sha256(
            json.dumps({**identity, "encoding_version": version}, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        payload["encoding_version"] = manifest["encoding_version"] = result["dicom_seg"]["encoding_version"] = version
    payload["export_key"] = hashlib.sha256((payload["artifact_key"] + ":osirix").encode()).hexdigest()
    manifest["artifact_key"] = payload["artifact_key"]
    segments = manifest["files"][0]["segments"]
    files, uploaded, resources = [], {}, []
    rig.archive.blobs.clear()
    rig.archive.uploads.clear()
    for index, segment in enumerate(segments, 1):
        sop_uid = f"2.25.{uuid.uuid4().int}"
        data = f"synthetic legacy SEG {segment['structure_id']} {segment['mask_sha256']} {sop_uid}".encode()
        path = directory / "dicom" / f"segment-{index:04d}.dcm"
        path.write_bytes(data)
        entry = {key: value for key, value in segment.items() if key != "segment_number"}
        entry.update(path=path.name, sop_instance_uid=sop_uid, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        files.append(entry)
        resource = rig.archive.upload_instance(path, expected_sha256=entry["sha256"])["id"]
        resources.append(resource)
        uploaded[segment["structure_id"]] = resource
    manifest["files"] = files
    (directory / "export.json").write_text(json.dumps(manifest))
    payload["artifact_manifest_sha256"] = hashlib.sha256((directory / "export.json").read_bytes()).hexdigest()
    receipt = json.loads((directory / "delivery.json").read_text())
    receipt.update(manifest_sha256=payload["artifact_manifest_sha256"], uploaded=uploaded)
    (directory / "delivery.json").write_text(json.dumps(receipt))
    remote_id, _, destination = rig.archive.sends[0]
    rig.archive.sends[0] = (remote_id, tuple(resources), destination)
    result["dicom_seg"]["instance_count"] = len(files)
    parsed = _ARTIFACT.validate_json((directory / "export.json").read_bytes())
    assert parsed.encoding_version == version and len(parsed.files) == len(segments)
    return rig.service.store.update_job(finished["id"], payload=payload, result=result)


@pytest.mark.parametrize("version", [1, 2])
def test_encoding_upgrade_requires_explicit_export_and_preserves_legacy_sops_and_receipts(rig, version):
    old = legacy_export(rig, version)
    old_directory, old_manifest = artifact(rig, old)
    old_bytes = {path.relative_to(old_directory): path.read_bytes() for path in old_directory.rglob("*") if path.is_file()}
    available = rig.http.get(rig.endpoint).json()
    assert available["encoding_version"] == 3
    assert available["exports"][0]["id"] == old["id"]
    assert available["exports"][0]["result"]["dicom_seg"].get("encoding_version", 1) == version
    rig.service.store.recover()
    rig.service.orthanc_exports.recover()
    assert len(rig.calls) == len(rig.archive.sends) == 1
    assert rig.http.post(rig.endpoint, json={"destination": "osirix", "resend": True}).status_code == 400
    fresh = start(rig, destination="osirix")
    assert fresh["result"]["dicom_seg"]["encoding_version"] == 3
    assert fresh["result"]["dicom_seg"]["export_id"] != old["payload"]["export_id"]
    assert len(rig.calls) == len(rig.archive.sends) == 1
    finished = execute(rig, fresh)
    _, new_manifest = artifact(rig, finished)
    assert finished["status"] == "completed" and finished["result"]["dicom_seg"]["encoding_version"] == 3
    assert new_manifest["encoding_version"] == 3
    assert len(new_manifest["files"]) == finished["result"]["dicom_seg"]["instance_count"] == 1
    assert {item["sop_instance_uid"] for item in old_manifest["files"]}.isdisjoint(item["sop_instance_uid"] for item in new_manifest["files"])
    assert old_bytes == {path.relative_to(old_directory): path.read_bytes() for path in old_directory.rglob("*") if path.is_file()}
    assert len(rig.calls) == len(rig.archive.sends) == 2
    assert start(rig, destination="osirix")["id"] == finished["id"]
    again = execute(rig, start(rig, destination="osirix", resend=True))
    assert again["result"]["dicom_seg"] == finished["result"]["dicom_seg"]
    assert len(rig.calls) == 2 and rig.archive.sends[-1][1:] == rig.archive.sends[-2][1:]


@pytest.mark.parametrize("version", [1, 2])
def test_encoding_upgrade_does_not_hide_a_changed_saved_mask(rig, version):
    legacy_export(rig, version)
    mask_path = Path(rig.revision["structures"][0]["mask_file"])
    image = nib.load(mask_path)
    values = np.asanyarray(image.dataobj).copy()
    values[0, 0, 0] = 1
    nib.save(nib.Nifti1Image(values, image.affine), mask_path)
    response = rig.http.post(rig.endpoint, json={"destination": "osirix"})
    assert response.status_code == 400 and "Saved masks changed" in response.json()["detail"]
    assert len(rig.calls) == len(rig.archive.sends) == 1


def test_newer_encoding_profile_reuses_input_identity_without_reusing_sops(rig, monkeypatch):
    import scan3d.orthanc_exports as exports

    old = execute(rig, start(rig))
    monkeypatch.setattr(exports, "EXPORT_ENCODING_VERSION", 4)
    finished = execute(rig, start(rig))
    assert finished["status"] == "completed" and finished["result"]["dicom_seg"]["encoding_version"] == 4
    assert finished["payload"]["input_key"] == old["payload"]["input_key"]
    assert finished["payload"]["artifact_key"] != old["payload"]["artifact_key"]
    assert finished["payload"]["export_id"] != old["payload"]["export_id"]
    assert len(rig.calls) == 2 and not rig.archive.sends


@pytest.mark.parametrize("version", [1, 2])
def test_queued_old_encoding_is_blocked_before_encoding_upload_or_delivery(rig, version):
    queued = start(rig, destination="osirix")
    payload = rig.service.store.get("jobs", queued["id"])["payload"]
    if version == 1:
        payload.pop("encoding_version")
    else:
        payload["encoding_version"] = version
    rig.service.store.update_job(queued["id"], payload=payload)
    failed = execute(rig, queued)
    assert failed["status"] == "failed" and "encoder was updated" in failed["error"]
    assert not rig.calls and not rig.archive.uploads and not rig.archive.sends


@pytest.mark.parametrize(
    "body,status",
    [
        ({"destination": "http://untrusted/"}, 422),
        ({"destination": "unlisted"}, 400),
        ({"structure_ids": []}, 422),
        ({"structure_ids": ["test:0", "test:0"]}, 400),
        ({"structure_ids": ["unknown"]}, 400),
        ({"structure_ids": [True]}, 422),
        ({"overlap_priority": []}, 422),
        ({"overlap_priority": ["test:0"]}, 422),
        ({"overlap_priority": ["test:0", True]}, 422),
        ({"overlap_priority": ["test:0", "test:0"]}, 400),
        ({"overlap_priority": ["test:0", "unknown"]}, 400),
        ({"structure_ids": ["test:0"], "overlap_priority": ["test:0", "test:1"]}, 400),
    ],
)
def test_export_rejects_unlisted_or_invalid_inputs_privately(rig, body, status):
    response = rig.http.post(rig.endpoint, json=body)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert not rig.archive.sends and not rig.archive.uploads and not rig.calls


def test_local_ct_and_empty_masks_have_clear_availability_reasons(rig):
    empty = copy.deepcopy(rig.revision)
    empty["structures"] = []
    rig.service.store.save("revisions", empty)
    assert "Select" in rig.http.get(rig.endpoint).json()["reason"]
    rig.service.store.save("revisions", rig.revision)
    case = rig.service.store.get("cases", rig.case["id"])
    case["references"] = {}
    rig.service.store.save("cases", case)
    response = rig.http.get(rig.endpoint)
    assert response.json()["available"] is False and "original Orthanc CT" in response.json()["reason"]
    assert rig.http.post(rig.endpoint, json={}).status_code == 400


def test_review_candidate_and_other_case_revision_are_never_saved_exports(rig):
    candidate = rig.endpoint.replace(rig.revision["id"], "review-" + identifier())
    assert rig.http.get(candidate).status_code == 400
    other = rig.service.new_case("Synthetic other")
    endpoint = rig.endpoint.replace(rig.case["id"], other["id"])
    assert rig.http.get(endpoint).status_code == 404
    assert rig.http.post(endpoint, json={}).status_code == 404


def test_partial_uncertain_upload_retry_reuses_exact_persisted_sop_bytes(rig):
    rig.archive.upload_error = OrthancError("private filename /patient/DICOM.dcm")
    failed = execute(rig, start(rig, destination="osirix"))
    directory, manifest = artifact(rig, failed)
    original = (directory / "export.json").read_bytes()
    assert failed["status"] == "failed" and "/patient" not in failed["error"]
    assert not rig.archive.sends and len(rig.archive.blobs) == 1
    retry = start(rig, destination="osirix")
    finished = execute(rig, retry)
    assert retry["id"] != failed["id"] and len(rig.calls) == 1
    assert finished["result"]["dicom_seg"]["sent"] is True
    assert (directory / "export.json").read_bytes() == original
    assert artifact(rig, retry)[1]["files"] == manifest["files"]
    assert len(rig.archive.blobs) == 1 and len(rig.archive.sends) == 1


@pytest.mark.parametrize("changed", ["mask", "seg", "manifest"])
def test_retry_blocks_changed_frozen_inputs_or_artifacts(rig, changed):
    rig.archive.upload_error = OrthancError("Synthetic interruption")
    first = execute(rig, start(rig, destination="osirix"))
    directory, manifest = artifact(rig, first)
    if changed == "mask":
        Path(rig.revision["structures"][0]["mask_file"]).write_bytes(b"changed saved mask")
    elif changed == "seg":
        (directory / "dicom" / manifest["files"][0]["path"]).write_bytes(b"changed SEG")
    else:
        manifest["files"][0]["segments"][0]["name"] = "changed name"
        (directory / "export.json").write_text(json.dumps(manifest))
    uploads = len(rig.archive.uploads)
    if changed == "mask":
        response = rig.http.post(rig.endpoint, json={"destination": "osirix"})
        assert response.status_code == 400 and "Saved masks changed" in response.json()["detail"]
        assert len(rig.archive.uploads) == uploads and not rig.archive.sends and len(rig.calls) == 1
        return
    failed = execute(rig, start(rig, destination="osirix"))
    assert failed["status"] == "failed"
    assert len(rig.archive.uploads) == uploads and not rig.archive.sends and len(rig.calls) == 1


@pytest.mark.parametrize("changed", ["original", "manifest", "reference", "saved-metadata"])
def test_queued_export_verifies_full_originals_and_saved_reference_before_encoding(rig, changed):
    job = start(rig, destination="osirix")
    source_dir = Path(rig.case["source_dir"])
    if changed == "original":
        path = next(source_dir.glob("*.dcm"))
        path.write_bytes(path.read_bytes() + b"changed source")
    elif changed == "manifest":
        path = source_dir.parent / "source-import.json"
        path.write_bytes(path.read_bytes() + b" ")
    elif changed == "reference":
        Path(rig.revision["ct_file"]).write_bytes(b"changed reference")
    else:
        saved = rig.service.store.revision(rig.case["id"], rig.revision["id"])
        saved["structures"][0]["name"] = "changed name"
        rig.service.store.save("revisions", saved)
    failed = execute(rig, job)
    assert failed["status"] == "failed"
    assert not rig.calls and not rig.archive.uploads and not rig.archive.sends
    assert str(rig.root) not in failed["error"]


def test_queued_export_refuses_valid_mask_rewritten_in_place_before_first_encode(rig):
    job = start(rig, destination="osirix")
    path = Path(rig.revision["structures"][0]["mask_file"])
    image = nib.load(path)
    values = np.asanyarray(image.dataobj).copy()
    before = int(values.sum())
    values[tuple(np.argwhere(values != 0)[0])] = 0
    values[0, 0, 0] = 1
    nib.save(nib.Nifti1Image(values, image.affine), path)
    rewritten = nib.load(path)
    assert rewritten.shape == image.shape and np.array_equal(rewritten.affine, image.affine)
    assert int(np.asanyarray(rewritten.dataobj).sum()) == before
    failed = execute(rig, job)
    assert failed["status"] == "failed" and "changed after this export was queued" in failed["error"]
    assert not rig.calls and not rig.archive.uploads and not rig.archive.sends


def test_cancel_after_last_upload_never_sends_and_explicit_retry_reuses_artifact(rig):
    job = start(rig, destination="osirix")
    rig.archive.on_upload = lambda: rig.service.store.update_job(job["id"], cancel_requested=True) if len(rig.archive.uploads) == 1 else None
    cancelled = execute(rig, job)
    assert cancelled["status"] == "cancelled" and not rig.archive.sends
    assert cancelled["result"]["dicom_seg"]["archived"] is True
    rig.archive.on_upload = None
    finished = execute(rig, start(rig, destination="osirix"))
    assert finished["result"]["dicom_seg"]["sent"] is True and len(rig.calls) == 1


def test_cancellation_after_remote_start_observes_success_instead_of_claiming_cancelled_delivery(rig):
    job = start(rig, destination="osirix")
    rig.archive.on_start = lambda: rig.service.store.update_job(job["id"], cancel_requested=True)
    finished = execute(rig, job)
    assert finished["status"] == "completed" and finished["cancel_requested"]
    assert finished["result"]["dicom_seg"]["sent"] is True


def test_the_public_job_says_once_cancelling_can_no_longer_stop_a_send(rig):
    job = start(rig, destination="osirix")
    assert "delivery_started" not in job
    seen = []
    rig.archive.on_upload = lambda: seen.append(public_job(rig.service.store.get("jobs", job["id"])))
    rig.archive.on_start = lambda: seen.append(public_job(rig.service.store.get("jobs", job["id"])))
    finished = execute(rig, job)
    assert ["delivery_started" in item for item in seen] == [False, True]
    assert public_job(finished)["delivery_started"] is True


def test_an_archive_never_reports_a_send_started(rig):
    assert "delivery_started" not in public_job(execute(rig, start(rig)))


def test_known_remote_job_survives_restart_and_retry_polls_it_without_resending(rig):
    rig.archive.poll_error = OrthancError("Synthetic lost poll")
    failed = execute(rig, start(rig, destination="osirix"))
    remote_id = rig.archive.sends[0][0]
    assert failed["status"] == "failed" and "not confirmed" in failed["error"]
    rig.service.store.update_job(failed["id"], status="running")
    rig.service.store.recover()
    rig.service.orthanc_exports.recover()
    assert len(rig.archive.sends) == 1 and rig.archive.polls == [remote_id]
    finished = execute(rig, start(rig, destination="osirix"))
    assert finished["result"]["dicom_seg"]["sent"] is True
    assert len(rig.archive.sends) == 1 and rig.archive.polls == [remote_id, remote_id]
    assert len(rig.calls) == 1


def test_uncertain_send_requires_explicit_retry_with_same_sops_and_never_autoresends(rig):
    rig.archive.start_error = OrthancDeliveryUnknown("Synthetic lost start response")
    failed = execute(rig, start(rig, destination="osirix"))
    directory, manifest = artifact(rig, failed)
    assert failed["status"] == "failed" and "Check the destination" in failed["error"]
    assert failed["result"]["dicom_seg"]["archived"] is True and failed["result"]["dicom_seg"]["sent"] is False
    receipt = json.loads((directory / "delivery.json").read_text())
    assert receipt["destinations"]["osirix"] == {"state": "unknown", "job_id": None, "attempt_id": failed["payload"]["delivery_attempt_id"]}
    rig.service.store.recover()
    rig.service.orthanc_exports.recover()
    assert len(rig.archive.sends) == 1 and not rig.archive.polls
    finished = execute(rig, start(rig, destination="osirix"))
    assert finished["result"]["dicom_seg"]["sent"] is True
    assert len(rig.archive.sends) == 2 and rig.archive.sends[0][1:] == rig.archive.sends[1][1:]
    assert artifact(rig, finished)[1]["files"] == manifest["files"] and len(rig.calls) == 1


@pytest.mark.parametrize("state,count,failed_count", [("Failure", 1, 1), ("Success", 0, 0), ("Success", 2, 0), ("Success", 1, 1)])
def test_delivery_requires_exact_successful_instance_count(rig, state, count, failed_count):
    rig.archive.remote_state, rig.archive.delivered_count, rig.archive.failed_count = state, count, failed_count
    failed = execute(rig, start(rig, destination="osirix"))
    assert failed["status"] == "failed" and failed["result"]["dicom_seg"]["sent"] is False
    rig.archive.remote_state, rig.archive.delivered_count, rig.archive.failed_count = "Success", None, 0
    finished = execute(rig, start(rig, destination="osirix"))
    assert finished["result"]["dicom_seg"]["sent"] is True
    assert len(rig.archive.sends) == 2 and rig.archive.sends[0][1:] == rig.archive.sends[1][1:]


def test_simultaneous_requests_share_one_job(rig):
    def enqueue(_):
        return rig.service.orthanc_exports.start(rig.case["id"], rig.revision["id"], ["test:0"], "osirix")

    with ThreadPoolExecutor(max_workers=4) as pool:
        jobs = list(pool.map(enqueue, range(8)))
    assert len({job["id"] for job in jobs}) == 1
    assert len(rig.service.orthanc_exports.availability(rig.case["id"], rig.revision["id"])["exports"]) == 1


def test_export_destination_env_defaults_to_no_outbound_targets(monkeypatch):
    monkeypatch.delenv("SCAN3D_ORTHANC_EXPORT_DESTINATIONS", raising=False)
    assert Settings.from_env().orthanc_export_destinations == ()
    monkeypatch.setenv("SCAN3D_ORTHANC_EXPORT_DESTINATIONS", "osirix, other,osirix")
    assert Settings.from_env().orthanc_export_destinations == ("osirix", "other")
    monkeypatch.setenv("SCAN3D_ORTHANC_EXPORT_DESTINATIONS", "http://elsewhere")
    with pytest.raises(ValueError, match="configured Orthanc modality"):
        Settings.from_env()


def test_explicit_send_again_reuses_sops_but_makes_one_new_delivery_attempt(rig):
    first = execute(rig, start(rig, destination="osirix"))
    assert first["result"]["dicom_seg"]["destination_id"] == "osirix"
    again = start(rig, destination="osirix", resend=True)
    assert again["id"] != first["id"]
    assert start(rig, destination="osirix", resend=True)["id"] == again["id"]
    finished = execute(rig, again)
    assert finished["result"]["dicom_seg"] == first["result"]["dicom_seg"]
    assert finished["payload"]["delivery_attempt_id"] != first["payload"]["delivery_attempt_id"]
    assert finished["payload"]["orthanc_job_id"] != first["payload"]["orthanc_job_id"]
    assert len(rig.archive.sends) == 2 and rig.archive.sends[0][1:] == rig.archive.sends[1][1:]
    assert len(rig.calls) == 1
    assert start(rig, destination="osirix")["id"] == finished["id"]


def test_send_again_refuses_archive_only_or_never_confirmed_selections(rig):
    assert rig.http.post(rig.endpoint, json={"resend": True}).status_code == 400
    assert rig.http.post(rig.endpoint, json={"resend": True, "destination": "osirix"}).status_code == 400
    execute(rig, start(rig, destination="osirix", structures=["test:0"]))
    assert rig.http.post(rig.endpoint, json={"resend": True, "destination": "osirix", "structure_ids": ["test:1"]}).status_code == 400


def test_resend_interruption_after_remote_success_reuses_known_attempt_instead_of_sending_again(rig, monkeypatch):
    execute(rig, start(rig, destination="osirix"))
    again = start(rig, destination="osirix", resend=True)
    update = rig.service.store.update_job

    def interrupted_terminal(job_id, **changes):
        if job_id == again["id"] and changes.get("status") == "completed":
            raise OSError("Synthetic lost final database update")
        return update(job_id, **changes)

    monkeypatch.setattr(rig.service.store, "update_job", interrupted_terminal)
    failed = execute(rig, again)
    assert failed["status"] == "failed" and len(rig.archive.sends) == 2
    monkeypatch.setattr(rig.service.store, "update_job", update)
    finished = execute(rig, start(rig, destination="osirix"))
    assert finished["status"] == "completed" and finished["result"]["dicom_seg"]["sent"] is True
    assert finished["payload"]["delivery_attempt_id"] == failed["payload"]["delivery_attempt_id"]
    assert len(rig.archive.sends) == 2 and len(rig.calls) == 1


def test_pending_remote_delivery_has_bounded_wait_and_retry_polls_same_job(rig, monkeypatch):
    import scan3d.orthanc_exports as exports

    monkeypatch.setattr(exports, "DELIVERY_WAIT_SECONDS", 0)
    rig.archive.remote_state = "Paused"
    failed = execute(rig, start(rig, destination="osirix"))
    assert failed["status"] == "failed" and "not confirmed" in failed["error"]
    remote_id = rig.archive.sends[0][0]
    rig.archive.remote_state = "Success"
    finished = execute(rig, start(rig, destination="osirix"))
    assert finished["result"]["dicom_seg"]["sent"] is True
    assert rig.archive.polls == [remote_id, remote_id] and len(rig.archive.sends) == 1


def test_restart_never_executes_a_previously_queued_outbound_attempt_automatically(rig):
    queued = start(rig, destination="osirix")
    rig.service.store.recover()
    rig.service.orthanc_exports.recover()
    recovered = rig.service.store.get("jobs", queued["id"])
    assert recovered["status"] == "failed" and recovered["stage"] == "Interrupted"
    assert rig.service.store.claim("cpu") is None
    assert not rig.calls and not rig.archive.uploads and not rig.archive.sends
    retried = execute(rig, start(rig, destination="osirix"))
    assert retried["status"] == "completed" and len(rig.archive.sends) == 1


def test_cancel_queued_retry_never_claims_an_existing_remote_delivery_was_cancelled(rig):
    rig.archive.poll_error = OrthancError("Synthetic poll interruption")
    execute(rig, start(rig, destination="osirix"))
    retry = start(rig, destination="osirix")
    response = rig.http.post(f"/api/jobs/{retry['id']}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "failed" and "delivery may continue" in response.json()["stage"]
    assert rig.service.store.claim("cpu") is None and len(rig.archive.sends) == 1


def test_manifest_publication_failure_retains_orphan_bytes_and_next_queued_export_uses_new_directory(rig, monkeypatch):
    import scan3d.orthanc_exports as exports

    first = start(rig)
    second = start(rig, destination="osirix")
    write = exports.atomic_json

    def fail_manifest(path, value):
        if path.name == "export.json":
            raise OSError("Synthetic manifest write failure")
        return write(path, value)

    monkeypatch.setattr(exports, "atomic_json", fail_manifest)
    failed = execute(rig, first)
    old_directory = rig.service.orthanc_exports._directory(rig.case["id"], failed["payload"]["export_id"])
    old_blobs = {path.name: path.read_bytes() for path in (old_directory / "dicom").glob("*.dcm")}
    assert old_blobs and failed["status"] == "failed" and not rig.archive.uploads
    monkeypatch.setattr(exports, "atomic_json", write)
    finished = execute(rig, second)
    assert finished["status"] == "completed" and finished["payload"]["export_id"] != failed["payload"]["export_id"]
    assert old_blobs == {path.name: path.read_bytes() for path in (old_directory / "dicom").glob("*.dcm")}
    assert len(rig.calls) == 2 and len(rig.archive.sends) == 1


def test_manager_uses_actual_encoder_for_eight_exact_saved_masks_in_one_instance(rig, monkeypatch):
    import scan3d

    eight_mask_revision(rig)
    with monkeypatch.context() as encoder_context:
        # Import the real encoder for this job, then restore both registry and
        # package attributes so neighboring fake-encoder fixtures stay isolated.
        encoder_context.setattr(scan3d, "dicom_seg", None, raising=False)
        encoder_context.delitem(sys.modules, "scan3d.dicom_seg")
        importlib.import_module("scan3d.dicom_seg")
        finished = execute(rig, start(rig, destination="osirix"))
    assert finished["status"] == "completed", finished["error"]
    directory, manifest = artifact(rig, finished)
    assert len(manifest["files"]) == len(rig.archive.blobs) == len(rig.archive.uploads) == len(rig.archive.sends) == 1
    item = manifest["files"][0]
    blob = next(iter(rig.archive.blobs.values()))[1]
    assert blob == (directory / "dicom" / item["path"]).read_bytes()
    ds = pydicom.dcmread(BytesIO(blob))
    assert len(ds.SegmentSequence) == len(item["segments"]) == 8
    assert [int(segment.SegmentNumber) for segment in ds.SegmentSequence] == list(range(1, 9))
    assert ds.StudyInstanceUID == STUDY_UID and ds.FrameOfReferenceUID == FRAME_UID
    assert ds.SeriesInstanceUID == manifest["series_instance_uid"] and ds.SOPInstanceUID == item["sop_instance_uid"]
    refs = ds.ReferencedSeriesSequence[0]
    source_ids = [row["sop_instance_uid"] for row in rig.archive.snapshot_doc["instances"]]
    assert refs.SeriesInstanceUID == SERIES_UID
    assert {str(row.ReferencedSOPInstanceUID) for row in refs.ReferencedInstanceSequence} == set(source_ids)
    frame_bits = np.unpackbits(np.frombuffer(ds.PixelData, dtype=np.uint8), bitorder="little")[: int(ds.NumberOfFrames) * int(ds.Rows) * int(ds.Columns)]
    frames = frame_bits.reshape(int(ds.NumberOfFrames), int(ds.Rows), int(ds.Columns))
    decoded = np.zeros((8, 3, 6, 8), dtype=np.uint8)
    covered = set()
    for frame, group in zip(frames, ds.PerFrameFunctionalGroupsSequence, strict=True):
        number = int(group.SegmentIdentificationSequence[0].ReferencedSegmentNumber)
        source = str(group.DerivationImageSequence[0].SourceImageSequence[0].ReferencedSOPInstanceUID)
        plane = source_ids.index(source)
        assert (number, plane) not in covered
        covered.add((number, plane))
        decoded[number - 1, plane] = frame
    for index, selected in enumerate(rig.revision["structures"]):
        expected = np.asanyarray(nib.load(selected["mask_file"]).dataobj).transpose(2, 1, 0)
        np.testing.assert_array_equal(decoded[index], expected)
        description = ds.SegmentSequence[index]
        assert description.SegmentLabel == selected["name"] and description.TrackingID == selected["id"]
        assert description.SegmentAlgorithmType == "AUTOMATIC"
    result = finished["result"]["dicom_seg"]
    assert result["sent"] and result["archived"] and result["segments_count"] == 8 and result["instance_count"] == 1
    assert len(rig.archive.sends[0][1]) == 1 and not rig.calls
    assert start(rig, destination="osirix")["id"] == finished["id"]
    assert len(rig.archive.sends) == len(rig.archive.uploads) == 1


def test_each_ranking_is_its_own_artifact_of_the_same_saved_inputs(rig):
    plain = execute(rig, start(rig))
    ranked = start(rig, overlap_priority=["test:1", "test:0"])
    assert ranked["result"]["dicom_seg"]["overlap_priority"] == ["test:1", "test:0"]
    assert start(rig, overlap_priority=["test:1", "test:0"])["id"] == ranked["id"]
    ranked = execute(rig, ranked)
    reversed_rank = execute(rig, start(rig, overlap_priority=["test:0", "test:1"]))
    assert len({job["result"]["dicom_seg"]["export_id"] for job in (plain, ranked, reversed_rank)}) == 3
    assert len(rig.calls) == 3
    payloads = [rig.service.store.get("jobs", job["id"])["payload"] for job in (plain, ranked, reversed_rank)]
    assert len({item["selection_key"] for item in payloads}) == len({item["input_key"] for item in payloads}) == 1
    assert len({item["artifact_key"] for item in payloads}) == 3
    manifest = artifact(rig, ranked)[1]
    assert manifest["overlap_priority"] == ["test:1", "test:0"]
    assert [item["structure_id"] for item in manifest["files"][0]["segments"]] == ["test:1", "test:0"]
    assert ranked["result"]["dicom_seg"]["structure_ids"] == ["test:1", "test:0"]
    assert ranked["result"]["dicom_seg"]["overlap_priority"] == ["test:1", "test:0"]
    assert ranked["result"]["dicom_seg"]["removed_voxels"] == {"test:1": 0, "test:0": 0}


def test_unranked_exports_keep_their_exact_manifest_and_result_fields(rig):
    plain = execute(rig, start(rig))
    manifest = artifact(rig, plain)[1]
    assert "overlap_priority" not in manifest
    assert all("removed_voxel_count" not in item for item in manifest["files"][0]["segments"])
    assert "overlap_priority" not in plain["result"]["dicom_seg"] and "removed_voxels" not in plain["result"]["dicom_seg"]


def test_ranked_send_reuses_its_archive_and_send_again_needs_the_exact_ranking(rig):
    ranking = ["test:1", "test:0"]
    archived = execute(rig, start(rig, overlap_priority=ranking))
    sent = execute(rig, start(rig, destination="osirix", overlap_priority=ranking))
    assert sent["result"]["dicom_seg"]["export_id"] == archived["result"]["dicom_seg"]["export_id"]
    assert len(rig.calls) == 1 and len(rig.archive.sends) == 1
    for body in ({"destination": "osirix", "resend": True}, {"destination": "osirix", "resend": True, "overlap_priority": ranking[::-1]}):
        response = rig.http.post(rig.endpoint, json=body)
        assert response.status_code == 400 and "Send again" in response.json()["detail"]
    again = execute(rig, start(rig, destination="osirix", resend=True, overlap_priority=ranking))
    assert again["id"] != sent["id"] and again["result"]["dicom_seg"]["export_id"] == archived["result"]["dicom_seg"]["export_id"]
    assert len(rig.calls) == 1 and len(rig.archive.sends) == 2


def test_changed_saved_mask_is_caught_between_plain_and_ranked_exports(rig):
    execute(rig, start(rig))
    Path(rig.revision["structures"][0]["mask_file"]).write_bytes(b"changed saved mask")
    response = rig.http.post(rig.endpoint, json={"overlap_priority": ["test:1", "test:0"]})
    assert response.status_code == 400 and "Saved masks changed" in response.json()["detail"]
    assert len(rig.calls) == 1


def test_cached_artifact_must_carry_its_ranking(rig):
    ranking = ["test:1", "test:0"]
    rig.archive.upload_error = OrthancError("Synthetic interruption")
    first = execute(rig, start(rig, overlap_priority=ranking))
    directory, manifest = artifact(rig, first)
    manifest["overlap_priority"] = ranking[::-1]
    (directory / "export.json").write_text(json.dumps(manifest))
    private = rig.service.store.get("jobs", first["id"])
    del private["payload"]["artifact_manifest_sha256"]
    rig.service.store.update_job(first["id"], payload=private["payload"])
    uploads = len(rig.archive.uploads)
    failed = execute(rig, start(rig, overlap_priority=ranking))
    assert failed["status"] == "failed" and "differs from its frozen source" in failed["error"]
    assert len(rig.archive.uploads) == uploads and len(rig.calls) == 1


def test_manager_ranks_real_masks_and_never_rewrites_saved_masks(rig, monkeypatch):
    import scan3d

    saved = {item["id"]: Path(item["mask_file"]).read_bytes() for item in rig.revision["structures"]}
    with monkeypatch.context() as encoder_context:
        encoder_context.setattr(scan3d, "dicom_seg", None, raising=False)
        encoder_context.delitem(sys.modules, "scan3d.dicom_seg")
        importlib.import_module("scan3d.dicom_seg")
        # test:1 lies wholly inside test:0, so ranking it second would empty it.
        refused = execute(rig, start(rig, overlap_priority=["test:0", "test:1"]))
        assert not rig.archive.uploads
        finished = execute(rig, start(rig, overlap_priority=["test:1", "test:0"]))
    assert refused["status"] == "failed" and refused["error"].startswith("Overlap rank 2 would lose every voxel")
    assert finished["status"] == "completed", finished["error"]
    result = finished["result"]["dicom_seg"]
    assert result["removed_voxels"] == {"test:1": 0, "test:0": 6} and result["structure_ids"] == ["test:1", "test:0"]
    directory, manifest = artifact(rig, finished)
    segments = manifest["files"][0]["segments"]
    assert [item["voxel_count"] for item in segments] == [6, 3]
    assert [item.get("removed_voxel_count", 0) for item in segments] == [0, 6]
    assert [item["mask_sha256"] for item in segments] == [hashlib.sha256(saved[key]).hexdigest() for key in ("test:1", "test:0")]
    assert {item["id"]: Path(item["mask_file"]).read_bytes() for item in rig.revision["structures"]} == saved
    ds = pydicom.dcmread(directory / "dicom" / manifest["files"][0]["path"], stop_before_pixels=True)
    assert ds.SegmentsOverlap == "NO" and ds.SeriesDescription.endswith("overlaps resolved")
    assert [item.TrackingID for item in ds.SegmentSequence] == ["test:1", "test:0"]
    assert len(rig.archive.uploads) == 1


@pytest.mark.parametrize("safe", [True, False])
def test_only_deliberately_safe_encoder_errors_reach_public_job(rig, monkeypatch, safe):
    module = sys.modules["scan3d.dicom_seg"]

    def fail(*args, **kwargs):
        raise DicomSegError("Original CT pixels do not match the saved reference grid") if safe else ValueError("private/patient/file")

    monkeypatch.setattr(module, "export_segmentation", fail)
    failed = execute(rig, start(rig))
    assert failed["status"] == "failed" and "private/patient" not in failed["error"]
    assert ("Original CT pixels" in failed["error"]) is safe
    assert not rig.archive.uploads
