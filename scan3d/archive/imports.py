"""Imports from a DICOMweb archive, on the Orthanc importer's queue, publish and recovery.

DICOMweb gives no stored-file identity, file size, expected count or "still receiving" flag.
So a snapshot names its instances by SOP, a series counts as stable only after its listing has
stayed unchanged for a quiet period, the listing is read again before and after the transfer,
and every downloaded file must carry the patient the snapshot was taken for.
"""

from dataclasses import replace
import hashlib
import math

import pydicom

from ..errors import NotFound
from ..orthanc_imports import OrthancImports
from ..orthanc_models import content_digest
from ..store import now
from .dicomweb import client_for
from .errors import ArchiveChanged, ArchiveError
from .models import WebFile, WebInstance, WebSnapshot, WebSource, fingerprint, json_record, parse_snapshot, parse_source, patient_key
from .stability import QuietPeriod

UNPROVEN = "An unchanged snapshot is not proof that the scanner sent the full acquisition."
_TAGS = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "SOPClassUID", "FrameOfReferenceUID", "Modality", "PatientID", "IssuerOfPatientID"]


def _public_study(study):
    # The UI keys studies by `id`, as it does Orthanc's.
    return {"id": study["study_uid"], **study}


def _frame(listed):
    """The series' one Frame of Reference, or None when the archive names none or several."""
    frames = {frame for _sop, _class, frame in listed if frame}
    return next(iter(frames)) if len(frames) == 1 else None


class WebImports(OrthancImports):
    kind = "archive_import"
    source_kind = "dicomweb"
    reference = "archive"
    default_label = "Archive imaging case"
    failed_error = "The archive import did not complete. Retry the selected series."
    cancelled_error = "Archive import cancelled"
    changed = ArchiveChanged
    parse_snapshot = staticmethod(parse_snapshot)
    parse_source = staticmethod(parse_source)
    json_record = staticmethod(json_record)
    fingerprint = staticmethod(fingerprint)

    def __init__(self, service):
        super().__init__(service)
        settings = service.settings.dicomweb
        # One per process, so every listing, start and launch advances the same countdown.
        self.quiet = QuietPeriod(settings.quiet_seconds if settings else 0)

    def client(self):
        settings = self.service.settings.dicomweb
        if settings is None:
            raise ValueError("A DICOMweb archive has not been configured on this workstation")
        return client_for(settings)

    def status(self):
        settings = self.service.settings.dicomweb
        if settings is None:
            return {"configured": False, "connected": False, "name": "DICOM archive", "error": None}
        try:
            with self.client() as client:
                client.status()
        except (ArchiveError, NotFound, ValueError):
            return {"configured": True, "connected": False, "name": settings.name, "error": "Cannot connect to the configured DICOM archive."}
        return {"configured": True, "connected": True, "name": settings.name, "wildcards": settings.wildcards, "error": None}

    def studies(self, query, offset=0, limit=25):
        with self.client() as client:
            rows = client.find_studies(query, offset=offset, limit=limit + 1)
        return {"studies": [_public_study(row) for row in rows[:limit]], "offset": offset, "limit": limit, "has_more": len(rows) > limit}

    def _snapshot(self, client, study, row):
        """One listed series as a snapshot, with seconds until it counts as stable; None when it has no instances."""
        listed = client.instances(study["study_uid"], row["series_uid"])
        if not listed:
            return None, 0
        draft = WebSnapshot(
            version=2,
            kind="dicomweb",
            source_key=client.source_key,
            study_uid=study["study_uid"],
            series_uid=row["series_uid"],
            patient_key=patient_key(client.source_key, study["patient_id"], study["issuer_of_patient_id"]),
            frame_of_reference_uid=_frame(listed),
            description=row["description"],
            modality=row["modality"],
            instance_count=len(listed),
            reported_instances=row["reported_instances"],
            stable=False,
            completeness="receiving",
            warnings=(),
            instances=tuple(WebInstance(sop_instance_uid=sop, sop_class_uid=sop_class) for sop, sop_class, _frame_uid in listed),
            snapshot="0" * 64,
        )
        digest = fingerprint(draft)
        wait = self.quiet.observe((client.source_key, draft.study_uid, draft.series_uid), digest)
        stable = wait == 0
        try:
            snapshot = parse_snapshot(replace(draft, stable=stable, completeness="unknown" if stable else "receiving", warnings=(UNPROVEN,), snapshot=digest))
        except ValueError:
            raise ArchiveError("The archive returned invalid series metadata.") from None
        return snapshot, wait

    def _listing(self, client, study_uid):
        """The study, and each of its series as `(row, snapshot, wait)`."""
        listing = client.study_series(study_uid)
        study = listing["study"]
        return study, [(row, *self._snapshot(client, study, row)) for row in listing["series"]]

    def _one(self, client, study_uid, series_uid):
        # Through the study's listing, which carries the series' description, modality and count.
        study, entries = self._listing(client, study_uid)
        found = [entry for entry in entries if entry[0]["series_uid"] == series_uid]
        if not found:
            raise NotFound("The selected archive series is no longer available")
        if found[0][1] is None:
            raise ArchiveChanged("The archive lists no instances for this series. Refresh it when they have arrived.")
        return study, *found[0]

    def _summary(self, row, snapshot, wait):
        if snapshot is None:
            fields = {"instance_count": 0, "stable": False, "completeness": "unknown", "snapshot": "", "warnings": []}
            reason = "The archive lists no instances for this series"
        else:
            fields = {
                "instance_count": snapshot.instance_count,
                "stable": snapshot.stable,
                "completeness": snapshot.completeness,
                "snapshot": snapshot.snapshot,
                "warnings": list(snapshot.warnings),
            }
            reason = self._modality_reason(snapshot.modality)
        return {
            "id": row["series_uid"],
            "series_uid": row["series_uid"],
            "description": row["description"],
            "modality": row["modality"],
            "reported_instances": row["reported_instances"],
            # Seconds until an unchanged series can be imported, for the countdown.
            "ready_in": math.ceil(wait),
            **fields,
            "supported": reason is None,
            "unsupported_reason": reason,
        }

    def series(self, study_uid):
        with self.client() as client:
            study, entries = self._listing(client, study_uid)
        return {"study": _public_study(study), "series": [self._summary(*entry) for entry in entries]}

    def launch(self, study_uid, series_uid, source_key):
        """Resolve a launch without importing or changing the local library."""
        with self.client() as client:
            if source_key != client.source_key:
                raise ValueError("This launch link belongs to a different archive. Open the configured archive and try again.")
            study, row, snapshot, wait = self._one(client, study_uid, series_uid)
        summary = self._summary(row, snapshot, wait)
        result = {"study": _public_study(study), "series": summary, "case_id": None, "job": None}
        if not summary["supported"] or not snapshot.stable:
            return result
        return self._find_import(result, snapshot)

    def start(self, study_uid, series_uid, fingerprint, label=None, *, case_id=None, reimport=False):
        """Import a series into a new case, or into `case_id`, which only an authorized route passes."""
        with self.client() as client:
            _study, _row, snapshot, wait = self._one(client, study_uid, series_uid)
        if snapshot.snapshot != fingerprint:
            raise ArchiveChanged("The selected series changed. Refresh its series list before importing.")
        if not snapshot.stable:
            raise ArchiveChanged(f"This series may still be arriving. It can be imported in {math.ceil(wait)} s if it stays unchanged.")
        if reason := self._modality_reason(snapshot.modality):
            raise ValueError(reason)
        return self._enqueue(snapshot, label, case_id, reimport=reimport)

    def _case_source(self, case):
        provenance = case.get("import_source")
        if not isinstance(provenance, dict) or provenance.get("kind") != self.source_kind:
            raise ValueError("Only a case imported from the DICOM archive can add an archive series")
        return parse_source(case.get("references", {}).get(self.reference))

    def case_series(self, case_id):
        """The series of a case's own study, each marked if the case already holds it."""
        case = self.service.store.get("cases", case_id)
        own = self._case_source(case).snapshot
        with self.client() as client:
            if client.source_key != own.source_key:
                raise ValueError("This case came from a different archive than the one configured now")
            study, entries = self._listing(client, own.study_uid)
            if patient_key(client.source_key, study["patient_id"], study["issuer_of_patient_id"]) != own.patient_key:
                raise ArchiveChanged("The archive study now names a different patient. Import its series as a new case.")
        held = {row["uid"] for row in case["series"]}
        return {"study": _public_study(study), "series": [{**self._summary(*entry), "in_case": entry[0]["series_uid"] in held} for entry in entries]}

    def _check_addition(self, case, snapshot):
        """A series joins a case only from that case's own study and patient, both bound when the case was made."""
        own = self._case_source(case).snapshot
        if (own.source_key, own.study_uid, own.patient_key) != (snapshot.source_key, snapshot.study_uid, snapshot.patient_key):
            raise ValueError("This series belongs to a different study or patient, so it cannot be added to this case")
        if any(row["uid"] == snapshot.series_uid for row in case["series"]):
            raise ValueError("This series is already in this case")

    @staticmethod
    def _same_members(source, snapshot):
        return {item.sop_instance_uid for item in source.files} == {item.sop_instance_uid for item in snapshot.instances}

    def _same_listing(self, client, snapshot, when):
        listed = client.instances(snapshot.study_uid, snapshot.series_uid)
        members = tuple((item.sop_instance_uid, item.sop_class_uid) for item in snapshot.instances)
        if tuple((sop, sop_class) for sop, sop_class, _frame_uid in listed) != members or _frame(listed) != snapshot.frame_of_reference_uid:
            raise ArchiveChanged(f"The archive series changed {when} the transfer. Refresh it before importing.")

    def _transfer(self, snapshot, directory, *, progress, cancelled):
        limit = self.service.settings.upload_limit
        files, total = [], 0
        with self.client() as client:
            if client.source_key != snapshot.source_key:
                raise ArchiveChanged("This series was selected in a different archive. Select it again.")
            self._same_listing(client, snapshot, "before")
            for index, item in enumerate(snapshot.instances):
                if cancelled():
                    raise InterruptedError(self.cancelled_error)
                if total >= limit:
                    raise ValueError("Archive import exceeds size limit")
                path = directory / f"{index:06d}.dcm"
                got = client.retrieve(snapshot.study_uid, snapshot.series_uid, item.sop_instance_uid, path, max_bytes=limit - total, cancelled=cancelled)
                total += got.size
                files.append(
                    WebFile(
                        sop_instance_uid=item.sop_instance_uid, sha256=got.sha256, size=got.size, path=path.name, transfer_syntax_uid=got.transfer_syntax_uid
                    )
                )
                progress(index + 1, snapshot.instance_count, total)
            if cancelled():
                raise InterruptedError(self.cancelled_error)
            self._same_listing(client, snapshot, "during")
        files = tuple(files)
        frame_uid = self._verify(directory, snapshot, files, cancelled=cancelled)
        return parse_source(
            WebSource(
                snapshot=snapshot,
                files=files,
                bytes=total,
                content_digest=content_digest(files),
                kind="dicomweb",
                imported_at=now(),
                frame_of_reference_uid=frame_uid,
            )
        )

    def _verify(self, directory, snapshot, files, *, cancelled=None):
        """Bind the downloaded bytes to their DICOM identity and to the snapshot's patient, not to archive metadata."""
        expected = {item.sop_instance_uid: item for item in snapshot.instances}
        frames, seen = set(), set()
        for item in files:
            try:
                path = directory / item.path
                if item.sop_instance_uid in seen or item.sop_instance_uid not in expected or path.is_symlink() or path.resolve().parent != directory.resolve():
                    raise ValueError()
                seen.add(item.sop_instance_uid)
                digest, size = hashlib.sha256(), 0
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        if cancelled and cancelled():
                            raise InterruptedError(self.cancelled_error)
                        digest.update(block)
                        size += len(block)
                if size != item.size or digest.hexdigest() != item.sha256:
                    raise ValueError()
                ds = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=_TAGS)
                values = {tag: str(ds.get(tag) or "") for tag in _TAGS}
                entry = expected[item.sop_instance_uid]
                if (
                    values["StudyInstanceUID"] != snapshot.study_uid
                    or values["SeriesInstanceUID"] != snapshot.series_uid
                    or values["SOPInstanceUID"] != entry.sop_instance_uid
                    or values["SOPClassUID"] != entry.sop_class_uid
                    or values["Modality"] != snapshot.modality
                    or not values["FrameOfReferenceUID"]
                    or str(ds.file_meta.MediaStorageSOPInstanceUID) != values["SOPInstanceUID"]
                    or str(ds.file_meta.MediaStorageSOPClassUID) != values["SOPClassUID"]
                    # A study listing may leave out IssuerOfPatientID, which is no default QIDO
                    # attribute, while the file carries it. PatientID and the study bind it then.
                    or snapshot.patient_key
                    not in {patient_key(snapshot.source_key, values["PatientID"], issuer) for issuer in (values["IssuerOfPatientID"], "")}
                ):
                    raise ValueError()
                frames.add(values["FrameOfReferenceUID"])
            except InterruptedError:
                raise
            except Exception:
                raise ValueError("The downloaded DICOM identity does not match the selected archive series") from None
        if seen != set(expected) or len(frames) != 1 or (snapshot.frame_of_reference_uid and frames != {snapshot.frame_of_reference_uid}):
            raise ValueError("The downloaded series has inconsistent DICOM membership or coordinate frames")
        return next(iter(frames))
