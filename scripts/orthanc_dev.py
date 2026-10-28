"""Manage the separate, synthetic-only Orthanc development archive."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import subprocess
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


VERSION = "1.13.0"
BINARY_SHA256 = "c30cbcb8e6af3d209ebc5458b6db751e073822433612c1108927c40184e8d4a8"
BINARY_URL = f"https://orthanc.uclouvain.be/downloads/linux-standard-base/orthanc/{VERSION}/Orthanc"
ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DIRECTORY = ROOT / ".runtime" / "orthanc-dev"
URL = "http://127.0.0.1:8043"
UNIT = "scan3d-orthanc-dev.service"


def private_json(path: Path, value: dict) -> None:
    with path.open("x") as stream:
        json.dump(value, stream, indent=2)
        stream.write("\n")
    path.chmod(0o600)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def install(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    binaries = directory / "bin"
    binaries.mkdir(exist_ok=True, mode=0o700)
    binary = binaries / f"Orthanc-{VERSION}"
    if not binary.exists():
        partial = binaries / f"Orthanc-{VERSION}.part"
        with urlopen(BINARY_URL, timeout=60) as response, partial.open("xb") as output:
            length = 0
            while chunk := response.read(1024 * 1024):
                length += len(chunk)
                if length > 64 * 1024**2:
                    raise RuntimeError("The Orthanc binary exceeds the expected download limit.")
                output.write(chunk)
        if digest(partial) != BINARY_SHA256:
            raise RuntimeError("The Orthanc binary checksum does not match the pinned release.")
        partial.rename(binary)
    if digest(binary) != BINARY_SHA256:
        raise RuntimeError("The existing Orthanc binary differs from the pinned release; it was left untouched.")
    binary.chmod(0o700)
    version = subprocess.check_output([str(binary), "--version"], text=True).splitlines()[0]
    if not version.endswith(f" {VERSION}"):
        raise RuntimeError("The downloaded Orthanc reported an unexpected version.")
    manifest = directory / "binary.json"
    if not manifest.exists():
        private_json(manifest, {"version": VERSION, "url": BINARY_URL, "sha256": BINARY_SHA256})
    return binary


def configure(directory: Path) -> Path:
    config = directory / "orthanc.json"
    connection = directory / "connection.env"
    if config.exists() != connection.exists():
        raise RuntimeError("The sandbox configuration is incomplete; existing files were left untouched.")
    if config.exists():
        current = json.loads(config.read_text())
        expected = {
            "HttpPort": 8043,
            "HttpBindAddresses": ["127.0.0.1"],
            "RemoteAccessAllowed": False,
            "AuthenticationEnabled": True,
            "DicomServerEnabled": False,
            "StorageDirectory": str(directory / "storage"),
            "IndexDirectory": str(directory / "index"),
            "TemporaryDirectory": str(directory / "tmp"),
        }
        if any(current.get(key) != value for key, value in expected.items()):
            raise RuntimeError("The existing configuration differs from the isolated loopback sandbox; it was left untouched.")
        if list(current.get("RegisteredUsers", {})) != ["scan3d-dev"]:
            raise RuntimeError("The existing sandbox authentication configuration is unexpected.")
        return config
    username, password = "scan3d-dev", secrets.token_urlsafe(36)
    for name in ["storage", "index", "tmp"]:
        (directory / name).mkdir(mode=0o700, exist_ok=True)
    private_json(
        config,
        {
            "Name": "3DScan synthetic development archive",
            "StorageDirectory": str(directory / "storage"),
            "IndexDirectory": str(directory / "index"),
            "TemporaryDirectory": str(directory / "tmp"),
            "HttpServerEnabled": True,
            "HttpPort": 8043,
            "HttpBindAddresses": ["127.0.0.1"],
            "RemoteAccessAllowed": False,
            "AuthenticationEnabled": True,
            "RegisteredUsers": {username: password},
            "DicomServerEnabled": False,
            "DicomPort": 4243,
            "DicomAet": "SCAN3D_DEV",
            "WebDavEnabled": False,
            "ExecuteLuaEnabled": False,
            "RestApiWriteToFileSystemEnabled": False,
            "OverwriteInstances": False,
            "StorageCompression": False,
            "StoreMD5ForAttachments": True,
            "StableAge": 2,
            "HttpThreadsCount": 4,
            "Plugins": [],
        },
    )
    values = {
        "SCAN3D_ORTHANC_URL": URL,
        "SCAN3D_ORTHANC_USERNAME": username,
        "SCAN3D_ORTHANC_PASSWORD": password,
    }
    with connection.open("x") as stream:
        stream.write("# Synthetic development archive only; never source production-env.sh here.\n")
        for name, value in values.items():
            stream.write(f"export {name}={shlex.quote(value)}\n")
    connection.chmod(0o600)
    return config


def process_record(directory: Path) -> dict | None:
    path = directory / "process.json"
    if not path.exists():
        return None
    record = json.loads(path.read_text())
    pid = int(record["pid"])
    try:
        executable = Path(f"/proc/{pid}/exe").resolve(strict=True)
        arguments = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        started = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except FileNotFoundError:
        return None
    if executable != directory / "bin" / f"Orthanc-{VERSION}" or os.fsencode(directory / "orthanc.json") not in arguments or started != record.get("started"):
        raise RuntimeError("The recorded PID does not belong to this sandbox; no process was changed.")
    return record


def system(directory: Path) -> dict:
    config = json.loads((directory / "orthanc.json").read_text())
    username, password = next(iter(config["RegisteredUsers"].items()))
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    request = Request(URL + "/system", headers={"Authorization": f"Basic {token}"})
    with urlopen(request, timeout=2) as response:
        value = json.load(response)
    return {"url": URL, "version": value["Version"], "name": value["Name"], "credentials_file": str(directory / "connection.env")}


def unit_state(directory: Path) -> dict[str, str]:
    response = subprocess.run(
        ["systemctl", "--user", "show", UNIT, "--property=LoadState,Transient,ExecStart,WorkingDirectory,MainPID,ActiveState", "--no-pager"],
        capture_output=True,
        text=True,
    )
    fields = dict(line.split("=", 1) for line in response.stdout.splitlines() if "=" in line)
    if fields.get("LoadState") == "not-found":
        return fields
    binary = directory / "bin" / f"Orthanc-{VERSION}"
    config = directory / "orthanc.json"
    arguments = re.search(r"argv\[\]=(.*?) ;", fields.get("ExecStart", ""))
    if (
        fields.get("LoadState") != "loaded"
        or fields.get("Transient") != "yes"
        or fields.get("WorkingDirectory") != str(directory)
        or not arguments
        or shlex.split(arguments[1]) != [str(binary), str(config), f"--logfile={directory / 'orthanc.log'}"]
    ):
        raise RuntimeError("The development unit is unavailable or belongs to another command; no unit was changed.")
    return fields


def record_process(directory: Path, pid: int, *, unit: bool = False) -> None:
    started = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    record = directory / "process.json"
    if record.exists():
        record.unlink()
    private_json(record, {"pid": pid, "started": started, **({"unit": UNIT} if unit else {})})


def start(directory: Path) -> dict:
    binary = install(directory)
    config = configure(directory)
    if process_record(directory):
        return {"status": "running", **system(directory)}
    state = unit_state(directory)
    if state.get("LoadState") == "loaded":
        raise RuntimeError("The owned development unit is already loaded; inspect its status before starting again.")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8043))
        except OSError as error:
            raise RuntimeError("Port 8043 is occupied; the existing listener was left untouched.") from error
    log = directory / "orthanc.log"
    log.touch(mode=0o600, exist_ok=True)
    log.chmod(0o600)
    result = subprocess.run(
        [
            "systemd-run",
            "--user",
            f"--unit={UNIT}",
            "--collect",
            "--quiet",
            f"--property=WorkingDirectory={directory}",
            "--property=UMask=0077",
            "--property=Restart=no",
            "--property=NoNewPrivileges=yes",
            str(binary),
            str(config),
            f"--logfile={log}",
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise RuntimeError("The development user unit could not start; no existing service was changed.")
    for _ in range(100):
        state = unit_state(directory)
        pid = int(state.get("MainPID", "0"))
        if state.get("ActiveState") == "failed":
            raise RuntimeError(f"Orthanc exited; inspect the private sandbox log at {log}.")
        try:
            result = system(directory)
            if pid:
                record_process(directory, pid, unit=True)
                process_record(directory)
                return {"status": "running", "unit": UNIT, **result}
        except (HTTPError, URLError, TimeoutError, ConnectionError):
            time.sleep(0.1)
    raise RuntimeError(f"Orthanc is still starting; inspect the private sandbox log at {log}.")


def stop(directory: Path) -> dict:
    record = process_record(directory)
    if not record:
        return {"status": "stopped", "data_retained": True}
    if record.get("unit"):
        state = unit_state(directory)
        if state.get("MainPID") != str(record["pid"]):
            raise RuntimeError("The owned development unit PID changed; no service was stopped.")
        subprocess.run(["systemctl", "--user", "stop", UNIT], check=True)
    else:
        os.kill(record["pid"], signal.SIGTERM)
    for _ in range(100):
        try:
            current = process_record(directory)
        except RuntimeError:
            # A terminated child can briefly remain as a zombie until reaped.
            current = None
        if not current:
            return {"status": "stopped", "data_retained": True}
        time.sleep(0.1)
    raise RuntimeError("The sandbox did not stop in 10 seconds; no force-kill was sent.")


def foreground(directory: Path) -> None:
    binary = install(directory)
    config = configure(directory)
    if process_record(directory):
        raise RuntimeError("This sandbox is already running; use status to inspect it.")
    record_process(directory, os.getpid())
    os.chdir(directory)
    os.execv(binary, [str(binary), str(config), f"--logfile={directory / 'orthanc.log'}"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "start", "run", "status", "stop"])
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY, help="Separate development storage, never the live case library")
    args = parser.parse_args()
    directory = args.directory.expanduser().resolve()
    temporary = Path(tempfile.gettempdir()).resolve()
    default = DEFAULT_DIRECTORY.absolute()
    if directory != default and not (directory.is_relative_to(temporary) and directory.name.startswith("scan3d-orthanc-")):
        parser.error("Use the owned .runtime/orthanc-dev directory or a temporary scan3d-orthanc-* directory.")
    os.umask(0o077)
    try:
        if args.action == "install":
            install(directory)
            configure(directory)
            result = {"status": "installed", "url": URL, "version": VERSION, "credentials_file": str(directory / "connection.env")}
        elif args.action == "start":
            result = start(directory)
        elif args.action == "run":
            foreground(directory)
            return
        elif args.action == "stop":
            result = stop(directory)
        elif process_record(directory):
            result = {"status": "running", **system(directory)}
        else:
            result = {"status": "stopped", "data_retained": directory.exists()}
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        parser.exit(1, f"Orthanc development sandbox: {error}\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
