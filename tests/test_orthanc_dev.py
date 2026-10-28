"""Guard the isolated Orthanc launcher without a server, network, or case library."""

import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest

from scripts import orthanc_dev


def test_configuration_is_private_loopback_only_and_reuses_credentials(tmp_path):
    config = orthanc_dev.configure(tmp_path)
    before = config.read_bytes()
    settings = json.loads(before)
    assert settings["HttpBindAddresses"] == ["127.0.0.1"]
    assert settings["HttpPort"] == 8043
    assert settings["AuthenticationEnabled"] is True
    assert settings["RemoteAccessAllowed"] is False
    assert settings["DicomServerEnabled"] is False
    assert settings["OverwriteInstances"] is False
    assert len(settings["RegisteredUsers"]["scan3d-dev"]) >= 32
    for name in ["StorageDirectory", "IndexDirectory", "TemporaryDirectory"]:
        assert Path(settings[name]).is_relative_to(tmp_path)
    credentials = tmp_path / "connection.env"
    assert stat.S_IMODE(credentials.stat().st_mode) == 0o600
    assert stat.S_IMODE(config.stat().st_mode) == 0o600
    assert "SCAN3D_ORTHANC_URL=http://127.0.0.1:8043" in credentials.read_text()
    assert orthanc_dev.configure(tmp_path) == config
    assert config.read_bytes() == before


@pytest.mark.parametrize(
    ("key", "value"),
    [("HttpBindAddresses", ["0.0.0.0"]), ("DicomServerEnabled", True), ("StorageDirectory", "/unrelated-library")],
)
def test_modified_configuration_is_not_started_or_overwritten(tmp_path, key, value):
    config = orthanc_dev.configure(tmp_path)
    settings = json.loads(config.read_text())
    settings[key] = value
    config.write_text(json.dumps(settings))
    before = config.read_bytes()
    with pytest.raises(RuntimeError, match="isolated loopback"):
        orthanc_dev.configure(tmp_path)
    assert config.read_bytes() == before


def test_incomplete_configuration_keeps_existing_files(tmp_path):
    config = tmp_path / "orthanc.json"
    config.write_text("{}")
    with pytest.raises(RuntimeError, match="incomplete"):
        orthanc_dev.configure(tmp_path)
    assert config.read_text() == "{}"
    assert not (tmp_path / "connection.env").exists()


def test_bad_cached_binary_fails_before_execution(tmp_path):
    binary = tmp_path / "bin" / f"Orthanc-{orthanc_dev.VERSION}"
    binary.parent.mkdir()
    marker = tmp_path / "executed"
    binary.write_text(f"#!/usr/bin/env bash\ntouch {marker}\n")
    binary.chmod(0o700)
    before = hashlib.sha256(binary.read_bytes()).hexdigest()
    with pytest.raises(RuntimeError, match="pinned release"):
        orthanc_dev.install(tmp_path)
    assert hashlib.sha256(binary.read_bytes()).hexdigest() == before
    assert not marker.exists()


def test_stop_refuses_an_unrelated_live_pid(tmp_path, monkeypatch):
    pid = os.getpid()
    started = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    (tmp_path / "process.json").write_text(json.dumps({"pid": pid, "started": started}))
    signals = []
    monkeypatch.setattr(orthanc_dev.os, "kill", lambda *args: signals.append(args))
    with pytest.raises(RuntimeError, match="does not belong"):
        orthanc_dev.stop(tmp_path)
    assert signals == []


def test_cli_rejects_unowned_storage_before_reading_it(tmp_path):
    result = subprocess.run(
        [sys.executable, str(Path(orthanc_dev.__file__)), "status", "--directory", str(tmp_path)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "owned .runtime/orthanc-dev" in result.stderr
    assert not list(tmp_path.iterdir())
