"""Archive and send a rendered 3D view as a new series in its source study.

The base class owns queuing, retries, receipts and delivery uncertainty. This
adds what a picture needs instead: a capture retained by content, so a retry
that moves to a new export directory can still read it, and proof that the
instance landed in the source study. Orthanc reports success either way, and a
render filed under a split patient cannot be spotted by looking at it.
"""

from dataclasses import dataclass
import hashlib
import json
import os
from typing import Annotated
import uuid

from pydantic import ConfigDict, Field, TypeAdapter
import pydicom

from .errors import NotFound
from .fileio import atomic_json
from .orthanc_exports import ExportUnavailable, OrthancExports, _hash, _Inputs
from .orthanc_models import DicomUID, Digest, Positive
from .store import now

VIEW_ENCODING_VERSION = 1


@dataclass(frozen=True, slots=True)
class _ViewFile:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    path: Annotated[str, Field(pattern=r"^view-[0-9]{4}\.dcm$")]
    sop_instance_uid: DicomUID
    sha256: Digest
    size: Positive


@dataclass(frozen=True, slots=True)
class _ViewArtifact:
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
    png_sha256: Digest
    rows: Positive
    columns: Positive
    created_at: str
    files: Annotated[tuple[_ViewFile, ...], Field(min_length=1, max_length=1)]
    encoding_version: Positive


_ARTIFACT = TypeAdapter(_ViewArtifact)


class OrthancViewExports(OrthancExports):
    kind = "orthanc_view_export"
    result_key = "dicom_view"
    encoding_version = VIEW_ENCODING_VERSION
    label = "DICOM view"
    short_label = "view"
    interrupted_error = "Export interrupted. Retry to reuse its exact view image and check any existing Orthanc delivery job."
    failed_error = "DICOM view export failed. Check the original CT, then retry."

    def _inputs(self, case_id, revision_id, structure_ids=None):
        revision, case, reference = self._reference(case_id, revision_id)
        source, directory = self._source(case_id, case, reference)
        return _Inputs(revision, reference, source, directory, ())

    def _captures(self, case_id):
        return self.service.store.case_dir(case_id) / "dicom-view-captures"

    def _retained(self, case_id, png_sha256):
        return self._captures(case_id) / f"{png_sha256}.png"

    def _retained_intact(self, case_id, png_sha256, cancelled=None):
        png = self._retained(case_id, png_sha256)
        try:
            return not png.is_symlink() and png.is_file() and _hash(png, cancelled) == png_sha256
        except InterruptedError:
            raise  # A subclass of OSError, but this is a cancellation.
        except OSError:
            return False

    def _stage(self, stream, staging):
        limit = self.service.settings.view_upload_limit
        digest, total = hashlib.sha256(), 0
        with os.fdopen(os.open(staging, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
            while block := stream.read(1024 * 1024):
                total += len(block)
                if total > limit:
                    raise ExportUnavailable("The captured view exceeds the upload limit. Make the pane smaller and capture again.")
                digest.update(block)
                output.write(block)
        return digest.hexdigest()

    def start(self, case_id, revision_id, stream, reference_image_id, destination=None, *, resend=False):
        """Queue one capture; ``stream`` is the request's uploaded file object."""
        from .dicom_view import read_view_png

        # Refuse everything that needs no bytes first: case_dir creates
        # directories for any well-formed ID, and a decode costs up to a second.
        inputs = self._inputs(case_id, revision_id)
        display_name = self._destination(destination, resend)
        if reference_image_id != inputs.reference["id"]:
            raise ExportUnavailable("This capture shows a different image from this saved revision. Capture the view again.")
        captures = self._captures(case_id)
        captures.mkdir(mode=0o700, exist_ok=True)
        # On the data filesystem, unlike the upload spool, so publishing is a rename.
        staging = captures / f".staging-{uuid.uuid4().hex}"
        try:
            png_sha256 = self._stage(stream, staging)
            read_view_png(staging)
            identity = {
                "revision_id": revision_id,
                "reference_image_id": inputs.reference["id"],
                "source_manifest_sha256": inputs.reference["source"]["source_manifest_sha256"],
                "png_sha256": png_sha256,
            }
            encoding = {**identity, "encoding_version": self.encoding_version}
            artifact_key = hashlib.sha256(json.dumps(encoding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            export_key = hashlib.sha256((artifact_key + ":" + (destination or "")).encode()).hexdigest()
            fields = {**encoding, "orthanc_study_id": inputs.source.snapshot.study_id, "artifact_key": artifact_key, "export_key": export_key}

            def summarize(payload):
                summary = {"export_id": payload["export_id"], "encoding_version": self.encoding_version, "archived": False, "sent": False, "instance_count": 1}
                if destination:
                    summary.update(destination=display_name, destination_id=destination)
                return summary

            def publish(_payload):
                # A running encode stamps this file, so an intact copy keeps
                # its inode. Inside the transaction, so POSTs cannot race this.
                if not self._retained_intact(case_id, png_sha256):
                    os.replace(staging, self._retained(case_id, png_sha256))

            return self._enqueue(case_id, revision_id, fields, {}, destination, display_name, resend, summarize, publish=publish)
        finally:
            staging.unlink(missing_ok=True)

    def _check_inputs(self, job):
        payload = job["payload"]
        if payload.get("encoding_version", 1) != self.encoding_version:
            raise ExportUnavailable("The DICOM view encoder was updated. Capture the view again.")
        inputs = self._inputs(job["case_id"], job["revision_id"])
        if (
            inputs.reference["id"] != payload["reference_image_id"]
            or inputs.reference["source"]["source_manifest_sha256"] != payload["source_manifest_sha256"]
            or inputs.source.snapshot.study_id != payload["orthanc_study_id"]
        ):
            raise ExportUnavailable("The saved revision changed after this export was queued. Capture the view again.")
        if not self._retained_intact(job["case_id"], payload["png_sha256"], lambda: self.service.cancelled(job)):
            raise ExportUnavailable("The retained view image is unavailable or changed. Capture the view again.")
        self.service.orthanc._verify(inputs.source_dir, inputs.source.snapshot, inputs.source, cancelled=lambda: self.service.cancelled(job))
        return inputs

    def _check_study(self, inputs, path):
        """Refuse before upload if the instance would not join the source study.

        Orthanc derives a study's identity from the patient ID as it decodes it.
        A source whose bytes disagree with its declared character set decodes
        differently once rewritten as UTF-8, so the instance would open a second
        patient and study while the upload still reports success.
        """
        snapshot = inputs.source.snapshot
        value = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=["PatientID"]).get("PatientID", "")
        # A backslash is illegal in LO, but pydicom splits on it and Orthanc keeps the text.
        patient_id = ("\\".join(value) if isinstance(value, pydicom.multival.MultiValue) else str(value)).strip()
        try:
            with self.service.orthanc.client() as client:
                study = client.study(snapshot.study_id)
        except NotFound:
            raise ExportUnavailable("The original study is no longer in Orthanc. Nothing was stored.") from None
        if study["study_uid"] != snapshot.study_uid or study["patient_id"].strip() != patient_id:
            raise ExportUnavailable("Orthanc files the original study under a different patient ID from the source images. Nothing was stored.")

    def _artifact(self, job, directory):
        from .dicom_view import DicomViewError, export_view

        inputs = self._check_inputs(job)
        payload = job["payload"]
        manifest_path = directory / "export.json"
        if manifest_path.exists():
            artifact = self._cached_manifest(job, manifest_path, _ARTIFACT)
        else:
            self.service.stage(job, "Encoding view as DICOM")
            try:
                result = export_view(
                    [inputs.source_dir / item.path for item in inputs.source.files],
                    self._retained(job["case_id"], payload["png_sha256"]),
                    directory / "dicom",
                    revision_id=job["revision_id"],
                    cancelled=lambda: self.service.cancelled(job),
                )
            except DicomViewError as exc:
                raise ExportUnavailable(str(exc)) from None
            artifact = _ViewArtifact(
                export_id=payload["export_id"],
                artifact_key=payload["artifact_key"],
                case_id=job["case_id"],
                revision_id=job["revision_id"],
                reference_image_id=inputs.reference["id"],
                reference_sha256=inputs.reference["content_sha256"],
                source_manifest_sha256=inputs.reference["source"]["source_manifest_sha256"],
                source_content_sha256=inputs.source.content_digest,
                source_snapshot=inputs.source.snapshot.snapshot,
                archive_key=inputs.source.snapshot.source_key,
                source_kind=inputs.revision["source"],
                series_instance_uid=result.series_instance_uid,
                study_instance_uid=result.study_instance_uid,
                png_sha256=payload["png_sha256"],
                rows=result.rows,
                columns=result.columns,
                created_at=now(),
                files=(_ViewFile(result.path.name, result.sop_instance_uid, result.sha256, result.size),),
                encoding_version=payload["encoding_version"],
            )
            # Persist the generated SOP identity before any network write.
            artifact = _ARTIFACT.validate_python(artifact)
            atomic_json(manifest_path, _ARTIFACT.dump_python(artifact, mode="json"))
            self._check_inputs(job)
        if (
            artifact.export_id != payload["export_id"]
            or artifact.artifact_key != payload["artifact_key"]
            or artifact.encoding_version != payload["encoding_version"]
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
            or artifact.png_sha256 != payload["png_sha256"]
        ):
            raise ExportUnavailable("The cached DICOM view export differs from its frozen source or saved revision.")
        self._check_files(job, directory, artifact)
        self._check_study(inputs, directory / "dicom" / artifact.files[0].path)
        self._pin_manifest(job, manifest_path)
        return artifact

    def _check_upload(self, job, upload):
        # The instance stays in the archive for inspection: never delete data here.
        if upload.get("parent_study") != job["payload"]["orthanc_study_id"]:
            raise ExportUnavailable("Orthanc stored this view outside the original study, so it was not sent. Check the study in Orthanc.")

    def _result(self, artifact, receipt, destination, display_name):
        result = {
            "export_id": artifact.export_id,
            "encoding_version": artifact.encoding_version,
            "archived": all(item.path in receipt.uploaded for item in artifact.files),
            "sent": bool(destination and destination in receipt.destinations and receipt.destinations[destination].state == "success"),
            "instance_count": len(artifact.files),
        }
        if destination:
            result["destination"] = display_name
            result["destination_id"] = destination
        return {self.result_key: result}
