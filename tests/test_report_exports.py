"""A measurement report as a DICOM series: the page, the encoder and the file and Orthanc exports, on synthetic data."""

import hashlib
from io import BytesIO
import json
import zipfile

import numpy as np
from PIL import Image
import pydicom
import pytest

from scan3d import aorta_report, pulmonary_report, report_page, reports, spine_report
from scan3d.dicom_view import DicomViewError, ImageText, export_images, export_view
from scan3d.store import identifier, now
from test_dicom_seg import fixture  # noqa: F401
from test_dicom_seg_file_export import make_rig
from test_dicom_view import REVISION, noise, png_bytes, write_sources
from test_orthanc_exports import execute, rig  # noqa: F401
from test_orthanc_view_exports import view  # noqa: F401

CURVED = png_bytes(noise(40, 30, seed=7))


def raw(dissection=True):
    """What the worker hands back: ten landmarks measured with both lumens, and the sinotubular junction not placed."""
    measured = dict(diameter=3.2, diameter_pd=3.1, area=8.0, start=[0, 0, 0], end=[32, 0, 0], start_pd=[16, -15.5, 0], end_pd=[16, 15.5, 0])
    measured.update(diameter_tl=2.1, diameter_tl_pd=2.0, area_tl=3.3, diameter_fl=1.4, diameter_fl_pd=1.2, area_fl=1.5)
    stats = {"length": 8.2, "max_diameter": 3.6, "volume": 41.3, "volume_true_lumen": 30.1, "volume_false_lumen": 11.2}
    return {
        "contrast": {"aorta_median_hu": 310.0, "native": False},
        "dissection": dissection,
        "upstream": {"landmarks": {str(n): measured for n in range(1, 12)}, "section_stats": {name: stats for name, _ in aorta_report.SECTIONS}},
        "geometry": {str(n): {"centre": [16, 0, -n], "normal": [0, 0, 1], "outline": [[0, 0, 0]]} for n in range(1, 12) if n != 3},
        "missing": {},
        "centreline": [[16, 0, 0]],
        "warnings": ["Sinotub. junc. is empty!"],
    }


def pulmonary_raw():
    """What the worker hands back for the pulmonary report: four landmarks measured, and the bifurcation not placed."""
    measured = dict(diameter=2.6, diameter_pd=2.4, area=5.1, start=[0, 0, 0], end=[26, 0, 0], start_pd=[13, -12, 0], end_pd=[13, 12, 0])
    return {
        "upstream": {"landmarks": {str(n): measured for n in range(1, 6)}},
        "geometry": {str(n): {"centre": [13, 0, -n], "normal": [0, 0, 1], "outline": [[0, 0, 0]]} for n in range(1, 6) if n != 3},
        "missing": {},
        "warnings": [],
    }


def spine_raw():
    """What the worker hands back for the spine report: L1 measured and flagged, L2 at the scan's edge."""
    upstream = {f"vertebrae_L1_height_{name}_mm": value for name, value in (("posterior", 25.46), ("middle", 23.0), ("anterior", 19.62))}
    upstream.update(
        vertebrae_L1_height_difference=0.23, vertebrae_L1_volume_ml=1.94, vertebrae_L1_intensity=141.2, vertebrae_L1_intensity_median=86.5, LWS_max=0.23
    )
    ends = {name: [[0, y, 0], [0, y, 20]] for name, y in (("posterior", -10), ("middle", 0), ("anterior", 10))}
    heights = {"posterior": 25.46, "middle": 23.0, "anterior": 19.62}
    geometry = {"centre": [0, 0, 10], "normal": [1, 0, 0], "ends": ends, "heights": heights, "outline": [[0, -15, 0]], "height_flag": True, "small_body": True}
    return {
        "upstream": upstream,
        "geometry": {"vertebrae_L1": geometry},
        "unavailable": {"vertebrae_L2": "Its body touches the edge of the scan"},
        "thresholds": {"native": 90.25, "arterial": 110.25, "portal_venous": 105.0},
        "warnings": [],
    }


def report_result(case_id="synthetic-case", revision_id=REVISION, key="ab" * 32, analysis_id="synthetic-analysis", dissection=True, report="aorta"):
    engine = {"name": "TotalSegmentator", "version": "2.18.0", "report_version": "0.1.2"}
    fields = {"format": reports.FORMAT, "notice": reports.NOTICE, "report": report, "analysis_id": analysis_id, "case_id": case_id}
    fields.update(revision_id=revision_id, key=key, created_at="2026-11-18T00:00:00+00:00", engine=engine, inputs=[])
    if report == "pulmonary":
        return {**fields, **pulmonary_report.shape(pulmonary_raw()), "files": [], "summary": {}}
    if report == "spine":
        return {**fields, **spine_report.shape(spine_raw()), "files": ["spine.png"], "summary": {}}
    return {**fields, **aorta_report.shape(raw(dissection)), "files": ["cpr.png"], "summary": {}}


def fake_report(service, case_id, revision_id, report="aorta"):
    """A complete run of this revision, as `reports.run` leaves it, with no worker; the aorta has a curved view and the spine a picture."""
    analysis_id, key = identifier(), hashlib.sha256(revision_id.encode()).hexdigest()
    folder = reports._folder(service, case_id, reports.definition(report), key)
    folder.mkdir(parents=True)
    files = {}
    if picture := {"aorta": "cpr.png", "spine": "spine.png"}.get(report):
        (folder / picture).write_bytes(CURVED)
        files[picture] = hashlib.sha256(CURVED).hexdigest()
    body = json.dumps(report_result(case_id, revision_id, key, analysis_id, report=report))
    (folder / "result.json").write_text(body)
    marker = {"format": reports.FORMAT, "key": key, "result_sha256": hashlib.sha256(body.encode()).hexdigest(), "files": files}
    (folder / "complete.json").write_text(json.dumps(marker))
    job = dict(id=analysis_id, case_id=case_id, kind="report", status="completed", stage="Complete", error=None, revision_id=revision_id, created_at=now())
    job.update(payload={"report": report, "key": key, "inputs": {}}, cancel_requested=False, result={"analysis_id": analysis_id, "key": key, "summary": {}})
    service.store.save("jobs", job)
    return analysis_id


def pixels(body):
    with Image.open(BytesIO(body)) as image:
        return np.asarray(image.convert("RGB"))


def test_two_pictures_become_one_series_in_order_and_the_view_keeps_its_text(tmp_path):
    sources = write_sources(tmp_path / "sources", 2)
    pngs = [tmp_path / "page.png", tmp_path / "curved.png"]
    for path, seed in zip(pngs, (1, 2)):
        path.write_bytes(png_bytes(noise(seed=seed)))
    text = ImageText(502, "3DScan aorta report abcdef01", "Measurement report export", "Synthetic comments.", "Synthetic derivation")
    result = export_images(sources, pngs, tmp_path / "export", revision_id=REVISION, text=text)
    images = [pydicom.dcmread(item.path) for item in result.images]
    source = pydicom.dcmread(sources[0], stop_before_pixels=True)
    assert [item.path.name for item in result.images] == ["view-0001.dcm", "view-0002.dcm"]
    assert {ds.SeriesInstanceUID for ds in images} == {result.series_instance_uid} and len({ds.SOPInstanceUID for ds in images}) == 2
    assert [ds.InstanceNumber for ds in images] == [1, 2] and len({(ds.SeriesDate, ds.SeriesTime) for ds in images}) == 1
    for ds, path in zip(images, pngs):
        assert ds.SeriesNumber == 502 and ds.SeriesDescription == text.series_description and ds.ManufacturerModelName == text.model_name
        assert ds.ImageComments == text.comments and ds.DerivationDescription == f"Synthetic derivation from source series {source.SeriesInstanceUID}"
        assert ds.StudyInstanceUID == source.StudyInstanceUID and ds.PatientID == source.PatientID
        np.testing.assert_array_equal(ds.pixel_array, pixels(path.read_bytes()))
    with pytest.raises(DicomViewError, match="one or two"):
        export_images(sources, pngs * 2, tmp_path / "three", revision_id=REVISION, text=text)
    single = pydicom.dcmread(export_view(sources, pngs[0], tmp_path / "view", revision_id=REVISION).path)
    assert (single.SeriesNumber, single.SeriesDescription, single.ManufacturerModelName) == (501, "3DScan view abcdef01", "Rendered view export")


def test_the_page_is_the_copied_table_drawn_the_same_every_time_and_names_no_case():
    result = report_result()
    page = report_page.render(result, "Aorta")
    assert report_page.render({**result, "case_id": "another-case", "analysis_id": "another"}, "Aorta") == page
    assert not any(chunk in page for chunk in (b"tEXt", b"iTXt", b"tIME"))
    with Image.open(BytesIO(page)) as image:
        assert image.mode == "RGB" and image.width == 1600
    head, rows, _ = report_page._landmarks(result)
    assert head == ["#", "Landmark", "Max (mm)", "Perp. (mm)", "Area (cm²)", "TL max (mm)", "FL max (mm)", "Note"]
    assert rows[0] == ["1", "Annulus", "32", "31", "8.0", "21", "14", ""]
    assert rows[2] == ["3", "Sinotubular junction", "", "", "", "", "", "Could not be placed on the centreline"]
    assert report_page._sections(result)[0][-2:] == ["TL volume (ml)", "FL volume (ml)"]
    plain = report_result(dissection=False)
    assert "TL max (mm)" not in report_page._landmarks(plain)[0] and len(report_page._sections(plain)[0]) == 4


def test_a_folder_case_saves_its_report_as_a_two_image_zip(tmp_path, fixture):  # noqa: F811
    folder = make_rig(tmp_path, fixture)
    case_id, revision_id = folder.case["id"], folder.saved["id"]
    analysis = fake_report(folder.service, case_id, revision_id)
    url = f"/api/cases/{case_id}/revisions/{revision_id}/reports/aorta/{analysis}/dicom-file"
    assert folder.http.get(url).json()["available"] is True
    job = folder.http.post(url, json={}).json()
    assert job["kind"] == "report_file_export" and folder.http.post(url, json={}).json()["id"] == job["id"]
    finished = execute(folder, job)
    assert finished["status"] == "completed" and finished["stage"] == "DICOM report file ready", finished["error"]
    result = finished["result"]["dicom_report"]
    assert (result["instance_count"], result["file"], result["archived"], result["sent"]) == (2, True, False, False)
    response = folder.http.get(f"{url}/{result['export_id']}/report.zip")
    assert response.status_code == 200 and response.headers["content-type"] == "application/zip"
    assert response.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(BytesIO(response.content)) as archive:
        assert [item.filename for item in archive.infolist()] == ["aorta-report-1.dcm", "aorta-report-2.dcm"]
        assert {item.compress_type for item in archive.infolist()} == {zipfile.ZIP_STORED}
        page, curved = (pydicom.dcmread(BytesIO(archive.read(name))) for name in archive.namelist())
    assert page.SeriesInstanceUID == curved.SeriesInstanceUID and [page.InstanceNumber, curved.InstanceNumber] == [1, 2]
    assert page.SeriesNumber == 502 and page.SeriesDescription == f"3DScan aorta report {revision_id[:8]}"
    assert page.ManufacturerModelName == "Measurement report export" and "do not measure" in page.ImageComments
    assert page.StudyInstanceUID == folder.fixture["images"][0].StudyInstanceUID and page.Columns == 1600
    np.testing.assert_array_equal(curved.pixel_array, pixels(CURVED))
    assert [item["id"] for item in folder.http.get(url).json()["exports"]] == [job["id"]]
    assert folder.http.get(f"/api/cases/{case_id}/revisions/{revision_id}/dicom-view-file").json()["exports"] == []
    # A changed file is not served, and an alias or a run that does not exist has nothing to export.
    exported = folder.service.report_file_exports._directory(case_id, result["export_id"]) / "dicom" / "view-0002.dcm"
    exported.write_bytes(exported.read_bytes()[:-2] + b"\0\0")
    assert folder.http.get(f"{url}/{result['export_id']}/report.zip").status_code == 404
    assert folder.http.get(url.replace(revision_id, f"review-{revision_id}")).status_code == 404
    assert folder.http.post(url.replace(analysis, identifier()), json={}).status_code == 404
    folder.http.close()


def test_a_pulmonary_report_saves_its_page_alone(tmp_path, fixture):  # noqa: F811
    result = report_result(report="pulmonary")
    assert len(result["landmarks"]) == 5 and [mark["available"] for mark in result["landmarks"]].count(False) == 1
    with Image.open(BytesIO(report_page.render(result, "Pulmonary arteries"))) as image:
        assert image.width == 1600
    folder = make_rig(tmp_path, fixture)
    case_id, revision_id = folder.case["id"], folder.saved["id"]
    analysis = fake_report(folder.service, case_id, revision_id, report="pulmonary")
    url = f"/api/cases/{case_id}/revisions/{revision_id}/reports/pulmonary/{analysis}/dicom-file"
    assert folder.http.get(url).json()["available"] is True
    finished = execute(folder, folder.http.post(url, json={}).json())
    assert finished["status"] == "completed", finished["error"]
    exported = finished["result"]["dicom_report"]
    assert exported["instance_count"] == 1
    with zipfile.ZipFile(BytesIO(folder.http.get(f"{url}/{exported['export_id']}/report.zip").content)) as archive:
        assert archive.namelist() == ["pulmonary-report-1.dcm"]
        page = pydicom.dcmread(BytesIO(archive.read("pulmonary-report-1.dcm")))
    assert page.SeriesDescription == f"3DScan pulmonary report {revision_id[:8]}" and page.Columns == 1600
    folder.http.close()


def test_orthanc_archives_and_sends_the_report_as_one_series(view):  # noqa: F811
    analysis = fake_report(view.service, view.case["id"], view.revision["id"])
    url = f"/api/cases/{view.case['id']}/revisions/{view.revision['id']}/reports/aorta/{analysis}/dicom"
    available = view.http.get(url).json()
    assert available["available"] is True and [item["id"] for item in available["destinations"]] == ["osirix"]
    job = view.http.post(url, json={"destination": "osirix"}).json()
    assert job["kind"] == "orthanc_report_export" and view.http.post(url, json={"destination": "osirix"}).json()["id"] == job["id"]
    finished = execute(view, job)
    assert finished["status"] == "completed" and finished["stage"] == "DICOM report sent", finished["error"]
    assert finished["result"]["dicom_report"] == {
        "export_id": finished["payload"]["export_id"],
        "encoding_version": 1,
        "archived": True,
        "sent": True,
        "instance_count": 2,
        "destination": "OsiriX",
        "destination_id": "osirix",
    }
    stored = [pydicom.dcmread(BytesIO(data)) for _, data in view.archive.blobs.values()]
    assert len(stored) == 2 and len({ds.SeriesInstanceUID for ds in stored}) == 1 and sorted(ds.InstanceNumber for ds in stored) == [1, 2]
    assert [len(ids) for _, ids, _ in view.archive.sends] == [2]
    # The 3D view's dialog lists its own exports only.
    assert view.http.get(view.url).json()["exports"] == []
    assert [item["id"] for item in view.http.get(url).json()["exports"]] == [job["id"]]


def test_a_spine_report_saves_its_page_and_picture(tmp_path, fixture):  # noqa: F811
    result = report_result(report="spine")
    head, rows, _ = report_page._vertebrae(result)
    assert head == ["Vertebra", "Posterior (mm)", "Middle (mm)", "Anterior (mm)", "Difference (%)", "Volume (ml)", "Median (HU)", "Note"]
    assert rows == [
        ["L1", "25.5", "23.0", "19.6", "23", "1.9", "87", "Height difference over 20%; Body under 2 ml"],
        ["L2", "", "", "", "", "", "", "Its body touches the edge of the scan"],
    ]
    assert (
        report_page._thresholds(result)
        == "TotalSegmentator reads L1 to L5 as low density under 90 HU without contrast, 110 HU arterial and 105 HU portal venous."
    )
    with Image.open(BytesIO(report_page.render(result, "Spine fractures"))) as image:
        assert image.width == 1600
    # A body with fewer than two heights has no difference.
    result["vertebrae"][0]["height_difference_percent"] = None
    assert report_page._vertebrae(result)[1][0][4] == ""
    folder = make_rig(tmp_path, fixture)
    case_id, revision_id = folder.case["id"], folder.saved["id"]
    analysis = fake_report(folder.service, case_id, revision_id, report="spine")
    url = f"/api/cases/{case_id}/revisions/{revision_id}/reports/spine/{analysis}/dicom-file"
    finished = execute(folder, folder.http.post(url, json={}).json())
    assert finished["status"] == "completed", finished["error"]
    exported = finished["result"]["dicom_report"]
    with zipfile.ZipFile(BytesIO(folder.http.get(f"{url}/{exported['export_id']}/report.zip").content)) as archive:
        assert archive.namelist() == ["spine-report-1.dcm", "spine-report-2.dcm"]
        page, picture = (pydicom.dcmread(BytesIO(archive.read(name))) for name in archive.namelist())
    assert page.SeriesDescription == f"3DScan spine report {revision_id[:8]}" and page.Columns == 1600
    np.testing.assert_array_equal(picture.pixel_array, pixels(CURVED))
    folder.http.close()
