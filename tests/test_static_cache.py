"""Mutable viewer entry points revalidate without changing clinical API policy."""

import pytest
from fastapi.testclient import TestClient

from scan3d.api import create_app
from scan3d.config import Settings


@pytest.fixture
def client(tmp_path):
    web = tmp_path / "dist"
    (web / "assets").mkdir(parents=True)
    for name, content in {
        "index.html": "<!doctype html><title>Viewer build</title>",
        "offline-viewer.js": "window.viewer = true;",
        "assets/index-Ab12_-xy.js": "console.log('hashed build');",
        "assets/reference.js": "console.log('mutable source');",
        "readme.txt": "ordinary static resource",
    }.items():
        (web / name).write_text(content)
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=web))
    return TestClient(app, base_url="http://127.0.0.1")


@pytest.mark.parametrize("path", ["/", "/offline-viewer.js"])
def test_viewer_entry_points_revalidate_including_conditional_responses(client, path):
    response = client.get(path)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    for headers in ({"If-None-Match": response.headers["etag"]}, {"If-Modified-Since": response.headers["last-modified"]}):
        conditional = client.get(path, headers=headers)
        assert conditional.status_code == 304
        assert conditional.headers["cache-control"] == "no-cache"
    head = client.head(path)
    assert head.status_code == 200 and not head.content
    assert head.headers["cache-control"] == "no-cache"


def test_content_hashed_build_assets_are_immutable(client):
    path = "/assets/index-Ab12_-xy.js"
    response = client.get(path)
    assert response.status_code == 200
    policy = "private, max-age=31536000, immutable"
    assert response.headers["cache-control"] == policy
    conditional = client.get(path, headers={"If-None-Match": response.headers["etag"]})
    assert conditional.status_code == 304
    assert conditional.headers["cache-control"] == policy


def test_other_static_files_remain_unmodified(client):
    for path in ("/assets/reference.js", "/readme.txt"):
        response = client.get(path)
        assert response.status_code == 200
        assert "cache-control" not in response.headers
    assert client.get("/assets/not-a-real-Ab12_-xy.js").status_code == 404
