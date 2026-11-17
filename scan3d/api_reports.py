"""Research-only measurement reports of a saved revision: status, the recipe segmentation, runs, results and pictures."""

from fastapi.responses import JSONResponse, Response

from .api_models import ReportSegment, ReportStart
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
