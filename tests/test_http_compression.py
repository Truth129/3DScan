"""Remote clients receive compressed JSON and viewer assets, never double-encoded data."""

from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from scan3d.api import create_app
from scan3d.config import Settings


@pytest.fixture
def http(tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>3DScan</title>" + "<!-- workspace -->" * 100)
    (dist / "assets" / "index-AbCdEf12.js").write_text("console.log('synthetic viewer bundle');\n" * 400)
    license_text = Path("web/public/licenses/Slicer-LICENSE.txt").read_text()
    (dist / "offline-viewer.js").write_text("/*! " + license_text + " */\nvar Scan3DOffline={initOffline(){}};")
    (dist / "offline-viewer.css").write_text("body{background:#111}")
    with TestClient(create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=dist)), base_url="http://127.0.0.1") as client:
        yield client


def test_json_and_viewer_assets_are_gzipped(http):
    case = http.post("/api/cases/demo").json()
    revision = http.get(f"/api/cases/{case['id']}/revisions/{case['active_revision_id']}")
    assert revision.headers["content-encoding"] == "gzip" and revision.json()["id"] == case["active_revision_id"]
    asset = http.get("/assets/index-AbCdEf12.js")
    assert asset.headers["content-encoding"] == "gzip" and "immutable" in asset.headers["cache-control"]
    assert asset.text == "console.log('synthetic viewer bundle');\n" * 400
    assert http.get("/").headers["content-encoding"] == "gzip"
    small = http.get("/api/health", headers={"accept-encoding": "identity"})
    assert "content-encoding" not in small.headers


def test_encoded_data_and_downloads_are_not_compressed_again(http):
    case = http.post("/api/cases/demo").json()
    prefix = f"/api/cases/{case['id']}/revisions/{case['active_revision_id']}"
    descriptor = http.get(f"{prefix}/ct-volume?quality=preview").json()
    chunk = http.get(descriptor["chunks"][0]["url"])
    # One gzip layer, decoded by the client to exactly the raw native slab.
    assert chunk.headers["content-encoding"] == "gzip"
    assert len(chunk.content) == descriptor["chunks"][0]["byte_length"]
    for url in (f"{prefix}/slicer.zip", f"{prefix}/export.html"):
        response = http.get(url)
        assert response.status_code == 200, (url, response.text[:200])
        assert "content-encoding" not in response.headers, url
    export = http.get(f"{prefix}/export.html")
    assert int(export.headers["content-length"]) == len(export.content)
