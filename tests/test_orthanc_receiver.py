"""Prepare an incoming receiver without exposing sockets or reading archive data."""

import hashlib
import json
from pathlib import Path
import stat

import pytest

from scripts import orthanc_receiver


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    directory = tmp_path / "incoming"
    source = tmp_path / "development"
    binary = source / "bin" / f"Orthanc-{orthanc_receiver.runtime.VERSION}"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nprintf 'Orthanc 1.13.0\\n'\n")
    binary.chmod(0o700)
    monkeypatch.setattr(orthanc_receiver, "DIRECTORY", directory)
    monkeypatch.setattr(orthanc_receiver.runtime, "DEFAULT_DIRECTORY", source)
    monkeypatch.setattr(orthanc_receiver.runtime, "BINARY_SHA256", hashlib.sha256(binary.read_bytes()).hexdigest())
    return directory, binary


def test_preparation_is_private_and_only_accepts_configured_osirix_sender(receiver):
    directory, binary = receiver
    result = orthanc_receiver.prepare(directory, "192.168.20.30", "OSIRIX")
    config = directory / "orthanc.json"
    settings = json.loads(config.read_text())
    assert result["status"] == "prepared" and "4243" in result["activation_requires"]
    assert settings["HttpBindAddresses"] == ["127.0.0.1"] and settings["HttpPort"] == 8044
    assert settings["AuthenticationEnabled"] and not settings["RemoteAccessAllowed"]
    assert settings["DicomAet"] == "SCAN3D" and settings["DicomPort"] == 4243
    assert settings["DicomCheckCalledAet"] and settings["DicomCheckModalityHost"]
    assert settings["StrictAetComparison"] is True
    for action in ("Echo", "Store", "Find", "FindWorklist", "Get", "Move"):
        assert settings["DicomAlwaysAllow" + action] is False
    modality = settings["DicomModalities"]["osirix"]
    assert modality["AET"] == "OSIRIX" and modality["Host"] == "192.168.20.30"
    assert modality["AllowEcho"] and modality["AllowStore"]
    assert not any(modality["Allow" + action] for action in ("Find", "FindWorklist", "Get", "Move", "StorageCommitment"))
    for key in ("StorageDirectory", "IndexDirectory", "TemporaryDirectory"):
        assert Path(settings[key]).parent == directory
    assert (directory / "bin" / binary.name).stat().st_ino == binary.stat().st_ino
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for path in (config, directory / "connection.env"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = config.read_bytes(), (directory / "connection.env").read_bytes()
    assert orthanc_receiver.prepare(directory, "192.168.20.30", "OSIRIX") == result
    assert before == (config.read_bytes(), (directory / "connection.env").read_bytes())
    assert settings["RegisteredUsers"]["scan3d"] not in json.dumps(result)


@pytest.mark.parametrize(
    ("host", "aet"),
    [
        ("8.8.8.8", "OSIRIX"),
        ("127.0.0.1", "OSIRIX"),
        ("::1", "OSIRIX"),
        ("192.168.1.1", "bad\\aet"),
        ("192.168.1.1", "bad\naet"),
        ("192.168.1.1", "a" * 17),
        ("192.168.1.1", " OsiriX"),
        ("192.168.1.1", "OsiriX "),
        ("192.168.1.1", "OsiriX\x7f"),
    ],
)
def test_invalid_sender_is_rejected_before_writing_configuration(receiver, host, aet):
    directory, _ = receiver
    with pytest.raises(ValueError):
        orthanc_receiver.prepare(directory, host, aet)
    assert not list(directory.iterdir())


@pytest.mark.parametrize("aet", ["OsiriX-mac", "OsiriXMac", "OsiriX/Mac", "Mac with space"])
def test_sender_ae_preserves_valid_printable_ascii_and_exact_case(receiver, aet):
    directory, _ = receiver
    orthanc_receiver.prepare(directory, "10.20.0.118", aet)
    config = json.loads((directory / "orthanc.json").read_text())
    assert config["DicomModalities"]["osirix"]["AET"] == aet
    assert config["DicomModalities"]["osirix"]["Host"] == "10.20.0.118"


def test_configuration_change_and_credential_mismatch_preserve_existing_files(receiver):
    directory, _ = receiver
    orthanc_receiver.prepare(directory, "192.168.20.30", "OSIRIX")
    before = (directory / "orthanc.json").read_bytes()
    with pytest.raises(RuntimeError, match="differs"):
        orthanc_receiver.prepare(directory, "192.168.20.31", "OSIRIX")
    credentials = directory / "connection.env"
    credentials.write_text("SCAN3D_ORTHANC_PASSWORD=unexpected\n")
    with pytest.raises(RuntimeError, match="credentials differ"):
        orthanc_receiver.prepare(directory, "192.168.20.30", "OSIRIX")
    assert credentials.read_text() == "SCAN3D_ORTHANC_PASSWORD=unexpected\n"
    assert (directory / "orthanc.json").read_bytes() == before


def test_receiver_refuses_symlinked_artifact_paths(receiver, tmp_path):
    directory, _ = receiver
    directory.mkdir()
    elsewhere = tmp_path / "unrelated"
    elsewhere.mkdir()
    (directory / "storage").symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(RuntimeError, match="symbolic links"):
        orthanc_receiver.prepare(directory, "192.168.20.30", "OSIRIX")
    assert not list(elsewhere.iterdir())
    assert not (directory / "orthanc.json").exists()


def test_receiver_refuses_unowned_directory_before_creating_it(tmp_path):
    directory = tmp_path / "unrelated"
    with pytest.raises(ValueError, match="orthanc-incoming"):
        orthanc_receiver.prepare(directory, "192.168.20.30", "OSIRIX")
    assert not directory.exists()


def test_wrong_installed_binary_is_never_executed(receiver):
    directory, binary = receiver
    marker = binary.parent / "executed"
    binary.write_text(f'#!/bin/sh\ntouch "{marker}"\n')
    with pytest.raises(RuntimeError, match="pinned"):
        orthanc_receiver.prepare(directory, "192.168.20.30", "OSIRIX")
    assert not marker.exists()
    assert not (directory / "orthanc.json").exists()


def test_scratch_cross_filesystem_copy_still_verifies_the_binary(receiver, monkeypatch):
    import errno

    directory, binary = receiver

    def cross_filesystem(*args):
        raise OSError(errno.EXDEV, "synthetic cross-device link")

    monkeypatch.setattr(orthanc_receiver.os, "link", cross_filesystem)
    orthanc_receiver.prepare(directory, "192.168.20.30", "OSIRIX")
    installed = directory / "bin" / binary.name
    assert installed.read_bytes() == binary.read_bytes()
    assert installed.stat().st_ino != binary.stat().st_ino
    assert stat.S_IMODE(installed.stat().st_mode) == 0o700
