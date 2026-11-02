"""Archive imports use actual synthetic DICOM and the existing CPU/API lifecycle."""

from concurrent.futures import ThreadPoolExecutor
import copy
import hashlib
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian
import pytest

from scan3d import dicom, identity
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.errors import NotFound
from scan3d.identity import EVERY_CASE
from scan3d.orthanc import OrthancChanged, OrthancError, snapshot_fingerprint
from scan3d.service import Service
from scan3d.orthanc_models import json_record, parse_download, parse_snapshot


def oid(value):
    return "-".join([f"{value:08x}"] * 5)


STUDY, SERIES = oid(1), oid(2)
STUDY_UID, SERIES_UID, FRAME_UID = "1.2.826.0.1.3680043.10.543.1", "1.2.826.0.1.3680043.10.543.2", "1.2.826.0.1.3680043.10.543.3"


def instance_bytes(index, *, study=STUDY_UID, series=SERIES_UID, frame=FRAME_UID):
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = f"1.2.826.0.1.3680043.10.543.10.{index + 1}"
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = meta.MediaStorageSOPClassUID, meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.FrameOfReferenceUID = study, series, frame
    ds.Modality, ds.SeriesDescription = "CT", "Synthetic Orthanc CT"
    ds.PatientName, ds.PatientID = "Synthetic^Orthanc", "SYNTHETIC-ORTHANC"
    ds.SeriesNumber, ds.InstanceNumber = 1, index + 1
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.ImageOrientationPatient, ds.ImagePositionPatient = [1, 0, 0, 0, 1, 0], [0, 0, index * 1.2]
    ds.Rows, ds.Columns, ds.PixelSpacing, ds.SliceThickness = 6, 8, [0.8, 0.7], 1.2
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit, ds.PixelRepresentation = 15, 0
    ds.RescaleSlope, ds.RescaleIntercept, ds.RescaleType = 1, -1024, "HU"
    ds.PixelData = (np.arange(48).reshape(6, 8) + 1000 + index * 100).astype("<u2").tobytes()
    ds["PixelData"].VR = "OW"
    output = io.BytesIO()
    ds.save_as(output, enforce_file_format=True)
    return output.getvalue()


class SyntheticArchive:
    def __init__(self):
        self.files = {oid(index + 3): instance_bytes(index) for index in range(3)}
        self.snapshot_doc = {
            "id": SERIES,
            "series_id": SERIES,
            "source_key": hashlib.sha256(b"synthetic archive").hexdigest(),
            "study_id": STUDY,
            "study_uid": STUDY_UID,
            "series_uid": SERIES_UID,
            "frame_of_reference_uid": FRAME_UID,
            "description": "Synthetic Orthanc CT",
            "modality": "CT",
            "instance_count": 3,
            "expected_instances": 3,
            "stable": True,
            "status": "Complete",
            "completeness": "complete",
            "warnings": [],
            "instances": [
                {
                    "id": resource_id,
                    "sop_instance_uid": f"1.2.826.0.1.3680043.10.543.10.{index + 1}",
                    "sop_class_uid": str(CTImageStorage),
                    "file_uuid": f"synthetic-file-{index}",
                    "size": len(data),
                }
                for index, (resource_id, data) in enumerate(self.files.items())
            ],
        }
        self.snapshot_doc["snapshot"] = "0" * 64
        self.snapshot_doc["snapshot"] = snapshot_fingerprint(parse_snapshot(self.snapshot_doc))
        self.error = None
        self.download_error = None
        self.manifest_mutation = None
        self.downloaded = 0
        self.entered = threading.Event()
        self.release = threading.Event()
        self.release.set()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def status(self):
        if self.error:
            raise self.error
        return {"name": "Synthetic archive", "version": "1.12.10"}

    def snapshot(self, series_id):
        if self.error:
            raise self.error
        assert series_id == SERIES
        return parse_snapshot(self.snapshot_doc)

    def find_studies(self, query, *, limit, offset):
        if self.error:
            raise self.error
        assert limit <= 100 and offset >= 0
        return [{"id": STUDY, "study_uid": STUDY_UID, "patient_id": "SYNTHETIC-ORTHANC", "study_date": "20261028", "series_count": 1}]

    def study_series(self, study_id):
        assert study_id == STUDY
        return {"study": {"id": STUDY, "study_uid": STUDY_UID}, "series": [json_record(self.snapshot(SERIES))]}

    def download(self, snapshot, destination, *, max_bytes, progress, cancelled):
        self.entered.set()
        while not self.release.wait(0.01):
            if cancelled():
                raise InterruptedError("Orthanc import cancelled")
        if self.download_error:
            raise self.download_error
        if cancelled():
            raise InterruptedError("Orthanc import cancelled")
        destination = Path(destination)
        files = []
        received = 0
        for index, item in enumerate(snapshot.instances):
            data = self.files[item.id]
            received += len(data)
            assert received <= max_bytes
            name = f"{index:06d}.dcm"
            (destination / name).write_bytes(data)
            files.append(
                {
                    "instance_id": item.id,
                    "sop_instance_uid": item.sop_instance_uid,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "size": len(data),
                    "path": name,
                }
            )
            progress(index + 1, len(snapshot.instances), received)
        self.downloaded += 1
        content = [{key: item[key] for key in ("sop_instance_uid", "sha256", "size")} for item in sorted(files, key=lambda item: item["sop_instance_uid"])]
        result = {
            "snapshot": json_record(snapshot),
            "files": files,
            "bytes": received,
            "content_digest": hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
        if self.manifest_mutation:
            self.manifest_mutation(result)
        return parse_download(result)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    archive = SyntheticArchive()
    settings = Settings(
        tmp_path / "data",
        (tmp_path,),
        web_dir=tmp_path / "dist",
        modalities=("CT",),
        orthanc_url="https://synthetic.example.test",
        orthanc_username="synthetic-user",
        orthanc_password="private-test-password",
    )
    app = create_app(settings)
    service = app.state.service
    monkeypatch.setattr(service.orthanc, "client", lambda: archive)
    # No lifespan worker: tests explicitly claim CPU jobs and never start a GPU lane.
    http = TestClient(app, base_url="http://127.0.0.1")
    yield SimpleNamespace(http=http, service=service, archive=archive, settings=settings, root=tmp_path)
    archive.release.set()
    http.close()


def start(rig, **overrides):
    return rig.http.post("/api/orthanc/imports", json={"series_id": SERIES, "snapshot": rig.archive.snapshot_doc["snapshot"], **overrides})


def execute_cpu(rig, job_id):
    assert rig.service.store.claim("gpu") is None
    job = rig.service.store.claim("cpu")
    assert job["id"] == job_id
    rig.service.execute(job)
    return rig.service.store.get("jobs", job_id)


def start_as(rig, user_id):
    identity.activate(identity.Principal(user_id, "name", "Name", "admin", True))
    try:
        return rig.service.orthanc.start(SERIES, rig.archive.snapshot_doc["snapshot"])
    finally:
        identity.activate(None)


def test_an_import_belongs_to_who_started_it_and_dedupe_grants_nobody_else(rig):
    def owners(case_id):
        with rig.service.store.connection() as con:
            return sorted(row["user_id"] for row in con.execute("SELECT user_id FROM case_access WHERE case_id=?", (case_id,)))

    first, second = "a" * 32, "b" * 32
    job = start_as(rig, first)
    assert job["requested_by"] == first and owners(job["case_id"]) == [first]
    # The second person gets the running job back, and must gain nothing by asking.
    assert start_as(rig, second)["id"] == job["id"] and owners(job["case_id"]) == [first]
    # A retry reuses the failed case, which still holds no scan. Its new job needs its starter to read it.
    rig.service.store.update_job(job["id"], status="failed")
    retry = start_as(rig, first)
    assert retry["case_id"] == job["case_id"] and owners(job["case_id"]) == [first]
    rig.service.store.update_job(retry["id"], status="failed")
    assert start_as(rig, second)["case_id"] == job["case_id"] and owners(job["case_id"]) == [first, second]


def test_queries_remain_private_and_modality_policy_applies_before_import(rig):
    status = rig.http.get("/api/orthanc/status")
    assert status.status_code == 200 and status.json()["connected"] is True
    assert status.headers["cache-control"] == "no-store"
    studies = rig.http.post("/api/orthanc/studies/query", json={"patient_id": "SYNTHETIC*"})
    assert studies.status_code == 200 and studies.json()["studies"][0]["id"] == STUDY
    assert studies.headers["cache-control"] == "no-store"
    series = rig.http.get(f"/api/orthanc/studies/{STUDY}/series")
    assert series.json()["series"][0]["supported"] is True
    rig.archive.snapshot_doc["modality"] = "MR"
    series = rig.http.get(f"/api/orthanc/studies/{STUDY}/series")
    assert series.json()["series"][0]["supported"] is False
    rejected = start(rig)
    assert rejected.status_code == 400 and "not enabled" in rejected.json()["detail"]
    assert rig.service.store.visible_cases(EVERY_CASE) == []


@pytest.mark.parametrize("error", [OrthancError("Synthetic unavailable"), NotFound("Synthetic wrong endpoint"), ValueError("Synthetic bad configuration")])
def test_connection_status_reports_unavailable_without_exposing_upstream_details(rig, error):
    rig.archive.error = error
    response = rig.http.get("/api/orthanc/status")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "configured": True,
        "connected": False,
        "name": "Orthanc",
        "error": "Cannot connect to the configured Orthanc archive.",
    }
    assert str(error) not in response.text


@pytest.mark.parametrize(
    ("method", "url", "body", "headers", "status"),
    [
        ("POST", "/api/orthanc/studies/query", {"patient_id": "SYNTHETIC-ORTHANC", "limit": 0}, {}, 422),
        ("POST", "/api/orthanc/studies/query", {"patient_id": "SYNTHETIC-ORTHANC"}, {}, 404),
        ("POST", "/api/orthanc/imports", {"series_id": "not-an-id", "snapshot": "a" * 64}, {}, 422),
        ("POST", "/api/orthanc/studies/query", {}, {"Origin": "https://unrelated.example.test"}, 403),
    ],
)
def test_archive_request_errors_are_not_cacheable(rig, method, url, body, headers, status):
    if status == 404:
        rig.archive.error = NotFound("Synthetic study no longer available")
    response = rig.http.request(method, url, json=body, headers=headers)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"


def test_concurrent_requests_and_completed_retries_reuse_one_job_and_case(rig):
    with ThreadPoolExecutor(max_workers=6) as pool:
        responses = list(pool.map(lambda _value: start(rig), range(12)))
    assert all(response.status_code == 200 for response in responses)
    identities = {response.json()["id"] for response in responses}
    assert len(identities) == 1
    assert len(rig.service.store.visible_cases(EVERY_CASE)) == len(rig.service.store.list("jobs")) == 1
    job_id = next(iter(identities))
    completed = execute_cpu(rig, job_id)
    assert completed["status"] == "completed", completed
    again = start(rig)
    assert again.json()["id"] == job_id and again.json()["status"] == "completed"
    assert rig.archive.downloaded == 1


@pytest.mark.parametrize("completeness", ["receiving", "incomplete"])
def test_receiving_or_incomplete_series_cannot_create_a_case(rig, completeness):
    rig.archive.snapshot_doc["completeness"] = completeness
    response = start(rig)
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
    assert rig.service.store.visible_cases(EVERY_CASE) == rig.service.store.list("jobs") == []


def test_changed_selection_size_limits_and_upstream_errors_have_distinct_meanings(rig):
    assert start(rig, snapshot="a" * 64).status_code == 409
    rig.archive.error = OrthancError("The synthetic archive cannot be reached.")
    response = start(rig)
    assert response.status_code == 502 and response.headers["cache-control"] == "no-store"
    assert "private-test-password" not in response.text
    rig.archive.error = None
    rig.archive.snapshot_doc["instances"][0]["size"] = rig.settings.upload_limit + 1
    response = start(rig)
    assert response.status_code == 400 and "size limit" in response.json()["detail"]
    assert rig.service.store.visible_cases(EVERY_CASE) == []


def test_success_promotes_exact_bytes_manifest_and_private_source_binding(rig):
    response = start(rig, label="Synthetic archive case")
    public_job = response.json()
    assert "payload" not in public_job and "cancel_requested" not in public_job
    completed = execute_cpu(rig, public_job["id"])
    assert completed["status"] == "completed", completed
    case = rig.service.store.get("cases", public_job["case_id"])
    assert case["status"] == "imported" and case["active_revision_id"] is None
    assert case["series"][0]["uid"] == SERIES_UID and case["series"][0]["slice_count"] == 3
    source_dir = Path(case["source_dir"])
    manifest_path = source_dir.parent / "source-import.json"
    manifest = json.loads(manifest_path.read_text())
    assert manifest == case["references"]["orthanc"]
    assert case["import_source"]["manifest_sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert case["import_source"]["content_digest"] == manifest["content_digest"]
    assert case["import_source"]["frame_of_reference_uid"] == FRAME_UID
    for entry in manifest["files"]:
        actual = (source_dir / entry["path"]).read_bytes()
        assert actual == rig.archive.files[entry["instance_id"]]
        assert entry["sha256"] == hashlib.sha256(actual).hexdigest() and entry["size"] == len(actual)
    public_case = rig.http.get(f"/api/cases/{case['id']}").json()
    assert "source_dir" not in public_case and "references" not in public_case
    assert all("files" not in row for row in public_case["series"])
    assert "private-test-password" not in json.dumps(public_case)
    assert list((rig.settings.data_dir / "orthanc-staging").iterdir()) == []


def test_queued_cancel_and_retry_reuse_the_destination_without_overwriting_sources(rig):
    job = start(rig).json()
    cancelled = rig.http.post(f"/api/jobs/{job['id']}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    assert rig.service.store.claim("cpu") is None
    retry = start(rig).json()
    assert retry["id"] != job["id"] and retry["case_id"] == job["case_id"]
    assert execute_cpu(rig, retry["id"])["status"] == "completed"
    assert rig.archive.downloaded == 1


def test_running_cancel_never_publishes_source_and_retry_remains_available(rig):
    rig.archive.release.clear()
    job = start(rig).json()
    claimed = rig.service.store.claim("cpu")
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(rig.service.execute, claimed)
        assert rig.archive.entered.wait(5)
        response = rig.http.post(f"/api/jobs/{job['id']}/cancel")
        assert response.status_code == 200
        future.result(timeout=5)
    cancelled = rig.service.store.get("jobs", job["id"])
    assert cancelled["status"] == "cancelled"
    case = rig.service.store.get("cases", job["case_id"])
    assert not case.get("source_dir") and not case.get("active_revision_id")
    assert case["status"] != "importing"
    rig.archive.release.set()
    retry = start(rig).json()
    assert retry["case_id"] == case["id"] and retry["id"] != job["id"]
    assert execute_cpu(rig, retry["id"])["status"] == "completed"


def test_cancellation_after_the_last_download_still_prevents_source_publication(rig):
    job = start(rig).json()
    rig.archive.manifest_mutation = lambda _manifest: rig.service.orthanc.cancel(job["id"])
    result = execute_cpu(rig, job["id"])
    assert result["status"] == "cancelled"
    case = rig.service.store.get("cases", job["case_id"])
    assert not case.get("source_dir") and case["references"] == {}
    assert list((rig.settings.data_dir / "orthanc-staging").iterdir()) == []


def test_transfer_progress_and_cancel_polls_do_not_rewrite_or_reparse_each_instance(rig, monkeypatch):
    from scan3d import orthanc_imports

    # A burst of tiny files and chunk checks shares one polling interval. The
    # first/final progress and unconditional pre-publication check still occur.
    monkeypatch.setattr(orthanc_imports, "time", SimpleNamespace(monotonic=lambda: 0.0))
    updates, polls = [], []
    update_job, cancelled = rig.service.store.update_job, rig.service.cancelled

    def update(job_id, **changes):
        if changes.get("progress", {}).get("phase") == "archive_import":
            updates.append(changes["progress"]["completed"])
        return update_job(job_id, **changes)

    def poll(job):
        polls.append(job["id"])
        return cancelled(job)

    monkeypatch.setattr(rig.service.store, "update_job", update)
    monkeypatch.setattr(rig.service, "cancelled", poll)
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "completed"
    assert updates == [1, 3] and len(polls) <= 3


@pytest.mark.parametrize("identity", ["study", "series", "frame", "sop", "sop_class", "file_meta", "modality"])
def test_downloaded_dicom_identity_is_verified_independently_of_archive_tags(rig, identity):
    first = next(iter(rig.archive.files))
    foreign = "1.2.826.0.1.3680043.10.543.999"
    if identity in {"study", "series", "frame"}:
        rig.archive.files[first] = instance_bytes(0, **{identity: foreign})
    else:
        dataset = pydicom.dcmread(io.BytesIO(rig.archive.files[first]))
        if identity == "sop":
            dataset.SOPInstanceUID = dataset.file_meta.MediaStorageSOPInstanceUID = foreign
        elif identity == "sop_class":
            dataset.SOPClassUID = dataset.file_meta.MediaStorageSOPClassUID = pydicom.uid.MRImageStorage
        elif identity == "file_meta":
            dataset.file_meta.MediaStorageSOPInstanceUID = foreign
        else:
            dataset.Modality = "MR"
        output = io.BytesIO()
        dataset.save_as(output, enforce_file_format=True)
        rig.archive.files[first] = output.getvalue()
        if identity == "file_meta":
            # pydicom intentionally repairs file meta while writing. Corrupt
            # only its first UID occurrence afterward to exercise that guard.
            original = str(dataset.SOPInstanceUID)
            changed = original[:-1] + "9"
            rig.archive.files[first] = rig.archive.files[first].replace(original.encode(), changed.encode(), 1)
            check = pydicom.dcmread(io.BytesIO(rig.archive.files[first]))
            assert check.file_meta.MediaStorageSOPInstanceUID != check.SOPInstanceUID
    # Keep the advertised file length truthful, so a failure proves independent
    # DICOM identity checking rather than only the simpler byte-size guard.
    for entry in rig.archive.snapshot_doc["instances"]:
        entry["size"] = len(rig.archive.files[entry["id"]])
    rig.archive.snapshot_doc["snapshot"] = snapshot_fingerprint(parse_snapshot(rig.archive.snapshot_doc))
    job = start(rig).json()
    result = execute_cpu(rig, job["id"])
    assert result["status"] == "failed"
    assert "identity" in result["error"] or "coordinate frames" in result["error"]
    case = rig.service.store.get("cases", job["case_id"])
    assert not case.get("source_dir") and case["references"] == {}
    assert not list(rig.service.store.case_dir(case["id"]).glob("sources/*/dicom"))


@pytest.mark.parametrize("mutation", ["hash", "membership", "digest", "path"])
def test_manifest_provenance_is_checked_against_actual_source_content(rig, mutation):
    def mutate(manifest):
        if mutation == "hash":
            manifest["files"][0]["sha256"] = "0" * 64
        elif mutation == "digest":
            manifest["content_digest"] = "0" * 64
        elif mutation == "path":
            manifest["files"][0]["path"] = "../source.dcm"
        else:
            manifest["files"][-1] = copy.deepcopy(manifest["files"][0])

    rig.archive.manifest_mutation = mutate
    job = start(rig).json()
    result = execute_cpu(rig, job["id"])
    assert result["status"] == "failed"
    assert not rig.service.store.get("cases", job["case_id"]).get("source_dir")


def test_transfer_failure_keeps_original_source_unpublished_and_can_retry(rig):
    rig.archive.download_error = OrthancError("The synthetic DICOM transfer was interrupted.")
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "failed"
    assert rig.service.store.get("cases", job["case_id"])["references"] == {}
    rig.archive.download_error = None
    retry = start(rig).json()
    assert retry["case_id"] == job["case_id"]
    assert execute_cpu(rig, retry["id"])["status"] == "completed"


def test_startup_recovers_interrupted_import_without_contacting_the_archive(rig, monkeypatch):
    job = start(rig).json()
    assert rig.service.store.claim("cpu")["id"] == job["id"]
    restarted = Service(rig.settings)
    monkeypatch.setattr(restarted.orthanc, "client", lambda: pytest.fail("Recovery must not contact Orthanc"))
    restarted.start(lanes=("cpu",))
    try:
        interrupted = restarted.store.get("jobs", job["id"])
        case = restarted.store.get("cases", job["case_id"])
        assert interrupted["status"] == "failed" and interrupted["stage"] == "Interrupted"
        assert case["status"] == "import_failed" and not case.get("source_dir")
    finally:
        restarted.stop()
    retry = start(rig).json()
    assert retry["case_id"] == job["case_id"] and retry["id"] != job["id"]
    assert execute_cpu(rig, retry["id"])["status"] == "completed"


def test_recovery_of_an_older_failed_attempt_preserves_a_queued_retry(rig):
    rig.archive.download_error = OrthancError("The synthetic transfer was interrupted.")
    failed = start(rig).json()
    assert execute_cpu(rig, failed["id"])["status"] == "failed"
    rig.archive.download_error = None
    retry = start(rig).json()
    rig.service.orthanc.recover()
    assert rig.service.store.get("cases", retry["case_id"])["status"] == "importing"
    assert rig.service.store.get("jobs", retry["id"])["status"] == "queued"
    assert execute_cpu(rig, retry["id"])["status"] == "completed"
    rig.service.orthanc.recover()
    assert rig.service.store.get("cases", retry["case_id"])["status"] == "imported"


def test_failed_manifest_publication_retains_unreferenced_source_bytes_and_retries_fresh(rig, monkeypatch):
    from scan3d import orthanc_imports

    write_manifest = orthanc_imports.atomic_json

    def unavailable(*_args):
        raise OSError("Synthetic manifest publication failure")

    monkeypatch.setattr(orthanc_imports, "atomic_json", unavailable)
    failed = start(rig).json()
    assert execute_cpu(rig, failed["id"])["status"] == "failed"
    case = rig.service.store.get("cases", failed["case_id"])
    assert case["status"] == "import_failed" and not case.get("source_dir") and case["references"] == {}
    retained = rig.service.store.case_dir(case["id"]) / "sources" / failed["id"] / "dicom"
    before = {file.name: file.read_bytes() for file in retained.iterdir()}
    assert before and set(before.values()) == set(rig.archive.files.values())
    monkeypatch.setattr(orthanc_imports, "atomic_json", write_manifest)
    retry = start(rig).json()
    assert retry["case_id"] == failed["case_id"] and retry["id"] != failed["id"]
    assert execute_cpu(rig, retry["id"])["status"] == "completed"
    case = rig.service.store.get("cases", retry["case_id"])
    assert Path(case["source_dir"]) != retained
    assert {file.name: file.read_bytes() for file in retained.iterdir()} == before


def test_open_scan_carries_the_archive_digest_into_immutable_image_identity(rig, monkeypatch):
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "completed"
    case = rig.service.store.get("cases", job["case_id"])

    def convert(files, destination):
        assert files == case["series"][0]["files"]
        image = nib.Nifti1Image(np.arange(8 * 6 * 3, dtype=np.int16).reshape(8, 6, 3), np.diag([-0.7, -0.8, 1.2, 1]))
        image.header.set_xyzt_units("mm")
        path = destination / "reference.nii.gz"
        nib.save(image, path)
        return path

    monkeypatch.setattr(dicom, "convert_series", convert)
    request = rig.http.post(f"/api/cases/{case['id']}/process", json={"series_uid": SERIES_UID, "profile": "chest_abdomen", "phase": "unknown"})
    assert request.status_code == 200, request.text
    prepared = execute_cpu(rig, request.json()["id"])
    assert prepared["status"] == "completed", prepared
    revision = rig.service.store.revision(case["id"], prepared["revision_id"])
    assert revision["structures"] == [] and revision["kind"] == "ct"
    asset = rig.service.store.resolve_reference(revision)
    assert asset["source"]["study_uid"] == STUDY_UID and asset["source"]["frame_of_reference_uid"] == FRAME_UID
    assert asset["source"]["source_archive_key"] == case["import_source"]["source_key"]
    assert asset["source"]["source_content_sha256"] == case["import_source"]["content_digest"]
    assert asset["source"]["source_manifest_sha256"] == case["import_source"]["manifest_sha256"]
    assert {row["kind"] for row in rig.service.store.list("jobs")} == {"orthanc_import", "process"}
