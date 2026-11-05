"""Search, import, launch and Add series for a DICOMweb archive, beside the Orthanc routes."""

from fastapi.responses import JSONResponse

from ..api_models import ArchiveImport, ArchiveLaunch, ArchiveSeries, ArchiveStudy, OrthancQuery, study_query
from ..store import public_job

_NO_STORE = {"Cache-Control": "no-store"}


def add_routes(app, service):
    @app.get("/api/archive/status")
    def archive_status():
        return JSONResponse(service.archive_imports.status(), headers=_NO_STORE)

    @app.post("/api/archive/studies/query")
    def archive_studies(body: OrthancQuery):
        return JSONResponse(service.archive_imports.studies(study_query(body), body.offset, body.limit), headers=_NO_STORE)

    @app.post("/api/archive/studies/series")
    def archive_series(body: ArchiveStudy):
        return JSONResponse(service.archive_imports.series(body.study_uid), headers=_NO_STORE)

    @app.post("/api/archive/imports")
    def archive_import(body: ArchiveImport):
        job = service.archive_imports.start(body.study_uid, body.series_uid, body.snapshot, body.label, reimport=body.reimport)
        return JSONResponse(public_job(job), headers=_NO_STORE)

    @app.post("/api/archive/launch")
    def archive_launch(body: ArchiveLaunch):
        return JSONResponse(service.archive_imports.launch(body.study_uid, body.series_uid, body.source_key), headers=_NO_STORE)

    @app.get("/api/cases/{case_id}/archive-series")
    def case_archive_series(case_id: str):
        return JSONResponse(service.archive_imports.case_series(case_id), headers=_NO_STORE)

    @app.post("/api/cases/{case_id}/archive-series")
    def add_archive_series(case_id: str, body: ArchiveSeries):
        job = service.archive_imports.start(body.study_uid, body.series_uid, body.snapshot, case_id=case_id)
        return JSONResponse(public_job(job), headers=_NO_STORE)
