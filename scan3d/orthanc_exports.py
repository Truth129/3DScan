"""Saved-mask DICOM SEG artifacts and explicit, restart-safe delivery.

Encoding and upload run only on the CPU lane. A prepared artifact is immutable;
delivery receipts are separate so a retry always uses the original SOP bytes.
Orthanc delivery needs a CT imported from Orthanc. A file delivery needs only
the retained source series, so a folder or ZIP import can use it, and it reaches
no archive.
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
import pydicom

from .dicom_seg_jobs import prepared_digest, series_files
from .errors import NotFound
from .fileio import atomic_json
from .identity import actor_id
from .image_assets import reference_image
from .orthanc import OrthancError
from .orthanc_models import DicomUID, Digest, OrthancID, Positive, SourceManifest, json_record, parse_source, series_source
from .store import identifier, now, public_job

log = logging.getLogger(__name__)
DELIVERY_WAIT_SECONDS = 120
EXPORT_ENCODING_VERSION = 3
ARCHIVE_OFF = "Your account cannot use the archive. An administrator can turn it on in People."


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
    removed_voxel_count: Annotated[int, Field(ge=0)] = 0


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
    source_kind: str
    series_instance_uid: DicomUID
    study_instance_uid: DicomUID
    frame_of_reference_uid: DicomUID
    created_at: str
    files: Annotated[tuple[_File | _LegacyFile, ...], Field(min_length=1, max_length=1000)]
    # A folder or ZIP import was never in an archive, so it has neither.
    source_snapshot: Digest | None = None
    archive_key: Digest | None = None
    encoding_version: Positive = 1
    overlap_priority: Annotated[tuple[str, ...], Field(min_length=2, max_length=1000)] | None = None


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
class _SegSource:
    """What an export binds to, whichever way its CT arrived.

    A folder or ZIP import has no manifest, so the digest of its bytes names it
    in both roles. That digest also names the prepared reference, which is what
    ties these files to the saved image.
    """

    paths: tuple[Path, ...]
    manifest_sha256: Digest
    content_sha256: Digest
    study_uid: DicomUID
    frame_of_reference_uid: DicomUID
    snapshot: Digest | None = None
    archive_key: Digest | None = None


@dataclass(frozen=True, slots=True)
class _Inputs:
    revision: dict
    reference: dict
    source: SourceManifest | None
    source_dir: Path | None
    structures: tuple[dict, ...]
    seg_source: _SegSource | None = None


def _hash(path, cancelled=None):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            if cancelled and cancelled():
                raise InterruptedError("DICOM SEG export cancelled before delivery")
            digest.update(block)
    return digest.hexdigest()


def _hash_series(paths, cancelled):
    """The stream a prepared reference is named after: every file, in sorted path order."""
    digest = hashlib.sha256()
    for path in sorted(paths, key=str):
        with Path(path).open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                if cancelled():
                    raise InterruptedError("DICOM SEG export cancelled before delivery")
                digest.update(block)
    return digest.hexdigest()


def _encoded_order(structures, overlap_priority):
    """Segment numbers follow a ranked export's order, otherwise the sorted IDs."""
    if overlap_priority is None:
        return structures
    selected = {item["id"]: item for item in structures}
    return tuple(selected[key] for key in overlap_priority)


def _patient_id(path):
    value = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=["PatientID"]).get("PatientID", "")
    # A backslash is illegal in LO, but pydicom splits on it and Orthanc keeps the text.
    return ("\\".join(value) if isinstance(value, pydicom.multival.MultiValue) else str(value)).strip()


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
    kind = "orthanc_export"
    result_key = "dicom_seg"
    encoding_version = EXPORT_ENCODING_VERSION
    label = "DICOM SEG"
    interrupted_error = "Export interrupted. Retry to reuse its exact SEG bytes and check any existing Orthanc delivery job."
    failed_error = "DICOM SEG export failed. Check the original CT and saved masks, then retry."
    undelivered_error = "Orthanc could not deliver every SEG instance. An explicit retry reuses the same SOP instances."
    unconfirmed_error = (
        "Delivery is not confirmed. Check the destination before retrying; a retry reuses the exact SEG instances and checks any known Orthanc job."
    )
    still_active_error = "An earlier Orthanc delivery may still be active. Check the destination or explicitly retry to check its status."
    lost_job_error = "Orthanc no longer has the delivery job, so the SEG may already have arrived. Check the destination before retrying; a retry sends the same SEG instances again."
    file_interrupted_error = "Export interrupted. Retry to reuse its exact SEG bytes."
    # None words it from the label.
    disabled_error = None
    # For an export of several instances that stopped with only some stored; None keeps the texts above.
    partial_error = None
    offers_file = True
    artifact_adapter = _ARTIFACT

    def __init__(self, service):
        self.service = service

    @property
    def noun(self):
        """What texts call the exported instances, where a plural reads better than the label."""
        return self.label

    def _directory(self, case_id, export_id):
        return self.service.store.case_dir(case_id) / "dicom-exports" / export_id

    def _reference(self, case_id, revision_id, *, archive=True):
        store = self.service.store
        revision = store.revision(case_id, revision_id)
        case = store.get("cases", case_id)
        if archive and not self.service.settings.orthanc_url:
            raise ExportUnavailable(f"Configure Orthanc before exporting {self.noun}.")
        try:
            reference = reference_image(revision)
        except (KeyError, OSError, ValueError):
            raise ExportUnavailable("This saved revision's reference image cannot be verified.") from None
        if reference["modality"] != "CT":
            raise ExportUnavailable(f"{self.label} export currently supports original classic CT sources only.")
        return revision, case, reference

    def _inputs(self, case_id, revision_id, structure_ids=None, *, delivery="orthanc"):
        archive = delivery == "orthanc"
        revision, case, reference = self._reference(case_id, revision_id, archive=archive)
        structures = {item["id"]: item for item in revision["structures"]}
        selected = sorted(structure_ids if structure_ids is not None else (key for key, item in structures.items() if item.get("voxel_count", 1)))
        if not selected or len(selected) != len(set(selected)) or any(key not in structures for key in selected):
            raise ExportUnavailable("Select one or more distinct structures from this saved revision.")
        if any(not structures[key].get("voxel_count", 1) for key in selected):
            raise ExportUnavailable(f"Empty masks cannot be exported as {self.noun}.")
        selection = tuple(structures[key] for key in selected)
        source, directory, seg_source = self._series_source(case_id, case, revision, reference, connect=archive)
        return _Inputs(revision, reference, source, directory, selection, seg_source)

    def _series_source(self, case_id, case, revision, reference, *, connect):
        """The CT series an export binds to: a retained archive import, or else a folder or ZIP import."""
        if not connect and "source_manifest_sha256" not in reference["source"]:
            return None, None, self._local_source(case_id, case, revision, reference)
        source, directory = self._source(case_id, case, reference, connect=connect)
        seg_source = _SegSource(
            tuple(directory / item.path for item in source.files),
            reference["source"]["source_manifest_sha256"],
            source.content_digest,
            source.snapshot.study_uid,
            source.frame_of_reference_uid,
            source.snapshot.snapshot,
            source.snapshot.source_key,
        )
        return source, directory, seg_source

    def _local_source(self, case_id, case, revision, reference):
        """A folder or ZIP import. Its bytes are hashed in the job, not on each request."""
        metadata = reference["source"]
        try:
            digest = prepared_digest(self.service, case_id, revision["ct_file"])
            if digest is None:
                raise ValueError()
            paths = tuple(series_files(self.service, case_id, case, metadata))
        except (KeyError, OSError, ValueError):
            raise ExportUnavailable(
                f"This saved revision has no verified original DICOM CT series. {self.label} needs a CT imported and prepared from DICOM."
            ) from None
        return _SegSource(paths, digest, digest, metadata["study_uid"], metadata["frame_of_reference_uid"])

    def _source(self, case_id, case, reference, *, connect=True):
        store = self.service.store
        source_metadata = reference["source"]
        try:
            retained = series_source(case, source_metadata["series_uid"])
            source = parse_source(retained["orthanc"])
            directory = Path(retained["source_dir"])
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
            if connect:
                with self.service.orthanc.client() as client:
                    if client.source_key != source.snapshot.source_key:
                        raise ValueError()
        except (KeyError, OSError, ValueError):
            raise ExportUnavailable(
                "This saved revision has no verified original Orthanc CT source. Reimport and prepare its original CT before export."
            ) from None
        return source, directory

    def availability(self, case_id, revision_id, *, archive=True, kinds=None):
        """``kinds`` lets one dialog list the jobs of every format it offers, newest first."""
        # Resolve saved identity first: candidates and revisions from another
        # case are never treated as exportable snapshots.
        self.service.store.revision(case_id, revision_id)
        kinds = kinds or (self.kind,)
        exports = [public_job(job) for job in self.service.store.list("jobs", case_id) if job["kind"] in kinds and job.get("revision_id") == revision_id]
        try:
            if not archive:
                raise ExportUnavailable(ARCHIVE_OFF)
            self._inputs(case_id, revision_id)
            with self.service.orthanc.client() as client:
                destinations = client.destinations(self.service.settings.orthanc_export_destinations)
        except (ExportUnavailable, OrthancError, NotFound) as exc:
            reason = str(exc) if isinstance(exc, ExportUnavailable) else "The configured Orthanc archive is unavailable. Retry its connection."
            result = {"available": False, "reason": reason, "destinations": [], "exports": exports, "encoding_version": self.encoding_version}
        else:
            result = {"available": True, "destinations": destinations, "exports": exports, "encoding_version": self.encoding_version}
        if self.offers_file:
            result["file"] = self._file_availability(case_id, revision_id)
        return result

    def _file_availability(self, case_id, revision_id):
        try:
            self._inputs(case_id, revision_id, delivery="file")
        except ExportUnavailable as exc:
            return {"available": False, "reason": str(exc)}
        return {"available": True}

    def start(self, case_id, revision_id, structure_ids=None, destination=None, *, resend=False, overlap_priority=None, delivery="orthanc"):
        file_delivery = delivery == "file"
        if file_delivery and (destination is not None or resend):
            raise ExportUnavailable(f"A {self.label} file has no destination.")
        inputs = self._inputs(case_id, revision_id, structure_ids, delivery=delivery)
        if overlap_priority is not None and (len(overlap_priority) < 2 or sorted(overlap_priority) != [item["id"] for item in inputs.structures]):
            raise ExportUnavailable("Rank every selected structure exactly once to resolve overlaps.")
        display_name = self._destination(destination, resend)
        identity, selection_key, input_key, changed_inputs = self._frozen_inputs(revision_id, inputs)
        encoding = {**identity, "encoding_version": EXPORT_ENCODING_VERSION}
        # A ranking changes the output, not the inputs. Keeping it out of the
        # selection and input keys lets the changed-mask guard compare it too.
        if overlap_priority is not None:
            encoding["overlap_priority"] = overlap_priority
        artifact_key = hashlib.sha256(json.dumps(encoding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        export_key = self._export_key(artifact_key, destination, file_delivery)
        fields = {
            **identity,
            "encoding_version": EXPORT_ENCODING_VERSION,
            "input_key": input_key,
            "artifact_key": artifact_key,
            "selection_key": selection_key,
            "export_key": export_key,
        }
        extras = {"overlap_priority": overlap_priority} if overlap_priority is not None else {}
        if file_delivery:
            extras["delivery"] = "file"

        def summarize(payload):
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
            if file_delivery:
                summary["file"] = True
            if overlap_priority is not None:
                summary["overlap_priority"] = overlap_priority
            return summary

        return self._enqueue(case_id, revision_id, fields, extras, destination, display_name, resend, summarize, guard=changed_inputs)

    def _export_key(self, artifact_key, destination, file_delivery):
        """One export of one artifact to one place. A destination cannot contain a colon, so a file never shares its key."""
        return hashlib.sha256((artifact_key + ":" + ("file:" if file_delivery else destination or "")).encode()).hexdigest()

    def _frozen_inputs(self, revision_id, inputs):
        """The saved masks an export binds to, its keys, and the guard that refuses a mask changed since an earlier export."""
        identity = {
            "revision_id": revision_id,
            "reference_image_id": inputs.reference["id"],
            "source_manifest_sha256": inputs.seg_source.manifest_sha256,
            "structures": [_frozen_structure(item) for item in inputs.structures],
        }
        selection = {**identity, "structures": [{key: value for key, value in item.items() if key != "mask_stamp"} for item in identity["structures"]]}
        selection_key = hashlib.sha256(json.dumps(selection, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        input_key = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

        def changed_inputs(previous):
            # Encoding upgrades create fresh SOPs, but cannot make a changed
            # saved mask look like the same immutable input. Legacy artifacts
            # used their input hash directly as the artifact key.
            if any(
                job["payload"].get("selection_key") == selection_key and job["payload"].get("input_key", job["payload"]["artifact_key"]) != input_key
                for job in previous
            ):
                raise ExportUnavailable("Saved masks changed since this revision's earlier export. Save a new revision before exporting those masks.")

        return identity, selection_key, input_key, changed_inputs

    def _destination(self, destination, resend):
        if resend and destination is None:
            raise ExportUnavailable("Choose a configured destination before requesting Send again.")
        if destination is None:
            return None
        if destination not in self.service.settings.orthanc_export_destinations:
            raise ExportUnavailable(f"Select a configured {self.label} destination.")
        with self.service.orthanc.client() as client:
            destinations = {item["id"]: item for item in client.destinations(self.service.settings.orthanc_export_destinations)}
        if destination not in destinations:
            raise ExportUnavailable(f"The selected {self.label} destination is unavailable.")
        return destinations[destination]["name"]

    def _enqueue(self, case_id, revision_id, fields, extras, destination, display_name, resend, summarize, *, guard=None, publish=None):
        """Dedupe and queue in one write transaction; publish runs only for a new job."""
        artifact_key, export_key = fields["artifact_key"], fields["export_key"]
        with self.service.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute("SELECT data FROM jobs WHERE case_id=? AND json_extract(data,'$.kind')=? ORDER BY rowid DESC", (case_id, self.kind)).fetchall()
            previous = [json.loads(row["data"]) for row in rows]
            if guard:
                guard(previous)
            matching = next((job for job in previous if job["payload"]["export_key"] == export_key), None)
            if matching and matching["status"] in {"queued", "running"}:
                return matching
            if resend and not (matching and matching["status"] == "completed" and matching.get("result", {}).get(self.result_key, {}).get("sent")):
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
                **fields,
                "export_id": artifact_job["payload"]["export_id"] if artifact_job else identifier(),
                "destination": destination,
                "destination_name": display_name,
                "delivery_attempt_id": matching["payload"]["delivery_attempt_id"] if matching and not resend else identifier(),
            }
            payload.update(extras)
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
                kind=self.kind,
                status="queued",
                stage="Queued",
                error=None,
                revision_id=revision_id,
                created_at=now(),
                cancel_requested=False,
                payload=payload,
                requested_by=actor_id(),
            )
            job["result"] = {self.result_key: summarize(payload)}
            con.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job["id"], case_id, job["status"], json.dumps(job, allow_nan=False)))
            if publish:
                publish(payload)
        return job

    def _check_inputs(self, job):
        payload = job["payload"]
        if payload.get("encoding_version", 1) != EXPORT_ENCODING_VERSION:
            raise ExportUnavailable("The DICOM SEG encoder was updated. Refresh export status and start a new export from the saved revision.")
        inputs = self._inputs(job["case_id"], job["revision_id"], [item["id"] for item in payload["structures"]], delivery=payload.get("delivery", "orthanc"))
        if (
            inputs.reference["id"] != payload["reference_image_id"]
            or inputs.seg_source.manifest_sha256 != payload["source_manifest_sha256"]
            or [_frozen_structure(item) for item in inputs.structures] != payload["structures"]
        ):
            raise ExportUnavailable("The saved revision changed after this export was queued. Start a fresh export from a saved revision.")
        self._verify_source(inputs, job)
        return inputs

    def _verify_source(self, inputs, job):
        """The source files must still hold the bytes this export binds to."""
        if inputs.source is not None:
            self.service.orthanc._verify(inputs.source_dir, inputs.source.snapshot, inputs.source, cancelled=lambda: self.service.cancelled(job))
        elif _hash_series(inputs.seg_source.paths, lambda: self.service.cancelled(job)) != inputs.seg_source.content_sha256:
            raise ExportUnavailable("The original CT files no longer match the saved reference image. Reimport and prepare the CT before export.")

    def _artifact(self, job, directory):
        from .dicom_seg import DicomSegError, SegmentInput, export_segmentation

        inputs = self._check_inputs(job)
        overlap_priority = job["payload"].get("overlap_priority")
        manifest_path = directory / "export.json"
        if manifest_path.exists():
            artifact = self._cached_manifest(job, manifest_path, _ARTIFACT)
        else:
            self.service.stage(job, "Resolving overlaps and encoding DICOM SEG" if overlap_priority else "Encoding exact saved masks as DICOM SEG")
            segments = tuple(SegmentInput(item["id"], item["name"], item["color"], Path(item["mask_file"])) for item in inputs.structures)
            try:
                result = export_segmentation(
                    list(inputs.seg_source.paths),
                    Path(inputs.revision["ct_file"]),
                    segments,
                    directory / "dicom",
                    revision_id=job["revision_id"],
                    source_kind=inputs.revision["source"],
                    cancelled=lambda: self.service.cancelled(job),
                    progress=lambda completed, total: self.service.store.update_job(
                        job["id"], progress={"phase": "dicom_seg", "completed": completed, "total": total}
                    ),
                    overlap_priority=overlap_priority,
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
                source_manifest_sha256=inputs.seg_source.manifest_sha256,
                source_content_sha256=inputs.seg_source.content_sha256,
                source_snapshot=inputs.seg_source.snapshot,
                archive_key=inputs.seg_source.archive_key,
                source_kind=inputs.revision["source"],
                series_instance_uid=result.series_instance_uid,
                study_instance_uid=result.study_instance_uid,
                frame_of_reference_uid=result.frame_of_reference_uid,
                created_at=now(),
                encoding_version=job["payload"]["encoding_version"],
                overlap_priority=overlap_priority,
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
                                segment.removed_voxel_count,
                            )
                            for segment in item.segments
                        ),
                    )
                    for item in result.files
                ),
            )
            # Persist the generated SOP identities before any network write.
            # Unranked manifests keep their exact fields, so a rollback reads them.
            artifact = _ARTIFACT.validate_python(artifact)
            atomic_json(manifest_path, _ARTIFACT.dump_python(artifact, mode="json", exclude_defaults=True))
            self._check_inputs(job)
        segments = tuple(segment for item in artifact.files if isinstance(item, _File) for segment in item.segments)
        encoded = _encoded_order(inputs.structures, overlap_priority)
        if (
            artifact.export_id != job["payload"]["export_id"]
            or artifact.artifact_key != job["payload"]["artifact_key"]
            or artifact.encoding_version != job["payload"]["encoding_version"]
            or artifact.overlap_priority != (None if overlap_priority is None else tuple(overlap_priority))
            or artifact.case_id != job["case_id"]
            or artifact.revision_id != job["revision_id"]
            or artifact.reference_image_id != inputs.reference["id"]
            or artifact.reference_sha256 != inputs.reference["content_sha256"]
            or artifact.source_manifest_sha256 != inputs.seg_source.manifest_sha256
            or artifact.source_content_sha256 != inputs.seg_source.content_sha256
            or artifact.source_snapshot != inputs.seg_source.snapshot
            or artifact.archive_key != inputs.seg_source.archive_key
            or artifact.source_kind != inputs.revision["source"]
            or artifact.study_instance_uid != inputs.seg_source.study_uid
            or artifact.frame_of_reference_uid != inputs.seg_source.frame_of_reference_uid
            or len(artifact.files) != 1
            or any(not isinstance(item, _File) for item in artifact.files)
            or [segment.structure_id for segment in segments] != [item["id"] for item in encoded]
            or [segment.segment_number for segment in segments] != list(range(1, len(segments) + 1))
            or len({item.sop_instance_uid for item in artifact.files}) != len(artifact.files)
            or len({item.path for item in artifact.files}) != len(artifact.files)
        ):
            raise ExportUnavailable("The cached DICOM SEG export differs from its frozen source or saved revision.")
        self._check_files(job, directory, artifact)
        for segment, selected in zip(segments, encoded, strict=True):
            if (
                _hash(selected["mask_file"], lambda: self.service.cancelled(job)) != segment.mask_sha256
                or segment.name != selected["name"]
                or segment.color != selected["color"]
            ):
                raise ExportUnavailable("The saved masks or prepared DICOM SEG bytes changed. Delivery was blocked.")
        self._pin_manifest(job, manifest_path)
        return artifact

    def _cached_manifest(self, job, manifest_path, adapter):
        try:
            if job["payload"].get("artifact_manifest_sha256", _hash(manifest_path)) != _hash(manifest_path):
                raise ExportUnavailable(f"The cached {self.label} export manifest changed. Delivery was blocked.")
            return adapter.validate_json(manifest_path.read_bytes())
        except (OSError, ValidationError):
            raise ExportUnavailable(f"The cached {self.label} export is invalid. Its bytes were retained for inspection.") from None

    def _check_files(self, job, directory, artifact):
        for output in artifact.files:
            path = directory / "dicom" / output.path
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_size != output.size
                or _hash(path, lambda: self.service.cancelled(job)) != output.sha256
            ):
                raise ExportUnavailable(f"The prepared {self.label} bytes changed. Delivery was blocked.")

    def _pin_manifest(self, job, manifest_path):
        self.service.check_cancel(job)
        job["payload"]["artifact_manifest_sha256"] = _hash(manifest_path)
        self.service.store.update_job(job["id"], payload=job["payload"])

    def prepared_file(self, case_id, revision_id, export_id):
        """The verified bytes of a finished file export, or NotFound for anything else."""
        self.service.store.revision(case_id, revision_id)
        job = next(
            (
                job
                for job in self.service.store.list("jobs", case_id)
                if job["kind"] == self.kind
                and job.get("revision_id") == revision_id
                and job["status"] == "completed"
                and job["payload"].get("delivery") == "file"
                and job["payload"]["export_id"] == export_id
            ),
            None,
        )
        if job is None:
            raise NotFound(f"No prepared {self.label} file")
        directory = self._directory(case_id, export_id)
        artifact = self._cached_manifest(job, directory / "export.json", self.artifact_adapter)
        self._check_files(job, directory, artifact)
        return directory / "dicom" / artifact.files[0].path

    def _check_study(self, inputs, path):
        """Refuse before upload if the instance would not join the source study.

        Orthanc derives a study's identity from the patient ID as it decodes it.
        A source whose bytes disagree with its declared character set decodes
        differently once rewritten as UTF-8, so the instance would open a second
        patient and study while the upload still reports success.
        """
        snapshot = inputs.source.snapshot
        patient_id = _patient_id(path)
        try:
            with self.service.orthanc.client() as client:
                study = client.study(snapshot.study_id)
        except NotFound:
            raise ExportUnavailable("The original study is no longer in Orthanc. Nothing was stored.") from None
        if study["study_uid"] != snapshot.study_uid or study["patient_id"].strip() != patient_id:
            raise ExportUnavailable("Orthanc files the original study under a different patient ID from the source images. Nothing was stored.")

    def _check_upload(self, job, upload):
        """Runs after each upload, before its receipt is saved or any send starts."""

    def recover(self):
        """Recovery is local only; a user request must resume any remote job."""
        for job in self.service.store.list("jobs"):
            if job["kind"] != self.kind:
                continue
            if job["status"] == "queued" or job["status"] == "failed" and job.get("stage") == "Interrupted":
                self.service.store.update_job(
                    job["id"],
                    status="failed",
                    stage="Interrupted",
                    completed_at=now(),
                    error=self.file_interrupted_error if job["payload"].get("delivery") == "file" else self.interrupted_error,
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
                    error=self.still_active_error if active else None,
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
        if artifact.overlap_priority:
            result["overlap_priority"] = list(artifact.overlap_priority)
            result["removed_voxels"] = {segment.structure_id: segment.removed_voxel_count for segment in segments}
        return {"dicom_seg": result}

    def run(self, job):
        directory = self._directory(job["case_id"], job["payload"]["export_id"])
        artifact, receipt, sending = None, None, False
        destination = job["payload"]["destination"]
        file_delivery = job["payload"].get("delivery") == "file"
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
            if receipt_path.exists() and not file_delivery:
                try:
                    receipt = _RECEIPT.validate_json(receipt_path.read_bytes())
                except (OSError, ValidationError):
                    raise ExportUnavailable(f"The {self.label} delivery receipt cannot be verified. Delivery was blocked.") from None
                previous_delivery = receipt.destinations.get(destination)
                sending = previous_delivery is not None and previous_delivery.state != "failure"
            artifact = self._artifact(job, directory)
            if file_delivery:
                # Nothing leaves this machine, so there is no delivery to record.
                result = self._result(artifact, _Receipt(job["payload"]["artifact_manifest_sha256"], {}, {}), None, None)
                result[self.result_key]["file"] = True
                self.service.store.update_job(job["id"], status="completed", stage=f"{self.label} file ready", error=None, completed_at=now(), result=result)
                return
            if receipt:
                if receipt.manifest_sha256 != job["payload"]["artifact_manifest_sha256"] or not set(receipt.uploaded).issubset(
                    item.path for item in artifact.files
                ):
                    raise ExportUnavailable(f"The delivery receipt does not belong to this {self.label} artifact.")
            else:
                receipt = _Receipt(job["payload"]["artifact_manifest_sha256"], {}, {})
            with self.service.orthanc.client() as client:
                for number, item in enumerate(artifact.files, 1):
                    count = f" ({number} of {len(artifact.files)})" if len(artifact.files) > 1 else ""
                    stage = f"Archiving {self.noun} in Orthanc{count}"
                    if number > 1 and not self.service.stop_event.is_set():
                        # Once one instance is stored, a cancel waits for the rest, so the archive
                        # never keeps part of an export. The check after this loop still stops a send.
                        self.service.store.update_job(job["id"], stage=stage)
                    else:
                        self.service.stage(job, stage)
                    # Orthanc may have been restored or changed independently.
                    # Reuploading identical SOP bytes is idempotent and verifies
                    # their readback before trusting a cached instance ID.
                    upload = client.upload_instance(directory / "dicom" / item.path, expected_sha256=item.sha256)
                    self._check_upload(job, upload)
                    receipt.uploaded[item.path] = upload["id"]
                    save_receipt()
                self.service.check_cancel(job)
                if destination:
                    if destination not in self.service.settings.orthanc_export_destinations:
                        raise ExportUnavailable(self.disabled_error or f"The selected destination is no longer enabled. {self.label} remains archived.")
                    delivery = receipt.destinations.get(destination)
                    if delivery is None or delivery.state != "success" or delivery.attempt_id != job["payload"]["delivery_attempt_id"]:
                        self.service.stage(job, f"Sending {self.noun} through Orthanc")
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
                        self.service.store.update_job(job["id"], stage=f"Checking Orthanc {self.label} delivery")
                        deadline = time.monotonic() + DELIVERY_WAIT_SECONDS
                        while True:
                            try:
                                remote = client.store_job(delivery.job_id)
                            except NotFound:
                                # Orthanc forgets old jobs, so this one can never be checked. Without
                                # a job ID, only another explicit retry starts a fresh send.
                                delivery = _Delivery("unknown", None, delivery.attempt_id)
                                receipt.destinations[destination] = delivery
                                save_receipt()
                                raise ExportUnavailable(self.lost_job_error) from None
                            state = remote["state"]
                            if state in {"Success", "Failure"}:
                                success = state == "Success" and remote["instances_count"] == len(artifact.files) and remote["failed_instances_count"] == 0
                                delivery = replace(delivery, state="success" if success else "failure")
                                receipt.destinations[destination] = delivery
                                save_receipt()
                                job["payload"]["delivery_state"] = delivery.state
                                self.service.store.update_job(job["id"], payload=job["payload"])
                                if not success:
                                    raise ExportUnavailable(self.undelivered_error)
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
                stage=f"{self.noun} sent" if destination else f"{self.noun} archived",
                error=None,
                completed_at=now(),
                result=self._result(artifact, receipt, destination, job["payload"]["destination_name"]),
            )
        except Exception as exc:
            stored = len(set(receipt.uploaded) & {item.path for item in artifact.files}) if artifact and receipt else 0
            # Only an export of several instances can stop with some of them in the archive. It must say so.
            partial = self.partial_error is not None and 0 < stored < len(artifact.files)
            cancelled = isinstance(exc, InterruptedError) and not sending and not partial
            if isinstance(exc, ExportUnavailable):
                error = str(exc)
            elif sending:
                error = self.unconfirmed_error
            else:
                error = self.partial_error.format(stored=stored, total=len(artifact.files)) if partial else self.failed_error
            if not cancelled:
                log.warning("%s export %s failed (%s)", self.label, job["id"], type(exc).__name__)
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
