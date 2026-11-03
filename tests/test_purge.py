"""Delete forever on synthetic cases: what a purge waits for and what it must find."""

import json
import secrets
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from scan3d import identity
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.store import identifier
from test_access import BASE, account
from test_library import off_mode

ROW_TABLES = ("cases", "case_summaries", "revisions", "jobs", "review_sessions", "image_assets", "case_access", "case_opened")


def with_password(app, client):
    """Give a test account a password generated here, so a purge has one to check."""
    client.password = secrets.token_urlsafe(16)
    with app.state.service.store.connection() as con:
        con.execute("UPDATE users SET password_hash=? WHERE id=?", (identity.hash_password(client.password), client.user_id))
    return client


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    root = tmp_path_factory.mktemp("purge")
    app = create_app(Settings(root / "data", (root,), web_dir=root / "dist", auth="password"))
    # First, so it is the oldest admin and owns what `service.new_case` makes.
    owner = with_password(app, account(app))
    return SimpleNamespace(app=app, service=app.state.service, owner=owner, other=account(app), member=account(app, role="member", archive_access=0))


def trashed(rig):
    """A bare case of the owner's, in Trash."""
    case_id = rig.service.new_case("Synthetic")["id"]
    assert rig.owner.post(f"/api/cases/{case_id}/trash").status_code == 200
    return case_id


def purge(client, case_id, password=None):
    return client.post(f"/api/trash/{case_id}/purge", json={"password": client.password if password is None else password})


def wrong():
    return "not-" + secrets.token_urlsafe(8)


def refused(response, status, detail):
    assert response.status_code == status, response.text
    assert response.json() == {"detail": detail} and response.headers["cache-control"] == "no-store"


def rows(service, case_id):
    with service.store.connection() as con:
        return {
            table: con.execute(f"SELECT COUNT(*) FROM {table} WHERE {'id' if table == 'cases' else 'case_id'}=?", (case_id,)).fetchone()[0]
            for table in ROW_TABLES
        }


def trash_state(service, case_id):
    with service.store.connection() as con:
        return con.execute("SELECT state FROM trash WHERE case_id=? AND kind='case'", (case_id,)).fetchone()["state"]


def test_purge_removes_every_row_and_audits_counts_only(rig):
    case = rig.owner.post("/api/cases/demo").json()
    assert rig.owner.post(f"/api/cases/{case['id']}/open").status_code == 200
    rig.service.store.enqueue(case["id"], "include_all", {})
    with rig.service.store.connection() as con:
        con.execute("INSERT INTO review_sessions VALUES (?,?,?)", (identifier(), case["id"], "{}"))
    assert rig.owner.post(f"/api/cases/{case['id']}/trash").status_code == 200
    before = rows(rig.service, case["id"])
    assert all(before.values()), before
    response = purge(rig.owner, case["id"])
    assert response.status_code == 200 and response.json() == {"id": case["id"]}
    assert response.headers["cache-control"] == "no-store"
    assert rows(rig.service, case["id"]) == dict.fromkeys(ROW_TABLES, 0)
    assert trash_state(rig.service, case["id"]) == "purging"
    with rig.service.store.connection() as con:
        audit = [dict(row) for row in con.execute("SELECT action,detail FROM audit_events WHERE case_id=? ORDER BY seq", (case["id"],))]
    counts = {table: before[table] for table in ("revisions", "jobs", "review_sessions", "image_assets")}
    assert audit[-1]["action"] == "case.purge" and json.loads(audit[-1]["detail"]) == counts
    assert case["label"] not in json.dumps(audit)


def test_purge_needs_the_password_and_refuses_a_wrong_one(rig):
    case_id = trashed(rig)
    refused(rig.owner.post(f"/api/trash/{case_id}/purge", json={}), 400, "Enter your password to delete forever.")
    guess = wrong()
    response = purge(rig.owner, case_id, guess)
    refused(response, 403, "Wrong password.")
    assert guess not in response.text
    with rig.service.store.connection() as con:
        failed = con.execute("SELECT actor_id,detail FROM audit_events WHERE case_id=? AND action='case.purge_password_failed'", (case_id,)).fetchall()
    assert [tuple(row) for row in failed] == [(rig.owner.user_id, "{}")]
    assert case_id in [case["id"] for case in rig.owner.get("/api/trash").json()]
    assert purge(rig.owner, case_id).status_code == 200


def test_purge_password_guesses_share_the_sign_in_wait(rig):
    # Its own account, so the wait reaches no other test.
    guesser = with_password(rig.app, account(rig.app))
    case_id = rig.service.new_case("Synthetic")["id"]
    with rig.service.store.connection() as con:
        con.execute("UPDATE case_access SET user_id=? WHERE case_id=?", (guesser.user_id, case_id))
        username = con.execute("SELECT username FROM users WHERE id=?", (guesser.user_id,)).fetchone()[0]
    assert guesser.post(f"/api/cases/{case_id}/trash").status_code == 200
    for _ in range(4):
        refused(purge(guesser, case_id, wrong()), 403, "Wrong password.")
    response = guesser.post("/api/session", json={"username": username, "password": guesser.password})
    assert response.status_code == 429 and response.headers["retry-after"] == "1"


def test_purge_with_sign_in_off_needs_a_confirmation(tmp_path):
    service, http = off_mode(tmp_path)
    case_id = service.new_case("Synthetic")["id"]
    assert http.post(f"/api/cases/{case_id}/trash").status_code == 200
    refused(http.post(f"/api/trash/{case_id}/purge", json={}), 400, "Confirm to delete forever.")
    assert http.post(f"/api/trash/{case_id}/purge", json={"confirm": True}).json() == {"id": case_id}
    assert trash_state(service, case_id) == "purging"


def test_purge_is_only_for_the_owner_of_a_case_in_trash(rig):
    case_id = trashed(rig)
    for client in (rig.other, rig.member):
        refused(purge(client, case_id, wrong()), 404, "Case not found")
    refused(purge(rig.owner, rig.service.new_case("Synthetic")["id"]), 404, "Case not found")
    assert purge(rig.owner, case_id).status_code == 200
    refused(purge(rig.owner, case_id), 404, "Case not found")


def test_purge_waits_for_a_running_job_and_drops_queued_ones(rig):
    store = rig.service.store
    case_id = trashed(rig)
    running = store.enqueue(case_id, "include_all", {})["id"]
    store.update_job(running, status="running")
    refused(purge(rig.owner, case_id), 409, "Work on this case is still stopping. Try again in a minute.")
    sending = store.enqueue(case_id, "orthanc_export", {"delivery_state": "pending"})["id"]
    store.update_job(running, status="completed")
    store.update_job(sending, status="running")
    refused(purge(rig.owner, case_id), 409, "A send to Orthanc is still finishing. Try again in a few minutes.")
    store.update_job(sending, status="completed")
    store.enqueue(case_id, "include_all", {})
    assert purge(rig.owner, case_id).status_code == 200
    assert rows(rig.service, case_id)["jobs"] == 0


def test_purge_waits_for_a_request_on_its_case(rig):
    case_id = trashed(rig)
    rig.service.activity.enter(case_id)
    try:
        refused(purge(rig.owner, case_id), 409, "This case is still in use. Try again in a minute.")
    finally:
        rig.service.activity.leave(case_id)
    assert purge(rig.owner, case_id).status_code == 200


def test_a_malformed_purge_body_does_not_echo_the_password(rig):
    case_id = trashed(rig)
    secret = secrets.token_urlsafe(16)
    response = rig.owner.post(f"/api/trash/{case_id}/purge", json={"password": secret, "extra": 1})
    refused(response, 422, "Invalid delete request.")
    assert secret not in response.text


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
