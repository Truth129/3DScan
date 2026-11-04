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
    source_kind: str
    series_instance_uid: DicomUID
    study_instance_uid: DicomUID
    png_sha256: Digest
    rows: Positive
    columns: Positive
    created_at: str
    files: Annotated[tuple[_ViewFile, ...], Field(min_length=1, max_length=1)]
    encoding_version: Positive
    # A file of a folder or ZIP import was never in an archive, so it has neither.
    source_snapshot: Digest | None = None
    archive_key: Digest | None = None


_ARTIFACT = TypeAdapter(_ViewArtifact)


class OrthancViewExports(OrthancExports):
    kind = "orthanc_view_export"
    result_key = "dicom_view"
    encoding_version = VIEW_ENCODING_VERSION
    label = "DICOM view"
    # A job can end after its dialog closed, and only that dialog can retry, because only it
    # holds the image. So these state the outcome, and the dialog adds the retry while it can.
    interrupted_error = "Export interrupted. Check the study in Orthanc."
    failed_error = "DICOM view export failed. Check the original CT."
    undelivered_error = "Orthanc could not deliver this view. Check the destination."
    unconfirmed_error = "Delivery is not confirmed. Check the destination."
    still_active_error = "An earlier Orthanc delivery may still be active. Check the destination."
    lost_job_error = "Orthanc no longer has the delivery job, so the view may already have arrived. Check the destination."
    offers_file = False
    artifact_adapter = _ARTIFACT
    # A file export reaches no archive, so it needs neither Orthanc nor an archive import.
    file_delivery = False

    def _inputs(self, case_id, revision_id, structure_ids=None):
        revision, case, reference = self._reference(case_id, revision_id, archive=not self.file_delivery)
        source, directory, seg_source = self._series_source(case_id, case, revision, reference, connect=not self.file_delivery)
        return _Inputs(revision, reference, source, directory, (), seg_source)

    def _payload_fields(self, inputs):
        """What a queued job records about its source, and must find unchanged when it runs."""
        return {"orthanc_study_id": inputs.source.snapshot.study_id}

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
                "source_manifest_sha256": inputs.seg_source.manifest_sha256,
                "png_sha256": png_sha256,
            }
            encoding = {**identity, "encoding_version": self.encoding_version}
            artifact_key = hashlib.sha256(json.dumps(encoding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            export_key = self._export_key(artifact_key, destination, self.file_delivery)
            fields = {**encoding, **self._payload_fields(inputs), "artifact_key": artifact_key, "export_key": export_key}
            extras = {"delivery": "file"} if self.file_delivery else {}

            def summarize(payload):
                summary = {"export_id": payload["export_id"], "encoding_version": self.encoding_version, "archived": False, "sent": False, "instance_count": 1}
                if destination:
                    summary.update(destination=display_name, destination_id=destination)
                if self.file_delivery:
                    summary["file"] = True
                return summary

            def publish(_payload):
                # A running encode stamps this file, so an intact copy keeps
                # its inode. Inside the transaction, so POSTs cannot race this.
                if not self._retained_intact(case_id, png_sha256):
                    os.replace(staging, self._retained(case_id, png_sha256))

            return self._enqueue(case_id, revision_id, fields, extras, destination, display_name, resend, summarize, publish=publish)
        finally:
            staging.unlink(missing_ok=True)

    def _check_inputs(self, job):
        payload = job["payload"]
        if payload.get("encoding_version", 1) != self.encoding_version:
            raise ExportUnavailable("The DICOM view encoder was updated. Capture the view again.")
        inputs = self._inputs(job["case_id"], job["revision_id"])
        if (
            inputs.reference["id"] != payload["reference_image_id"]
            or inputs.seg_source.manifest_sha256 != payload["source_manifest_sha256"]
            or any(payload.get(key) != value for key, value in self._payload_fields(inputs).items())
        ):
            raise ExportUnavailable("The saved revision changed after this export was queued. Capture the view again.")
        if not self._retained_intact(job["case_id"], payload["png_sha256"], lambda: self.service.cancelled(job)):
            raise ExportUnavailable("The retained view image is unavailable or changed. Capture the view again.")
        self._verify_source(inputs, job)
        return inputs

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
                    list(inputs.seg_source.paths),
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
                source_manifest_sha256=inputs.seg_source.manifest_sha256,
                source_content_sha256=inputs.seg_source.content_sha256,
                source_snapshot=inputs.seg_source.snapshot,
                archive_key=inputs.seg_source.archive_key,
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
            or artifact.source_manifest_sha256 != inputs.seg_source.manifest_sha256
            or artifact.source_content_sha256 != inputs.seg_source.content_sha256
            or artifact.source_snapshot != inputs.seg_source.snapshot
            or artifact.archive_key != inputs.seg_source.archive_key
            or artifact.source_kind != inputs.revision["source"]
            or artifact.study_instance_uid != inputs.seg_source.study_uid
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
