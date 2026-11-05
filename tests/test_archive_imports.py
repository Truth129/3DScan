"""DICOMweb imports with synthetic DICOM, a fake archive and a fake clock, through the API and CPU lane."""

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pydicom
from pydicom.uid import ExplicitVRLittleEndian
import pytest

from scan3d.api import create_app
from scan3d.archive.dicomweb import Retrieved
from scan3d.archive.settings import DicomWebSettings
from scan3d.archive.stability import QuietPeriod
from scan3d.config import Settings
from scan3d.service import Service
from scan3d.store import public_case
from test_orthanc_imports import FRAME_UID, SECOND_SERIES_UID, SERIES_UID, STUDY_UID, execute_cpu, fake_convert, instance_bytes

PATIENT = "SYNTHETIC-ORTHANC"


def with_patient(data, patient_id, issuer=None):
    ds = pydicom.dcmread(io.BytesIO(data))
    ds.PatientID = patient_id
    if issuer:
        ds.IssuerOfPatientID = issuer
    output = io.BytesIO()
    ds.save_as(output, enforce_file_format=True)
    return output.getvalue()


class FakeDicomWeb:
    """The DicomWebClient calls an importer makes, answered from synthetic files."""

    source_key = hashlib.sha256(b"dicomweb-id:synthetic").hexdigest()

    def __init__(self):
        self.patient_id = PATIENT
        self.series = {SERIES_UID: [instance_bytes(index) for index in range(3)]}
        self.after_first_file = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def study(self):
        return {
            "study_uid": STUDY_UID,
            "description": "Synthetic study",
            "study_date": "20261105",
            "patient_id": self.patient_id,
            "issuer_of_patient_id": "",
            "patient_name": "Synthetic^Archive",
            "accession_number": "",
            "series_count": len(self.series),
            "modalities": "CT",
        }

    def study_series(self, study_uid):
        assert study_uid == STUDY_UID
        rows = [{"series_uid": uid, "description": "Synthetic CT", "modality": "CT", "reported_instances": len(files)} for uid, files in self.series.items()]
        return {"study": self.study(), "series": rows}

    def _files(self, series_uid):
        return {str(ds.SOPInstanceUID): (ds, data) for data in self.series[series_uid] for ds in [pydicom.dcmread(io.BytesIO(data), stop_before_pixels=True)]}

    def instances(self, study_uid, series_uid):
        return tuple(sorted((sop, str(ds.SOPClassUID), str(ds.FrameOfReferenceUID)) for sop, (ds, _data) in self._files(series_uid).items()))

    def retrieve(self, study_uid, series_uid, sop_uid, path, *, max_bytes, cancelled=None):
        data = self._files(series_uid)[sop_uid][1]
        with Path(path).open("xb") as output:
            output.write(data)
        if self.after_first_file:
            self.after_first_file()
            self.after_first_file = None
        return Retrieved(sha256=hashlib.sha256(data).hexdigest(), size=len(data), transfer_syntax_uid=str(ExplicitVRLittleEndian))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    archive = FakeDicomWeb()
    clock = SimpleNamespace(now=0.0)
    settings = Settings(
        tmp_path / "data",
        (tmp_path,),
        web_dir=tmp_path / "dist",
        modalities=("CT",),
        # Beside an Orthanc, which no test contacts, so its exporter reaches its source check.
        orthanc_url="https://orthanc.example.test",
        dicomweb=DicomWebSettings(url="https://archive.example.test/dicom-web", archive_id="synthetic", quiet_seconds=60),
    )
    app = create_app(settings)
    service = app.state.service
    service.archive_imports.quiet = QuietPeriod(60, clock=lambda: clock.now)
    monkeypatch.setattr(service.archive_imports, "client", lambda: archive)
    http = TestClient(app, base_url="http://127.0.0.1")
    yield SimpleNamespace(http=http, service=service, archive=archive, clock=clock, settings=settings)
    http.close()


def listing(rig):
    response = rig.http.post("/api/archive/studies/series", json={"study_uid": STUDY_UID})
    assert response.status_code == 200, response.text
    return {row["series_uid"]: row for row in response.json()["series"]}


def start(rig, series_uid=SERIES_UID, *, case_id=None, **extra):
    """Import once the series has stayed unchanged for the quiet period, as a user who waits would."""
    listing(rig)
    rig.clock.now += 61
    body = {"study_uid": STUDY_UID, "series_uid": series_uid, "snapshot": listing(rig)[series_uid]["snapshot"]}
    url = f"/api/cases/{case_id}/archive-series" if case_id else "/api/archive/imports"
    return rig.http.post(url, json={**body, **extra})


def imported(rig):
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "completed"
    return rig.service.store.get("cases", job["case_id"])


def test_a_series_imports_only_after_its_listing_stays_unchanged(rig):
    row = listing(rig)[SERIES_UID]
    assert (row["stable"], row["completeness"], row["ready_in"]) == (False, "receiving", 60)
    body = {"study_uid": STUDY_UID, "series_uid": SERIES_UID, "snapshot": row["snapshot"]}
    early = rig.http.post("/api/archive/imports", json=body)
    assert early.status_code == 409 and "60 s" in early.json()["detail"]
    rig.clock.now += 30
    # A new instance restarts the wait, and the old snapshot no longer matches.
    rig.archive.series[SERIES_UID].append(instance_bytes(3))
    assert listing(rig)[SERIES_UID]["ready_in"] == 60
    assert rig.http.post("/api/archive/imports", json=body).status_code == 409
    rig.clock.now += 60
    row = listing(rig)[SERIES_UID]
    assert (row["stable"], row["ready_in"]) == (True, 0)
    response = rig.http.post("/api/archive/imports", json={**body, "snapshot": row["snapshot"]})
    assert response.status_code == 200 and response.json()["kind"] == "archive_import"


def test_an_imported_series_prepares_with_its_archive_source_and_keeps_the_patient_key_private(rig, monkeypatch):
    case = imported(rig)
    source = case["import_source"]
    assert source["kind"] == "dicomweb" and source["source_key"] == rig.archive.source_key and source["frame_of_reference_uid"] == FRAME_UID
    # The same provenance keys as an Orthanc import, so image identity and exports read it alike.
    assert set(source) == {
        "kind",
        "source_key",
        "study_uid",
        "series_uid",
        "frame_of_reference_uid",
        "snapshot",
        "instance_count",
        "content_digest",
        "manifest_sha256",
        "completeness",
        "warnings",
    }
    patient = case["references"]["archive"]["snapshot"]["patient_key"]
    assert patient not in json.dumps(public_case(case, rig.settings))
    fake_convert(monkeypatch, case["series"][0]["files"])
    request = rig.http.post(f"/api/cases/{case['id']}/process", json={"series_uid": SERIES_UID, "profile": "chest_abdomen", "phase": "unknown"})
    prepared = execute_cpu(rig, request.json()["id"])
    assert prepared["status"] == "completed", prepared
    asset = rig.service.store.resolve_reference(rig.service.store.revision(case["id"], prepared["revision_id"]))
    assert asset["source"]["source_archive_key"] == source["source_key"]
    assert asset["source"]["source_manifest_sha256"] == source["manifest_sha256"]


def test_a_listing_that_changes_during_the_transfer_publishes_nothing(rig):
    job = start(rig).json()
    rig.archive.after_first_file = lambda: rig.archive.series[SERIES_UID].append(instance_bytes(3))
    assert execute_cpu(rig, job["id"])["status"] == "failed"
    case = rig.service.store.get("cases", job["case_id"])
    assert case["status"] == "import_failed" and case["references"] == {} and not case.get("source_dir")
    assert not any((rig.settings.data_dir / "orthanc-staging").iterdir())


def test_a_downloaded_file_naming_another_patient_fails_the_import(rig):
    rig.archive.series[SERIES_UID][1] = with_patient(rig.archive.series[SERIES_UID][1], "ANOTHER-PATIENT")
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "failed"
    assert rig.service.store.get("cases", job["case_id"])["status"] == "import_failed"


def test_files_may_carry_an_issuer_that_the_study_listing_leaves_out(rig):
    rig.archive.series[SERIES_UID] = [with_patient(data, PATIENT, "SYNTHETIC-ISSUER") for data in rig.archive.series[SERIES_UID]]
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "completed"


def test_only_a_series_of_the_cases_own_patient_can_join_it(rig):
    case = imported(rig)
    rig.archive.series[SECOND_SERIES_UID] = [instance_bytes(index + 10, series=SECOND_SERIES_UID) for index in range(3)]
    rig.archive.patient_id = "MERGED-PATIENT"
    assert rig.http.get(f"/api/cases/{case['id']}/archive-series").status_code == 409
    refused = start(rig, SECOND_SERIES_UID, case_id=case["id"])
    assert refused.status_code == 400 and "different study or patient" in refused.json()["detail"]
    rig.archive.patient_id = PATIENT
    rows = rig.http.get(f"/api/cases/{case['id']}/archive-series").json()["series"]
    assert {row["series_uid"]: row["in_case"] for row in rows} == {SERIES_UID: True, SECOND_SERIES_UID: False}
    added = start(rig, SECOND_SERIES_UID, case_id=case["id"]).json()
    assert execute_cpu(rig, added["id"])["status"] == "completed"
    assert set(rig.service.store.get("cases", case["id"])["added_sources"]) == {SECOND_SERIES_UID}


def test_import_again_makes_a_fresh_case_beside_a_completed_one(rig):
    first = imported(rig)
    assert start(rig).json()["case_id"] == first["id"]
    again = start(rig, reimport=True).json()
    assert again["case_id"] != first["id"]
    assert execute_cpu(rig, again["id"])["status"] == "completed"
    launch = rig.http.post("/api/archive/launch", json={"study_uid": STUDY_UID, "series_uid": SERIES_UID, "source_key": rig.archive.source_key})
    assert launch.json()["case_id"] == again["case_id"]


def test_a_cancelled_import_again_leaves_the_earlier_case_in_use(rig):
    first = imported(rig)
    again = start(rig, reimport=True).json()
    assert rig.service.cancel_job(again["id"])["status"] == "cancelled"
    launch = rig.http.post("/api/archive/launch", json={"study_uid": STUDY_UID, "series_uid": SERIES_UID, "source_key": rig.archive.source_key})
    assert launch.json()["case_id"] == first["id"]
    assert start(rig).json()["case_id"] == first["id"]


def test_startup_recovers_an_interrupted_archive_import_without_contacting_the_archive(rig, monkeypatch):
    job = start(rig).json()
    assert rig.service.store.claim("cpu")["id"] == job["id"]
    restarted = Service(rig.settings)
    monkeypatch.setattr(restarted.archive_imports, "client", lambda: pytest.fail("Recovery must not contact the archive"))
    restarted.start(lanes=("cpu",))
    try:
        case = restarted.store.get("cases", job["case_id"])
        assert restarted.store.get("jobs", job["id"])["status"] == "failed"
        assert case["status"] == "import_failed" and case["error"] == "The archive import did not complete. Retry the selected series."
    finally:
        restarted.stop()


def test_the_orthanc_exporter_refuses_a_dicomweb_case(rig, monkeypatch):
    case = imported(rig)
    fake_convert(monkeypatch, case["series"][0]["files"])
    request = rig.http.post(f"/api/cases/{case['id']}/process", json={"series_uid": SERIES_UID, "profile": "chest_abdomen", "phase": "unknown"})
    revision = execute_cpu(rig, request.json()["id"])["revision_id"]
    availability = rig.http.get(f"/api/cases/{case['id']}/revisions/{revision}/dicom-view").json()
    assert availability["available"] is False and "Orthanc CT source" in availability["reason"]
