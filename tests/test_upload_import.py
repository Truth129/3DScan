"""Browser imports arrive in bounded batches and publish a case only when complete.

DICOM indexing is a deterministic double; these tests exercise the staged
transport, its idempotent retries and its limits through the HTTP API.
"""

import fcntl
import io
import json
import os
from pathlib import Path
import time
from types import SimpleNamespace
import zipfile

from fastapi.testclient import TestClient
import pytest

from scan3d import dicom
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.uploads import MAX_PARTS


@pytest.fixture
def rig(tmp_path, monkeypatch):
    indexed, failure = [], {}

    def index(source):
        if failure:
            raise ValueError(failure["message"])
        indexed.append(Path(source))
        files = sorted(str(path) for path in Path(source).rglob("*") if path.is_file())
        return [
            {
                "uid": "fixture-series",
                "description": "Synthetic series",
                "modality": "CT",
                "slice_count": len(files),
                "spacing": [1, 1, 1],
                "warnings": [],
                "files": files,
            }
        ]

    monkeypatch.setattr(dicom, "index_dicom", index)
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", upload_limit=1024**2)
    http = TestClient(create_app(settings), base_url="http://127.0.0.1")
    yield SimpleNamespace(http=http, service=http.app.state.service, settings=settings, indexed=indexed, failure=failure)
    http.close()


def start(rig, total_files, total_bytes, label=None):
    response = rig.http.post("/api/imports", json={"label": label, "total_files": total_files, "total_bytes": total_bytes})
    assert response.status_code == 200, response.text
    return response.json()


def send(rig, upload, pieces):
    """Post (path, offset, file size, bytes) pieces as one multipart batch."""
    manifest = json.dumps([{"path": path, "offset": offset, "size": size} for path, offset, size, _ in pieces])
    files = [("files", (f"part-{index}", data, "application/octet-stream")) for index, (*_, data) in enumerate(pieces)]
    return rig.http.post(f"/api/imports/{upload['id']}/parts", data={"manifest": manifest}, files=files)


def finish(rig, upload):
    return rig.http.post(f"/api/imports/{upload['id']}/finish")


def staged(rig, upload):
    return rig.settings.data_dir / "import-uploads" / upload["id"]


def test_folder_import_accepts_more_files_than_one_multipart_form(rig):
    contents = {f"Study/Series 1/IM{index:05d}.dcm": f"slice {index}".encode() for index in range(1001)}
    upload = start(rig, len(contents), sum(map(len, contents.values())), label="Batched")
    assert upload["max_parts"] == MAX_PARTS < 1000
    names = list(contents)
    for first in range(0, len(names), upload["max_parts"]):
        response = send(rig, upload, [(name, 0, len(contents[name]), contents[name]) for name in names[first : first + upload["max_parts"]]])
        assert response.status_code == 200, response.text
    assert response.json()["received_files"] == 1001
    assert rig.service.store.list("cases") == []
    response = finish(rig, upload)
    assert response.status_code == 200, response.text
    case = response.json()
    assert case["status"] == "imported" and case["label"] == "Batched"
    assert "files" not in case["series"][0]
    source = rig.service.store.case_dir(case["id"]) / "dicom"
    assert rig.indexed == [source]
    assert {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()} == contents
    assert not (staged(rig, upload) / "files").exists()


def test_large_files_arrive_as_ordered_ranges_and_retries_change_nothing(rig):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as z:
        z.writestr("series/IM1.dcm", b"first slice")
        z.writestr("series/IM2.dcm", b"second slice")
    data = archive.getvalue()
    third = len(data) // 3
    upload = start(rig, 1, len(data))

    def piece(begin, end, content=None):
        return send(rig, upload, [("study.zip", begin, len(data), data[begin:end] if content is None else content)])

    assert piece(0, third).json()["received_bytes"] == third
    assert piece(0, third).json()["received_bytes"] == third
    assert piece(2 * third, len(data)).status_code == 409
    altered = piece(0, third, b"x" * third)
    assert altered.status_code == 400 and altered.json()["detail"] == "Duplicate upload filename"
    assert piece(third, 2 * third).status_code == 200
    assert piece(2 * third, len(data)).json()["received_files"] == 1
    retried = piece(2 * third, len(data))
    assert retried.status_code == 200 and retried.json()["received_bytes"] == len(data)
    case = finish(rig, upload).json()
    source = rig.service.store.case_dir(case["id"]) / "dicom"
    assert (source / "study.zip").read_bytes() == data
    assert (source / "study_expanded" / "series" / "IM2.dcm").read_bytes() == b"second slice"


def test_imports_reject_unsafe_duplicate_undeclared_and_oversized_files(rig):
    assert rig.http.post("/api/imports", json={"total_files": 1, "total_bytes": rig.settings.upload_limit + 1}).status_code == 400
    assert rig.http.post("/api/imports", json={"total_files": 100_001, "total_bytes": 1}).status_code == 422
    upload = start(rig, 2, 8)
    assert send(rig, upload, [("../outside.dcm", 0, 4, b"evil")]).status_code == 400
    assert send(rig, upload, [("a/IM1", 0, 4, b"same")]).status_code == 200
    duplicate = send(rig, upload, [("a/IM1", 0, 4, b"diff")])
    assert duplicate.status_code == 400 and duplicate.json()["detail"] == "Duplicate upload filename"
    larger = send(rig, upload, [("a/IM2", 0, 5, b"12345")])
    assert larger.status_code == 400 and "larger than its declared size" in larger.json()["detail"]
    incomplete = finish(rig, upload)
    assert incomplete.status_code == 409 and "1 of 2 files" in incomplete.json()["detail"]
    nested = send(rig, upload, [("a/IM1/child", 0, 4, b"1234")])
    assert nested.status_code == 400 and "share a name" in nested.json()["detail"]
    extra = start(rig, 1, 8)
    assert send(rig, extra, [("IM1", 0, 4, b"1234")]).status_code == 200
    undeclared = send(rig, extra, [("IM2", 0, 4, b"5678")])
    assert undeclared.status_code == 400 and "more files than declared" in undeclared.json()["detail"]
    assert rig.service.store.list("cases") == []


def test_finish_repeats_safely_and_cancel_discards_staged_files(rig):
    upload = start(rig, 1, 4)
    assert send(rig, upload, [("IM1", 0, 4, b"data")]).status_code == 200
    first, second = finish(rig, upload), finish(rig, upload)
    assert first.status_code == second.status_code == 200
    assert first.json()["id"] == second.json()["id"]
    assert [case["id"] for case in rig.service.store.list("cases")] == [first.json()["id"]]
    assert send(rig, upload, [("IM1", 0, 4, b"data")]).status_code == 409
    assert rig.http.delete(f"/api/imports/{upload['id']}").status_code == 409

    abandoned = start(rig, 1, 4)
    assert send(rig, abandoned, [("IM1", 0, 4, b"da")]).status_code == 200
    assert staged(rig, abandoned).is_dir()
    assert rig.http.delete(f"/api/imports/{abandoned['id']}").json()["status"] == "cancelled"
    assert not staged(rig, abandoned).exists()
    assert send(rig, abandoned, [("IM1", 2, 4, b"ta")]).status_code == 404
    assert finish(rig, abandoned).status_code == 404

    rig.failure["message"] = "No supported DICOM image series found"
    failed = start(rig, 1, 4)
    assert send(rig, failed, [("notes.txt", 0, 4, b"text")]).status_code == 200
    for _ in range(2):
        response = finish(rig, failed)
        assert response.status_code == 400 and response.json()["detail"] == rig.failure["message"]
    case = next(case for case in rig.service.store.list("cases") if case["id"] != first.json()["id"])
    assert case["status"] == "import_failed" and case["error"] == rig.failure["message"]


def test_starting_an_import_discards_only_idle_staged_uploads(rig):
    idle, busy = start(rig, 1, 4), start(rig, 1, 4)
    yesterday = time.time() - 2 * 24 * 3600
    for upload in (idle, busy):
        os.utime(staged(rig, upload) / "state.json", (yesterday, yesterday))
    with (staged(rig, busy) / "lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        recent = start(rig, 1, 4)
    assert not staged(rig, idle).exists()
    assert staged(rig, busy).is_dir() and staged(rig, recent).is_dir()
