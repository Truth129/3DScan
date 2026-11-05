"""Store SEG and 3D-view exports in a DICOMweb archive with STOW-RS, and prove what it kept.

The Orthanc exporters own the artifact: encoding, caching, frozen inputs and their checks.
This replaces only the delivery. A STOW answer does not show which bytes an archive keeps:
Orthanc answers "stored" for a second file under a known identity and keeps the first. So
every instance is read back, and its bytes must match, or else its identity and decoded
pixels. The receipt is `delivery-v2.json`, so an Orthanc receipt is never read as one.
"""

from dataclasses import dataclass
import hashlib
import logging
from types import SimpleNamespace
from typing import Annotated, Literal
import uuid

from pydantic import ConfigDict, Field, TypeAdapter, ValidationError
import pydicom
from pydicom.pixels import iter_pixels

from ..errors import NotFound
from ..fileio import atomic_json
from ..orthanc_exports import ARCHIVE_OFF, ExportUnavailable, OrthancExports
from ..orthanc_models import DicomUID, Digest, series_source
from ..orthanc_view_exports import OrthancViewExports
from ..store import now, public_job
from .errors import ArchiveChanged, ArchiveError
from .models import parse_source, patient_key

log = logging.getLogger(__name__)
_READ_BACK_MINIMUM = 16 * 1024**2
_NATIVE = frozenset({"1.2.840.10008.1.2", "1.2.840.10008.1.2.1"})


@dataclass(frozen=True, slots=True)
class StoredInstance:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    sop_instance_uid: DicomUID
    # "bytes": the archive returns the exact file. "canonical": the same identity and pixels, re-encoded.
    verified: Literal["bytes", "canonical"]
    # STOW warning reasons, such as "B000".
    warnings: tuple[Annotated[str, Field(pattern=r"^[0-9A-F]{4}$")], ...] = ()


@dataclass(frozen=True, slots=True)
class ArchiveReceipt:
    __pydantic_config__ = ConfigDict(extra="forbid", revalidate_instances="always")

    version: Literal[2]
    manifest_sha256: Digest
    # By archive identity, so a reconfigured archive never counts another's copies as its own.
    archives: dict[Digest, dict[Annotated[str, Field(pattern=r"^[a-z]+-[0-9]{4}\.dcm$")], StoredInstance]]


_RECEIPT = TypeAdapter(ArchiveReceipt)


def _patient(dataset):
    return str(dataset.get("PatientID") or ""), str(dataset.get("IssuerOfPatientID") or "")


def _kept_patient(sent, held):
    """The archive kept the patient: the same ID, and the same issuer or one it assigned where none was sent.

    dcm4che, for one, gives every instance stored without an issuer its own default issuer.
    """
    (sent_id, sent_issuer), (held_id, held_issuer) = _patient(sent), _patient(held)
    return sent_id == held_id and (sent_issuer == held_issuer or not sent_issuer)


def _same_patient(source_key, wanted, patient_id, issuer):
    # A study listing may leave IssuerOfPatientID out, as at import.
    return wanted in {patient_key(source_key, patient_id, issuer), patient_key(source_key, patient_id, "")}


class _Unverifiable(Exception):
    """The archive's copy could not be read here, which proves nothing either way."""


def _pixel_digest(path, native):
    if native:
        return hashlib.sha256(pydicom.dcmread(path).PixelData).digest()
    # One frame at a time: a SEG unpacks to eight times its stored size.
    digest = hashlib.sha256()
    for frame in iter_pixels(path):
        digest.update(frame.tobytes())
    return digest.digest()


def _same_content(sent, held):
    """The same instance, re-encoded: identity, patient and pixels all match. Raises _Unverifiable if unreadable."""
    try:
        a, b = (pydicom.dcmread(path, stop_before_pixels=True) for path in (sent, held))
        tags = ("SOPInstanceUID", "SOPClassUID", "StudyInstanceUID", "SeriesInstanceUID")
        if any(str(a.get(tag)) != str(b.get(tag)) for tag in tags) or not _kept_patient(a, b):
            return False
        # Uncompressed little-endian pixels are the same bytes whichever VR encoding carries them.
        native = {str(a.file_meta.TransferSyntaxUID), str(b.file_meta.TransferSyntaxUID)} <= _NATIVE
        return _pixel_digest(sent, native) == _pixel_digest(held, native)
    except Exception:
        raise _Unverifiable() from None


class _ArchiveDelivery:
    """Delivery to the DICOMweb archive, for an Orthanc exporter subclass that keeps its artifact."""

    transport = "archive"
    receipt_name = "delivery-v2.json"
    no_source_error = "This saved revision has no verified original CT from the DICOM archive. Reimport and prepare its original CT before export."

    def _archive_name(self):
        settings = self.service.settings.dicomweb
        return settings.name if settings else "DICOM archive"

    def _require_transport(self):
        settings = self.service.settings.dicomweb
        if settings is None:
            raise ExportUnavailable(f"Configure a DICOM archive before archiving {self.noun}.")
        if not settings.store:
            raise ExportUnavailable("This workstation keeps the DICOM archive read-only, so nothing can be stored in it.")

    def _transport_client(self):
        return self.service.archive_imports.client()

    def _destination(self, destination, resend):
        if destination is not None or resend:
            raise ExportUnavailable(f"{self.label} can only be archived in the DICOM archive, not sent.")
        return None

    def _export_key(self, artifact_key, destination, file_delivery):
        """One artifact in one archive, named by the archive's identity rather than its address."""
        with self._transport_client() as client:
            return hashlib.sha256(f"{artifact_key}:archive:{client.source_key}".encode()).hexdigest()

    def availability(self, case_id, revision_id, *, archive=True, kinds=None):
        self.service.store.revision(case_id, revision_id)
        kinds = kinds or (self.kind,)
        exports = [public_job(job) for job in self.service.store.list("jobs", case_id) if job["kind"] in kinds and job.get("revision_id") == revision_id]
        result = {"available": True, "destinations": [], "exports": exports, "encoding_version": self.encoding_version, "archive_name": self._archive_name()}
        try:
            if not archive:
                raise ExportUnavailable(ARCHIVE_OFF)
            self._inputs(case_id, revision_id)
            with self._transport_client() as client:
                client.status()
        except ExportUnavailable as exc:
            result.update(available=False, reason=str(exc))
        except (ArchiveError, NotFound, ValueError):
            result.update(available=False, reason="The configured DICOM archive is unavailable. Retry its connection.")
        if self.offers_file:
            result["file"] = self._file_availability(case_id, revision_id)
        return result

    def _web_source(self, job):
        """The DICOMweb record of the series this export's revision shows."""
        _revision, case, reference = self._reference(job["case_id"], job["revision_id"])
        try:
            return parse_source(series_source(case, reference["source"]["series_uid"])["archive"])
        except (KeyError, OSError, ValueError):
            raise ExportUnavailable(self.no_source_error) from None

    def _check_patient(self, client, source, path, *, stored):
        """The archive's study must still be the source's, under the same patient; checked before and after storing."""
        snapshot = source.snapshot
        outcome = "Check the study in the archive." if stored else "Nothing was stored."
        try:
            study = client.study(snapshot.study_uid)
        except NotFound:
            raise ExportUnavailable(f"The original study is no longer in the archive. {outcome}") from None
        except ArchiveChanged:
            raise ExportUnavailable(f"The archive holds more than one study under the original study's identity. {outcome}") from None
        if not _same_patient(client.source_key, snapshot.patient_key, study["patient_id"], study["issuer_of_patient_id"]):
            raise ExportUnavailable(f"The archive files the original study under a different patient now. {outcome}")
        if path is not None:
            patient_id, issuer = _patient(pydicom.dcmread(path, stop_before_pixels=True, specific_tags=["PatientID", "IssuerOfPatientID"]))
            if not _same_patient(client.source_key, snapshot.patient_key, patient_id, issuer):
                raise ExportUnavailable(f"The prepared {self.label} names a different patient from the original study. Nothing was stored.")

    def _read_back(self, client, artifact, item, path, directory):
        held = directory / f"read-back-{uuid.uuid4().hex}.dcm"
        try:
            got = client.read_back(
                artifact.study_instance_uid, artifact.series_instance_uid, item.sop_instance_uid, held, max_bytes=max(_READ_BACK_MINIMUM, 8 * item.size)
            )
            if got.sha256 == item.sha256:
                return "bytes"
            if _same_content(path, held):
                return "canonical"
            raise ExportUnavailable(f"The archive holds a different {self.label} under this instance's identity. Check the study in the archive.")
        except _Unverifiable:
            raise ExportUnavailable(
                f"The archive returned this {self.label} in a form that could not be checked here. Check the study in the archive."
            ) from None
        except NotFound:
            raise ExportUnavailable(f"The archive did not return the stored {self.label}. Check the study in the archive.") from None
        finally:
            held.unlink(missing_ok=True)

    def _load_receipt(self, path, job):
        manifest = job["payload"]["artifact_manifest_sha256"]
        if not path.exists():
            return ArchiveReceipt(2, manifest, {})
        try:
            receipt = _RECEIPT.validate_json(path.read_bytes())
        except (OSError, ValidationError):
            raise ExportUnavailable(f"The {self.label} archive record cannot be verified. Delivery was blocked.") from None
        if receipt.manifest_sha256 != manifest:
            raise ExportUnavailable(f"The archive record does not belong to this {self.label} artifact.")
        return receipt

    def _archive_result(self, artifact, stored):
        # The Orthanc result reads only which paths are archived, and sends nowhere here.
        result = self._result(artifact, SimpleNamespace(uploaded=stored, destinations={}), None, None)
        result[self.result_key]["archive"] = self._archive_name()
        return result

    def run(self, job):
        # What this attempt stored and proved: its result never reports another attempt's copies.
        artifact, stored = None, {}
        try:
            directory = self._prepare_directory(job)
            artifact = self._artifact(job, directory)
            receipt_path = directory / self.receipt_name
            receipt = self._load_receipt(receipt_path, job)
            source = self._web_source(job)
            name = self._archive_name()
            with self._transport_client() as client:
                if client.source_key != source.snapshot.source_key:
                    raise ExportUnavailable(self.no_source_error)
                record = receipt.archives.setdefault(client.source_key, {})
                self._check_patient(client, source, directory / "dicom" / artifact.files[0].path, stored=False)
                for item in artifact.files:
                    self.service.stage(job, f"Storing {self.noun} in {name}")
                    path = directory / "dicom" / item.path
                    # An earlier attempt's proof no longer stands once this one stores again.
                    if record.pop(item.path, None):
                        atomic_json(receipt_path, _RECEIPT.dump_python(receipt, mode="json"))
                    # Storing the same bytes again is harmless, and the read-back proves what the archive keeps.
                    answer = client.store(path, expected_sha256=item.sha256)
                    verified = self._read_back(client, artifact, item, path, directory)
                    record[item.path] = stored[item.path] = StoredInstance(item.sop_instance_uid, verified, answer.warnings)
                    atomic_json(receipt_path, _RECEIPT.dump_python(receipt, mode="json"))
                self._check_patient(client, source, None, stored=True)
            self.service.store.update_job(
                job["id"], status="completed", stage=f"{self.noun} archived", error=None, completed_at=now(), result=self._archive_result(artifact, stored)
            )
        except Exception as exc:
            cancelled = isinstance(exc, InterruptedError) and not stored
            # Archive errors are fixed text naming no patient or query.
            error = str(exc) if isinstance(exc, (ExportUnavailable, ArchiveError, ArchiveChanged)) else self.failed_error
            if not cancelled:
                log.warning("%s archive export %s failed (%s)", self.label, job["id"], type(exc).__name__)
            changes = dict(
                status="cancelled" if cancelled else "failed",
                stage="Cancelled before storing" if cancelled else "Export failed",
                error=None if cancelled else error,
                completed_at=now(),
            )
            if artifact:
                changes["result"] = self._archive_result(artifact, stored)
            self.service.store.update_job(job["id"], **changes)


class ArchiveSegExports(_ArchiveDelivery, OrthancExports):
    kind = "archive_seg_export"
    # A SEG file of the same masks, which the Orthanc exporter makes, is this same instance.
    shared_kinds = ("orthanc_export",)
    interrupted_error = "Export interrupted. Retry to reuse its exact SEG bytes."


class ArchiveViewExports(_ArchiveDelivery, OrthancViewExports):
    kind = "archive_view_export"
    # Only the dialog that holds the image can retry, so these state the outcome alone.
    interrupted_error = "Export interrupted. Check the study in the archive."

    def _payload_fields(self, inputs):
        return {}

    def _check_study(self, inputs, path):
        """Checked against the archive when storing, before and after."""
