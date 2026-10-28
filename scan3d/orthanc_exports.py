"""Saved-mask DICOM SEG artifacts and explicit, restart-safe Orthanc delivery.

Encoding and upload run only on the CPU lane. A prepared artifact is immutable;
delivery receipts are separate so a retry always uses the original SOP bytes.
"""

from dataclasses import dataclass, replace
import hashlib
import json
import logging
from pathlib import Path
import stat
import time
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError

from .errors import NotFound
from .fileio import atomic_json
from .image_assets import reference_image
from .orthanc import OrthancError
from .orthanc_models import DicomUID, Digest, OrthancID, Positive, SourceManifest, json_record, parse_source
from .store import identifier, now, public_job

log = logging.getLogger(__name__)
DELIVERY_WAIT_SECONDS = 120
EXPORT_ENCODING_VERSION = 3


class ExportUnavailable(ValueError):
    """A deliberately public, patient-independent explanation."""


@dataclass(frozen=True, slots=True)
class _LegacyFile:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    structure_id: str
    name: str
    color: str
    mask_sha256: Digest
    path: Annotated[str, Field(pattern=r"^segment-[0-9]{4}\.dcm$")]
    sop_instance_uid: DicomUID
    sha256: Digest
    size: Positive
    voxel_count: Positive


@dataclass(frozen=True, slots=True)
class _Segment:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    structure_id: str
    name: str
    color: str
    mask_sha256: Digest
    voxel_count: Positive
    segment_number: Annotated[int, Field(ge=1, le=1000)]


@dataclass(frozen=True, slots=True)
class _File:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    path: Annotated[str, Field(pattern=r"^segment-[0-9]{4}\.dcm$")]
    sop_instance_uid: DicomUID
    sha256: Digest
    size: Positive
    segments: Annotated[tuple[_Segment, ...], Field(min_length=1, max_length=1000)]


@dataclass(frozen=True, slots=True)
class _Artifact:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    export_id: str
    artifact_key: Digest
    case_id: str
    revision_id: str
    reference_image_id: str
    reference_sha256: Digest
    source_manifest_sha256: Digest
    source_content_sha256: Digest
    source_snapshot: Digest
    archive_key: Digest
    source_kind: str
    series_instance_uid: DicomUID
    study_instance_uid: DicomUID
    frame_of_reference_uid: DicomUID
    created_at: str
    files: Annotated[tuple[_File | _LegacyFile, ...], Field(min_length=1, max_length=1000)]
    encoding_version: Positive = 1


_ARTIFACT = TypeAdapter(_Artifact)


@dataclass(frozen=True, slots=True)
class _Delivery:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    state: Literal["starting", "pending", "success", "failure", "unknown"]
    job_id: Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")] | None
    attempt_id: Annotated[str, Field(pattern=r"^[0-9a-f]{32}$")]


@dataclass(frozen=True, slots=True)
class _Receipt:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    manifest_sha256: Digest
    uploaded: dict[str, OrthancID]
    destinations: dict[Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")], _Delivery]


_RECEIPT = TypeAdapter(_Receipt)


@dataclass(frozen=True, slots=True)
class _Inputs:
    revision: dict
    reference: dict
    source: SourceManifest
    source_dir: Path
    structures: tuple[dict, ...]


def _hash(path, cancelled=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            if cancelled and cancelled():
                raise InterruptedError("DICOM SEG export cancelled before delivery")
            digest.update(block)
    return digest.hexdigest()


def _frozen_structure(structure):
    try:
        stamp = Path(structure["mask_file"]).lstat()
        if not stat.S_ISREG(stamp.st_mode):
            raise OSError()
    except OSError:
        raise ExportUnavailable("A saved mask is unavailable or is not a regular file. Delivery was blocked.") from None
    return {
        **{key: structure[key] for key in ("id", "name", "color", "mask_file")},
        "mask_stamp": [stamp.st_dev, stamp.st_ino, stamp.st_size, stamp.st_mtime_ns, stamp.st_ctime_ns],
    }


class OrthancExports:
    def __init__(self, service):
        self.service = service

    def _directory(self, case_id, export_id):
        return self.service.store.case_dir(case_id) / "dicom-exports" / export_id

    def _inputs(self, case_id, revision_id, structure_ids=None):
        store = self.service.store
        revision = store.revision(case_id, revision_id)
        case = store.get("cases", case_id)
        if not self.service.settings.orthanc_url:
            raise ExportUnavailable("Configure Orthanc before exporting DICOM SEG.")
        try:
            reference = reference_image(revision)
        except (KeyError, OSError, ValueError):
            raise ExportUnavailable("This saved revision's reference image cannot be verified.") from None
        if reference["modality"] != "CT":
            raise ExportUnavailable("DICOM SEG return currently supports original classic CT sources only.")
        structures = {item["id"]: item for item in revision["structures"]}
        selected = sorted(structure_ids if structure_ids is not None else (key for key, item in structures.items() if item.get("voxel_count", 1)))
        if not selected or len(selected) != len(set(selected)) or any(key not in structures for key in selected):
            raise ExportUnavailable("Select one or more distinct structures from this saved revision.")
        if any(not structures[key].get("voxel_count", 1) for key in selected):
            raise ExportUnavailable("Empty masks cannot be exported as DICOM SEG.")
        source_metadata = reference["source"]
        try:
            source = parse_source(case.get("references", {})["orthanc"])
            directory = Path(case["source_dir"])
            manifest_path = directory.parent / "source-import.json"
            if (
                directory.is_symlink()
                or not directory.resolve().is_relative_to(store.case_dir(case_id).resolve())
                or manifest_path.is_symlink()
                or not manifest_path.is_file()
                or _hash(manifest_path) != source_metadata["source_manifest_sha256"]
                or json.loads(manifest_path.read_text()) != json_record(source)
                or source.content_digest != source_metadata["source_content_sha256"]
                or source.snapshot.source_key != source_metadata["source_archive_key"]
                or source.snapshot.series_uid != source_metadata["series_uid"]
                or source.snapshot.study_uid != source_metadata["study_uid"]
                or source.frame_of_reference_uid != source_metadata["frame_of_reference_uid"]
                or source.snapshot.modality != "CT"
            ):
                raise ValueError()
            with self.service.orthanc.client() as client:
                if client.source_key != source.snapshot.source_key:
                    raise ValueError()
        except (KeyError, OSError, ValueError):
            raise ExportUnavailable(
                "This saved revision has no verified original Orthanc CT source. Reimport and prepare its original CT before export."
            ) from None
        return _Inputs(revision, reference, source, directory, tuple(structures[key] for key in selected))

    def availability(self, case_id, revision_id):
        # Resolve saved identity first: candidates and revisions from another
        # case are never treated as exportable snapshots.
        self.service.store.revision(case_id, revision_id)
        exports = [
            public_job(job) for job in self.service.store.list("jobs", case_id) if job["kind"] == "orthanc_export" and job.get("revision_id") == revision_id
        ]
        try:
            self._inputs(case_id, revision_id)
            with self.service.orthanc.client() as client:
                destinations = client.destinations(self.service.settings.orthanc_export_destinations)
        except (ExportUnavailable, OrthancError, NotFound) as exc:
            reason = str(exc) if isinstance(exc, ExportUnavailable) else "The configured Orthanc archive is unavailable. Retry its connection."
            return {"available": False, "reason": reason, "destinations": [], "exports": exports, "encoding_version": EXPORT_ENCODING_VERSION}
        return {"available": True, "destinations": destinations, "exports": exports, "encoding_version": EXPORT_ENCODING_VERSION}

    def start(self, case_id, revision_id, structure_ids=None, destination=None, *, resend=False):
        inputs = self._inputs(case_id, revision_id, structure_ids)
        if resend and destination is None:
            raise ExportUnavailable("Choose a configured destination before requesting Send again.")
        display_name = None
        if destination is not None:
            if destination not in self.service.settings.orthanc_export_destinations:
                raise ExportUnavailable("Select a configured DICOM SEG destination.")
            with self.service.orthanc.client() as client:
                destinations = {item["id"]: item for item in client.destinations(self.service.settings.orthanc_export_destinations)}
            if destination not in destinations:
                raise ExportUnavailable("The selected DICOM SEG destination is unavailable.")
            display_name = destinations[destination]["name"]
        identity = {
            "revision_id": revision_id,
            "reference_image_id": inputs.reference["id"],
            "source_manifest_sha256": inputs.reference["source"]["source_manifest_sha256"],
            "structures": [_frozen_structure(item) for item in inputs.structures],
        }
        selection = {**identity, "structures": [{key: value for key, value in item.items() if key != "mask_stamp"} for item in identity["structures"]]}
        selection_key = hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        input_key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        encoding = {**identity, "encoding_version": EXPORT_ENCODING_VERSION}
        artifact_key = hashlib.sha256(json.dumps(encoding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        export_key = hashlib.sha256((artifact_key + ":" + (destination or "")).encode()).hexdigest()
        with self.service.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT data FROM jobs WHERE case_id=? AND json_extract(data,'$.kind')='orthanc_export' ORDER BY rowid DESC", (case_id,)
            ).fetchall()
            previous = [json.loads(row["data"]) for row in rows]
            # Encoding upgrades create fresh SOPs, but cannot make a changed
            # saved mask look like the same immutable input. Legacy artifacts
            # used their input hash directly as the artifact key.
            if any(
                job["payload"].get("selection_key") == selection_key and job["payload"].get("input_key", job["payload"]["artifact_key"]) != input_key
                for job in previous
            ):
                raise ExportUnavailable("Saved masks changed since this revision's earlier export. Save a new revision before exporting those masks.")
            matching = next((job for job in previous if job["payload"]["export_key"] == export_key), None)
            if matching and matching["status"] in {"queued", "running"}:
                return matching
            if resend and not (matching and matching["status"] == "completed" and matching.get("result", {}).get("dicom_seg", {}).get("sent")):
                raise ExportUnavailable("Send again requires a confirmed successful export of this selection to this destination. Use Export or Retry first.")
            if matching and matching["status"] == "completed" and not resend:
                return matching
            artifact_job = next(
                (
                    job
                    for job in previous
                    if job["payload"]["artifact_key"] == artifact_key
                    and (job["status"] in {"queued", "running"} or (self._directory(case_id, job["payload"]["export_id"]) / "export.json").is_file())
                ),
                None,
            )
            payload = {
                **identity,
                "encoding_version": EXPORT_ENCODING_VERSION,
                "input_key": input_key,
                "artifact_key": artifact_key,
                "selection_key": selection_key,
                "export_key": export_key,
                "export_id": artifact_job["payload"]["export_id"] if artifact_job else identifier(),
                "destination": destination,
                "destination_name": display_name,
                "delivery_attempt_id": matching["payload"]["delivery_attempt_id"] if matching and not resend else identifier(),
            }
            if artifact_job:
                manifest_sha = next(
                    (
                        item["payload"].get("artifact_manifest_sha256")
                        for item in previous
                        if item["payload"]["export_id"] == payload["export_id"] and item["payload"].get("artifact_manifest_sha256")
                    ),
                    None,
                )
                if manifest_sha:
                    payload["artifact_manifest_sha256"] = manifest_sha
            job = dict(
                id=identifier(),
                case_id=case_id,
                kind="orthanc_export",
                status="queued",
                stage="Queued",
                error=None,
                revision_id=revision_id,
                created_at=now(),
                cancel_requested=False,
                payload=payload,
            )
            summary = {
                "export_id": payload["export_id"],
                "encoding_version": EXPORT_ENCODING_VERSION,
                "archived": False,
                "sent": False,
                "segments_count": len(inputs.structures),
                "instance_count": 1,
                "structure_ids": [item["id"] for item in inputs.structures],
            }
            if destination:
                summary.update(destination=display_name, destination_id=destination)
            job["result"] = {"dicom_seg": summary}
            con.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job["id"], case_id, job["status"], json.dumps(job, allow_nan=False)))
        return job

    def _check_inputs(self, job):
        payload = job["payload"]
        if payload.get("encoding_version", 1) != EXPORT_ENCODING_VERSION:
            raise ExportUnavailable("The DICOM SEG encoder was updated. Refresh export status and start a new export from the saved revision.")
        inputs = self._inputs(job["case_id"], job["revision_id"], [item["id"] for item in payload["structures"]])
        if (
            inputs.reference["id"] != payload["reference_image_id"]
            or inputs.reference["source"]["source_manifest_sha256"] != payload["source_manifest_sha256"]
            or [_frozen_structure(item) for item in inputs.structures] != payload["structures"]
        ):
            raise ExportUnavailable("The saved revision changed after this export was queued. Start a fresh export from a saved revision.")
        self.service.orthanc._verify(inputs.source_dir, inputs.source.snapshot, inputs.source, cancelled=lambda: self.service.cancelled(job))
        return inputs

    def _artifact(self, job, directory):
        from .dicom_seg import DicomSegError, SegmentInput, export_segmentation

        inputs = self._check_inputs(job)
        manifest_path = directory / "export.json"
        if manifest_path.exists():
            try:
                if job["payload"].get("artifact_manifest_sha256", _hash(manifest_path)) != _hash(manifest_path):
                    raise ExportUnavailable("The cached DICOM SEG export manifest changed. Delivery was blocked.")
                artifact = _ARTIFACT.validate_json(manifest_path.read_bytes())
            except (OSError, ValidationError):
                raise ExportUnavailable("The cached DICOM SEG export is invalid. Its bytes were retained for inspection.") from None
        else:
            self.service.stage(job, "Encoding exact saved masks as DICOM SEG")
            segments = tuple(SegmentInput(item["id"], item["name"], item["color"], Path(item["mask_file"])) for item in inputs.structures)
            try:
                result = export_segmentation(
                    [inputs.source_dir / item.path for item in inputs.source.files],
                    Path(inputs.revision["ct_file"]),
                    segments,
                    directory / "dicom",
                    revision_id=job["revision_id"],
                    source_kind=inputs.revision["source"],
                    cancelled=lambda: self.service.cancelled(job),
                    progress=lambda completed, total: self.service.store.update_job(
                        job["id"], progress={"phase": "dicom_seg", "completed": completed, "total": total}
                    ),
                )
            except DicomSegError as exc:
                raise ExportUnavailable(str(exc)) from None
            metadata = {item["id"]: item for item in inputs.structures}
            artifact = _Artifact(
                export_id=job["payload"]["export_id"],
                artifact_key=job["payload"]["artifact_key"],
                case_id=job["case_id"],
                revision_id=job["revision_id"],
                reference_image_id=inputs.reference["id"],
                reference_sha256=result.reference_sha256,
                source_manifest_sha256=inputs.reference["source"]["source_manifest_sha256"],
                source_content_sha256=inputs.source.content_digest,
                source_snapshot=inputs.source.snapshot.snapshot,
                archive_key=inputs.source.snapshot.source_key,
                source_kind=inputs.revision["source"],
                series_instance_uid=result.series_instance_uid,
                study_instance_uid=result.study_instance_uid,
                frame_of_reference_uid=result.frame_of_reference_uid,
                created_at=now(),
                encoding_version=job["payload"]["encoding_version"],
                files=tuple(
                    _File(
                        item.path.name,
                        item.sop_instance_uid,
                        item.sha256,
                        item.size,
                        tuple(
                            _Segment(
                                segment.structure_id,
                                metadata[segment.structure_id]["name"],
                                metadata[segment.structure_id]["color"],
                                segment.mask_sha256,
                                segment.voxel_count,
                                segment.segment_number,
                            )
                            for segment in item.segments
                        ),
                    )
                    for item in result.files
                ),
            )
            # Persist the generated SOP identities before any network write.
            artifact = _ARTIFACT.validate_python(artifact)
            atomic_json(manifest_path, _ARTIFACT.dump_python(artifact, mode="json"))
            self._check_inputs(job)
        segments = tuple(segment for item in artifact.files if isinstance(item, _File) for segment in item.segments)
        if (
            artifact.export_id != job["payload"]["export_id"]
            or artifact.artifact_key != job["payload"]["artifact_key"]
            or artifact.encoding_version != job["payload"]["encoding_version"]
            or artifact.case_id != job["case_id"]
            or artifact.revision_id != job["revision_id"]
            or artifact.reference_image_id != inputs.reference["id"]
            or artifact.reference_sha256 != inputs.reference["content_sha256"]
            or artifact.source_manifest_sha256 != inputs.reference["source"]["source_manifest_sha256"]
            or artifact.source_content_sha256 != inputs.source.content_digest
            or artifact.source_snapshot != inputs.source.snapshot.snapshot
            or artifact.archive_key != inputs.source.snapshot.source_key
            or artifact.source_kind != inputs.revision["source"]
            or artifact.study_instance_uid != inputs.source.snapshot.study_uid
            or artifact.frame_of_reference_uid != inputs.source.frame_of_reference_uid
            or len(artifact.files) != 1
            or any(not isinstance(item, _File) for item in artifact.files)
            or [segment.structure_id for segment in segments] != [item["id"] for item in inputs.structures]
            or [segment.segment_number for segment in segments] != list(range(1, len(segments) + 1))
            or len({item.sop_instance_uid for item in artifact.files}) != len(artifact.files)
            or len({item.path for item in artifact.files}) != len(artifact.files)
        ):
            raise ExportUnavailable("The cached DICOM SEG export differs from its frozen source or saved revision.")
        for output in artifact.files:
            path = directory / "dicom" / output.path
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != output.size
                or _hash(path, lambda: self.service.cancelled(job)) != output.sha256
            ):
                raise ExportUnavailable("The prepared DICOM SEG bytes changed. Delivery was blocked.")
        for segment, selected in zip(segments, inputs.structures, strict=True):
            if (
                _hash(selected["mask_file"], lambda: self.service.cancelled(job)) != segment.mask_sha256
                or segment.name != selected["name"]
                or segment.color != selected["color"]
            ):
                raise ExportUnavailable("The saved masks or prepared DICOM SEG bytes changed. Delivery was blocked.")
        self.service.check_cancel(job)
        job["payload"]["artifact_manifest_sha256"] = _hash(manifest_path)
        self.service.store.update_job(job["id"], payload=job["payload"])
        return artifact

    def recover(self):
        """Recovery is local only; a user request must resume any remote job."""
        for job in self.service.store.list("jobs"):
            if job["kind"] != "orthanc_export":
                continue
            if job["status"] == "queued" or job["status"] == "failed" and job.get("stage") == "Interrupted":
                self.service.store.update_job(
                    job["id"],
                    status="failed",
                    stage="Interrupted",
                    completed_at=now(),
                    error="Export interrupted. Retry to reuse its exact SEG bytes and check any existing Orthanc delivery job.",
                )

    def cancel(self, job_id):
        with self.service.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise NotFound("Job not found")
            job = json.loads(row["data"])
            if job["status"] not in {"queued", "running"}:
                return job
            job["cancel_requested"] = True
            if job["status"] == "queued":
                receipt_path = self._directory(job["case_id"], job["payload"]["export_id"]) / "delivery.json"
                active = False
                if job["payload"]["destination"] and receipt_path.exists():
                    try:
                        receipt = _RECEIPT.validate_json(receipt_path.read_bytes())
                        delivery = receipt.destinations.get(job["payload"]["destination"])
                        active = delivery is not None and delivery.state != "failure"
                    except (OSError, ValidationError):
                        active = True
                job.update(
                    status="failed" if active else "cancelled",
                    stage="Stopped waiting; delivery may continue" if active else "Cancelled before delivery",
                    error="An earlier Orthanc delivery may still be active. Check OsiriX or explicitly retry to check its status." if active else None,
                    completed_at=now(),
                )
            con.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job["status"], json.dumps(job, allow_nan=False), job_id))
        return job

    def _result(self, artifact, receipt, destination, display_name):
        uploaded = receipt.uploaded
        segments = tuple(segment for item in artifact.files for segment in item.segments)
        result = {
            "export_id": artifact.export_id,
            "encoding_version": artifact.encoding_version,
            "archived": all(item.path in uploaded for item in artifact.files),
            "sent": bool(destination and destination in receipt.destinations and receipt.destinations[destination].state == "success"),
            "segments_count": len(segments),
            "instance_count": len(artifact.files),
            "structure_ids": [segment.structure_id for segment in segments],
        }
        if destination:
            result["destination"] = display_name
            result["destination_id"] = destination
        return {"dicom_seg": result}

    def run(self, job):
        directory = self._directory(job["case_id"], job["payload"]["export_id"])
        artifact, receipt, sending = None, None, False
        destination = job["payload"]["destination"]
        receipt_path = directory / "delivery.json"

        def save_receipt():
            atomic_json(receipt_path, _RECEIPT.dump_python(receipt, mode="json"))

        try:
            directory.mkdir(parents=True, exist_ok=True)
            if not (directory / "export.json").exists() and (directory / "dicom").exists():
                # An encoder completed before its manifest was published. Keep
                # those orphan bytes; no network operation could have begun.
                job["payload"]["export_id"] = identifier()
                job["payload"].pop("artifact_manifest_sha256", None)
                self.service.store.update_job(job["id"], payload=job["payload"])
                directory = self._directory(job["case_id"], job["payload"]["export_id"])
                directory.mkdir(parents=True, exist_ok=True)
                receipt_path = directory / "delivery.json"
            if receipt_path.exists():
                try:
                    receipt = _RECEIPT.validate_json(receipt_path.read_bytes())
                except (OSError, ValidationError):
                    raise ExportUnavailable("The DICOM SEG delivery receipt cannot be verified. Delivery was blocked.") from None
                previous_delivery = receipt.destinations.get(destination)
                sending = previous_delivery is not None and previous_delivery.state != "failure"
            artifact = self._artifact(job, directory)
            if receipt:
                if receipt.manifest_sha256 != job["payload"]["artifact_manifest_sha256"] or not set(receipt.uploaded).issubset(
                    item.path for item in artifact.files
                ):
                    raise ExportUnavailable("The delivery receipt does not belong to this DICOM SEG artifact.")
            else:
                receipt = _Receipt(job["payload"]["artifact_manifest_sha256"], {}, {})
            with self.service.orthanc.client() as client:
                for item in artifact.files:
                    self.service.stage(job, "Archiving DICOM SEG in Orthanc")
                    # Orthanc may have been restored or changed independently.
                    # Reuploading identical SOP bytes is idempotent and verifies
                    # their readback before trusting a cached instance ID.
                    upload = client.upload_instance(directory / "dicom" / item.path, expected_sha256=item.sha256)
                    receipt.uploaded[item.path] = upload["id"]
                    save_receipt()
                self.service.check_cancel(job)
                if destination:
                    if destination not in self.service.settings.orthanc_export_destinations:
                        raise ExportUnavailable("The selected destination is no longer enabled. DICOM SEG remains archived.")
                    delivery = receipt.destinations.get(destination)
                    if delivery is None or delivery.state != "success" or delivery.attempt_id != job["payload"]["delivery_attempt_id"]:
                        self.service.stage(job, "Sending DICOM SEG through Orthanc")
                        if (
                            delivery is None
                            or not delivery.job_id
                            or delivery.state == "failure"
                            or delivery.attempt_id != job["payload"]["delivery_attempt_id"]
                        ):
                            # Write-ahead intent survives a lost start response.
                            # Only another explicit POST can retry unknown sends.
                            self.service.check_cancel(job)
                            delivery = _Delivery("starting", None, job["payload"]["delivery_attempt_id"])
                            receipt.destinations[destination] = delivery
                            save_receipt()
                            sending = True
                            job["payload"]["delivery_state"] = "starting"
                            self.service.store.update_job(job["id"], payload=job["payload"])
                            remote_id = client.start_store([receipt.uploaded[item.path] for item in artifact.files], destination)
                            delivery = _Delivery("pending", remote_id, delivery.attempt_id)
                            receipt.destinations[destination] = delivery
                            save_receipt()
                        sending = True
                        job["payload"].update(orthanc_job_id=delivery.job_id, delivery_state=delivery.state)
                        self.service.store.update_job(job["id"], payload=job["payload"])
                        self.service.store.update_job(job["id"], stage="Checking Orthanc DICOM SEG delivery")
                        deadline = time.monotonic() + DELIVERY_WAIT_SECONDS
                        while True:
                            remote = client.store_job(delivery.job_id)
                            state = remote["state"]
                            if state in {"Success", "Failure"}:
                                success = state == "Success" and remote["instances_count"] == len(artifact.files) and remote["failed_instances_count"] == 0
                                delivery = replace(delivery, state="success" if success else "failure")
                                receipt.destinations[destination] = delivery
                                save_receipt()
                                job["payload"]["delivery_state"] = delivery.state
                                self.service.store.update_job(job["id"], payload=job["payload"])
                                if not success:
                                    raise ExportUnavailable("Orthanc could not deliver every SEG instance. An explicit retry reuses the same SOP instances.")
                                break
                            if time.monotonic() >= deadline:
                                raise OrthancError("The Orthanc delivery job is still pending")
                            # Once C-STORE starts, cancelling a local waiter does
                            # not cancel delivery. Observe its true outcome.
                            if self.service.stop_event.wait(0.5):
                                raise OrthancError("Delivery was interrupted")
            self.service.store.update_job(
                job["id"],
                status="completed",
                stage="DICOM SEG sent" if destination else "DICOM SEG archived",
                error=None,
                completed_at=now(),
                result=self._result(artifact, receipt, destination, job["payload"]["destination_name"]),
            )
        except Exception as exc:
            cancelled = isinstance(exc, InterruptedError) and not sending
            error = (
                str(exc)
                if isinstance(exc, ExportUnavailable)
                else (
                    "Delivery is not confirmed. Check OsiriX before retrying; a retry reuses the exact SEG instances and checks any known Orthanc job."
                    if sending
                    else "DICOM SEG export failed. Check the original CT and saved masks, then retry."
                )
            )
            if not cancelled:
                log.warning("DICOM SEG export %s failed (%s)", job["id"], type(exc).__name__)
            if sending and receipt and destination in receipt.destinations and receipt.destinations[destination].state == "starting":
                receipt.destinations[destination] = replace(receipt.destinations[destination], state="unknown")
                try:
                    save_receipt()
                except OSError:
                    pass  # Existing write-ahead intent still prevents a blind resend.
            changes = dict(
                status="cancelled" if cancelled else "failed",
                stage="Cancelled before delivery" if cancelled else "Delivery not confirmed" if sending else "Export failed",
                error=None if cancelled else error,
                completed_at=now(),
            )
            if artifact and receipt:
                changes["result"] = self._result(artifact, receipt, destination, job["payload"]["destination_name"])
            self.service.store.update_job(job["id"], **changes)
