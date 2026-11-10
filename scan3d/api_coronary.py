"""Research-only coronary analysis of a saved revision: start a run, list the revision's runs, read one."""

from fastapi.responses import JSONResponse

from .api_models import CoronaryInput
from .store import public_job

_ROUTE = "/api/cases/{case_id}/revisions/{revision_id}/coronary-analysis"
_NO_STORE = {"Cache-Control": "private, no-store"}


def add_routes(app, service):
    # Imported in each route, so starting the app loads no solver.
    @app.post(_ROUTE)
    def start_coronary_analysis(case_id: str, revision_id: str, body: CoronaryInput):
        from . import coronary

        return public_job(coronary.start(service, case_id, revision_id, body.model_dump()))

    @app.get(_ROUTE)
    def coronary_analyses(case_id: str, revision_id: str):
        from . import coronary

        return JSONResponse(coronary.analyses(service, case_id, revision_id), headers=_NO_STORE)

    # Not {job_id}: the access rules give that parameter the job rule, never the case rule.
    @app.get(_ROUTE + "/{analysis_id}")
    def coronary_analysis(case_id: str, revision_id: str, analysis_id: str):
        from . import coronary

        return JSONResponse(coronary.read(service, case_id, revision_id, analysis_id), headers=_NO_STORE)
