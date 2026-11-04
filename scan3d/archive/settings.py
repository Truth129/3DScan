"""DICOMweb archive settings, parsed once at startup and frozen.

Only syntax, ranges and choices are checked here, because every command reads settings, and a
missing token or CA file must not stop a backup. The server checks those files when it starts.
Messages name a variable, never its value.
"""

from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
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
