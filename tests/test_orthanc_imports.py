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
from scan3d.orthanc import OrthancError, snapshot_fingerprint
from scan3d.service import Service
from scan3d.orthanc_models import json_record, parse_download, parse_snapshot


def oid(value):
    return "-".join([f"{value:08x}"] * 5)


STUDY, SERIES = oid(1), oid(2)
STUDY_UID, SERIES_UID, FRAME_UID = "1.2.826.0.1.3680043.10.543.1", "1.2.826.0.1.3680043.10.543.2", "1.2.826.0.1.3680043.10.543.3"
SECOND_SERIES_UID = "1.2.826.0.1.3680043.10.543.4"


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


def start_as(rig, user_id, label=None):
    identity.activate(identity.Principal(user_id, "name", "Name", "admin", True))
    try:
        return rig.service.orthanc.start(SERIES, rig.archive.snapshot_doc["snapshot"], label)
    finally:
        identity.activate(None)


@pytest.mark.parametrize("status", ["running", "completed", "failed", "cancelled"])
def test_one_import_serves_everyone_and_a_retry_keeps_who_added_it(rig, status):
    def owners(case_id):
        with rig.service.store.connection() as con:
            return [row["user_id"] for row in con.execute("SELECT user_id FROM case_access WHERE case_id=?", (case_id,))]

    first, second = "a" * 32, "b" * 32
    job = start_as(rig, first, "First label")
    rig.service.store.update_job(job["id"], status=status)
    other = start_as(rig, second, "Second label")
    assert other["case_id"] == job["case_id"]
    assert (other["id"] == job["id"]) == (status in {"running", "completed"})
    assert owners(job["case_id"]) == [first]
    assert rig.service.store.get("cases", job["case_id"])["label"] == "First label"


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


@pytest.mark.parametrize("error", [OrthancError("Synthetic unavailable"), ValueError("Synthetic bad configuration")])
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


def test_a_repeat_import_of_a_trashed_case_makes_a_new_case(rig):
    first = start(rig).json()
    assert execute_cpu(rig, first["id"])["status"] == "completed"
    rig.service.store.trash_case(first["case_id"], None)
    second = start(rig).json()
    assert second["id"] != first["id"] and second["case_id"] != first["case_id"]
    assert execute_cpu(rig, second["id"])["status"] == "completed"
    assert [case["id"] for case in rig.service.store.visible_cases(EVERY_CASE)] == [second["case_id"]]
    assert [case["id"] for case in rig.service.store.trashed_cases(EVERY_CASE, None)] == [first["case_id"]]


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


def replace_instance(rig, entry, data):
    """Keep the advertised size truthful, so only the DICOM checks can refuse the new bytes."""
    rig.archive.files[entry["id"]] = data
    entry["size"] = len(data)
    rig.archive.snapshot_doc["snapshot"] = snapshot_fingerprint(parse_snapshot(rig.archive.snapshot_doc))


def edited_instance(rig, entry, edit):
    dataset = pydicom.dcmread(io.BytesIO(rig.archive.files[entry["id"]]))
    edit(dataset)
    output = io.BytesIO()
    dataset.save_as(output, enforce_file_format=True)
    replace_instance(rig, entry, output.getvalue())


@pytest.mark.parametrize("identity", ["series", "sop_class", "file_meta", "modality"])
def test_downloaded_dicom_identity_is_verified_independently_of_archive_tags(rig, identity):
    entry = rig.archive.snapshot_doc["instances"][0]
    if identity == "series":
        replace_instance(rig, entry, instance_bytes(0, series="1.2.826.0.1.3680043.10.543.999"))
    elif identity == "sop_class":
        edited_instance(rig, entry, lambda ds: setattr(ds, "SOPClassUID", pydicom.uid.MRImageStorage))
    elif identity == "modality":
        edited_instance(rig, entry, lambda ds: setattr(ds, "Modality", "MR"))
    else:
        # pydicom repairs file meta while writing, so corrupt its first UID occurrence afterwards.
        uid = entry["sop_instance_uid"]
        replace_instance(rig, entry, rig.archive.files[entry["id"]].replace(uid.encode(), (uid[:-1] + "9").encode(), 1))
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


def break_spacing(rig):
    edited_instance(rig, rig.archive.snapshot_doc["instances"][-1], lambda ds: setattr(ds, "ImagePositionPatient", [0, 0, 500]))


def test_a_refused_series_reports_the_validators_own_reason(rig):
    break_spacing(rig)
    job = start(rig).json()
    result = execute_cpu(rig, job["id"])
    assert result["status"] == "failed"
    assert result["error"] == "The selected DICOM series cannot form a supported reference image: Nonuniform slice spacing or missing slices"
    assert not rig.service.store.get("cases", job["case_id"]).get("source_dir")


def fake_convert(monkeypatch, expected_files):
    def convert(files, destination):
        assert files == expected_files
        image = nib.Nifti1Image(np.arange(8 * 6 * 3, dtype=np.int16).reshape(8, 6, 3), np.diag([-0.7, -0.8, 1.2, 1]))
        image.header.set_xyzt_units("mm")
        nib.save(image, destination / "reference.nii.gz")
        return destination / "reference.nii.gz"

    monkeypatch.setattr(dicom, "convert_series", convert)


def imported_case(rig):
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "completed"
    return job["case_id"]


def become_second_series(rig, study=STUDY_UID, study_id=STUDY):
    """The archive's one series turns into another series of the study, so one fixture covers both."""
    for index, entry in enumerate(rig.archive.snapshot_doc["instances"]):
        rig.archive.files[entry["id"]] = instance_bytes(index, study=study, series=SECOND_SERIES_UID)
        entry["size"] = len(rig.archive.files[entry["id"]])
    rig.archive.snapshot_doc.update(series_uid=SECOND_SERIES_UID, study_uid=study, study_id=study_id)
    rig.archive.snapshot_doc["snapshot"] = snapshot_fingerprint(parse_snapshot(rig.archive.snapshot_doc))


def add_series(rig, case_id):
    return rig.http.post(f"/api/cases/{case_id}/orthanc-series", json={"series_id": SERIES, "snapshot": rig.archive.snapshot_doc["snapshot"]})


def test_a_series_of_the_same_study_joins_the_case_with_its_own_source(rig, monkeypatch):
    case_id = imported_case(rig)
    first = rig.service.store.get("cases", case_id)
    become_second_series(rig)
    assert [row["in_case"] for row in rig.http.get(f"/api/cases/{case_id}/orthanc-series").json()["series"]] == [False]
    job = add_series(rig, case_id)
    assert job.status_code == 200, job.text
    assert add_series(rig, case_id).json()["id"] == job.json()["id"]
    assert execute_cpu(rig, job.json()["id"])["status"] == "completed"
    case = rig.service.store.get("cases", case_id)
    added = case["added_sources"][SECOND_SERIES_UID]
    assert [row["uid"] for row in case["series"]] == [SERIES_UID, SECOND_SERIES_UID]
    assert (case["status"], case["source_dir"], case["import_source"]) == ("imported", first["source_dir"], first["import_source"])
    assert added["source_dir"] != case["source_dir"] and Path(case["series"][1]["files"][0]).is_relative_to(added["source_dir"])
    assert [row["in_case"] for row in rig.http.get(f"/api/cases/{case_id}/orthanc-series").json()["series"]] == [True]
    assert "added_sources" not in rig.http.get(f"/api/cases/{case_id}").json()
    fake_convert(monkeypatch, case["series"][1]["files"])
    request = rig.http.post(f"/api/cases/{case_id}/process", json={"series_uid": SECOND_SERIES_UID, "profile": "chest_abdomen", "phase": "unknown"})
    prepared = execute_cpu(rig, request.json()["id"])
    assert prepared["status"] == "completed", prepared
    asset = rig.service.store.resolve_reference(rig.service.store.revision(case_id, prepared["revision_id"]))
    assert asset["source"]["source_manifest_sha256"] == added["import_source"]["manifest_sha256"] != first["import_source"]["manifest_sha256"]
    source, directory = rig.service.orthanc_exports._source(case_id, rig.service.store.get("cases", case_id), asset, connect=False)
    assert source.snapshot.series_uid == SECOND_SERIES_UID and str(directory) == added["source_dir"]


def test_only_a_new_series_of_the_cases_own_orthanc_study_can_join(rig):
    case_id = imported_case(rig)
    assert "already in this case" in add_series(rig, case_id).json()["detail"]
    become_second_series(rig, study="1.2.826.0.1.3680043.10.543.998")
    refused = add_series(rig, case_id)
    assert refused.status_code == 400 and "different study" in refused.json()["detail"]
    # Orthanc files one StudyInstanceUID under each PatientID as its own study, and this is the other patient's.
    become_second_series(rig, study_id=oid(99))
    refused = add_series(rig, case_id)
    assert refused.status_code == 400 and "different study" in refused.json()["detail"]
    assert "Only a case imported from Orthanc" in add_series(rig, rig.service.new_case("Folder case")["id"]).json()["detail"]
    assert len(rig.service.store.get("cases", case_id)["series"]) == 1


def test_a_refused_addition_leaves_the_working_case_unchanged(rig):
    case_id = imported_case(rig)
    before = rig.service.store.get("cases", case_id)
    become_second_series(rig)
    break_spacing(rig)
    result = execute_cpu(rig, add_series(rig, case_id).json()["id"])
    assert result["status"] == "failed" and "Nonuniform slice spacing" in result["error"]
    assert rig.service.store.get("cases", case_id) == before


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


def test_a_failed_transfer_publishes_nothing_and_recovery_preserves_its_queued_retry(rig):
    rig.archive.download_error = OrthancError("The synthetic transfer was interrupted.")
    failed = start(rig).json()
    assert execute_cpu(rig, failed["id"])["status"] == "failed"
    assert rig.service.store.get("cases", failed["case_id"])["references"] == {}
    rig.archive.download_error = None
    retry = start(rig).json()
    assert retry["case_id"] == failed["case_id"]
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
    fake_convert(monkeypatch, case["series"][0]["files"])
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
