"""The local config file fills gaps; it never overrides a real environment variable."""

import os
from pathlib import Path

import pytest

from scan3d import __main__ as cli
from scan3d import config
from scan3d.config import load_environment, read_env_file


def write(tmp_path, text):
    path = tmp_path / "scan3d.env"
    path.write_text(text)
    return path


def test_read_env_file_never_evaluates_a_value(tmp_path):
    path = write(
        tmp_path,
        """
# a comment
; another comment
SCAN3D_PORT=9100
export SCAN3D_HOST=0.0.0.0
SCAN3D_ORTHANC_PASSWORD="quoted # value"
SCAN3D_ORTHANC_USERNAME='$(touch pwned) $HOME'
EMPTY=
""",
    )
    assert read_env_file(path) == {
        "SCAN3D_PORT": "9100",
        "SCAN3D_HOST": "0.0.0.0",
        "SCAN3D_ORTHANC_PASSWORD": "quoted # value",
        "SCAN3D_ORTHANC_USERNAME": "$(touch pwned) $HOME",
        "EMPTY": "",
    }
    assert not Path("pwned").exists()


@pytest.mark.parametrize("line", ["not an assignment", "BAD KEY=value", "1BAD=value"])
def test_malformed_line_is_reported_without_its_contents(tmp_path, line):
    path = write(tmp_path, f"SCAN3D_ORTHANC_PASSWORD=fine\n{line}\n")
    with pytest.raises(ValueError) as error:
        read_env_file(path)
    assert ":2 " in str(error.value) and line not in str(error.value)


def test_real_environment_wins_over_the_file(tmp_path):
    path = write(tmp_path, "SCAN3D_PORT=9100\nSCAN3D_HOST=0.0.0.0\n")
    environ = {"SCAN3D_CONFIG": str(path), "SCAN3D_PORT": "9200", "SCAN3D_ALLOWED_HOSTS": ""}
    assert load_environment(environ) == path
    assert environ["SCAN3D_PORT"] == "9200" and environ["SCAN3D_HOST"] == "0.0.0.0"
    assert environ["SCAN3D_ALLOWED_HOSTS"] == ""


def test_missing_default_file_is_fine_but_a_named_file_must_exist(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "absent.env")
    environ = {}
    assert load_environment(environ) is None
    assert environ["TOTALSEG_HOME_DIR"].endswith("totalsegmentator")
    with pytest.raises(ValueError, match="SCAN3D_CONFIG"):
        load_environment({"SCAN3D_CONFIG": str(tmp_path / "absent.env")})


def test_built_converter_precedes_the_system_one(tmp_path, monkeypatch):
    (tmp_path / ".tools" / "bin").mkdir(parents=True)
    monkeypatch.setattr(config, "ROOT", tmp_path)
    monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "absent.env")
    environ = {"PATH": "/usr/bin"}
    load_environment(environ)
    assert environ["PATH"] == f"{tmp_path / '.tools' / 'bin'}{os.pathsep}/usr/bin"


def test_serve_defaults_come_from_the_config_file(tmp_path, monkeypatch):
    path = write(tmp_path, "SCAN3D_HOST=127.0.0.2\nSCAN3D_PORT=9123\n")
    monkeypatch.setenv("SCAN3D_CONFIG", str(path))
    # Set first, so teardown removes what loading the file and serve write: a leaked
    # non-loopback host would turn sign-in on for every later Settings.from_env().
    for name in ("SCAN3D_HOST", "SCAN3D_PORT"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setenv("TOTALSEG_HOME_DIR", str(tmp_path))
    monkeypatch.setattr("sys.argv", ["scan3d", "serve"])
    captured = {}
    monkeypatch.setattr("scan3d.api.create_app", lambda: "app")
    monkeypatch.setattr("uvicorn.run", lambda app, **options: captured.update(options, app=app))
    monkeypatch.setattr(os, "umask", lambda mask: captured.update(umask=mask))
    cli.main()
    assert (captured["app"], captured["host"], captured["port"], captured["umask"]) == ("app", "127.0.0.2", 9123, 0o077)


def test_the_example_is_all_defaults_and_documents_every_setting():
    root = Path(__file__).resolve().parents[1]
    example = (root / "scan3d.env.example").read_text()
    assert not [line for line in example.splitlines() if line.strip() and not line.startswith("#")]
    names = {
        "SCAN3D_HOST",
        "SCAN3D_PORT",
        "SCAN3D_ALLOWED_HOSTS",
        "SCAN3D_AUTH",
        "SCAN3D_DATA_DIR",
        "SCAN3D_IMPORT_ROOTS",
        "SCAN3D_MODALITIES",
        "SCAN3D_DEVICE",
        "SCAN3D_INFERENCE_PATH_DIR",
        "SCAN3D_ORTHANC_URL",
        "SCAN3D_ORTHANC_USERNAME",
        "SCAN3D_ORTHANC_PASSWORD",
        "SCAN3D_ORTHANC_EXPORT_DESTINATIONS",
    }
    assert all(f"#{name}=" in example for name in names)
