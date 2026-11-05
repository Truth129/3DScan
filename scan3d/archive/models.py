"""Validated records for series imported from a DICOMweb archive.

These sit beside the Orthanc records in ``orthanc_models`` and never replace them: a case
records which kind it holds, and readers dispatch on that. DICOMweb has no stored-file
identity and no expected count, so a snapshot names its instances by SOP alone, and binds its
patient through a salted key instead of an archive's patient-linked study ID.
"""

from dataclasses import dataclass
import hashlib
import json
from typing import Annotated, Literal, TypeVar

from pydantic import Field, TypeAdapter, ValidationError

from ..orthanc_models import DicomUID, Digest, Positive, Text, _Record


@dataclass(frozen=True, slots=True)
class WebInstance(_Record):
    sop_instance_uid: DicomUID
    sop_class_uid: DicomUID


@dataclass(frozen=True, slots=True)
class WebSnapshot(_Record):
    version: Literal[2]
    kind: Literal["dicomweb"]
    source_key: Digest
    study_uid: DicomUID
    series_uid: DicomUID
    patient_key: Digest
    frame_of_reference_uid: DicomUID | None
    description: Text
    modality: Text
    instance_count: Positive
    # The archive's own count, which is optional and can be stale: shown, never trusted.
    reported_instances: Positive | None
    stable: Annotated[bool, Field(strict=True)]
    completeness: Literal["receiving", "unknown"]
    warnings: tuple[Text, ...]
    instances: Annotated[tuple[WebInstance, ...], Field(min_length=1, max_length=100_000)]
    snapshot: Digest


@dataclass(frozen=True, slots=True)
class WebFile(_Record):
    sop_instance_uid: DicomUID
    sha256: Digest
    size: Positive
    path: Annotated[str, Field(strict=True, pattern=r"^[0-9]{6}\.dcm$")]
    transfer_syntax_uid: DicomUID


@dataclass(frozen=True, slots=True)
class WebDownload(_Record):
    snapshot: WebSnapshot
    files: Annotated[tuple[WebFile, ...], Field(min_length=1, max_length=100_000)]
    bytes: Positive
    content_digest: Digest


@dataclass(frozen=True, slots=True)
class WebSource(WebDownload):
    kind: Literal["dicomweb"]
    imported_at: Annotated[str, Field(strict=True, min_length=1)]
    frame_of_reference_uid: DicomUID


_T = TypeVar("_T")
_ADAPTERS = {record: TypeAdapter(record) for record in (WebInstance, WebSnapshot, WebFile, WebDownload, WebSource)}


def _parse(record: type[_T], value: object) -> _T:
    try:
        return _ADAPTERS[record].validate_python(value)
    except ValidationError:
        # Pydantic diagnostics include rejected input; never leak archive data.
        raise ValueError("Invalid archive import record. Refresh the archive selection and retry.") from None


def json_record(value) -> dict:
    """Serialize only at an API, persistence or canonical-hash boundary."""
    return _ADAPTERS[type(value)].dump_python(value, mode="json")


def patient_key(source_key: str, patient_id: str, issuer: str = "") -> str:
    """One patient in one archive. Salted with the archive, so keys cannot link patients across sites."""
    return hashlib.sha256("\0".join(("scan3d-patient-v1", source_key, patient_id.strip(), issuer.strip())).encode()).hexdigest()


def fingerprint(snapshot: WebSnapshot) -> str:
    """What an import binds to. Stability, counts and text are left out, so they can change without a new import."""
    fields = json_record(snapshot)
    selected = {key: fields[key] for key in ("kind", "source_key", "study_uid", "series_uid", "patient_key", "frame_of_reference_uid", "instances")}
    return hashlib.sha256(json.dumps({"v": 2, **selected}, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def parse_snapshot(value: object) -> WebSnapshot:
    snapshot = _parse(WebSnapshot, value)
    sops = [item.sop_instance_uid for item in snapshot.instances]
    if snapshot.instance_count != len(sops) or len(set(sops)) != len(sops) or sops != sorted(sops) or snapshot.snapshot != fingerprint(snapshot):
        raise ValueError("The archive series membership is invalid. Refresh the archive selection and retry.")
    return snapshot


def parse_source(value: object) -> WebSource:
    return _parse(WebSource, value)
