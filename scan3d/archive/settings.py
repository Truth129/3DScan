"""DICOMweb archive and direct DICOM send settings, parsed once at startup and frozen.

Only syntax, ranges and choices are checked here, because every command reads settings, and a
missing token or CA file must not stop a backup. The server checks those files when it starts.
Messages name a variable, never its value.
"""

from dataclasses import dataclass, field
import ipaddress
import json
import os
from pathlib import Path
import re
import ssl
from typing import Literal
from urllib.parse import urlsplit

AUTH_MODES = ("none", "basic", "bearer")
_ARCHIVE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")


def _loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _origin(url):
    """Scheme, host and port, with the scheme's default port filled in."""
    parsed = urlsplit(url)
    return parsed.scheme, (parsed.hostname or "").lower(), parsed.port or {"http": 80, "https": 443}.get(parsed.scheme)


def _number(environ, name, default, low, high):
    raw = environ.get(name, "").strip()
    try:
        value = float(raw) if raw else default
    except ValueError:
        raise ValueError(f"{name} must be a number") from None
    if not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return value


def _flag(environ, name, default):
    raw = environ.get(name, "").strip().lower()
    if not raw:
        return default
    if raw not in ("true", "false"):
        raise ValueError(f"{name} must be true or false")
    return raw == "true"


@dataclass(frozen=True)
class DicomWebSettings:
    url: str = field(repr=False)
    name: str = "DICOM archive"
    archive_id: str = ""
    auth: Literal["none", "basic", "bearer"] = "none"
    username: str = field(default="", repr=False)
    password: str = field(default="", repr=False)
    token_file: str = ""
    ca_bundle: str = ""
    timeout: float = 30
    quiet_seconds: float = 60
    wildcards: bool = False
    store: bool = True

    @classmethod
    def from_env(cls, environ=None, *, orthanc_url=""):
        """None while SCAN3D_DICOMWEB_URL is unset, which leaves the feature off."""
        environ = os.environ if environ is None else environ
        url = environ.get("SCAN3D_DICOMWEB_URL", "").strip()
        if not url:
            return None
        try:
            parsed = urlsplit(url)
            _ = parsed.port  # Raises on a malformed port.
        except ValueError:
            raise ValueError("SCAN3D_DICOMWEB_URL is not a valid URL") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("SCAN3D_DICOMWEB_URL must be an HTTP(S) URL without credentials, query or fragment")
        # One server under two identities would split its cases between them.
        if orthanc_url and _origin(url) == _origin(orthanc_url):
            raise ValueError("SCAN3D_DICOMWEB_URL must not be on the same host and port as SCAN3D_ORTHANC_URL")
        auth = environ.get("SCAN3D_DICOMWEB_AUTH", "none").strip().lower() or "none"
        if auth not in AUTH_MODES:
            raise ValueError("SCAN3D_DICOMWEB_AUTH must be none, basic or bearer")
        if auth != "none" and parsed.scheme == "http" and not _loopback(parsed.hostname):
            raise ValueError("SCAN3D_DICOMWEB_AUTH needs an https SCAN3D_DICOMWEB_URL unless the archive is on this machine")
        token_file = environ.get("SCAN3D_DICOMWEB_TOKEN_FILE", "").strip()
        if (auth == "bearer") != bool(token_file):
            raise ValueError("SCAN3D_DICOMWEB_TOKEN_FILE is needed with SCAN3D_DICOMWEB_AUTH=bearer, and only then")
        archive_id = environ.get("SCAN3D_DICOMWEB_ID", "").strip()
        if archive_id and not _ARCHIVE_ID.fullmatch(archive_id):
            raise ValueError("SCAN3D_DICOMWEB_ID must be 1-64 letters, digits, dots, underscores or dashes")
        for name in ("SCAN3D_DICOMWEB_TOKEN_FILE", "SCAN3D_DICOMWEB_CA_BUNDLE"):
            value = environ.get(name, "").strip()
            if value and not Path(value).is_absolute():
                raise ValueError(f"{name} must be an absolute path")
        return cls(
            url=url,
            name=environ.get("SCAN3D_DICOMWEB_NAME", "").strip()[:64] or "DICOM archive",
            archive_id=archive_id,
            auth=auth,
            username=environ.get("SCAN3D_DICOMWEB_USERNAME", ""),
            password=environ.get("SCAN3D_DICOMWEB_PASSWORD", ""),
            token_file=token_file,
            ca_bundle=environ.get("SCAN3D_DICOMWEB_CA_BUNDLE", "").strip(),
            timeout=_number(environ, "SCAN3D_DICOMWEB_TIMEOUT", 30, 1, 300),
            quiet_seconds=_number(environ, "SCAN3D_DICOMWEB_QUIET_SECONDS", 60, 0, 3600),
            wildcards=_flag(environ, "SCAN3D_DICOMWEB_WILDCARDS", False),
            store=_flag(environ, "SCAN3D_DICOMWEB_STORE", True),
        )

    def missing_files(self):
        """Settings whose file cannot be read now. The server refuses to start with any."""
        named = (("SCAN3D_DICOMWEB_TOKEN_FILE", self.token_file), ("SCAN3D_DICOMWEB_CA_BUNDLE", self.ca_bundle))
        return [name for name, value in named if value and not Path(value).is_file()]


# DICOM AE titles: up to 16 characters, no backslash or control character, not only spaces.
_AE_TITLE = re.compile(r"[\x20-\x5b\x5d-\x7e]{1,16}")
_DESTINATION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_HOST = re.compile(r"[A-Za-z0-9.:-]{1,253}")
_DESTINATION_KEYS = {"id", "name", "ae_title", "host", "port", "calling_ae", "timeout", "tls", "ca_bundle"}


def _ae_title(value, what):
    if not isinstance(value, str) or not _AE_TITLE.fullmatch(value) or not value.strip():
        raise ValueError(f"{what} must be a DICOM AE title: 1-16 characters, with no backslash")
    return value.strip()


@dataclass(frozen=True)
class Destination:
    """A DICOM node that 3DScan sends to itself, with C-STORE."""

    id: str
    name: str
    ae_title: str
    host: str
    port: int
    calling_ae: str = ""
    timeout: float = 30
    tls: bool = False
    ca_bundle: str = ""

    @property
    def identity(self):
        """Where a send goes, so a renamed alias is the same destination and a moved one is not."""
        return f"{self.ae_title}@{self.host.lower()}:{self.port}{'+tls' if self.tls else ''}"


def _destination(entry, index):
    where = f"Destination {index + 1} in SCAN3D_DICOM_DESTINATIONS_FILE"
    if not isinstance(entry, dict) or set(entry) - _DESTINATION_KEYS or not {"id", "ae_title", "host", "port"} <= set(entry):
        raise ValueError(f"{where} needs id, ae_title, host and port, and only the documented keys")
    if not isinstance(entry["id"], str) or not _DESTINATION_ID.fullmatch(entry["id"]):
        raise ValueError(f"{where} needs an id of 1-64 letters, digits, underscores or dashes")
    name = entry.get("name", entry["id"])
    if not isinstance(name, str) or not 0 < len(name.strip()) <= 64:
        raise ValueError(f"{where} needs a name of 1-64 characters")
    if not isinstance(entry["host"], str) or not _HOST.fullmatch(entry["host"]):
        raise ValueError(f"{where} needs a host name or address")
    port, timeout = entry["port"], entry.get("timeout", 30)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        raise ValueError(f"{where} needs a port from 1 to 65535")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 1 <= timeout <= 300:
        raise ValueError(f"{where} needs a timeout from 1 to 300 seconds")
    tls, ca_bundle = entry.get("tls", False), entry.get("ca_bundle", "")
    if not isinstance(tls, bool) or not isinstance(ca_bundle, str) or (ca_bundle and (not tls or not Path(ca_bundle).is_absolute())):
        raise ValueError(f"{where} may name an absolute ca_bundle only with tls set to true")
    calling = _ae_title(entry["calling_ae"], f"{where}'s calling_ae") if "calling_ae" in entry else ""
    return Destination(entry["id"], name.strip(), _ae_title(entry["ae_title"], f"{where}'s ae_title"), entry["host"], port, calling, timeout, tls, ca_bundle)


@dataclass(frozen=True)
class DicomSendSettings:
    """The AE title 3DScan sends as, and the file listing where it may send.

    The file is read when the server starts, which refuses an invalid one, and again before each
    send, so a destination that moved is refused rather than sent to on an old address.
    """

    ae_title: str = "SCAN3D"
    destinations_file: str = ""

    @classmethod
    def from_env(cls, environ=None):
        """None unless SCAN3D_DICOM_DESTINATIONS_FILE is set, which leaves direct sends off."""
        environ = os.environ if environ is None else environ
        path = environ.get("SCAN3D_DICOM_DESTINATIONS_FILE", "").strip()
        if not path:
            return None
        if not Path(path).is_absolute():
            raise ValueError("SCAN3D_DICOM_DESTINATIONS_FILE must be an absolute path")
        return cls(_ae_title(environ.get("SCAN3D_DICOM_AE_TITLE", "SCAN3D").strip() or "SCAN3D", "SCAN3D_DICOM_AE_TITLE"), path)

    def destinations(self):
        """The configured destinations, read now. Messages name the file's setting, never a value from it."""
        try:
            entries = json.loads(Path(self.destinations_file).read_text())
        except OSError:
            raise ValueError("SCAN3D_DICOM_DESTINATIONS_FILE cannot be read") from None
        except ValueError:
            raise ValueError("SCAN3D_DICOM_DESTINATIONS_FILE is not valid JSON") from None
        if not isinstance(entries, list) or len(entries) > 100:
            raise ValueError("SCAN3D_DICOM_DESTINATIONS_FILE must hold a list of at most 100 destinations")
        result = tuple(_destination(entry, index) for index, entry in enumerate(entries))
        if len({item.id for item in result}) != len(result):
            raise ValueError("SCAN3D_DICOM_DESTINATIONS_FILE lists one id twice")
        return result

    def missing_files(self):
        """Files a destination names that cannot be loaded now. The server refuses to start with any."""
        return [f"The ca_bundle of destination {item.id}" for item in self.destinations() if item.ca_bundle and not _loads(item.ca_bundle)]


def _loads(ca_bundle):
    """Whether TLS can use this file: it exists, is readable and holds PEM certificates."""
    try:
        ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=ca_bundle)
    except OSError:  # ssl.SSLError included
        return False
    return True
