"""Explorer 2 launch config is an inert, credential-free fragment."""

import hashlib
from urllib.parse import urlsplit

import pytest

from scripts.orthanc_launch_config import configuration


def test_button_uses_configured_archive_identity_in_a_fragment_without_network(monkeypatch):
    import requests

    def no_network(*args, **kwargs):
        raise AssertionError("Generating the launch button must not contact an archive")

    monkeypatch.setattr(requests.Session, "request", no_network)
    result = configuration("HTTP://LOCALHOST:80/", "https://scan.example.test:8443")
    button = result["OrthancExplorer2"]["UiOptions"]["CustomButtons"]["series"][0]
    expected = hashlib.sha256(b"http://localhost").hexdigest()
    assert button == {
        "HttpMethod": "GET",
        "Id": "open-in-3dscan",
        "Tooltip": "Open in 3DScan",
        "Target": "_blank",
        "Icon": "bi bi-box",
        "Url": "https://scan.example.test:8443/#orthanc-series={UUID}&orthanc-source=" + expected,
    }
    assert urlsplit(button["Url"]).query == ""
    assert "localhost" not in button["Url"]


def test_browser_url_can_be_loopback_ipv6_for_synthetic_qualification():
    result = configuration("http://127.0.0.1:18043", "http://[::1]:9033/")
    assert result["OrthancExplorer2"]["UiOptions"]["CustomButtons"]["series"][0]["Url"].startswith("http://[::1]:9033/#")


@pytest.mark.parametrize(
    "value",
    [
        "/relative",
        "javascript:alert(1)",
        "https://user:secret@scan.example.test/",
        "https://scan.example.test/?token=secret",
        "https://scan.example.test/#case",
        "https://scan.example.test/subpath/",
        "https://scan.example.test:bad/",
        "https://scan.example.test/\n",
    ],
)
def test_rejects_unsafe_or_unsupported_app_urls(value):
    with pytest.raises(ValueError):
        configuration("http://127.0.0.1:8044", value)


def test_rejects_archive_credentials_in_url():
    with pytest.raises(ValueError, match="without credentials"):
        configuration("http://user:secret@127.0.0.1:8044", "https://scan.example.test/")
