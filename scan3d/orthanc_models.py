"""Validated immutable archive records; JSON is confined to wire/storage edges."""

from dataclasses import dataclass
import hashlib
import json
from typing import Annotated, Literal, TypeVar

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

OrthancID = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{8}){4}$")]
DicomUID = Annotated[str, Field(strict=True, max_length=64, pattern=r"^[0-9]+(?:\.[0-9]+)*$")]
Digest = Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{64}$")]
Positive = Annotated[int, Field(strict=True, gt=0)]
Text = Annotated[str, Field(strict=True, max_length=1024)]


class _Record:
    __slots__ = ()
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")


@dataclass(frozen=True, slots=True)
class ArchiveInstance(_Record):
    id: OrthancID
    sop_instance_uid: DicomUID
    sop_class_uid: DicomUID
    file_uuid: Annotated[str, Field(strict=True, min_length=1, max_length=128)]
    size: Positive


@dataclass(frozen=True, slots=True)
class SeriesSnapshot(_Record):
    # Field order also preserves existing private manifest JSON bytes.
    id: OrthancID
    source_key: Digest
    series_id: OrthancID
    series_uid: DicomUID
    study_id: OrthancID
    study_uid: DicomUID
    frame_of_reference_uid: DicomUID | None
    description: Text
    modality: Text
    instance_count: Positive
    expected_instances: Positive | None
    stable: Annotated[bool, Field(strict=True)]
    status: Literal["Complete", "Unknown", "Missing", "Inconsistent"]
    completeness: Literal["receiving", "incomplete", "complete", "unknown"]
    warnings: tuple[Text, ...]
    instances: Annotated[tuple[ArchiveInstance, ...], Field(min_length=1, max_length=100_000)]
    snapshot: Digest


@dataclass(frozen=True, slots=True)
class DownloadedFile(_Record):
    instance_id: OrthancID
    sop_instance_uid: DicomUID
    sha256: Digest
    size: Positive
    path: Annotated[str, Field(strict=True, pattern=r"^[0-9]{6}\.dcm$")]


@dataclass(frozen=True, slots=True)
class DownloadManifest(_Record):
    snapshot: SeriesSnapshot
    files: Annotated[tuple[DownloadedFile, ...], Field(min_length=1, max_length=100_000)]
    bytes: Positive
    content_digest: Digest


@dataclass(frozen=True, slots=True)
class SourceManifest(DownloadManifest):
    kind: Literal["orthanc"]
    imported_at: Annotated[str, Field(strict=True, min_length=1)]
    frame_of_reference_uid: DicomUID


_T = TypeVar("_T")
_ADAPTERS = {record: TypeAdapter(record) for record in (ArchiveInstance, SeriesSnapshot, DownloadedFile, DownloadManifest, SourceManifest)}


def _parse(record: type[_T], value: object) -> _T:
    try:
        return _ADAPTERS[record].validate_python(value)
    except ValidationError:
        # Pydantic diagnostics include rejected input; never leak archive data.
        raise ValueError("Invalid Orthanc import record. Refresh the archive selection and retry.") from None


def parse_snapshot(value: object) -> SeriesSnapshot:
    snapshot = _parse(SeriesSnapshot, value)
    if (
        snapshot.id != snapshot.series_id
        or snapshot.instance_count != len(snapshot.instances)
        or len({item.id for item in snapshot.instances}) != snapshot.instance_count
        or len({item.sop_instance_uid for item in snapshot.instances}) != snapshot.instance_count
    ):
        raise ValueError("The Orthanc series membership is invalid. Refresh the archive selection and retry.")
    return snapshot


def parse_download(value: object) -> DownloadManifest:
    return _parse(DownloadManifest, value)


def parse_source(value: object) -> SourceManifest:
    return _parse(SourceManifest, value)


def json_record(value: ArchiveInstance | SeriesSnapshot | DownloadedFile | DownloadManifest | SourceManifest) -> dict:
    """Serialize only at an API, persistence or canonical-hash boundary."""
    return _ADAPTERS[type(value)].dump_python(value, mode="json")


def snapshot_fingerprint(snapshot: SeriesSnapshot) -> str:
    fields = json_record(snapshot)
    selected = {
        key: fields[key]
        for key in ("source_key", "study_id", "study_uid", "series_id", "series_uid", "frame_of_reference_uid", "instances", "expected_instances", "status")
    }
    return hashlib.sha256(json.dumps(selected, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def content_digest(files: tuple[DownloadedFile, ...]) -> str:
    content = [
        {"sop_instance_uid": item.sop_instance_uid, "sha256": item.sha256, "size": item.size} for item in sorted(files, key=lambda item: item.sop_instance_uid)
    ]
    return hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
