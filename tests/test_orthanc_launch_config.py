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
    ipv6 = configuration("http://127.0.0.1:18043", "http://[::1]:9033/")
    assert ipv6["OrthancExplorer2"]["UiOptions"]["CustomButtons"]["series"][0]["Url"].startswith("http://[::1]:9033/#")


def test_automatic_button_uses_same_archive_fragment_and_relative_launcher():
    from urllib.parse import urljoin

    result = configuration("http://127.0.0.1:8044", "https://scan.example.test:8443/", launcher_path="/3dscan-launch/index.html")
    button = result["OrthancExplorer2"]["UiOptions"]["CustomButtons"]["series"][0]
    absolute = configuration("http://127.0.0.1:8044", "https://scan.example.test:8443/")["OrthancExplorer2"]["UiOptions"]["CustomButtons"]["series"][0]
    assert button["Url"].startswith("../../3dscan-launch/index.html#orthanc-series={UUID}&orthanc-source=")
    assert urlsplit(button["Url"]).fragment == urlsplit(absolute["Url"]).fragment
    for origin in ("http://localhost:8044", "https://scan.example.test:8444"):
        for page in ("/ui/app/", "/ui/app/filtered-studies"):
            destination = urlsplit(urljoin(origin + page, button["Url"]))
            assert destination.netloc == urlsplit(origin).netloc
            assert destination.scheme == urlsplit(origin).scheme
            assert destination.path == "/3dscan-launch/index.html"


@pytest.mark.parametrize("path", ["//other-host/launcher.html", "/../orthanc.json", "/3dscan-launch/index.html?target=bad"])
def test_automatic_button_refuses_other_launcher_locations(path):
    with pytest.raises(ValueError, match="launcher path"):
        configuration("http://127.0.0.1:8044", "https://scan.example.test:8443/", launcher_path=path)


@pytest.mark.parametrize(
    "value",
    [
        "javascript:alert(1)",
        "https://user:secret@scan.example.test/",
        "https://scan.example.test/?token=secret",
        "https://scan.example.test/subpath/",
    ],
)
def test_rejects_unsafe_or_unsupported_app_urls(value):
    with pytest.raises(ValueError):
        configuration("http://127.0.0.1:8044", value)
    with pytest.raises(ValueError, match="without credentials"):
        configuration("http://user:secret@127.0.0.1:8044", "https://scan.example.test/")
