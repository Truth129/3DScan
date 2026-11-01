"""GLB transport reuses exact gzip bytes while retaining raw and conditional downloads."""

from concurrent.futures import ThreadPoolExecutor
import gzip
import json
import os
from pathlib import Path
import time
import zlib

import pytest

from scan3d import api_viewer, compressed_files
from scan3d.surfaces import surface_asset_path
from test_api import client


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "model.glb"
    path.write_bytes(b"glTF" + bytes(range(256)) * 4096)
    return path


def _siblings(path):
    encoded = path.with_name(path.name + ".gz")
    return encoded, encoded.with_name(encoded.name + ".json")


def test_gzip_is_exact_deterministic_and_reused(source, monkeypatch):
    original = source.read_bytes()
    encoded = compressed_files.cached_gzip(source)
    assert gzip.decompress(encoded.read_bytes()) == original
    assert encoded.read_bytes()[3] == 0  # No original filename or optional header.
    assert encoded.read_bytes()[4:8] == b"\x00" * 4
    duplicate = source.with_name("other.glb")
    duplicate.write_bytes(original)
    assert compressed_files.cached_gzip(duplicate).read_bytes() == encoded.read_bytes()

    def unexpected_compression(*args, **kwargs):
        raise AssertionError("A warm cache must not read or recompress the source")

    monkeypatch.setattr(compressed_files.gzip, "GzipFile", unexpected_compression)
    assert compressed_files.cached_gzip(source) == encoded
    assert source.read_bytes() == original


def test_concurrent_cold_requests_compress_once(source, monkeypatch):
    original = compressed_files.gzip.GzipFile
    calls = []

    def observed(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(compressed_files.gzip, "GzipFile", observed)
    with ThreadPoolExecutor(max_workers=8) as pool:
        paths = list(pool.map(lambda _: compressed_files.cached_gzip(source), range(8)))
    assert len(calls) == 1
    assert len(set(paths)) == 1 and paths[0] is not None
    assert gzip.decompress(paths[0].read_bytes()) == source.read_bytes()


def _rewrite_keeping_size_and_mtime(path, content):
    """Leave only ctime to show the change, as a later edit would.

    Linux before 6.13 stamps files once per clock tick, so a rewrite straight after
    creation can keep the same ctime. Repeat until the clock has moved on.
    """
    previous = path.stat()
    path.write_bytes(content)
    while True:
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns))
        if path.stat().st_ctime_ns != previous.st_ctime_ns:
            return
        time.sleep(0.001)


@pytest.mark.parametrize("damage", ["missing_metadata", "malformed_metadata", "missing_gzip", "damaged_gzip", "source_changed"])
def test_incomplete_or_changed_entries_rebuild(source, damage):
    encoded = compressed_files.cached_gzip(source)
    _, metadata = _siblings(source)
    if damage == "missing_metadata":
        metadata.unlink()
    elif damage == "malformed_metadata":
        metadata.write_text("{")
    elif damage == "missing_gzip":
        encoded.unlink()
    elif damage == "damaged_gzip":
        _rewrite_keeping_size_and_mtime(encoded, b"x" * encoded.stat().st_size)
    else:
        _rewrite_keeping_size_and_mtime(source, b"new!" + source.read_bytes()[4:])
    assert compressed_files.cached_gzip(source) == encoded
    assert gzip.decompress(encoded.read_bytes()) == source.read_bytes()
    assert isinstance(json.loads(metadata.read_text()), dict)
    assert not list(source.parent.glob("*.writing"))


def test_failed_compression_preserves_source_and_published_cache(source, monkeypatch):
    encoded = compressed_files.cached_gzip(source)
    previous = encoded.read_bytes()
    source.write_bytes(source.read_bytes() + b"changed")
    current = source.read_bytes()

    def failed_copy(*args, **kwargs):
        raise OSError("Synthetic full cache")

    monkeypatch.setattr(compressed_files.shutil, "copyfileobj", failed_copy)
    assert compressed_files.cached_gzip(source) is None
    assert source.read_bytes() == current
    assert encoded.read_bytes() == previous
    assert not list(source.parent.glob("*.writing"))


def test_source_change_during_compression_cannot_publish_stale_bytes(source, monkeypatch):
    original_copy = compressed_files.shutil.copyfileobj

    def changing_copy(raw, encoded, length):
        original_copy(raw, encoded, length)
        source.write_bytes(b"glTF" + b"new source" * 1000)

    monkeypatch.setattr(compressed_files.shutil, "copyfileobj", changing_copy)
    assert compressed_files.cached_gzip(source) is None
    assert not _siblings(source)[0].exists()
    assert not list(source.parent.glob("*.writing"))


@pytest.fixture
def glb_case(client):
    http, service = client
    case = http.post("/api/cases/demo").json()
    revision = service.store.revision(case["id"], case["active_revision_id"])
    prefix = f"/api/cases/{case['id']}/revisions/{revision['id']}"
    manifest = http.get(f"{prefix}/model-manifest").json()
    surface = manifest["structures"][0]
    model = Path(revision["directory"]) / "model.glb"
    assert _siblings(model)[0].is_file()
    assert _siblings(surface_asset_path(service.surface_cache, surface["key"]))[0].is_file()
    return http, service, prefix, model, surface


def test_all_glb_routes_reuse_single_gzip_layer_and_preserve_raw(glb_case, monkeypatch):
    http, service, prefix, model, surface = glb_case
    detailed = model.with_name("detailed.glb")
    urls = [
        (f"{prefix}/model.glb", model),
        (f"{prefix}/model.glb?detail=true", detailed),
        (surface["url"], surface_asset_path(service.surface_cache, surface["key"])),
    ]
    for url, path in urls:
        response = http.get(url, headers={"accept-encoding": "gzip"})
        assert response.status_code == 200, response.text[:200]
        assert response.content == path.read_bytes()
        assert response.headers["content-type"] == "model/gltf-binary"
        assert response.headers["content-encoding"] == "gzip"
        assert "accept-encoding" in response.headers["vary"].lower()
        assert int(response.headers["content-length"]) == _siblings(path)[0].stat().st_size

    def unexpected_compression(*args, **kwargs):
        raise AssertionError("HTTP retries must reuse precompressed bytes")

    monkeypatch.setattr(compressed_files.gzip, "GzipFile", unexpected_compression)
    for url, path in urls:
        with http.stream("GET", url, headers={"accept-encoding": "gzip"}) as response:
            encoded = b"".join(response.iter_raw())
            # GzipFile above is deliberately unavailable during these retries.
            assert zlib.decompress(encoded, 16 + zlib.MAX_WBITS) == path.read_bytes()
        raw = http.get(url, headers={"accept-encoding": "identity"})
        assert raw.status_code == 200 and "content-encoding" not in raw.headers
        assert raw.content == path.read_bytes()
        assert "accept-encoding" in raw.headers["vary"].lower()
    assert http.get(surface["url"]).headers["cache-control"] == "private, no-cache"


def test_glb_negotiates_encoding_qualities_and_explicit_refusals(glb_case):
    http, _, prefix, model, _ = glb_case
    url = f"{prefix}/model.glb"
    for accept in ("gzip", "GZip", "br, gzip;q=0.4", "*", "gzip;q=0.6, identity;q=0.1", "*;q=0, gzip;q=1"):
        response = http.get(url, headers={"accept-encoding": accept})
        assert response.status_code == 200 and response.headers.get("content-encoding") == "gzip", accept
        assert response.content == model.read_bytes()
    for accept in ("", "identity", "gzip;q=0", "gzip;q=0, *;q=1", "gzip;q=0.4, identity;q=0.8"):
        response = http.get(url, headers={"accept-encoding": accept})
        assert response.status_code == 200 and "content-encoding" not in response.headers, accept
        assert response.content == model.read_bytes()
    request = http.build_request("GET", url)
    del request.headers["accept-encoding"]
    response = http.send(request)
    assert response.status_code == 200 and "content-encoding" not in response.headers
    for accept in ("gzip;q=0, identity;q=0", "*;q=0", "identity;q=0"):
        response = http.get(url, headers={"accept-encoding": accept})
        assert response.status_code == 406, accept
        assert "accept-encoding" in response.headers["vary"].lower()


def test_glb_validators_belong_to_selected_representation(glb_case):
    http, _, _, _, surface = glb_case
    raw = http.get(surface["url"], headers={"accept-encoding": "identity"})
    encoded = http.get(surface["url"], headers={"accept-encoding": "gzip"})
    assert raw.headers["etag"] != encoded.headers["etag"]
    for accept, original in (("identity", raw), ("gzip", encoded)):
        headers = {"accept-encoding": accept, "if-none-match": original.headers["etag"]}
        response = http.get(surface["url"], headers=headers)
        assert response.status_code == 304 and not response.content
        assert response.headers["etag"] == original.headers["etag"]
        assert response.headers["cache-control"] == "private, no-cache"
        assert "accept-encoding" in response.headers["vary"].lower()
        assert "content-length" not in response.headers
        assert http.get(surface["url"], headers={**headers, "if-none-match": f"W/{original.headers['etag']}"}).status_code == 304
        assert http.get(surface["url"], headers={"accept-encoding": accept, "if-none-match": "*"}).status_code == 304
        modified = {"accept-encoding": accept, "if-modified-since": original.headers["last-modified"]}
        assert http.get(surface["url"], headers=modified).status_code == 304
        assert http.get(surface["url"], headers={**modified, "if-none-match": '"different"'}).status_code == 200
    response = http.get(surface["url"], headers={"accept-encoding": "gzip", "if-none-match": raw.headers["etag"]})
    assert response.status_code == 200 and response.content == raw.content


def test_glb_ranges_keep_raw_offsets_and_if_range_semantics(glb_case):
    http, _, prefix, model, _ = glb_case
    url = f"{prefix}/model.glb"
    raw = http.get(url, headers={"accept-encoding": "identity"})
    encoded = http.get(url, headers={"accept-encoding": "gzip"})
    headers = {"accept-encoding": "gzip", "range": "bytes=0-15"}
    response = http.get(url, headers=headers)
    assert response.status_code == 206 and "content-encoding" not in response.headers
    assert response.content == model.read_bytes()[:16]
    assert response.headers["content-range"] == f"bytes 0-15/{model.stat().st_size}"
    assert "accept-encoding" in response.headers["vary"].lower()
    assert http.get(url, headers={**headers, "if-range": raw.headers["etag"]}).status_code == 206
    response = http.get(url, headers={**headers, "if-range": encoded.headers["etag"]})
    assert response.status_code == 200 and response.content == model.read_bytes()
    assert "content-encoding" not in response.headers
    assert http.get(url, headers={**headers, "range": f"bytes={model.stat().st_size}-"}).status_code == 416
    assert http.get(url, headers={**headers, "accept-encoding": "gzip, identity;q=0"}).status_code == 406


def test_old_assets_fill_cache_once_and_cache_failure_serves_raw(glb_case, monkeypatch):
    http, _, prefix, model, _ = glb_case
    url = f"{prefix}/model.glb"
    for path in _siblings(model):
        path.unlink(missing_ok=True)
    response = http.get(url, headers={"accept-encoding": "gzip"})
    assert response.status_code == 200 and response.headers["content-encoding"] == "gzip"
    assert response.content == model.read_bytes()
    for path in _siblings(model):
        path.unlink()

    def unavailable(*args, **kwargs):
        raise OSError("Synthetic read-only cache")

    monkeypatch.setattr(compressed_files.gzip, "GzipFile", unavailable)
    response = http.get(url, headers={"accept-encoding": "gzip"})
    assert response.status_code == 200 and "content-encoding" not in response.headers
    assert response.content == model.read_bytes()
    assert "accept-encoding" in response.headers["vary"].lower()
    assert http.get(url, headers={"accept-encoding": "gzip, identity;q=0"}).status_code == 406


def test_unknown_surface_is_rejected_before_compression(glb_case, monkeypatch):
    http, _, prefix, _, _ = glb_case

    def unexpected(*args, **kwargs):
        raise AssertionError("Unknown revision assets must not reach the gzip cache")

    monkeypatch.setattr(api_viewer, "cached_gzip", unexpected)
    assert http.get(f"{prefix}/surfaces/{'0' * 64}.glb").status_code == 404
    candidate = prefix.rsplit("/", 1)[0] + "/review-unknown"
    assert http.get(f"{candidate}/model.glb").status_code == 404
