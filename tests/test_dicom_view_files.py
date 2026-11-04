"""A 3D view of any CT prepared from DICOM becomes a DICOM file, with no Orthanc in reach."""

from io import BytesIO

import pydicom

from test_dicom_seg import fixture  # noqa: F401
from test_dicom_seg_file_export import execute, rig  # noqa: F401
from test_orthanc_view_exports import PNG

SECONDARY_CAPTURE = "1.2.840.10008.5.1.4.1.1.7"


def endpoint(rig):
    return f"/api/cases/{rig.case['id']}/revisions/{rig.saved['id']}/dicom-view-file"


def post(rig):
    data = {"reference_image_id": rig.saved["reference_image_id"]}
    response = rig.http.post(endpoint(rig), files={"image": ("view.png", PNG, "image/png")}, data=data)
    assert response.status_code == 200, response.text
    return response.json()


def test_folder_case_saves_its_view_as_a_file_without_orthanc(rig):
    assert rig.http.get(endpoint(rig)).json()["available"] is True
    job = post(rig)
    assert job["kind"] == "view_file_export" and post(rig)["id"] == job["id"]
    finished = execute(rig, job)
    assert finished["status"] == "completed" and finished["stage"] == "DICOM view file ready"
    result = finished["result"]["dicom_view"]
    assert result["file"] is True and result["archived"] is False and result["sent"] is False
    response = rig.http.get(f"{endpoint(rig)}/{result['export_id']}/view.dcm")
    assert response.status_code == 200 and response.headers["content-type"] == "application/dicom"
    assert response.headers["cache-control"] == "no-store" and int(response.headers["content-length"]) == len(response.content)
    view, source = pydicom.dcmread(BytesIO(response.content)), rig.fixture["images"][0]
    assert view.SOPClassUID == SECONDARY_CAPTURE
    assert view.StudyInstanceUID == source.StudyInstanceUID and view.PatientID == source.PatientID
    assert rig.http.get(endpoint(rig)).json()["exports"][0]["id"] == job["id"]
