"""A measurement report as a new DICOM series in its source study: a page of the result, then its overview picture.

The view exporters own delivery, receipts and the checks on the source CT. A report differs in where
its pictures come from: the server draws the page and holds the overview, so any retry can run
without a new capture. Both are retained by content at enqueue, as a capture is, and the export
binds to a complete result, which it reads again before encoding. Each delivery has its own job
kind, so the 3D view's listings never show a report.
"""

from dataclasses import dataclass
import hashlib
from io import BytesIO
import json
import os
from typing import Annotated
import uuid
import zipfile

from pydantic import ConfigDict, Field, TypeAdapter

from .archive.exports import ArchiveViewExports
from .archive.sends import DirectViewExports
from .dicom_view_files import ViewFileExports
from .errors import NotFound
from .fileio import atomic_json
from .orthanc_exports import ExportUnavailable
from .orthanc_models import DicomUID, Digest, Positive
from .orthanc_view_exports import OrthancViewExports, _ViewFile
from .store import now, public_job

REPORT_ENCODING_VERSION = 1
_COMMENTS = (
    "3DScan research measurement report from TotalSegmentator. Not calibrated: do not measure on these images. Research use only. Not for diagnostic use."
)
# The picture after the page, whichever the result lists: the aorta's curved view or the spine picture.
_OVERVIEWS = ("cpr.png", "spine.png")


@dataclass(frozen=True, slots=True)
class _ReportArtifact:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    export_id: str
    artifact_key: Digest
    case_id: str
    revision_id: str
    report: str
    analysis_id: str
    result_key: Digest
    reference_image_id: str
    reference_sha256: Digest
    source_manifest_sha256: Digest
    source_content_sha256: Digest
    source_kind: str
    series_instance_uid: DicomUID
    study_instance_uid: DicomUID
    # In series order: the page, then the overview picture when the result has one.
    png_sha256s: Annotated[tuple[Digest, ...], Field(min_length=1, max_length=2)]
    created_at: str
    files: Annotated[tuple[_ViewFile, ...], Field(min_length=1, max_length=2)]
    encoding_version: Positive
    # A file of a folder or ZIP import was never in an archive, so it has neither.
    source_snapshot: Digest | None = None
    archive_key: Digest | None = None


_ARTIFACT = TypeAdapter(_ReportArtifact)


class _ReportImages:
    """Placed before a view exporter, so its delivery stays and only the pictures change."""

    result_key = "dicom_report"
    encoding_version = REPORT_ENCODING_VERSION
    label = "DICOM report"
    artifact_adapter = _ARTIFACT
    failed_error = "DICOM report export failed. Check the original CT, then retry."
    partial_error = "The export stopped with {stored} of {total} report images in Orthanc. Retry to store the rest as the same instances."

    def _captures(self, case_id):
        return self.service.store.case_dir(case_id) / "dicom-report-images"

    def _result_of(self, case_id, revision_id, report_id, analysis_id):
        """The complete result, or NotFound for a candidate alias, another case's run or one not finished."""
        from . import reports

        return reports.read(self.service, case_id, revision_id, report_id, analysis_id)

    def _pictures(self, case_id, revision_id, report_id, analysis_id, result):
        from . import reports
        from .report_page import render

        images = [render(result, reports.definition(report_id).title)]
        if overview := next((name for name in _OVERVIEWS if name in result["files"]), None):
            images.append(reports.picture(self.service, case_id, revision_id, report_id, analysis_id, overview))
        return images

    def start(self, case_id, revision_id, report_id, analysis_id, destination=None, *, resend=False):
        from .dicom_view import DicomViewError, read_view_png

        result = self._result_of(case_id, revision_id, report_id, analysis_id)
        inputs = self._inputs(case_id, revision_id)
        display_name = self._destination(destination, resend)
        images = self._pictures(case_id, revision_id, report_id, analysis_id, result)
        hashes = [hashlib.sha256(body).hexdigest() for body in images]
        captures = self._captures(case_id)
        captures.mkdir(mode=0o700, exist_ok=True)
        staged = []
        try:
            for body in images:
                # On the data filesystem, so publishing is a rename.
                staged.append(captures / f".staging-{uuid.uuid4().hex}")
                with os.fdopen(os.open(staged[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as output:
                    output.write(body)
                try:
                    read_view_png(staged[-1])
                except DicomViewError:
                    raise ExportUnavailable("The report's pictures cannot be made into DICOM images. Run the report again.") from None
            identity = {
                "revision_id": revision_id,
                "reference_image_id": inputs.reference["id"],
                "source_manifest_sha256": inputs.seg_source.manifest_sha256,
                "report": report_id,
                "analysis_id": analysis_id,
                "result_key": result["key"],
                "png_sha256s": hashes,
            }
            encoding = {**identity, "encoding_version": self.encoding_version}
            artifact_key = hashlib.sha256(json.dumps(encoding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            export_key = self._export_key(artifact_key, destination, self.file_delivery)
            fields = {**encoding, **self._payload_fields(inputs), "artifact_key": artifact_key, "export_key": export_key}
            extras = {"delivery": "file"} if self.file_delivery else {}

            def summarize(payload):
                summary = {
                    "export_id": payload["export_id"],
                    "encoding_version": self.encoding_version,
                    "archived": False,
                    "sent": False,
                    "instance_count": len(images),
                }
                if destination:
                    summary.update(destination=display_name, destination_id=destination)
                if self.file_delivery:
                    summary["file"] = True
                return summary

            def publish(_payload):
                # Inside the transaction, as a capture is, so POSTs cannot race this.
                for staging, digest in zip(staged, hashes, strict=True):
                    if not self._retained_intact(case_id, digest):
                        os.replace(staging, self._retained(case_id, digest))

            return self._enqueue(case_id, revision_id, fields, extras, destination, display_name, resend, summarize, publish=publish)
        finally:
            for staging in staged:
                staging.unlink(missing_ok=True)

    def _check_inputs(self, job):
        payload = job["payload"]
        if payload.get("encoding_version") != self.encoding_version:
            raise ExportUnavailable("The DICOM report encoder was updated. Start the export again.")
        inputs = self._inputs(job["case_id"], job["revision_id"])
        if (
            inputs.reference["id"] != payload["reference_image_id"]
            or inputs.seg_source.manifest_sha256 != payload["source_manifest_sha256"]
            or any(payload.get(key) != value for key, value in self._payload_fields(inputs).items())
        ):
            raise ExportUnavailable("The saved revision changed after this export was queued. Start the export again.")
        try:
            result = self._result_of(job["case_id"], job["revision_id"], payload["report"], payload["analysis_id"])
        except NotFound:
            result = None
        if result is None or result["key"] != payload["result_key"]:
            raise ExportUnavailable("The report result is missing or changed. Run the report again, then export it.")
        if not all(self._retained_intact(job["case_id"], digest, lambda: self.service.cancelled(job)) for digest in payload["png_sha256s"]):
            raise ExportUnavailable("The retained report images are unavailable or changed. Start the export again.")
        self._verify_source(inputs, job)
        return inputs

    def _artifact(self, job, directory):
        from .dicom_view import DicomViewError, ImageText, export_images

        inputs = self._check_inputs(job)
        payload = job["payload"]
        manifest_path = directory / "export.json"
        if manifest_path.exists():
            artifact = self._cached_manifest(job, manifest_path, _ARTIFACT)
        else:
            self.service.stage(job, "Encoding report as DICOM")
            report, revision_id = payload["report"], job["revision_id"]
            # The report's id in words, as its page says it: "body stats", not "body_stats".
            words = report.replace("_", " ")
            text = ImageText(
                502,
                f"3DScan {words} report {revision_id[:8]}",
                "Measurement report export",
                _COMMENTS,
                f"TotalSegmentator {words} report of 3DScan revision {revision_id}",
            )
            try:
                result = export_images(
                    list(inputs.seg_source.paths),
                    [self._retained(job["case_id"], digest) for digest in payload["png_sha256s"]],
                    directory / "dicom",
                    revision_id=revision_id,
                    text=text,
                    cancelled=lambda: self.service.cancelled(job),
                )
            except DicomViewError as exc:
                raise ExportUnavailable(str(exc)) from None
            artifact = _ReportArtifact(
                export_id=payload["export_id"],
                artifact_key=payload["artifact_key"],
                case_id=job["case_id"],
                revision_id=revision_id,
                report=report,
                analysis_id=payload["analysis_id"],
                result_key=payload["result_key"],
                reference_image_id=inputs.reference["id"],
                reference_sha256=inputs.reference["content_sha256"],
                source_manifest_sha256=inputs.seg_source.manifest_sha256,
                source_content_sha256=inputs.seg_source.content_sha256,
                source_snapshot=inputs.seg_source.snapshot,
                archive_key=inputs.seg_source.archive_key,
                source_kind=inputs.revision["source"],
                series_instance_uid=result.series_instance_uid,
                study_instance_uid=result.study_instance_uid,
                png_sha256s=tuple(payload["png_sha256s"]),
                created_at=now(),
                files=tuple(_ViewFile(item.path.name, item.sop_instance_uid, item.sha256, item.size) for item in result.images),
                encoding_version=payload["encoding_version"],
            )
            # Persist the generated SOP identities before any network write.
            artifact = _ARTIFACT.validate_python(artifact)
            atomic_json(manifest_path, _ARTIFACT.dump_python(artifact, mode="json"))
            self._check_inputs(job)
        if (
            artifact.export_id != payload["export_id"]
            or artifact.artifact_key != payload["artifact_key"]
            or artifact.encoding_version != payload["encoding_version"]
            or artifact.case_id != job["case_id"]
            or artifact.revision_id != job["revision_id"]
            or artifact.report != payload["report"]
            or artifact.analysis_id != payload["analysis_id"]
            or artifact.result_key != payload["result_key"]
            or artifact.reference_image_id != inputs.reference["id"]
            or artifact.reference_sha256 != inputs.reference["content_sha256"]
            or artifact.source_manifest_sha256 != inputs.seg_source.manifest_sha256
            or artifact.source_content_sha256 != inputs.seg_source.content_sha256
            or artifact.source_snapshot != inputs.seg_source.snapshot
            or artifact.archive_key != inputs.seg_source.archive_key
            or artifact.source_kind != inputs.revision["source"]
            or artifact.study_instance_uid != inputs.seg_source.study_uid
            or list(artifact.png_sha256s) != list(payload["png_sha256s"])
            or len(artifact.files) != len(artifact.png_sha256s)
            or len({item.sop_instance_uid for item in artifact.files}) != len(artifact.files)
        ):
            raise ExportUnavailable("The cached DICOM report export differs from its frozen source or saved revision.")
        self._check_files(job, directory, artifact)
        # Every image copies its patient and study from the same source headers, so the first speaks for all.
        self._check_study(inputs, directory / "dicom" / artifact.files[0].path)
        self._pin_manifest(job, manifest_path)
        return artifact

    def availability(self, case_id, revision_id, report_id, analysis_id, *, kinds=None):
        """As the view's, with `exports` this report run's jobs only; `kinds` adds a direct sender's."""
        self._result_of(case_id, revision_id, report_id, analysis_id)
        result = super().availability(case_id, revision_id)
        kinds = kinds or (self.kind,)
        result["exports"] = [
            public_job(job)
            for job in self.service.store.list("jobs", case_id)
            if job["kind"] in kinds
            and job.get("revision_id") == revision_id
            and job["payload"].get("report") == report_id
            and job["payload"].get("analysis_id") == analysis_id
        ]
        return result


class OrthancReportExports(_ReportImages, OrthancViewExports):
    kind = "orthanc_report_export"
    # The server holds the images, so any retry reuses them.
    interrupted_error = "Export interrupted. Check the study in Orthanc, then retry."
    undelivered_error = "Orthanc could not deliver this report. Check the destination, then retry."
    unconfirmed_error = (
        "Delivery is not confirmed. Check the destination before retrying; a retry reuses the exact report images and checks any known Orthanc job."
    )
    still_active_error = "An earlier Orthanc delivery may still be active. Check the destination, or retry to check its status."
    lost_job_error = "Orthanc no longer has the delivery job, so the report may already have arrived. Check the destination before retrying; a retry sends the same report again."

    def _check_upload(self, job, upload):
        # The instance stays in the archive for inspection: never delete data here.
        if upload.get("parent_study") != job["payload"]["orthanc_study_id"]:
            raise ExportUnavailable("Orthanc stored this report outside the original study, so it was not sent. Check the study in Orthanc.")


class ReportFileExports(_ReportImages, ViewFileExports):
    kind = "report_file_export"
    interrupted_error = file_interrupted_error = "Export interrupted. Retry to prepare the file again."

    def _destination(self, destination, resend):
        if destination is not None or resend:
            raise ExportUnavailable("A DICOM report file has no destination.")
        return None

    def prepared_zip(self, case_id, revision_id, report_id, analysis_id, export_id):
        """A finished file export's images as one stored ZIP, each checked against its artifact, or NotFound."""
        self.service.store.revision(case_id, revision_id)
        job = next(
            (
                job
                for job in self.service.store.list("jobs", case_id)
                if job["kind"] == self.kind
                and job.get("revision_id") == revision_id
                and job["status"] == "completed"
                and job["payload"].get("delivery") == "file"
                and job["payload"].get("report") == report_id
                and job["payload"].get("analysis_id") == analysis_id
                and job["payload"]["export_id"] == export_id
            ),
            None,
        )
        if job is None:
            raise NotFound(f"No prepared {self.label} file")
        directory = self._directory(case_id, export_id)
        changed = NotFound(f"The prepared {self.label} file changed. Prepare it again.")
        try:
            artifact = self._cached_manifest(job, directory / "export.json", _ARTIFACT)
        except ExportUnavailable:
            raise changed from None
        stream = BytesIO()
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_STORED) as archive:
            for number, item in enumerate(artifact.files, 1):
                path = directory / "dicom" / item.path
                try:
                    # The bytes served are the bytes checked, read once.
                    body = None if path.is_symlink() else path.read_bytes()
                except OSError:
                    body = None
                if body is None or len(body) != item.size or hashlib.sha256(body).hexdigest() != item.sha256:
                    raise changed
                # A fixed date, so the same export always downloads as the same bytes.
                info = zipfile.ZipInfo(f"{artifact.report}-report-{number}.dcm")
                info.external_attr = 0o600 << 16
                archive.writestr(info, body)
        return stream.getvalue()


class ArchiveReportExports(_ReportImages, ArchiveViewExports):
    kind = "archive_report_export"
    interrupted_error = "Export interrupted. Check the study in the archive, then retry."


class DirectReportExports(_ReportImages, DirectViewExports):
    kind = "direct_report_export"
    interrupted_error = "Export interrupted before sending. Retry to send the report."
