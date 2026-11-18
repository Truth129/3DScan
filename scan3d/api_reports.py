"""Research-only measurement reports of a saved revision: status, the recipe segmentation, runs, results and pictures."""

from fastapi.responses import JSONResponse, Response

from . import identity
from .api_models import ReportDicomExport, ReportSegment, ReportStart
from .store import public_job

_ROUTE = "/api/cases/{case_id}/revisions/{revision_id}/reports"
_NO_STORE = {"Cache-Control": "private, no-store"}


def add_routes(app, service):
    # Imported in each route, so starting the app loads no report code.
    @app.get(_ROUTE)
    def report_statuses(case_id: str, revision_id: str):
        from . import reports

        return JSONResponse(reports.statuses(service, case_id, revision_id), headers=_NO_STORE)

    @app.get(_ROUTE + "/{report_id}")
    def report_runs(case_id: str, revision_id: str, report_id: str):
        from . import reports

        return JSONResponse(reports.report(service, case_id, revision_id, report_id), headers=_NO_STORE)

    @app.post(_ROUTE + "/{report_id}")
    def start_report(case_id: str, revision_id: str, report_id: str, body: ReportStart):
        from . import reports

        return public_job(reports.start(service, case_id, revision_id, report_id))

    @app.post(_ROUTE + "/{report_id}/segment")
    def segment_for_report(case_id: str, revision_id: str, report_id: str, body: ReportSegment):
        from . import reports

        return public_job(reports.segment(service, case_id, revision_id, report_id, body.inference_path))

    # Not {job_id}: the access rules give that parameter the job rule, never the case rule.
    @app.get(_ROUTE + "/{report_id}/{analysis_id}")
    def report_result(case_id: str, revision_id: str, report_id: str, analysis_id: str):
        from . import reports

        return JSONResponse(reports.read(service, case_id, revision_id, report_id, analysis_id), headers=_NO_STORE)

    @app.get(_ROUTE + "/{report_id}/{analysis_id}/files/{name}")
    def report_picture(case_id: str, revision_id: str, report_id: str, analysis_id: str, name: str):
        from . import reports

        return Response(reports.picture(service, case_id, revision_id, report_id, analysis_id, name), media_type="image/png", headers=_NO_STORE)

    # A report's page and curved view as a DICOM series, chosen and guarded as the 3D view's DICOM export is.
    def report_exporter(case_id):
        return service.archive_report_exports if service.from_archive(case_id) else service.orthanc_report_exports

    @app.get(_ROUTE + "/{report_id}/{analysis_id}/dicom")
    def report_dicom_availability(case_id: str, revision_id: str, report_id: str, analysis_id: str):
        exporter, direct = report_exporter(case_id), service.direct_report_exports
        result = exporter.availability(case_id, revision_id, report_id, analysis_id, kinds=(exporter.kind, direct.kind))
        # Absent until destinations are configured, so the dialog stays as it was.
        if service.settings.dicom_send and service.import_kind(case_id) != "orthanc":
            result["direct"] = direct.direct_availability(case_id, revision_id, archive=identity.current().archive_access)
        return result

    @app.post(_ROUTE + "/{report_id}/{analysis_id}/dicom")
    def report_dicom_export(case_id: str, revision_id: str, report_id: str, analysis_id: str, body: ReportDicomExport):
        if body.delivery == "direct":
            # A direct send binds to the case's own CT, so an Orthanc case keeps sending through Orthanc.
            if service.import_kind(case_id) == "orthanc":
                raise ValueError("This case came from Orthanc, so it sends through Orthanc's destinations.")
            manager = service.direct_report_exports
        else:
            manager = report_exporter(case_id)
        return public_job(manager.start(case_id, revision_id, report_id, analysis_id, body.destination, resend=body.resend))

    # A file stays on this machine, so it needs neither the archive nor a CT imported from one.
    @app.get(_ROUTE + "/{report_id}/{analysis_id}/dicom-file")
    def report_dicom_file_availability(case_id: str, revision_id: str, report_id: str, analysis_id: str):
        return service.report_file_exports.availability(case_id, revision_id, report_id, analysis_id)

    @app.post(_ROUTE + "/{report_id}/{analysis_id}/dicom-file")
    def report_dicom_file_export(case_id: str, revision_id: str, report_id: str, analysis_id: str, body: ReportStart | None = None):
        return public_job(service.report_file_exports.start(case_id, revision_id, report_id, analysis_id))

    @app.get(_ROUTE + "/{report_id}/{analysis_id}/dicom-file/{export_id}/report.zip")
    def report_dicom_file(case_id: str, revision_id: str, report_id: str, analysis_id: str, export_id: str):
        body = service.report_file_exports.prepared_zip(case_id, revision_id, report_id, analysis_id, export_id)
        # Names no patient: the file may leave this machine without its case.
        name = f"scan3d-{report_id}-report-{revision_id[:8]}-{export_id[-8:]}.zip"
        return Response(body, media_type="application/zip", headers={"Content-Disposition": f'attachment; filename="{name}"'})
