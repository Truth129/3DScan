"""Explicit, snapshot-bound archive imports on the existing CPU job lane."""

import hashlib
import json
from pathlib import Path
import tempfile
import time

import pydicom

from .config import MODALITIES
from .errors import NotFound
from .orthanc import OrthancClient, OrthancChanged, OrthancError
from .orthanc_models import DownloadedFile, DownloadManifest, SeriesSnapshot, SourceManifest, content_digest, json_record, parse_snapshot, parse_source
from .service import atomic_json
from .store import identifier, now


class OrthancImports:
    def __init__(self, service):
        self.service = service

    def client(self):
        settings = self.service.settings
        if not settings.orthanc_url:
            raise ValueError("An Orthanc connection has not been configured on this workstation")
        return OrthancClient(settings.orthanc_url, settings.orthanc_username, settings.orthanc_password)

    def status(self):
        if not self.service.settings.orthanc_url:
            return {"configured": False, "connected": False, "name": "Orthanc", "error": None}
        try:
            with self.client() as client:
                return {"configured": True, "connected": True, **client.status(), "error": None}
        except (OrthancError, NotFound, ValueError):
            return {"configured": True, "connected": False, "name": "Orthanc", "error": "Cannot connect to the configured Orthanc archive."}

    def studies(self, query, offset=0, limit=25):
        with self.client() as client:
            rows = client.find_studies(query, offset=offset, limit=limit + 1)
        return {"studies": rows[:limit], "offset": offset, "limit": limit, "has_more": len(rows) > limit}

    def series(self, study_id):
        with self.client() as client:
            result = client.study_series(study_id)
        for row in result["series"]:
            reason = self._modality_reason(row["modality"])
            row.update(supported=reason is None, unsupported_reason=reason)
        return result

    def _modality_reason(self, modality):
        if modality not in MODALITIES:
            return "This DICOM modality has no installed image adapter"
        return self.service.settings.modality_unavailable(modality)

    def recover(self):
        """Reconcile interrupted imports after the worker lock and job recovery.

        An older failed attempt must not change a newer queued retry or a case
        that already owns source bytes. This reads only local records, never
        contacting the archive or resuming a transfer without a user request.
        """
        with self.service.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            rows = con.execute(
                "SELECT c.data AS case_data,j.data AS job_data FROM cases c JOIN jobs j ON j.case_id=c.id "
                "WHERE json_extract(c.data,'$.status')='importing' AND json_extract(j.data,'$.kind')='orthanc_import' "
                "AND j.rowid=(SELECT MAX(rowid) FROM jobs WHERE case_id=c.id)"
            ).fetchall()
            for row in rows:
                case, job = json.loads(row["case_data"]), json.loads(row["job_data"])
                if job["status"] not in {"failed", "cancelled"} or case.get("source_dir") or case.get("active_revision_id"):
                    continue
                cancelled = job["status"] == "cancelled"
                case.update(
                    status="import_cancelled" if cancelled else "import_failed",
                    error=None if cancelled else "The Orthanc import did not complete. Retry the selected series.",
                )
                con.execute("UPDATE cases SET data=? WHERE id=?", (json.dumps(case, allow_nan=False), case["id"]))

    def start(self, series_id, fingerprint, label=None):
        with self.client() as client:
            snapshot = client.snapshot(series_id)
        if snapshot.snapshot != fingerprint:
            raise OrthancChanged("The selected series changed. Refresh its series list before importing.")
        if snapshot.completeness not in {"complete", "unknown"} or snapshot.stable is not True:
            raise OrthancChanged("This series is still receiving or incomplete. Refresh it when the acquisition is finished.")
        if reason := self._modality_reason(snapshot.modality):
            raise ValueError(reason)
        if sum(item.size for item in snapshot.instances) > self.service.settings.upload_limit:
            raise ValueError("Orthanc import exceeds size limit")
        key = hashlib.sha256((snapshot.source_key + snapshot.snapshot).encode()).hexdigest()
        store = self.service.store
        with store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute(
                "CREATE INDEX IF NOT EXISTS orthanc_import_key ON jobs(json_extract(data,'$.payload.import_key')) WHERE json_extract(data,'$.kind')='orthanc_import'"
            )
            previous = con.execute(
                "SELECT data FROM jobs WHERE json_extract(data,'$.kind')='orthanc_import' AND json_extract(data,'$.payload.import_key')=? ORDER BY rowid DESC LIMIT 1",
                (key,),
            ).fetchone()
            previous = json.loads(previous["data"]) if previous else None
            if previous and previous["status"] in {"queued", "running", "completed"}:
                return previous
            case = None
            if previous:
                row = con.execute("SELECT data FROM cases WHERE id=?", (previous["case_id"],)).fetchone()
                case = json.loads(row["data"]) if row else None
                if case and (case.get("active_revision_id") or case.get("source_dir")):
                    case = None
            if case is None:
                case = dict(
                    id=identifier(),
                    label=label or snapshot.description or "Orthanc imaging case",
                    created_at=now(),
                    active_revision_id=None,
                    selected_series_uid=None,
                    phase=None,
                    series=[],
                    references={},
                )
            case.update(status="importing", error=None)
            job = dict(
                id=identifier(),
                case_id=case["id"],
                kind="orthanc_import",
                status="queued",
                stage="Queued",
                error=None,
                revision_id=None,
                created_at=now(),
                cancel_requested=False,
                payload={"import_key": key, "snapshot": json_record(snapshot)},
            )
            con.execute("INSERT OR REPLACE INTO cases VALUES (?,?)", (case["id"], json.dumps(case, allow_nan=False)))
            con.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job["id"], case["id"], job["status"], json.dumps(job, allow_nan=False)))
        return job

    def _verify(self, directory, snapshot: SeriesSnapshot, manifest: DownloadManifest, *, cancelled=None):
        """Bind downloaded bytes to actual DICOM identity, independently of REST tags."""
        tags = ["StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID", "SOPClassUID", "FrameOfReferenceUID", "Modality"]
        expected = {item.id: item for item in snapshot.instances}
        frames = set()
        seen, paths, total, content = set(), set(), 0, []
        for item in manifest.files:
            try:
                instance_id = item.instance_id
                relative = Path(item.path)
                path = directory / relative
                if (
                    instance_id in seen
                    or instance_id not in expected
                    or relative.is_absolute()
                    or len(relative.parts) != 1
                    or path.is_symlink()
                    or path.resolve().parent != directory.resolve()
                    or relative.name in paths
                ):
                    raise ValueError()
                entry = expected[instance_id]
                seen.add(instance_id)
                paths.add(relative.name)
                digest = hashlib.sha256()
                size = 0
                with path.open("rb") as stream:
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        if cancelled and cancelled():
                            raise InterruptedError("Orthanc import cancelled")
                        digest.update(block)
                        size += len(block)
                        if size > entry.size:
                            raise ValueError()
                if size != entry.size or size != item.size or digest.hexdigest() != item.sha256 or item.sop_instance_uid != entry.sop_instance_uid:
                    raise ValueError()
                total += size
                content.append(
                    DownloadedFile(instance_id=instance_id, sop_instance_uid=item.sop_instance_uid, sha256=digest.hexdigest(), size=size, path=item.path)
                )
                ds = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=tags)
                values = {tag: str(ds.get(tag, "")) for tag in tags}
                if (
                    values["StudyInstanceUID"] != snapshot.study_uid
                    or values["SeriesInstanceUID"] != snapshot.series_uid
                    or values["SOPInstanceUID"] != entry.sop_instance_uid
                    or values["SOPClassUID"] != entry.sop_class_uid
                    or values["Modality"] != snapshot.modality
                    or not values["FrameOfReferenceUID"]
                    or str(ds.file_meta.MediaStorageSOPInstanceUID) != values["SOPInstanceUID"]
                    or str(ds.file_meta.MediaStorageSOPClassUID) != values["SOPClassUID"]
                ):
                    raise ValueError()
                frames.add(values["FrameOfReferenceUID"])
            except InterruptedError:
                raise
            except Exception:
                raise ValueError("The downloaded DICOM identity does not match the selected Orthanc series") from None
        digest = content_digest(tuple(content))
        if (
            seen != set(expected)
            or len(frames) != 1
            or manifest.bytes != total
            or manifest.content_digest != digest
            or manifest.snapshot != snapshot
            or (snapshot.frame_of_reference_uid and frames != {snapshot.frame_of_reference_uid})
        ):
            raise ValueError("The downloaded series has inconsistent DICOM membership or coordinate frames")
        return next(iter(frames))

    def cancel(self, job_id):
        with self.service.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            job = json.loads(con.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()["data"])
            if job["status"] in {"queued", "running"}:
                job["cancel_requested"] = True
                if job["status"] == "queued":
                    job.update(status="cancelled", stage="Cancelled", completed_at=now())
                    case = json.loads(con.execute("SELECT data FROM cases WHERE id=?", (job["case_id"],)).fetchone()["data"])
                    if case["status"] == "importing":
                        case.update(status="import_cancelled", error=None)
                        con.execute("UPDATE cases SET data=? WHERE id=?", (json.dumps(case), case["id"]))
                con.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job["status"], json.dumps(job), job_id))
        return job

    def run(self, job):
        from .dicom import index_dicom

        service = self.service
        try:
            snapshot = parse_snapshot(job["payload"]["snapshot"])
            root = service.settings.data_dir / "orthanc-staging"
            root.mkdir(parents=True, exist_ok=True)
            if reason := self._modality_reason(snapshot.modality):
                raise ValueError(reason)
            with tempfile.TemporaryDirectory(prefix="transfer-", dir=root) as temporary:
                directory = Path(temporary)

                last_poll = last_progress = float("-inf")
                was_cancelled = False

                def cancelled():
                    nonlocal last_poll, was_cancelled
                    if service.stop_event.is_set() or was_cancelled:
                        return True
                    moment = time.monotonic()
                    if moment - last_poll >= 0.1:
                        was_cancelled = service.cancelled(job)
                        last_poll = moment
                    return was_cancelled

                def progress(completed, total, received):
                    nonlocal last_progress
                    if cancelled():
                        raise InterruptedError("Orthanc import cancelled")
                    moment = time.monotonic()
                    # Jobs retain the complete source snapshot. Avoid rewriting
                    # it for every tiny DICOM file or rereading it per CT block.
                    if completed in {1, total} or moment - last_progress >= 0.2:
                        service.store.update_job(
                            job["id"],
                            stage=f"Importing DICOM instances {completed}/{total}",
                            progress={"phase": "archive_import", "completed": completed, "total": total, "bytes": received},
                        )
                        last_progress = moment

                with self.client() as client:
                    manifest = client.download(snapshot, directory, max_bytes=service.settings.upload_limit, progress=progress, cancelled=cancelled)
                frame_uid = self._verify(directory, snapshot, manifest, cancelled=cancelled)
                series = index_dicom(directory)
                if len(series) != 1 or series[0]["uid"] != snapshot.series_uid or not series[0]["supported"]:
                    raise ValueError("The selected DICOM series cannot form a supported reference image; check its acquisition geometry")
                service.check_cancel(job)
                destination = service.store.case_dir(job["case_id"]) / "sources" / job["id"] / "dicom"
                destination.parent.mkdir(parents=True, exist_ok=True)
                for row in series:
                    row["files"] = [str(destination / Path(file).relative_to(directory)) for file in row["files"]]
                source = parse_source(
                    SourceManifest(
                        snapshot=manifest.snapshot,
                        files=manifest.files,
                        bytes=manifest.bytes,
                        content_digest=manifest.content_digest,
                        kind="orthanc",
                        imported_at=now(),
                        frame_of_reference_uid=frame_uid,
                    )
                )
                source_data = json_record(source)
                encoded = json.dumps(source_data, indent=2, allow_nan=False).encode()
                provenance = {
                    "kind": "orthanc",
                    "source_key": snapshot.source_key,
                    "study_uid": snapshot.study_uid,
                    "series_uid": snapshot.series_uid,
                    "frame_of_reference_uid": frame_uid,
                    "snapshot": snapshot.snapshot,
                    "instance_count": len(manifest.files),
                    "content_digest": manifest.content_digest,
                    "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
                    "completeness": snapshot.completeness,
                    "warnings": list(snapshot.warnings),
                }
                # A successful import publishes bytes, case metadata and its job
                # together. Every retry has a fresh destination, never overwriting
                # an earlier source or accepted revision after an interruption.
                with service.store.connection() as con:
                    con.execute("BEGIN IMMEDIATE")
                    current = json.loads(con.execute("SELECT data FROM jobs WHERE id=?", (job["id"],)).fetchone()["data"])
                    if current.get("cancel_requested") or current["status"] != "running" or service.stop_event.is_set():
                        raise InterruptedError("Orthanc import cancelled")
                    case = json.loads(con.execute("SELECT data FROM cases WHERE id=?", (job["case_id"],)).fetchone()["data"])
                    if case.get("active_revision_id") or case.get("source_dir"):
                        raise OrthancChanged("The destination case changed while this archive import was running.")
                    directory.rename(destination)
                    atomic_json(destination.parent / "source-import.json", source_data)
                    case.update(status="imported", error=None, series=series, source_dir=str(destination), import_source=provenance)
                    case["references"]["orthanc"] = source_data
                    current.update(status="completed", stage="Imported; choose Open scan to prepare viewing", error=None, completed_at=now())
                    con.execute("UPDATE cases SET data=? WHERE id=?", (json.dumps(case, allow_nan=False), case["id"]))
                    con.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (current["status"], json.dumps(current, allow_nan=False), job["id"]))
        except BaseException as reason:
            # Keep failed/cancelled attempts recognizable and retryable. Staging
            # is disposable; any promoted source from an interrupted commit stays.
            with service.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                row = con.execute("SELECT data FROM cases WHERE id=?", (job["case_id"],)).fetchone()
                if row:
                    case = json.loads(row["data"])
                    if case["status"] == "importing":
                        case.update(
                            status="import_cancelled" if isinstance(reason, InterruptedError) else "import_failed",
                            error=None if isinstance(reason, InterruptedError) else "The Orthanc import did not complete. Retry the selected series.",
                        )
                        con.execute("UPDATE cases SET data=? WHERE id=?", (json.dumps(case), case["id"]))
            raise
