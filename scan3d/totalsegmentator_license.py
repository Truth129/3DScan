"""Write-only license management for the pinned TotalSegmentator installation.

Use its configuration file and validation protocol without importing its torch
runtime or exposing its key-containing diagnostics. Status reads stay offline.
"""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import string
import tempfile
import threading

import requests


VALIDATION_URL = "https://backend.totalsegmentator.com:443/is_valid_license_number"
_VALIDATION_FIELD = "_scan3d_license_validation"
_LOCK = threading.RLock()


class LicenseError(Exception):
    """Only fixed, display-safe errors may cross the API boundary."""

    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        super().__init__(message)


def _config_path() -> Path:
    home = Path("/tmp") if str(Path.home()) == "/" else Path.home()
    return Path(os.environ.get("TOTALSEG_HOME_DIR", str(home / ".totalsegmentator"))) / "config.json"


def _read_config() -> dict:
    try:
        config = json.loads(_config_path().read_text())
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise LicenseError(500, "The workstation's TotalSegmentator configuration could not be read.") from None
    if not isinstance(config, dict):
        raise LicenseError(500, "The workstation's TotalSegmentator configuration could not be read.")
    return config


def _configured(config: dict) -> bool:
    # Match the pinned engine's local check, including keys installed outside
    # this app. Entitlement is still enforced upstream when weights are fetched.
    key = config.get("license_number")
    return isinstance(key, str) and len(key) == 18


def _status(config: dict) -> dict:
    configured = _configured(config)
    record = config.get(_VALIDATION_FIELD)
    validated_at = None
    if configured and isinstance(record, dict):
        digest = hashlib.sha256(config["license_number"].encode()).hexdigest()
        if record.get("sha256") == digest and isinstance(record.get("validated_at"), str):
            try:
                stamp = datetime.fromisoformat(record["validated_at"])
                if stamp.tzinfo is not None:
                    validated_at = stamp.isoformat()
            except ValueError:
                pass
    return {"configured": configured, "validated": True if validated_at else None, "validated_at": validated_at}


def license_status() -> dict:
    with _LOCK:
        return _status(_read_config())


def license_configured() -> bool:
    """The task catalog never loads torch, contacts a server or returns a key."""
    try:
        with _LOCK:
            return _configured(_read_config())
    except LicenseError:
        return False


def _validate(key: str) -> None:
    try:
        response = requests.post(VALIDATION_URL, json={"license_number": key}, timeout=(3.05, 5), allow_redirects=False)
    except requests.RequestException:
        raise LicenseError(503, "The TotalSegmentator license service could not be reached. Your saved license is unchanged.") from None
    try:
        if not 200 <= response.status_code < 300:
            raise LicenseError(502, "The TotalSegmentator license service could not validate this key. Your saved license is unchanged.")
        try:
            result = response.json()
        except (ValueError, requests.RequestException):
            raise LicenseError(502, "The TotalSegmentator license service returned an unreadable response. Your saved license is unchanged.") from None
        status = result.get("status") if isinstance(result, dict) else None
        if not isinstance(status, str) or not status:
            raise LicenseError(502, "The TotalSegmentator license service returned an unreadable response. Your saved license is unchanged.")
        if status != "valid_license":
            raise LicenseError(400, "TotalSegmentator did not accept this license key. Your saved license is unchanged.")
    finally:
        response.close()


def _write_config(config: dict) -> None:
    path = _config_path()
    temporary = None
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", prefix=".license-", suffix=".tmp", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            os.fchmod(stream.fileno(), 0o600)
            json.dump(config, stream, indent=4, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    except (OSError, ValueError, TypeError):
        raise LicenseError(500, "The license could not be saved on this workstation. Your saved license is unchanged.") from None
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def save_license(key: str) -> dict:
    # This is the format accepted by the pinned totalseg_set_license CLI. Check
    # locally first, before sending a credential to the fixed official endpoint.
    key = key.strip() if isinstance(key, str) else ""
    if re.fullmatch(r"aca_[A-Za-z0-9_-]{14}", key) is None:
        raise LicenseError(400, "Enter the 18-character TotalSegmentator license key beginning with aca_.")
    with _LOCK:
        _read_config()  # Never overwrite an unreadable existing installation.
        _validate(key)
        # Re-read after validation so unrelated changes made in the meantime are
        # preserved. API writes also hold the job-store transaction idle guard.
        config = _read_config()
        if not config:
            config = {
                "totalseg_id": "totalseg_" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8)),
                "send_usage_stats": False,
                "prediction_counter": 0,
            }
        config["license_number"] = key
        config[_VALIDATION_FIELD] = {"sha256": hashlib.sha256(key.encode()).hexdigest(), "validated_at": datetime.now(timezone.utc).isoformat()}
        _write_config(config)
        return _status(config)


def remove_license() -> dict:
    with _LOCK:
        config = _read_config()
        changed = "license_number" in config or _VALIDATION_FIELD in config
        config.pop("license_number", None)
        config.pop(_VALIDATION_FIELD, None)
        if changed:
            _write_config(config)
        return _status(config)
