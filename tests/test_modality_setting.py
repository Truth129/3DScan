"""A deployment can offer fewer modalities than the installed adapters support."""

from fastapi.testclient import TestClient
import pytest

from scan3d.api import create_app
from scan3d.config import MODALITIES, Settings
from scan3d.modalities import _ADAPTERS

REASON = "MR images are not enabled on this workstation"


def test_modality_setting_defaults_to_every_adapter_and_rejects_unknown_names(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("SCAN3D_MODALITIES", raising=False)
    assert Settings.from_env().modalities == MODALITIES == tuple(modality.value for modality in _ADAPTERS)
    monkeypatch.setenv("SCAN3D_MODALITIES", " ct, MR ,ct")
    assert Settings.from_env().modalities == ("CT", "MR")
    monkeypatch.setenv("SCAN3D_MODALITIES", "CT")
    settings = Settings.from_env()
    assert settings.modalities == ("CT",) and settings.modality_unavailable("CT") is None
    assert settings.modality_unavailable("MR") == REASON
    for value in ("", "CT,XA"):
        monkeypatch.setenv("SCAN3D_MODALITIES", value)
        with pytest.raises(ValueError, match="SCAN3D_MODALITIES"):
            Settings.from_env()


def test_ct_only_deployment_shows_mr_series_as_unavailable_without_changing_records(tmp_path):
    def client(**options):
        return TestClient(create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", **options)), base_url="http://127.0.0.1")

    http = client(modalities=("CT",))
    service = http.app.state.service
    case = service.new_case("Mixed study")
    case.update(
        status="imported",
        series=[
            {"uid": "1.2.3", "modality": "CT", "supported": True, "unsupported_reason": None, "warnings": [], "files": ["/x/ct.dcm"]},
            {"uid": "1.2.4", "modality": "MR", "supported": True, "unsupported_reason": None, "warnings": [], "files": ["/x/mr.dcm"]},
        ],
    )
    service.store.save("cases", case)
    shown = http.get(f"/api/cases/{case['id']}").json()["series"]
    assert [(series["supported"], series["unsupported_reason"]) for series in shown] == [(True, None), (False, REASON)]
    assert all("files" not in series for series in shown)
    assert http.get("/api/cases").json()[0]["series"][1]["supported"] is False
    refused = http.post(f"/api/cases/{case['id']}/process", json={"series_uid": "1.2.4", "profile": "unknown", "phase": "unknown"})
    assert refused.status_code == 400 and refused.json()["detail"] == REASON
    assert service.store.list("jobs") == [] and service.store.get("cases", case["id"])["series"][1]["supported"] is True
    assert http.get("/api/health").json()["modalities"] == ["CT"]
    enabled = {entry["modality"]: entry["enabled"] for entry in http.get("/api/imaging-capabilities").json()["modalities"]}
    assert enabled == {"CT": True, "MR": False}
    # Offering MR later makes the same stored series available again.
    assert [series["supported"] for series in client().get(f"/api/cases/{case['id']}").json()["series"]] == [True, True]
