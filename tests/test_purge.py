"""Delete forever on synthetic cases: what a purge waits for and what it must find."""

import json

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


def test_review_cache_entries_name_their_case(tmp_path):
    service, http = off_mode(tmp_path)
    case = http.post("/api/cases/demo").json()
    revision = service.store.get("revisions", case["active_revision_id"])
    # A revision from before image assets, whose export entries were once keyed without the image.
    revision.pop("reference_image_id", None)
    revision.pop("image_asset", None)
    service.store.save("revisions", revision)
    prefix = f"/api/cases/{case['id']}/revisions/{revision['id']}"
    assert http.get(f"{prefix}/ct-volume?quality=full").status_code == 200
    service.settings.web_dir.mkdir()
    (service.settings.web_dir / "offline-viewer.js").write_text("var Scan3DOffline={initOffline(){}};")
    exported = http.post(f"{prefix}/export.html", json={"layers": {"segmentation": False, "ct_volume": True, "ct_plane": False}})
    assert exported.status_code == 200, exported.text
    records = [json.loads(path.read_text()) for path in (service.settings.data_dir / "review-cache" / "volumes").glob("*/*/manifest.json")]
    assert records and all(record.get("case_id") == case["id"] for record in records)
    assert sum(record["descriptor"]["kind"] == "ct" and record["descriptor"]["quality"] == "full" for record in records) == 1
