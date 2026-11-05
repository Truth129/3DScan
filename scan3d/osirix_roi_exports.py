"""Archive and send saved masks as native OsiriX brush ROIs in their source study.

The base class owns queuing, retries, receipts and delivery uncertainty, so a
retry or Send again reuses the exact ROI instances. The writer makes one report
per CT slice that a mask touches, each a new series, so this adds what many
instances need: every one bound to the source study and one patient, checked
before anything is stored, as the view export does for its single picture.
"""

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Annotated

from pydantic import ConfigDict, Field, TypeAdapter

from .fileio import atomic_json
from .orthanc_exports import ExportUnavailable, OrthancExports, _frozen_structure, _hash, _patient_id
from .orthanc_models import DicomUID, Digest, Positive
from .store import now

ROI_ENCODING_VERSION = 1


@dataclass(frozen=True, slots=True)
class _RoiStructure:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    structure_id: str
    name: str
    color: str
    mask_sha256: Digest


@dataclass(frozen=True, slots=True)
class _Roi:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    structure_id: str
    voxel_count: Positive
    roi_uuid: Annotated[str, Field(pattern=r"^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$")]


@dataclass(frozen=True, slots=True)
class _RoiFile:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    path: Annotated[str, Field(pattern=r"^roi-[0-9]{4}\.dcm$")]
    source_sop_instance_uid: DicomUID
    sop_instance_uid: DicomUID
    series_instance_uid: DicomUID
    sha256: Digest
    size: Positive
    rois: Annotated[tuple[_Roi, ...], Field(min_length=1, max_length=1000)]


@dataclass(frozen=True, slots=True)
class _RoiArtifact:
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
    study_instance_uid: DicomUID
    frame_of_reference_uid: DicomUID
    created_at: str
    encoding_version: Positive
    structures: Annotated[tuple[_RoiStructure, ...], Field(min_length=1, max_length=1000)]
    files: Annotated[tuple[_RoiFile, ...], Field(min_length=1, max_length=1000)]


_ARTIFACT = TypeAdapter(_RoiArtifact)


class OsirixRoiExports(OrthancExports):
    kind = "orthanc_roi_export"
    shared_kinds = ()
    result_key = "osirix_roi"
    encoding_version = ROI_ENCODING_VERSION
    label = "OsiriX ROI"
    noun = "OsiriX ROIs"
    interrupted_error = (
        "Export interrupted, and some OsiriX ROI reports may already be in Orthanc. "
        "Retry to store the rest with the same instances and check any existing Orthanc delivery job."
    )
    failed_error = "OsiriX ROI export failed. Check the original CT and saved masks, then retry."
    undelivered_error = "Orthanc could not deliver every OsiriX ROI instance. An explicit retry reuses the same SOP instances."
    unconfirmed_error = (
        "Delivery is not confirmed. Check the destination before retrying; a retry reuses the exact OsiriX ROI instances and checks any known Orthanc job."
    )
    still_active_error = "An earlier Orthanc delivery may still be active. Check the destination or explicitly retry to check its status."
    lost_job_error = (
        "Orthanc no longer has the delivery job, so the OsiriX ROIs may already have arrived. "
        "Check the destination before retrying; a retry sends the same OsiriX ROI instances again."
    )
    file_interrupted_error = "Export interrupted. Retry to reuse its exact OsiriX ROI instances."
    disabled_error = "The selected destination is no longer enabled. The OsiriX ROIs remain archived."
    partial_error = (
        "Only {stored} of {total} OsiriX ROI reports reached Orthanc before the export stopped, and nothing was sent. "
        "Archive or send again to store the rest; it reuses the same instances."
    )
    offers_file = False

    def start(self, case_id, revision_id, structure_ids=None, destination=None, *, resend=False, overlap_priority=None, delivery="orthanc"):
        # The SEG dialog sends both formats one body, so refuse what only a SEG can use.
        if delivery != "orthanc":
            raise ExportUnavailable("OsiriX ROIs go through Orthanc only; there is no file to download.")
        if overlap_priority is not None:
            raise ExportUnavailable("OsiriX ROIs keep overlapping masks as they are, so they take no ranking.")
        inputs = self._inputs(case_id, revision_id, structure_ids)
        # A SEG numbers its segments, but OsiriX tells ROIs apart by name, and the writer would fail the job.
        names = [item["name"] for item in inputs.structures]
        if len(set(names)) != len(names):
            raise ExportUnavailable("OsiriX names each ROI after its mask, so the selected masks need different names. Select only one mask of each name.")
        display_name = self._destination(destination, resend)
        identity, selection_key, input_key, changed_inputs = self._frozen_inputs(revision_id, inputs)
        # The format keeps a ROI artifact from ever sharing a key with a SEG of the same masks.
        encoding = {**identity, "format": "osirix_roi", "encoding_version": self.encoding_version}
        artifact_key = hashlib.sha256(json.dumps(encoding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        export_key = hashlib.sha256((artifact_key + ":" + (destination or "")).encode()).hexdigest()
        fields = {
            **identity,
            "encoding_version": self.encoding_version,
            "orthanc_study_id": inputs.source.snapshot.study_id,
            "input_key": input_key,
            "artifact_key": artifact_key,
            "selection_key": selection_key,
            "export_key": export_key,
        }

        def summarize(payload):
            # How many ROIs and instances there are is known only once the masks are encoded.
            summary = {
                "export_id": payload["export_id"],
                "encoding_version": self.encoding_version,
                "archived": False,
                "sent": False,
                "segments_count": len(inputs.structures),
                "structure_ids": [item["id"] for item in inputs.structures],
            }
            if destination:
                summary.update(destination=display_name, destination_id=destination)
            return summary

        return self._enqueue(case_id, revision_id, fields, {}, destination, display_name, resend, summarize, guard=changed_inputs)

    def _check_inputs(self, job):
        payload = job["payload"]
        if payload.get("encoding_version") != self.encoding_version:
            raise ExportUnavailable("The OsiriX ROI writer was updated. Refresh export status and start a new export from the saved revision.")
        inputs = self._inputs(job["case_id"], job["revision_id"], [item["id"] for item in payload["structures"]])
        if (
            inputs.reference["id"] != payload["reference_image_id"]
            or inputs.seg_source.manifest_sha256 != payload["source_manifest_sha256"]
            or [_frozen_structure(item) for item in inputs.structures] != payload["structures"]
            or inputs.source.snapshot.study_id != payload["orthanc_study_id"]
        ):
            raise ExportUnavailable("The saved revision changed after this export was queued. Start a fresh export from a saved revision.")
        self.service.orthanc._verify(inputs.source_dir, inputs.source.snapshot, inputs.source, cancelled=lambda: self.service.cancelled(job))
        return inputs

    def _artifact(self, job, directory):
        # SegmentInput comes from the writer's own module, which is the class it checks against.
        from .osirix_roi import NativeROIError, SegmentInput, export_native_rois

        inputs = self._check_inputs(job)
        payload = job["payload"]

        def cancelled():
            return self.service.cancelled(job)

        manifest_path = directory / "export.json"
        if manifest_path.exists():
            artifact = self._cached_manifest(job, manifest_path, _ARTIFACT)
        else:
            self.service.stage(job, "Encoding exact saved masks as OsiriX ROIs")
            # The writer refuses a mask that changes while it runs, so these name what it read.
            hashes = {item["id"]: _hash(item["mask_file"], cancelled) for item in inputs.structures}
            segments = tuple(SegmentInput(item["id"], item["name"], item["color"], Path(item["mask_file"])) for item in inputs.structures)
            try:
                result = export_native_rois(
                    list(inputs.seg_source.paths),
                    Path(inputs.revision["ct_file"]),
                    segments,
                    directory / "dicom",
                    revision_id=job["revision_id"],
                    cancelled=cancelled,
                    progress=lambda completed, total: self.service.store.update_job(
                        job["id"], progress={"phase": "osirix_roi", "completed": completed, "total": total}
                    ),
                )
            except NativeROIError as exc:
                raise ExportUnavailable(str(exc)) from None
            artifact = _RoiArtifact(
                export_id=payload["export_id"],
                artifact_key=payload["artifact_key"],
                case_id=job["case_id"],
                revision_id=job["revision_id"],
                reference_image_id=inputs.reference["id"],
                reference_sha256=result.reference_sha256,
                source_manifest_sha256=inputs.seg_source.manifest_sha256,
                source_content_sha256=inputs.seg_source.content_sha256,
                source_snapshot=inputs.seg_source.snapshot,
                archive_key=inputs.seg_source.archive_key,
                source_kind=inputs.revision["source"],
                study_instance_uid=result.study_instance_uid,
                frame_of_reference_uid=result.frame_of_reference_uid,
                created_at=now(),
                encoding_version=payload["encoding_version"],
                structures=tuple(_RoiStructure(item["id"], item["name"], item["color"], hashes[item["id"]]) for item in inputs.structures),
                files=tuple(
                    _RoiFile(
                        item.path.name,
                        item.source_sop_instance_uid,
                        item.sop_instance_uid,
                        item.series_instance_uid,
                        item.sha256,
                        item.size,
                        tuple(_Roi(roi.structure_id, roi.voxel_count, roi.roi_uuid) for roi in item.rois),
                    )
                    for item in result.files
                ),
            )
            # Persist the generated SOP identities before any network write.
            artifact = _ARTIFACT.validate_python(artifact)
            atomic_json(manifest_path, _ARTIFACT.dump_python(artifact, mode="json"))
            self._check_inputs(job)
        structures = [item["id"] for item in inputs.structures]
        rois = [roi for item in artifact.files for roi in item.rois]
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
            or artifact.frame_of_reference_uid != inputs.seg_source.frame_of_reference_uid
            or [(item.structure_id, item.name, item.color) for item in artifact.structures]
            != [(item["id"], item["name"], item["color"]) for item in inputs.structures]
            # Every selected mask is nonempty, so each has a ROI on some slice, and only once on any one.
            or {roi.structure_id for roi in rois} != set(structures)
            or any(len({roi.structure_id for roi in item.rois}) != len(item.rois) for item in artifact.files)
            or len({roi.roi_uuid for roi in rois}) != len(rois)
            or not {item.source_sop_instance_uid for item in artifact.files} <= {item.sop_instance_uid for item in inputs.source.files}
            or len({item.source_sop_instance_uid for item in artifact.files}) != len(artifact.files)
            or len({item.sop_instance_uid for item in artifact.files}) != len(artifact.files)
            or len({item.path for item in artifact.files}) != len(artifact.files)
        ):
            raise ExportUnavailable("The cached OsiriX ROI export differs from its frozen source or saved revision.")
        self._check_files(job, directory, artifact)
        for structure, selected in zip(artifact.structures, inputs.structures, strict=True):
            if _hash(selected["mask_file"], cancelled) != structure.mask_sha256:
                raise ExportUnavailable("The saved masks or prepared OsiriX ROI bytes changed. Delivery was blocked.")
        # Orthanc files each instance by its own patient ID, so one that differs would split the study.
        if len({_patient_id(directory / "dicom" / item.path) for item in artifact.files}) != 1:
            raise ExportUnavailable("The OsiriX ROI instances do not all carry the source images' patient ID. Nothing was stored.")
        self._check_study(inputs, directory / "dicom" / artifact.files[0].path)
        self._pin_manifest(job, manifest_path)
        return artifact

    def _check_upload(self, job, upload):
        # What was stored stays in the archive for inspection: never delete data here.
        if upload.get("parent_study") != job["payload"]["orthanc_study_id"]:
            raise ExportUnavailable("Orthanc stored an OsiriX ROI instance outside the original study, so nothing was sent. Check the study in Orthanc.")

    def _result(self, artifact, receipt, destination, display_name):
        result = {
            "export_id": artifact.export_id,
            "encoding_version": artifact.encoding_version,
            "archived": all(item.path in receipt.uploaded for item in artifact.files),
            "sent": bool(destination and destination in receipt.destinations and receipt.destinations[destination].state == "success"),
            "segments_count": len(artifact.structures),
            "roi_count": sum(len(item.rois) for item in artifact.files),
            "instance_count": len(artifact.files),
            "structure_ids": [item.structure_id for item in artifact.structures],
        }
        if destination:
            result["destination"] = display_name
            result["destination_id"] = destination
        return {self.result_key: result}
