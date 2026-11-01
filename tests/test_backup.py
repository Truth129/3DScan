"""Backups snapshot the database first, copy case files, and update incrementally."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import time

import pytest

from scan3d.backup import backup
from scan3d.config import Settings
from scan3d.demo import create_demo
from scan3d.service import Service


@pytest.fixture
def library(tmp_path):
    service = Service(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    case = create_demo(service)
    data = service.settings.data_dir
    (data / "review-cache" / "volumes").mkdir(parents=True, exist_ok=True)
    (data / "review-cache" / "volumes" / "slab").write_bytes(b"rebuilt on demand")
    (data / "orthanc-staging").mkdir()
    (data / "orthanc-staging" / "transfer").write_bytes(b"an import in progress")
    (data / "worker.lock").touch()
    return service, case, data


def files(root):
    return {path.relative_to(root): path for path in root.rglob("*") if path.is_file()}


def test_backup_snapshots_database_and_case_files_and_skips_rebuildable_caches(library, tmp_path):
    service, case, data = library
    manifest = backup(data, tmp_path / "backups", log=lambda message: None)
    snapshot = tmp_path / "backups" / manifest["snapshot"]
    assert manifest["database"]["integrity_check"] == "ok" and manifest["previous"] is None
    copy = sqlite3.connect(snapshot / "scan3d.sqlite3")
    assert copy.execute("SELECT id FROM cases").fetchall() == [(case["id"],)]
    assert copy.execute("SELECT count(*) FROM revisions").fetchone()[0] == len(service.store.list("revisions"))
    originals, copies = files(data / "cases"), files(snapshot / "cases")
    assert originals and copies.keys() == originals.keys()
    assert all(copies[name].read_bytes() == originals[name].read_bytes() for name in originals)
    assert (data / "surface-cache").is_dir() and files(snapshot / "surface-cache").keys() == files(data / "surface-cache").keys()
    assert {"review-cache", "orthanc-staging", "worker.lock"} <= set(manifest["skipped"])
    assert not (snapshot / "review-cache").exists() and not (snapshot / "orthanc-staging").exists()
    assert json.loads((snapshot / "manifest.json").read_text()) == manifest


def test_later_backups_link_unchanged_files_and_never_link_live_data(library, tmp_path):
    _, case, data = library
    first = backup(data, tmp_path / "backups", log=lambda message: None)
    (data / "cases" / case["id"] / "notes.txt").write_text("added after the first backup")
    time.sleep(1.1)  # Snapshot names have one-second resolution.
    second = backup(data, tmp_path / "backups", log=lambda message: None)
    assert second["previous"] == first["snapshot"]
    assert second["files"]["copied_files"] == 1 and second["files"]["linked_files"] == first["files"]["copied_files"]
    old, new = tmp_path / "backups" / first["snapshot"], tmp_path / "backups" / second["snapshot"]
    for name, path in files(old / "cases").items():
        assert (new / "cases" / name).stat().st_ino == path.stat().st_ino
        assert (data / "cases" / name).stat().st_ino != path.stat().st_ino
    assert (new / "cases" / case["id"] / "notes.txt").read_text() == "added after the first backup"


def test_backup_refuses_nested_destination_and_leaves_no_partial_snapshot(library, tmp_path, monkeypatch):
    _, _, data = library
    with pytest.raises(ValueError, match="outside the data directory"):
        backup(data, data / "backups", log=lambda message: None)

    def full_disk(*args, **kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(shutil, "copy2", full_disk)
    with pytest.raises(OSError, match="No space"):
        backup(data, tmp_path / "backups", log=lambda message: None)
    assert list((tmp_path / "backups").iterdir()) == []


def test_backup_command_uses_the_configured_data_directory(library, tmp_path):
    _, case, data = library
    environment = dict(os.environ, SCAN3D_DATA_DIR=str(data), PYTHONPATH=str(Path(__file__).resolve().parents[1]))
    result = subprocess.run([sys.executable, "-m", "scan3d", "backup", str(tmp_path / "copies")], env=environment, check=True, capture_output=True, text=True)
    manifest = json.loads(result.stdout)
    assert manifest["data_dir"] == str(data) and (tmp_path / "copies" / manifest["snapshot"] / "cases" / case["id"]).is_dir()
