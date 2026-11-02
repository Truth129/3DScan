"""Case creation from the demo phantom, local folders, browser uploads and Orthanc."""

import zipfile

from fastapi import File, Form, UploadFile
from fastapi.responses import JSONResponse

from .api_models import ImportLocal, ImportStart, OrthancAddSeries, OrthancImport, OrthancLaunch, OrthancQuery, import_manifest
from .fileio import extract_zip, safe_relative
from .store import public_case, public_job
from .uploads import Uploads


def add_routes(app, service):
    @app.post("/api/cases/demo")
    def demo():
        from .demo import create_demo

        return public_case(create_demo(service), service.settings)

    @app.post("/api/cases/import-local")
    def import_local(body: ImportLocal):
        return public_case(service.import_local(body.path, body.label), service.settings)

    @app.get("/api/orthanc/status")
    def orthanc_status():
        return JSONResponse(service.orthanc.status(), headers={"Cache-Control": "no-store"})

    @app.post("/api/orthanc/studies/query")
    def orthanc_studies(body: OrthancQuery):
        names = {"patient_id": "PatientID", "patient_name": "PatientName", "accession_number": "AccessionNumber", "study_date": "StudyDate"}
        query = {tag: value.strip() for name, tag in names.items() if (value := getattr(body, name)) and value.strip()}
        return JSONResponse(service.orthanc.studies(query, body.offset, body.limit), headers={"Cache-Control": "no-store"})

    @app.get("/api/orthanc/studies/{study_id}/series")
    def orthanc_series(study_id: str):
        return JSONResponse(service.orthanc.series(study_id), headers={"Cache-Control": "no-store"})

    @app.post("/api/orthanc/imports")
    def orthanc_import(body: OrthancImport):
        return JSONResponse(public_job(service.orthanc.start(body.series_id, body.snapshot, body.label)), headers={"Cache-Control": "no-store"})

    # Under the case path, so the access rule for a case guards both routes.
    @app.get("/api/cases/{case_id}/orthanc-series")
    def case_orthanc_series(case_id: str):
        return JSONResponse(service.orthanc.case_series(case_id), headers={"Cache-Control": "no-store"})

    @app.post("/api/cases/{case_id}/orthanc-series")
    def add_orthanc_series(case_id: str, body: OrthancAddSeries):
        return JSONResponse(public_job(service.orthanc.start(body.series_id, body.snapshot, case_id=case_id)), headers={"Cache-Control": "no-store"})

    @app.post("/api/orthanc/launch")
    def orthanc_launch(body: OrthancLaunch):
        return JSONResponse(service.orthanc.launch(body.series_id, body.source_key), headers={"Cache-Control": "no-store"})

    @app.post("/api/cases/import")
    def import_files(files: list[UploadFile] = File(...), label: str | None = Form(None)):
        if label and len(label) > 128:
            raise ValueError("Case label is too long")
        case = service.new_case(label)
        source = service.store.case_dir(case["id"]) / "dicom"
        source.mkdir()
        total = 0
        try:
            if len(files) > 100_000:
                raise ValueError("Too many uploaded files")
            for uploaded in files:
                relative = safe_relative(uploaded.filename or "scan.dcm")
                destination = source / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    raise ValueError("Duplicate upload filename")
                with destination.open("wb") as output:
                    while chunk := uploaded.file.read(1024 * 1024):
                        total += len(chunk)
                        if total > service.settings.upload_limit:
                            raise ValueError("Upload exceeds import limit")
                        output.write(chunk)
                if zipfile.is_zipfile(destination):
                    expanded = source / (relative.stem + "_expanded")
                    extract_zip(destination, expanded, service.settings.upload_limit - total)
                    total += sum(p.stat().st_size for p in expanded.rglob("*") if p.is_file())
                    if total > service.settings.upload_limit:
                        raise ValueError("Expanded uploads exceed import limit")
            return public_case(service.finish_import(case, source), service.settings)
        except Exception as exc:
            case.update(status="import_failed", error=str(exc))
            service.store.save("cases", case)
            raise

    # Browsers upload in bounded batches: one multipart form cannot carry more
    # than 1,000 files, and batches give progress and retry over remote links.
    uploads = Uploads(service)

    @app.post("/api/imports")
    def import_start(body: ImportStart):
        return uploads.start(body.label, body.total_files, body.total_bytes)

    @app.post("/api/imports/{upload_id}/parts")
    def import_parts(upload_id: str, manifest: str = Form(...), files: list[UploadFile] = File(...)):
        entries = [entry.model_dump() for entry in import_manifest.validate_json(manifest)]
        return uploads.receive(upload_id, entries, [(uploaded.file, uploaded.size) for uploaded in files])

    @app.post("/api/imports/{upload_id}/finish")
    def import_finish(upload_id: str):
        return public_case(uploads.finish(upload_id), service.settings)

    @app.delete("/api/imports/{upload_id}")
    def import_cancel(upload_id: str):
        return uploads.cancel(upload_id)
