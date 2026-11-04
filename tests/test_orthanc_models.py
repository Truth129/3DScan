"""Archive record validation must preserve existing identity and manifest bytes."""

import hashlib
import json

import pytest

from scan3d.orthanc import OrthancClient
from scan3d.orthanc_models import DownloadedFile, SourceManifest, content_digest, json_record, parse_download, parse_snapshot, parse_source
from test_orthanc import A, B, SERIES, STUDY, Archive


def fixture_snapshot():
    client = OrthancClient("https://archive.example.test/orthanc/", session=Archive())
    return client, client.snapshot(SERIES)


def test_record_and_manifest_json_order_hashes_and_round_trip_are_unchanged():
    client, snapshot = fixture_snapshot()
    # The pre-dataclass wire format in insertion order: manifest SHA-256 hashes indent=2 JSON, not sorted JSON.
    legacy = {
        "id": SERIES,
        "source_key": hashlib.sha256(client.base_url.encode()).hexdigest(),
        "series_id": SERIES,
        "series_uid": "1.2.3.4",
        "study_id": STUDY,
        "study_uid": "1.2.3",
        "frame_of_reference_uid": "1.2.3.9",
        "description": "Synthetic axial CT",
        "modality": "CT",
        "instance_count": 2,
        "expected_instances": 2,
        "stable": True,
        "status": "Complete",
        "completeness": "complete",
        "warnings": [],
        "instances": [
            {"id": A, "sop_instance_uid": "1.2.3.4.2", "sop_class_uid": "1.2.840.10008.5.1.4.1.1.2", "file_uuid": "synthetic-file-2", "size": 11},
            {"id": B, "sop_instance_uid": "1.2.3.4.1", "sop_class_uid": "1.2.840.10008.5.1.4.1.1.2", "file_uuid": "synthetic-file-1", "size": 11},
        ],
    }
    fields = {
        key: legacy[key]
        for key in ("source_key", "study_id", "study_uid", "series_id", "series_uid", "frame_of_reference_uid", "instances", "expected_instances", "status")
    }
    legacy["snapshot"] = hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    assert snapshot.snapshot == legacy["snapshot"]
    assert json.dumps(json_record(snapshot), indent=2) == json.dumps(legacy, indent=2)
    files = [
        {"instance_id": A, "sop_instance_uid": "1.2.3.4.2", "sha256": hashlib.sha256(b"synthetic-1").hexdigest(), "size": 11, "path": "000000.dcm"},
        {"instance_id": B, "sop_instance_uid": "1.2.3.4.1", "sha256": hashlib.sha256(b"synthetic-2").hexdigest(), "size": 11, "path": "000001.dcm"},
    ]
    content = [{key: item[key] for key in ("sop_instance_uid", "sha256", "size")} for item in sorted(files, key=lambda item: item["sop_instance_uid"])]
    digest = hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    typed_files = tuple(DownloadedFile(**item) for item in files)
    assert content_digest(typed_files) == digest
    source = SourceManifest(snapshot, typed_files, 22, digest, "orthanc", "2026-10-28T12:34:56+00:00", "1.2.3.9")
    expected = {
        "snapshot": legacy,
        "files": files,
        "bytes": 22,
        "content_digest": digest,
        "kind": "orthanc",
        "imported_at": source.imported_at,
        "frame_of_reference_uid": "1.2.3.9",
    }
    encoded = json.dumps(json_record(source), indent=2, allow_nan=False).encode()
    assert encoded == json.dumps(expected, indent=2, allow_nan=False).encode()
    assert parse_source(json.loads(encoded)) == source
    assert json_record(parse_download({key: expected[key] for key in ("snapshot", "files", "bytes", "content_digest")}))["files"] == files


@pytest.mark.parametrize(("field", "value"), [("instance_count", True), ("expected_instances", 0), ("stable", 1)])
def test_counts_and_flags_are_strict_and_validation_errors_are_sanitized(field, value):
    _, snapshot = fixture_snapshot()
    wire = json_record(snapshot)
    wire[field] = value
    wire["description"] = "Synthetic private metadata must not appear in validation errors"
    with pytest.raises(ValueError) as caught:
        parse_snapshot(wire)
    assert str(caught.value) == "Invalid Orthanc import record. Refresh the archive selection and retry."
    assert caught.value.__suppress_context__


def test_unknown_count_is_preserved_but_extra_or_corrupt_nested_records_are_rejected():
    _, snapshot = fixture_snapshot()
    wire = json_record(snapshot)
    wire.update(expected_instances=None, status="Unknown", completeness="unknown", warnings=["Declared count unavailable"])
    unknown = parse_snapshot(wire)
    assert unknown.expected_instances is None and unknown.warnings == ("Declared count unavailable",)
    wire["instances"][0]["password"] = "synthetic-secret"
    with pytest.raises(ValueError, match="Invalid Orthanc import record. Refresh the archive selection and retry.") as caught:
        parse_snapshot(wire)
    assert "synthetic-secret" not in str(caught.value)
    del wire["instances"][0]["password"]
    wire["instances"][0]["size"] = "11"
    with pytest.raises(ValueError, match="Invalid Orthanc import record. Refresh the archive selection and retry."):
        parse_snapshot(wire)
