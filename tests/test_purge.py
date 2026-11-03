"""Delete forever on synthetic cases: the count of requests in flight that a purge waits for."""

from fastapi.testclient import TestClient

from test_access import BASE
from test_library import off_mode


def test_the_gate_counts_a_request_and_a_new_case_until_the_response_ends(tmp_path):
    service, http = off_mode(tmp_path)
    made = []

    @http.app.post("/api/cases/{case_id}/probe")
    def probe(case_id: str, fail: bool = False):
        made.append(service.new_case("Synthetic")["id"])
        if fail:
            raise RuntimeError("Synthetic failure")
        return {"case": service.activity.busy(case_id), "made": service.activity.busy(made[-1])}

    def settled(*case_ids):
        with service.activity.quiet() as idle:
            return idle and not any(service.activity.busy(case_id) for case_id in case_ids)

    case_id = service.new_case("Synthetic")["id"]
    assert http.post(f"/api/cases/{case_id}/probe").json() == {"case": True, "made": True}
    assert settled(case_id, *made)
    # A handler that fails gives its counts back too.
    failing = TestClient(http.app, base_url=BASE, raise_server_exceptions=False)
    assert failing.post(f"/api/cases/{case_id}/probe?fail=true").status_code == 500
    assert settled(case_id, *made)
    service.store.trash_case(case_id, None)
    response = http.post(f"/api/cases/{case_id}/probe")
    assert response.status_code == 404 and response.json() == {"detail": "Case not found"}
    assert len(made) == 2 and settled(case_id)
