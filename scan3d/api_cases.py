"""Case, job and revision records, and the actions that create new revisions."""

import uuid

from fastapi import File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from starlette.concurrency import run_in_threadpool

from . import identity
from .access import ARCHIVE_REFUSED
from .accounts import TooManyAttempts, WrongPassword
from .api_models import Activate, CaseRename, DicomSegExport, Process, Purge, Refine, Review, Segment
from .api_session import secure_enough
from .disk_usage import case_sizes, storage_report
from .errors import NotFound
from .store import public_case, public_job, public_revision


def add_routes(app, service):
    password_mode = service.settings.sign_in_required

    @app.get("/api/cases")
    def cases():
        principal = identity.current()
        return [public_case(case, service.settings) for case in service.store.case_summaries(identity.case_scope(principal), principal.id)]

    @app.get("/api/cases/{case_id}")
    def case(case_id: str):
        return public_case(service.store.get("cases", case_id), service.settings)

    @app.patch("/api/cases/{case_id}")
    def rename_case(case_id: str, body: CaseRename):
        return public_case(service.store.rename_case(case_id, body.label), service.settings)

    @app.post("/api/cases/{case_id}/open")
    def mark_opened(case_id: str):
        service.store.mark_opened(case_id, identity.actor_id())
        return {"id": case_id}

    @app.post("/api/cases/{case_id}/trash")
    def trash_case(case_id: str):
        return service.trash_case(case_id)

    @app.post("/api/trash/{case_id}/restore")
    def restore_case(case_id: str):
        service.store.restore_case(case_id, identity.actor_id())
        return public_case(service.store.get("cases", case_id), service.settings)

    @app.post("/api/trash/{case_id}/purge")
    async def purge_case(case_id: str, body: Purge, request: Request):
        if password_mode:
            password = body.password.get_secret_value() if body.password else ""
            if not password:
                raise ValueError("Enter your password to delete forever.")
            if not secure_enough(request):
                raise HTTPException(403, "Delete forever through the HTTPS address")
        elif not body.confirm:
            raise ValueError("Confirm to delete forever.")
        # A case that cannot go yet costs no password hash.
        await run_in_threadpool(service.check_purge, case_id)
        if password_mode:
            try:
                await service.accounts.queued_recheck(identity.current(), password, case_id)
            except TooManyAttempts as exc:
                raise HTTPException(429, f"Too many attempts. Try again in {exc.seconds} seconds.", headers={"Retry-After": str(exc.seconds)}) from None
            except WrongPassword:
                raise HTTPException(403, "Wrong password.") from None
        return await run_in_threadpool(service.purge_case, case_id)

    @app.get("/api/trash")
    def trashed_cases():
        principal = identity.current()
        return [public_case(case, service.settings) for case in service.store.trashed_cases(identity.case_scope(principal), principal.id)]

    @app.get("/api/sizes")
    def sizes():
        scope = identity.case_scope(identity.current())
        return {"sizes": case_sizes(service.settings.data_dir, service.store.case_ids(scope))}

    @app.get("/api/storage")
    def storage():
        return storage_report(service.store, service.settings.data_dir, identity.case_scope(identity.current()))

    @app.post("/api/storage/clear-caches")
    def clear_caches():
        freed = service.clear_caches()
        return {"freed": freed, "storage": storage_report(service.store, service.settings.data_dir, identity.case_scope(identity.current()))}

    @app.post("/api/cases/{case_id}/process")
    def process(case_id: str, body: Process):
        return public_job(service.process(case_id, body.series_uid, body.profile, body.phase, cardiac_phase=body.cardiac_phase, rr_percent=body.rr_percent))

    @app.get("/api/cases/{case_id}/images")
    def image_assets(case_id: str):
        from .image_assets import public_image_asset

        service.store.get("cases", case_id)
        return [public_image_asset(asset) for asset in service.store.image_assets(case_id)]

    @app.get("/api/cases/{case_id}/images/{image_id}")
    def image_asset(case_id: str, image_id: str):
        from .image_assets import public_image_asset

        return public_image_asset(service.store.image_asset(case_id, image_id))

    @app.post("/api/cases/{case_id}/segment")
    def segment(case_id: str, body: Segment):
        return public_job(service.segment_case(case_id, **body.model_dump()))

    @app.get("/api/jobs/{job_id}")
    def job(job_id: str):
        return public_job(service.store.get("jobs", job_id))

    @app.get("/api/jobs/{job_id}/measurements")
    def measurements(job_id: str):
        record = service.store.get("jobs", job_id)
        structures = record.get("measurements", [])
        return {"structures": structures, "completed": len(structures), "total": record.get("progress", {}).get("total", 0)}

    @app.get("/api/cases/{case_id}/jobs")
    def case_jobs(case_id: str):
        service.store.get("cases", case_id)
        return [public_job(record) for record in service.store.list("jobs", case_id)]

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return public_job(service.cancel_job(job_id))

    @app.get("/api/cases/{case_id}/revisions")
    def revisions(case_id: str):
        service.store.get("cases", case_id)
        return [public_revision(r) for r in service.store.list("revisions", case_id)]

    @app.get("/api/cases/{case_id}/revisions/{revision_id}")
    def revision(case_id: str, revision_id: str):
        result = public_revision(service.ct_revision(case_id, revision_id))
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"}) if revision_id.startswith("review-") else result

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/dicom-seg")
    def dicom_seg_availability(case_id: str, revision_id: str):
        # A file stays on this machine, so this still answers. Without the archive it stops short of Orthanc.
        return service.orthanc_exports.availability(case_id, revision_id, archive=identity.current().archive_access)

    @app.post("/api/cases/{case_id}/revisions/{revision_id}/dicom-seg")
    def dicom_seg_export(case_id: str, revision_id: str, body: DicomSegExport):
        # The gate never reads a body, and a file stays on this machine, so only a send needs the archive.
        if body.delivery == "orthanc" and not identity.current().archive_access:
            raise HTTPException(403, ARCHIVE_REFUSED)
        return public_job(
            service.orthanc_exports.start(
                case_id, revision_id, body.structure_ids, body.destination, resend=body.resend, overlap_priority=body.overlap_priority, delivery=body.delivery
            )
        )

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/dicom-seg/{export_id}/segment.dcm")
    def dicom_seg_file(case_id: str, revision_id: str, export_id: str):
        path = service.orthanc_exports.prepared_file(case_id, revision_id, export_id)
        # Names no patient: the file may leave this machine without its case. IDs
        # open with a timestamp, so only the export ID's tail tells two files apart.
        name = f"scan3d-seg-{revision_id[:8]}-{export_id[-8:]}.dcm"
        return FileResponse(path, media_type="application/dicom", filename=name)

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/dicom-view")
    def dicom_view_availability(case_id: str, revision_id: str):
        return service.orthanc_view_exports.availability(case_id, revision_id)

    # A plain def, so that decoding the capture never runs on the event loop.
    @app.post("/api/cases/{case_id}/revisions/{revision_id}/dicom-view")
    def dicom_view_export(
        case_id: str,
        revision_id: str,
        image: UploadFile = File(...),
        reference_image_id: str = Form(..., min_length=1, max_length=128),
        destination: str | None = Form(None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$"),
        resend: bool = Form(False),
    ):
        return public_job(service.orthanc_view_exports.start(case_id, revision_id, image.file, reference_image_id, destination, resend=resend))

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/cinematic-reference")
    def cinematic_reference(case_id: str, revision_id: str):
        from .cinematic import reference_values

        # Statistics apply to an immutable saved revision, never an unsaved
        # candidate. Ownership is checked before cached statistics are read.
        if revision_id.startswith("review-"):
            raise NotFound("Save the segmentation before measuring cinematic references")
        selected = service.store.revision(case_id, revision_id)
        result = reference_values(selected, service.settings.data_dir / "cinematic-cache")
        return JSONResponse(result, headers={"Cache-Control": "private, no-store"})

    @app.get("/api/cases/{case_id}/revisions/{revision_id}/agreement")
    def revision_agreement_report(case_id: str, revision_id: str):
        from .agreement import revision_agreement

        # Agreement measures what a reviewer accepted. A candidate alias is an
        # unsaved proposal, never an accepted revision.
        if revision_id.startswith("review-"):
            raise NotFound("Save the correction before measuring agreement")
        return JSONResponse(revision_agreement(service, case_id, revision_id), headers={"Cache-Control": "private, no-store"})

    @app.get("/api/agreement")
    def agreement_summary():
        from .agreement import aggregate_agreement

        return JSONResponse(aggregate_agreement(service, identity.case_scope(identity.current())), headers={"Cache-Control": "private, no-store"})

    @app.post("/api/cases/{case_id}/refinements")
    def refine(case_id: str, body: Refine):
        parent = service.store.revision(case_id, body.revision_id)
        if body.structure_id not in {s["id"] for s in parent["structures"]}:
            raise ValueError("Structure does not belong to the selected revision")
        return public_job(service.store.enqueue(case_id, "refine", body.model_dump()))

    @app.post("/api/cases/{case_id}/activate")
    def activate(case_id: str, body: Activate):
        return public_case(service.store.activate(case_id, body.revision_id), service.settings)

    @app.patch("/api/cases/{case_id}/revisions/{revision_id}")
    def review(case_id: str, revision_id: str, body: Review):
        return public_revision(service.store.set_review_status(case_id, revision_id, body.review_status))

    @app.post("/api/cases/{case_id}/corrections")
    def corrections(case_id: str, revision_id: str = Form(...), segmentation: UploadFile = File(...)):
        service.store.revision(case_id, revision_id)
        path = service.store.case_dir(case_id) / "uploads" / f"{uuid.uuid4().hex}.seg.nrrd"
        path.parent.mkdir(exist_ok=True)
        total = 0
        with path.open("wb") as out:
            while chunk := segmentation.file.read(1024 * 1024):
                total += len(chunk)
                if total > service.settings.upload_limit:
                    raise ValueError("Correction exceeds import limit")
                out.write(chunk)
        return public_job(service.store.enqueue(case_id, "correction", {"revision_id": revision_id, "seg_file": str(path)}))

    @app.post("/api/cases/{case_id}/dicom-seg/imports")
    def import_dicom_seg(case_id: str, revision_id: str = Form(...), files: list[UploadFile] = File(...)):
        from .dicom_seg_jobs import stage_import

        return JSONResponse(public_job(stage_import(service, case_id, revision_id, files)), headers={"Cache-Control": "no-store"})
