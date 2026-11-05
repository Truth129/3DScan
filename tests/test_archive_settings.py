"""DICOMweb and direct send settings: off unless set, refused when unsafe, and never echoing a value."""

import json

import pytest

from scan3d.api import create_app
from scan3d.archive.settings import DicomSendSettings, DicomWebSettings
from scan3d.config import Settings

URL = "https://pacs.example.test/dicom-web"


def test_unset_is_off_and_a_full_setting_parses():
    assert DicomWebSettings.from_env({}) is None
    parsed = DicomWebSettings.from_env(
        {
            "SCAN3D_DICOMWEB_URL": URL,
            "SCAN3D_DICOMWEB_AUTH": "bearer",
            "SCAN3D_DICOMWEB_TOKEN_FILE": "/run/scan3d/token",
            "SCAN3D_DICOMWEB_ID": "site-pacs",
            "SCAN3D_DICOMWEB_QUIET_SECONDS": "0",
            "SCAN3D_DICOMWEB_WILDCARDS": "true",
            "SCAN3D_DICOMWEB_STORE": "false",
        },
        orthanc_url="http://127.0.0.1:8042",
    )
    assert (parsed.auth, parsed.archive_id, parsed.quiet_seconds, parsed.wildcards, parsed.store) == ("bearer", "site-pacs", 0, True, False)
    assert URL not in repr(parsed)


@pytest.mark.parametrize(
    "values, setting",
    [
        ({"SCAN3D_DICOMWEB_URL": "ftp://pacs.example.test/secret-path"}, "SCAN3D_DICOMWEB_URL"),
        ({"SCAN3D_DICOMWEB_URL": "https://user:secret-password@pacs.example.test"}, "SCAN3D_DICOMWEB_URL"),
        ({"SCAN3D_DICOMWEB_URL": "http://localhost:8042/dicom-web/"}, "SCAN3D_ORTHANC_URL"),
        ({"SCAN3D_DICOMWEB_URL": "http://pacs.example.test/secret-path", "SCAN3D_DICOMWEB_AUTH": "basic"}, "SCAN3D_DICOMWEB_AUTH"),
        ({"SCAN3D_DICOMWEB_URL": URL, "SCAN3D_DICOMWEB_AUTH": "bearer"}, "SCAN3D_DICOMWEB_TOKEN_FILE"),
        ({"SCAN3D_DICOMWEB_URL": URL, "SCAN3D_DICOMWEB_TOKEN_FILE": "/run/token"}, "SCAN3D_DICOMWEB_TOKEN_FILE"),
        ({"SCAN3D_DICOMWEB_URL": URL, "SCAN3D_DICOMWEB_ID": "has space"}, "SCAN3D_DICOMWEB_ID"),
        ({"SCAN3D_DICOMWEB_URL": URL, "SCAN3D_DICOMWEB_CA_BUNDLE": "relative/ca.pem"}, "SCAN3D_DICOMWEB_CA_BUNDLE"),
        ({"SCAN3D_DICOMWEB_URL": URL, "SCAN3D_DICOMWEB_QUIET_SECONDS": "7200"}, "SCAN3D_DICOMWEB_QUIET_SECONDS"),
    ],
)
def test_unsafe_or_malformed_settings_are_refused_by_name_alone(values, setting):
    with pytest.raises(ValueError, match=setting) as refused:
        DicomWebSettings.from_env(values, orthanc_url="http://localhost:8042")
    assert "secret" not in str(refused.value) and "pacs.example.test" not in str(refused.value)


def test_the_server_refuses_to_start_without_its_token_file(tmp_path):
    archive = DicomWebSettings(url=URL, auth="bearer", token_file=str(tmp_path / "missing-token"))
    with pytest.raises(ValueError, match="SCAN3D_DICOMWEB_TOKEN_FILE cannot be read"):
        create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", dicomweb=archive))


@pytest.mark.parametrize(
    "entries, refusal",
    [
        ([{"id": "pacs", "ae_title": "PACS", "host": "pacs.example.test"}], "needs id, ae_title, host and port"),
        ([{"id": "pacs", "ae_title": "PACS\\SECRET", "host": "pacs.example.test", "port": 104}], "ae_title must be a DICOM AE title"),
        ([{"id": "pacs", "ae_title": "PACS", "host": "pacs.example.test", "port": 104, "ca_bundle": "/etc/ca.pem"}], "only with tls"),
        ([{"id": "pacs", "ae_title": "PACS", "host": "pacs.example.test", "port": 104}] * 2, "one id twice"),
    ],
)
def test_a_destinations_file_is_read_and_refused_without_echoing_it(tmp_path, entries, refusal):
    listed = tmp_path / "destinations.json"
    assert DicomSendSettings.from_env({}) is None
    settings = DicomSendSettings.from_env({"SCAN3D_DICOM_DESTINATIONS_FILE": str(listed), "SCAN3D_DICOM_AE_TITLE": "WORKSTATION"})
    listed.write_text(json.dumps([{"id": "pacs", "name": "Site PACS", "ae_title": "PACS", "host": "PACS.example.test", "port": 104, "tls": True}]))
    (pacs,) = settings.destinations()
    assert (settings.ae_title, pacs.name, pacs.identity) == ("WORKSTATION", "Site PACS", "PACS@pacs.example.test:104+tls")
    listed.write_text(json.dumps(entries))
    with pytest.raises(ValueError, match=refusal) as refused:
        settings.destinations()
    assert "SECRET" not in str(refused.value) and "example.test" not in str(refused.value)
