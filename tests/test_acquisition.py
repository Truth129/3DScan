"""Acquisition labels preserve selected CT data and explicit task selection."""

from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest

from scan3d import dicom, segmentation
from scan3d.acquisition import CARDIAC_PHASE_IDS, PROFILE_IDS, acquisition_labels
from scan3d.api import create_app
from scan3d.api_models import Process
from scan3d.config import Settings
from scan3d.segmentation_options import normalize_options


@pytest.fixture
def acquisition_case(tmp_path, monkeypatch):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "web"))
    service = app.state.service
    case = service.new_case("Acquisition metadata fixture")
    source = tmp_path / "source.dcm"
    source.write_bytes(b"synthetic original DICOM")
    values = np.arange(7 * 9 * 11, dtype=np.int16).reshape((7, 9, 11)) - 700
    affine = np.array([[-0.7, 0.1, 0, 31], [0, 1.2, 0.2, -42], [0, 0, 2.1, 88], [0, 0, 0, 1.0]])
    case["series"] = [
        dict(
            uid="selected-series",
            description="Synthetic cardiac reconstruction",
            modality="CT",
            slice_count=11,
            spacing=[0.7, 1.2, 2.1],
            warnings=[],
            files=[str(source)],
        )
    ]
    service.store.save("cases", case)
    calls = []

    def convert(files, directory):
        calls.append(list(files))
        image = nib.Nifti1Image(values, affine)
        image.header.set_xyzt_units("mm")
        path = Path(directory) / "reference.nii.gz"
        nib.save(image, path)
        return path

    monkeypatch.setattr(dicom, "convert_series", convert)
    monkeypatch.setattr(segmentation, "segment", lambda *args, **kwargs: pytest.fail("Acquisition labels ran inference"))
    monkeypatch.setattr(segmentation, "_device_info", lambda *args, **kwargs: pytest.fail("Acquisition labels initialized GPU"))
    http = TestClient(app, base_url="http://127.0.0.1")
    yield http, service, case, source, values, affine, calls
    http.close()


@pytest.mark.parametrize("profile", PROFILE_IDS)
def test_all_studies_preserve_selected_ct_without_inference(acquisition_case, profile):
    http, service, case, source, values, affine, calls = acquisition_case
    before = source.read_bytes()
    body = dict(series_uid="selected-series", profile=profile, phase="coronary_arterial")
    if profile in ("corcta", "cardiac"):
        body.update(cardiac_phase="best_diastolic", rr_percent=69)
    response = http.post(f"/api/cases/{case['id']}/process", json=body)
    assert response.status_code == 200, response.text
    queued = service.store.get("jobs", response.json()["id"])
    assert queued["payload"] == body
    claimed = service.store.claim()
    assert claimed["id"] == queued["id"]
    service.execute(claimed)
    completed = service.store.get("jobs", queued["id"])
    assert completed["status"] == "completed", completed.get("error")
    revision = service.store.revision(case["id"], completed["revision_id"])
    assert revision["acquisition"] == body and revision["structures"] == []
    image = nib.load(revision["ct_file"])
    np.testing.assert_array_equal(image.dataobj, values)
    np.testing.assert_allclose(image.affine, affine, atol=1e-5)
    assert calls == [[str(source)]] and source.read_bytes() == before
    assert http.get(f"/api/cases/{case['id']}/revisions/{revision['id']}").json()["acquisition"] == body
    # Region labels cannot add, remove or change explicitly requested tasks.
    assert normalize_options(profile, {})["tasks"] == ["total"]
    assert normalize_options(profile, {"tasks": ["lung_vessels"]})["tasks"] == ["lung_vessels"]


@pytest.mark.parametrize("rr_percent", [0, 69, 69.5, 100])
@pytest.mark.parametrize("cardiac_phase", CARDIAC_PHASE_IDS)
def test_cardiac_timing_is_independent_of_contrast_timing(rr_percent, cardiac_phase):
    result = acquisition_labels("series", "corcta", "noncontrast", cardiac_phase=cardiac_phase, rr_percent=rr_percent)
    assert result["phase"] == "noncontrast"
    assert result["cardiac_phase"] == cardiac_phase and result["rr_percent"] == rr_percent
    parsed = Process.model_validate(result)
    assert parsed.rr_percent == rr_percent


def test_legacy_calls_and_custom_contrast_labels_remain_compatible(acquisition_case):
    _, service, case, *_ = acquisition_case
    old = service.process(case["id"], "selected-series", "chest", "venous")
    assert old["payload"] == dict(series_uid="selected-series", profile="chest", phase="venous")
    assert acquisition_labels("s", "abdomen", "custom older phase")["phase"] == "custom older phase"
    assert normalize_options("chest")["tasks"] == ["total", "lung_vessels"]
    assert normalize_options("chest_abdomen")["tasks"] == ["total", "lung_vessels"]
    for profile in set(PROFILE_IDS) - {"chest", "chest_abdomen"}:
        assert normalize_options(profile)["tasks"] == ["total"]


@pytest.mark.parametrize(
    "extra",
    [
        {"rr_percent": -0.01},
        {"rr_percent": 100.01},
        {"rr_percent": True},
        {"rr_percent": "69"},
        {"cardiac_phase": "arterial"},
        {"profile": "not-a-study"},
        {"phase": " "},
        {"profile": "abdomen", "cardiac_phase": "unknown"},
        {"profile": "chest", "rr_percent": 69},
    ],
)
def test_invalid_metadata_is_rejected_before_enqueue(acquisition_case, extra):
    http, service, case, *_ = acquisition_case
    body = dict(series_uid="selected-series", profile="corcta", phase="coronary_arterial")
    body.update(extra)
    response = http.post(f"/api/cases/{case['id']}/process", json=body)
    assert response.status_code in (400, 422), response.text
    assert service.store.list("jobs") == []


@pytest.mark.parametrize("rr_percent", [float("nan"), float("inf"), -1, 101, True, "69"])
def test_direct_service_timing_validation_cannot_bypass_api_limits(rr_percent):
    with pytest.raises(ValueError, match="finite number"):
        acquisition_labels("s", "corcta", "arterial", rr_percent=rr_percent)


def test_series_ownership_is_unchanged_for_cardiac_metadata(acquisition_case):
    http, service, case, *_ = acquisition_case
    other = service.new_case("Other synthetic case")
    response = http.post(
        f"/api/cases/{other['id']}/process",
        json=dict(series_uid="selected-series", profile="corcta", phase="coronary_arterial", cardiac_phase="best_diastolic", rr_percent=69),
    )
    assert response.status_code == 400
    assert "series belonging" in response.json()["detail"]
    assert not service.store.list("jobs")


def test_cli_exposes_cardiac_labels_without_loading_model_runtime():
    result = subprocess.run([sys.executable, "-m", "scan3d", "process", "--help"], check=True, capture_output=True, text=True)
    for label in ("corcta", "cardiac", "whole_body", "--cardiac-phase", "--rr-percent"):
        assert label in result.stdout
