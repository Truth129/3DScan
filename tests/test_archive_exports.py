"""DICOMweb archive exports through the routes, real encoders and a fake archive that keeps what it is sent."""

import copy
import dataclasses
import hashlib
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pydicom
from pydicom.encaps import encapsulate
from pydicom.uid import ImplicitVRLittleEndian, JPEG2000Lossless
import pytest

from scan3d.api import create_app
from scan3d.archive.dicomweb import Retrieved, Stored
from scan3d.archive.errors import ArchiveError
from scan3d.archive.settings import DicomSendSettings, DicomWebSettings
from scan3d.config import Settings
from scan3d.store import identifier
from test_archive_imports import FakeDicomWeb
from test_cstore import Node
from test_orthanc_imports import SERIES_UID, STUDY_UID, execute_cpu
from test_orthanc_view_exports import PNG


class ArchiveStore(FakeDicomWeb):
    """Keeps the first copy of each SOP, as Orthanc does, and returns it, optionally altered."""

    def __init__(self):
        super().__init__()
        self.held, self.stores, self.study_errors = {}, [], []
        self.altered = self.store_error = None

    def status(self):
        return {"name": "DICOMweb", "version": "", "api_version": None}

    def study(self, study_uid=None):
        # Each queued error answers one study lookup; None lets that lookup through.
        if self.study_errors and (error := self.study_errors.pop(0)):
            raise error
        return super().study()

    def store(self, path, *, expected_sha256):
        data = Path(path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == expected_sha256
        if self.store_error:
            error, self.store_error = self.store_error, None
            raise error
        sop = str(pydicom.dcmread(BytesIO(data), stop_before_pixels=True).SOPInstanceUID)
        self.stores.append(sop)
        self.held.setdefault(sop, data)
        return Stored("stored", sop)

    def read_back(self, study_uid, series_uid, sop_uid, path, *, max_bytes):
        data = self.held[sop_uid]
        if self.altered:
            data = self.altered(data)
        Path(path).write_bytes(data)
        return Retrieved(hashlib.sha256(data).hexdigest(), len(data), str(pydicom.dcmread(BytesIO(data)).file_meta.TransferSyntaxUID))


@pytest.fixture
def rig(tmp_path, monkeypatch):
    archive = ArchiveStore()
    settings = Settings(
        tmp_path / "data",
        (tmp_path,),
        web_dir=tmp_path / "dist",
        modalities=("CT",),
        dicomweb=DicomWebSettings(url="https://archive.example.test/dicom-web", name="Synthetic PACS", archive_id="synthetic", quiet_seconds=0),
    )
    app = create_app(settings)
    service = app.state.service
    monkeypatch.setattr(service.archive_imports, "client", lambda: archive)
    http = TestClient(app, base_url="http://127.0.0.1")
    listed = http.post("/api/archive/studies/series", json={"study_uid": STUDY_UID}).json()["series"][0]
    imported = http.post("/api/archive/imports", json={"study_uid": STUDY_UID, "series_uid": SERIES_UID, "snapshot": listed["snapshot"]}).json()
    assert execute_cpu(SimpleNamespace(service=service), imported["id"])["status"] == "completed"
    case = service.store.get("cases", imported["case_id"])
    # A prepared CT and a saved revision with two masks on its grid, as the Orthanc export tests make.
    directory = service.store.case_dir(case["id"])
    affine = np.diag([-0.7, -0.8, 1.2, 1])
    nib.save(nib.Nifti1Image(np.zeros((8, 6, 3), dtype=np.int16), affine), directory / "reference.nii.gz")
    source = case["import_source"]
    image_source = {key: source[key] for key in ("series_uid", "study_uid", "frame_of_reference_uid")}
    image_source.update(
        source_archive_key=source["source_key"], source_content_sha256=source["content_digest"], source_manifest_sha256=source["manifest_sha256"]
    )
    base = service.build_ct_revision(case["id"], directory / "reference.nii.gz", acquisition={"series_uid": SERIES_UID}, image_source=image_source)
    service.store.commit_revision(base)
    saved = copy.deepcopy(base)
    saved.update(id=identifier(), parent_id=base["id"], source="totalsegmentator", kind="segmentation", structures=[])
    for index in range(2):
        values = np.zeros((8, 6, 3), dtype=np.uint8)
        values[1 + index : 4, 2:5, 1] = 1
        nib.save(nib.Nifti1Image(values, affine), directory / f"saved-mask-{index}.nii.gz")
        saved["structures"].append(
            {
                "id": f"test:{index}",
                "name": f"Synthetic mask {index}",
                "color": ["#ff0000", "#00ffff"][index],
                "mask_file": str(directory / f"saved-mask-{index}.nii.gz"),
                "voxel_count": int(values.sum()),
                "reference_image_id": base["reference_image_id"],
            }
        )
    service.store.save("revisions", saved)
    revision_url = f"/api/cases/{case['id']}/revisions/{saved['id']}"
    yield SimpleNamespace(
        http=http, service=service, archive=archive, case=case, revision=saved, seg=f"{revision_url}/dicom-seg", view=f"{revision_url}/dicom-view"
    )
    http.close()


def run(rig, response):
    assert response.status_code == 200, response.text
    return execute_cpu(rig, response.json()["id"])


def export_directory(rig, job):
    return rig.service.store.case_dir(rig.case["id"]) / "dicom-exports" / rig.service.store.get("jobs", job["id"])["payload"]["export_id"]


def test_a_seg_is_stored_in_the_archive_and_proven_by_reading_it_back(rig):
    availability = rig.http.get(rig.seg).json()
    assert (availability["available"], availability["archive_name"], availability["destinations"]) == (True, "Synthetic PACS", [])
    assert availability["file"]["available"] is True and "osirix_roi" not in availability
    job = run(rig, rig.http.post(rig.seg, json={}))
    assert (job["kind"], job["status"]) == ("archive_seg_export", "completed"), job["error"]
    assert job["result"]["dicom_seg"]["archived"] is True and job["result"]["dicom_seg"]["archive"] == "Synthetic PACS"
    directory = export_directory(rig, job)
    receipt = json.loads((directory / "delivery-v2.json").read_text())
    (stored,) = receipt["archives"][rig.archive.source_key].values()
    assert "destinations" not in receipt, "the version before direct sends refuses the key"
    assert stored["verified"] == "bytes" and rig.archive.stores == [stored["sop_instance_uid"]]
    # The Orthanc receipt is never written, so an older version never reads this as its own.
    assert not (directory / "delivery.json").exists()


def test_a_retry_stores_the_same_instance_again(rig):
    rig.archive.store_error = ArchiveError("The archive could not be reached. Check the configured connection.")
    failed = run(rig, rig.http.post(rig.seg, json={}))
    assert failed["status"] == "failed" and failed["error"] == "The archive could not be reached. Check the configured connection."
    retried = run(rig, rig.http.post(rig.seg, json={}))
    assert retried["status"] == "completed" and retried["id"] != failed["id"]
    sop = json.loads((export_directory(rig, retried) / "export.json").read_text())["files"][0]["sop_instance_uid"]
    assert rig.archive.stores == [sop]


def implicit(data):
    ds = pydicom.dcmread(BytesIO(data))
    ds.file_meta.TransferSyntaxUID = ImplicitVRLittleEndian
    output = BytesIO()
    ds.save_as(output, implicit_vr=True, little_endian=True)
    return output.getvalue()


def other_pixels(data):
    ds = pydicom.dcmread(BytesIO(data))
    pixels = bytearray(ds.PixelData)
    pixels[0] ^= 0xFF
    ds.PixelData = bytes(pixels)
    output = BytesIO()
    ds.save_as(output)
    return output.getvalue()


def undecodable(data):
    """A compressed copy whose frames no decoder here can read."""
    ds = pydicom.dcmread(BytesIO(data))
    ds.PixelData = encapsulate([b"\x00\x01"] * int(ds.get("NumberOfFrames", 1)))
    ds["PixelData"].VR = "OB"
    ds.file_meta.TransferSyntaxUID = JPEG2000Lossless
    output = BytesIO()
    ds.save_as(output, enforce_file_format=True)
    return output.getvalue()


def patient(**tags):
    """The archive's own patient attributes, as dcm4che gives an issuer to an instance stored without one."""

    def change(data):
        ds = pydicom.dcmread(BytesIO(data))
        for key, value in tags.items():
            setattr(ds, key, value)
        output = BytesIO()
        ds.save_as(output, enforce_file_format=True)
        return output.getvalue()

    return change


DIFFERENT = "The archive holds a different DICOM SEG under this instance's identity. Check the study in the archive."
UNCHECKED = "The archive returned this DICOM SEG in a form that could not be checked here. Check the study in the archive."


@pytest.mark.parametrize(
    ("altered", "outcome"),
    [
        (implicit, "canonical"),
        (patient(IssuerOfPatientID="DCM4CHEE.SYNTHETIC.null"), "canonical"),
        (patient(PatientID="ANOTHER-PATIENT"), DIFFERENT),
        (other_pixels, DIFFERENT),
        (undecodable, UNCHECKED),
    ],
)
def test_an_archive_may_re_encode_the_instance_but_not_change_it(rig, altered, outcome):
    rig.archive.altered = altered
    job = run(rig, rig.http.post(rig.seg, json={}))
    if outcome == "canonical":
        assert job["status"] == "completed", job["error"]
        receipt = json.loads((export_directory(rig, job) / "delivery-v2.json").read_text())
        assert [item["verified"] for item in receipt["archives"][rig.archive.source_key].values()] == ["canonical"]
    else:
        assert (job["status"], job["error"]) == ("failed", outcome)
        assert job["result"]["dicom_seg"]["archived"] is False


def test_a_retry_reports_only_what_it_proved_itself(rig):
    # The first attempt stores and proves the SEG, then loses the archive at its last check.
    rig.archive.study_errors = [None, ArchiveError("The archive could not be reached. Check the configured connection.")]
    assert run(rig, rig.http.post(rig.seg, json={}))["status"] == "failed"
    rig.archive.altered = other_pixels
    retried = run(rig, rig.http.post(rig.seg, json={}))
    assert (retried["status"], retried["error"]) == ("failed", DIFFERENT)
    assert retried["result"]["dicom_seg"]["archived"] is False
    assert json.loads((export_directory(rig, retried) / "delivery-v2.json").read_text())["archives"][rig.archive.source_key] == {}


def test_a_view_of_a_study_now_filed_under_another_patient_stores_nothing(rig):
    rig.archive.patient_id = "MERGED-PATIENT"
    response = rig.http.post(rig.view, files={"image": ("view.png", PNG, "image/png")}, data={"reference_image_id": rig.revision["reference_image_id"]})
    job = run(rig, response)
    assert job["kind"] == "archive_view_export" and job["status"] == "failed"
    assert job["error"] == "The archive files the original study under a different patient now. Nothing was stored."
    assert rig.archive.stores == []


def test_a_dicomweb_case_saves_its_files_on_this_machine_and_archives_the_same_seg(rig):
    job = run(rig, rig.http.post(rig.seg, json={"delivery": "file"}))
    assert (job["kind"], job["status"]) == ("orthanc_export", "completed"), job["error"]
    download = rig.http.get(f"{rig.seg}/{job['result']['dicom_seg']['export_id']}/segment.dcm")
    saved = pydicom.dcmread(BytesIO(download.content))
    assert download.status_code == 200 and saved.StudyInstanceUID == STUDY_UID
    assert rig.http.get(f"{rig.view}-file").json()["available"] is True
    assert rig.archive.stores == []
    # Archiving the same masks stores the instance already saved as a file, not a second SEG.
    assert run(rig, rig.http.post(rig.seg, json={}))["status"] == "completed"
    assert rig.archive.stores == [str(saved.SOPInstanceUID)]


def test_a_dicomweb_case_sends_the_seg_it_archives_straight_to_a_node(rig, tmp_path):
    node = Node()
    listed = tmp_path / "destinations.json"
    listed.write_text(json.dumps([{"id": "pacs", "ae_title": "RECEIVER", "host": "127.0.0.1", "port": node.port}]))
    rig.service.settings = dataclasses.replace(rig.service.settings, dicom_send=DicomSendSettings(destinations_file=str(listed)))
    try:
        assert rig.http.get(rig.seg).json()["direct"]["available"] is True
        sent = run(rig, rig.http.post(rig.seg, json={"delivery": "direct", "destination": "pacs"}))
        assert (sent["kind"], sent["status"]) == ("direct_seg_export", "completed"), sent["error"]
        assert rig.archive.stores == [] and run(rig, rig.http.post(rig.seg, json={}))["status"] == "completed"
        assert [sop for sop, _syntax in node.received] == rig.archive.stores
    finally:
        node.server.shutdown()
