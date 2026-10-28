"""Prepare the separate incoming archive; activation requires a reviewed network rule.

Orthanc 1.13 cannot bind its DICOM SCP to a selected interface. Its HTTP API
stays loopback-only, while a host firewall must restrict TCP 4243 before start.
"""

from __future__ import annotations

import argparse
import errno
import ipaddress
import json
import os
from pathlib import Path
import secrets
import shlex
import shutil
import tempfile

try:
    from . import orthanc_dev as runtime
except ImportError:  # Direct CLI execution has no package context.
    import orthanc_dev as runtime

DIRECTORY = runtime.ROOT / ".runtime" / "orthanc-incoming"
URL = "http://127.0.0.1:8044"
UNIT = "scan3d-orthanc.service"


def owned_directory(directory: Path, *, create=True) -> Path:
    directory = directory.absolute()
    temporary = Path(tempfile.gettempdir()).resolve()
    if directory != DIRECTORY and not (directory.parent.resolve() == temporary and directory.name.startswith("scan3d-orthanc-receiver-")):
        raise ValueError("Use .runtime/orthanc-incoming or a temporary scan3d-orthanc-receiver-* directory")
    if any(parent.is_symlink() for parent in (directory, *directory.parents)):
        raise ValueError("Receiver paths must not traverse symbolic links")
    if create:
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
    return directory


def private_text(path: Path, value: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(value)


def configuration(directory: Path, remote_host: str, remote_aet: str, password: str) -> dict:
    address = ipaddress.ip_address(remote_host)
    networks = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "127.0.0.0/8")
    if address.version != 4 or not any(address in ipaddress.ip_network(network) for network in networks):
        raise ValueError("Use the Mac's private IPv4 address; public addresses are not accepted")
    if address.is_loopback and not directory.name.startswith("scan3d-orthanc-receiver-"):
        raise ValueError("Loopback senders are permitted only in temporary synthetic receiver tests")
    # AE is printable ASCII excluding the DICOM value separator. Preserve case;
    # reject padding rather than silently changing the supplied identity.
    if not 1 <= len(remote_aet) <= 16 or remote_aet != remote_aet.strip() or "\\" in remote_aet or any(not 32 <= ord(char) <= 126 for char in remote_aet):
        raise ValueError("Use a 1–16 character printable ASCII DICOM AE title without backslashes or surrounding spaces")
    return {
        "Name": "3DScan incoming archive",
        "StorageDirectory": str(directory / "storage"),
        "IndexDirectory": str(directory / "index"),
        "TemporaryDirectory": str(directory / "tmp"),
        "HttpServerEnabled": True,
        "HttpPort": 8044,
        "HttpBindAddresses": ["127.0.0.1"],
        "RemoteAccessAllowed": False,
        "AuthenticationEnabled": True,
        "RegisteredUsers": {"scan3d": password},
        "DicomServerEnabled": True,
        "DicomAet": "SCAN3D",
        "DicomPort": 4243,
        "DicomCheckCalledAet": True,
        "StrictAetComparison": True,
        "DicomCheckModalityHost": True,
        "DicomAlwaysAllowEcho": False,
        "DicomAlwaysAllowStore": False,
        "DicomAlwaysAllowFind": False,
        "DicomAlwaysAllowFindWorklist": False,
        "DicomAlwaysAllowGet": False,
        "DicomAlwaysAllowMove": False,
        "DicomModalities": {
            "osirix": {
                "AET": remote_aet,
                "Host": str(address),
                # Required remote-modality metadata, unused by incoming C-STORE.
                # Sending results back needs the Mac's confirmed listener port.
                "Port": 11112,
                "AllowEcho": True,
                "AllowStore": True,
                "AllowFind": False,
                "AllowFindWorklist": False,
                "AllowGet": False,
                "AllowMove": False,
                "AllowStorageCommitment": False,
            },
        },
        "WebDavEnabled": False,
        "ExecuteLuaEnabled": False,
        "RestApiWriteToFileSystemEnabled": False,
        "OverwriteInstances": False,
        "StorageCompression": False,
        "StoreMD5ForAttachments": True,
        "StableAge": 5,
        "HttpThreadsCount": 4,
        "Plugins": [],
    }


def prepare(directory: Path, remote_host: str, remote_aet: str) -> dict:
    directory = owned_directory(directory)
    config, connection = directory / "orthanc.json", directory / "connection.env"
    if config.is_symlink() or connection.is_symlink() or config.exists() != connection.exists():
        raise RuntimeError("Receiver configuration is incomplete or linked; existing files were left untouched")
    password = secrets.token_urlsafe(36)
    if config.exists():
        current = json.loads(config.read_text())
        password = current.get("RegisteredUsers", {}).get("scan3d", "")
        if not password or current != configuration(directory, remote_host, remote_aet, password):
            raise RuntimeError("Receiver configuration differs; review changes explicitly before replacing it")
    expected = configuration(directory, remote_host, remote_aet, password)
    for name in ("storage", "index", "tmp", "bin"):
        child = directory / name
        if child.is_symlink():
            raise RuntimeError("Receiver artifact paths must not be symbolic links")
        child.mkdir(mode=0o700, exist_ok=True)
        child.chmod(0o700)
    # Reuse the checksum-pinned executable without downloading or copying a
    # second 35 MiB binary. install() validates an existing file before execution.
    source = runtime.DEFAULT_DIRECTORY / "bin" / f"Orthanc-{runtime.VERSION}"
    target = directory / "bin" / source.name
    if not target.exists():
        if source.is_symlink() or runtime.digest(source) != runtime.BINARY_SHA256:
            raise RuntimeError("Install the pinned development Orthanc binary first")
        try:
            os.link(source, target)
        except OSError as error:
            if error.errno != errno.EXDEV:
                raise
            # Synthetic /tmp receivers can be on a different filesystem.
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700)
            with source.open("rb") as stream, os.fdopen(descriptor, "wb") as output:
                shutil.copyfileobj(stream, output)
    if target.is_symlink():
        raise RuntimeError("Receiver executable must not be a symbolic link")
    runtime.install(directory)
    values = {"SCAN3D_ORTHANC_URL": URL, "SCAN3D_ORTHANC_USERNAME": "scan3d", "SCAN3D_ORTHANC_PASSWORD": password}
    text = "".join(f"{key}={shlex.quote(value)}\n" for key, value in values.items())
    if not config.exists():
        private_text(config, json.dumps(expected, indent=2) + "\n")
        private_text(connection, text)
    elif connection.read_text() != text:
        raise RuntimeError("Receiver credentials differ; existing files were left untouched")
    for path in (config, connection):
        path.chmod(0o600)
    return {
        "status": "prepared",
        "http_url": URL,
        "dicom_aet": "SCAN3D",
        "dicom_port": 4243,
        "allowed_sender": {"host": remote_host, "aet": remote_aet},
        "credentials_file": str(connection),
        "configuration": str(config),
        "unit": UNIT,
        "activation_requires": "Restrict inbound TCP 4243 to the selected private interface and sender before starting the unit",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "status"))
    parser.add_argument("--directory", type=Path, default=DIRECTORY)
    parser.add_argument("--remote-host")
    parser.add_argument("--remote-aet")
    args = parser.parse_args()
    if args.command == "prepare":
        if not args.remote_host or not args.remote_aet:
            parser.error("prepare requires --remote-host and --remote-aet")
        result = prepare(args.directory, args.remote_host, args.remote_aet)
    else:
        directory = owned_directory(args.directory, create=False)
        if not directory.exists():
            result = {"status": "not_configured"}
        else:
            if any((directory / name).is_symlink() for name in ("orthanc.json", "connection.env")):
                raise RuntimeError("Receiver configuration must not be a symbolic link")
            result = runtime.system(directory, URL)
            if result["name"] != "3DScan incoming archive" or result["version"] != runtime.VERSION:
                raise RuntimeError("The listener is not the expected incoming archive")
            result["status"] = "running"
    print(json.dumps(result))


if __name__ == "__main__":
    main()
