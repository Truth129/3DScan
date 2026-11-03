"""One gate decides every route, by the route the router will pick, and refuses by default."""

from datetime import datetime, timedelta, timezone
import json
import re
import secrets
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
from starlette.routing import Mount

from scan3d.access import RULES, Rule, check_policy, matched_route, rule_for
from scan3d.accounts import token_hash
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.identity import EVERY_CASE
from scan3d.orthanc_exports import ARCHIVE_OFF
from scan3d.store import identifier, now

BASE = "http://127.0.0.1"
ORIGIN = {"Origin": BASE}


def account(app, role="admin", archive_access=1):
    """A signed-in browser for a row written directly, so these tests run no scrypt."""
    user_id, token = identifier(), secrets.token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    with app.state.service.store.connection() as con:
        con.execute(
            "INSERT INTO users(id,username,display_name,role,archive_access,created_at) VALUES (?,?,?,?,?,?)",
            (user_id, f"user-{user_id[-8:]}", "Someone", role, archive_access, now()),
        )
        con.execute(
            "INSERT INTO sessions(token_hash,user_id,created_at,last_seen_at,expires_at) VALUES (?,?,?,?,?)",
            (token_hash(token), user_id, now(), now(), expires),
        )
    client = TestClient(app, base_url=BASE, headers=ORIGIN)
    client.cookies.set("scan3d_session", token)
    client.user_id = user_id
    return client


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    root = tmp_path_factory.mktemp("access")
    (root / "dist").mkdir()
    (root / "dist" / "index.html").write_text("<!doctype html><title>Viewer</title>")
    app = create_app(Settings(root / "data", (root,), web_dir=root / "dist", auth="password"))
    owner = account(app)
    case = owner.post("/api/cases/demo").json()
    return SimpleNamespace(
        app=app,
        service=app.state.service,
        anonymous=TestClient(app, base_url=BASE),
        owner=owner,
        other=account(app),
        member=account(app, role="member", archive_access=0),
        case=case,
    )


def filled(path, value="0" * 32):
    return re.sub(r"\{[^}]+\}", value, path)


def ruled_routes(app):
    for route in app.routes:
        if not isinstance(route, Mount):
            for method in sorted(route.methods):
                yield route, method, rule_for(route, method)


def test_every_route_but_the_public_ones_refuses_an_anonymous_request(rig):
    walked = 0
    for route, method, rule in ruled_routes(rig.app):
        if rule.kind == "public":
            continue
        response = rig.anonymous.request(method, filled(route.path))
        assert response.status_code == 401, (method, route.path)
        assert response.json() == {"detail": "Sign in to continue"} and response.headers["cache-control"] == "no-store"
        walked += 1
    assert walked > 60


def test_another_users_case_reads_as_absent_on_every_case_route(rig):
    case_id = rig.case["id"]
    assert rig.owner.get(f"/api/cases/{case_id}").status_code == 200
    for route, method, rule in ruled_routes(rig.app):
        if rule.kind not in ("case", "case_archive"):
            continue
        path = route.path.replace("{case_id}", case_id)
        response = rig.other.request(method, filled(path))
        assert response.status_code == 404, (method, path)
        assert response.json() == {"detail": "Case not found"} and response.headers["cache-control"] == "no-store"


def test_a_case_refusal_matches_one_that_does_not_exist(rig):
    assert rig.other.get(f"/api/cases/{'0' * 32}").json() == rig.other.get(f"/api/cases/{rig.case['id']}").json()


def test_another_users_job_reads_as_absent(rig):
    job = rig.service.store.enqueue(rig.case["id"], "include_all", {})
    for reader in (
        rig.other.get(f"/api/jobs/{job['id']}"),
        rig.other.get(f"/api/jobs/{job['id']}/measurements"),
        rig.other.post(f"/api/jobs/{job['id']}/cancel"),
    ):
        assert reader.status_code == 404 and reader.json() == {"detail": "Job not found"}
    assert rig.service.store.get("jobs", job["id"])["status"] == "queued"
    assert rig.owner.get(f"/api/jobs/{job['id']}").status_code == 200


def test_a_job_response_does_not_say_who_asked(rig):
    job = rig.owner.post(
        f"/api/cases/{rig.case['id']}/refinements",
        json={
            "revision_id": rig.case["active_revision_id"],
            "structure_id": "synthetic:liver",
            "operation": {"type": "erase_region", "bounds_ras": [[0, 0, 0], [1, 1, 1]]},
        },
    ).json()
    assert "requested_by" not in job
    assert rig.service.store.get("jobs", job["id"])["requested_by"] == rig.owner.user_id


def start_upload(client):
    response = client.post("/api/imports", json={"label": "Synthetic", "total_files": 1, "total_bytes": 4})
    assert response.status_code == 200
    return response.json()["id"]


def test_another_users_upload_reads_as_absent(rig):
    upload_id = start_upload(rig.owner)
    state = json.loads((rig.service.settings.data_dir / "import-uploads" / upload_id / "state.json").read_text())
    assert state["owner_id"] == rig.owner.user_id
    for refused in (
        rig.other.post(f"/api/imports/{upload_id}/finish"),
        rig.other.delete(f"/api/imports/{upload_id}"),
        rig.other.post(f"/api/imports/{upload_id}/parts", data={"manifest": "[]"}, files={"files": ("x", b"x")}),
    ):
        assert refused.status_code == 404 and refused.json() == {"detail": "Upload not found"}
    assert rig.owner.delete(f"/api/imports/{upload_id}").status_code == 200


@pytest.mark.parametrize("owner", [None, "local"])
def test_an_upload_with_no_real_owner_is_for_admins_only(rig, owner):
    upload_id = start_upload(rig.owner)
    path = rig.service.settings.data_dir / "import-uploads" / upload_id / "state.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "owner_id": owner}))
    assert rig.member.delete(f"/api/imports/{upload_id}").status_code == 404
    assert rig.other.delete(f"/api/imports/{upload_id}").status_code == 200


@pytest.mark.parametrize("owner", [None, "local"])
def test_a_finished_upload_follows_its_case(rig, owner):
    upload_id = start_upload(rig.owner)
    path = rig.service.settings.data_dir / "import-uploads" / upload_id / "state.json"
    path.write_text(json.dumps({**json.loads(path.read_text()), "owner_id": owner, "case_id": rig.case["id"]}))
    refused = rig.other.post(f"/api/imports/{upload_id}/finish")
    assert refused.status_code == 404 and refused.json() == {"detail": "Upload not found"}
    assert rig.owner.post(f"/api/imports/{upload_id}/finish").status_code == 200


@pytest.mark.parametrize(
    "path, detail",
    [("/api/cases/not-a-record", "Case not found"), (f"/api/cases/{'A' * 32}", "Case not found"), ("/api/jobs/1%20OR%201%3D1", "Job not found")],
)
def test_a_malformed_id_is_absent_rather_than_an_error(rig, path, detail):
    response = rig.owner.get(path)
    assert response.status_code == 404 and response.json() == {"detail": detail}


@pytest.mark.parametrize("upload_id", ["%2e%2e", "not-a-record", "A" * 32])
def test_a_malformed_upload_id_never_reaches_the_disk(rig, upload_id):
    response = rig.owner.post(f"/api/imports/{upload_id}/finish")
    assert response.status_code == 404 and response.json() == {"detail": "Upload not found"}


def test_administrator_routes_refuse_a_member(rig):
    for response in (
        rig.member.post("/api/cases/import-local", json={"path": "/"}),
        rig.member.put("/api/totalsegmentator/license", json={"license_key": "aca_ABCDEFGHIJKLMNOP"}),
        rig.member.delete("/api/totalsegmentator/license"),
        rig.member.post("/api/storage/clear-caches"),
    ):
        assert response.status_code == 403 and response.json() == {"detail": "Administrator access is required"}


def test_archive_routes_refuse_an_account_without_the_archive(rig):
    for response in (
        rig.member.get("/api/orthanc/status"),
        rig.member.post("/api/orthanc/studies/query", json={}),
        rig.member.get(f"/api/orthanc/studies/{'0' * 32}/series"),
        rig.member.post("/api/orthanc/imports", json={"series_id": "00000000-00000000-00000000-00000000-00000000", "snapshot": "0" * 64}),
        rig.member.post("/api/orthanc/launch", json={"series_id": "00000000-00000000-00000000-00000000-00000000", "source_key": "0" * 64}),
    ):
        assert response.status_code == 403 and response.json() == {"detail": "This account cannot use the archive"}


def test_a_cases_archive_series_need_the_case_and_the_archive(rig):
    body = {"series_id": "00000000-00000000-00000000-00000000-00000000", "snapshot": "0" * 64}
    own = rig.member.post("/api/cases/demo").json()["id"]
    for response in (rig.member.get(f"/api/cases/{own}/orthanc-series"), rig.member.post(f"/api/cases/{own}/orthanc-series", json=body)):
        assert response.status_code == 403 and response.json() == {"detail": "This account cannot use the archive"}
    assert rig.member.get(f"/api/cases/{rig.case['id']}/orthanc-series").status_code == 404
    assert rig.member.post(f"/api/cases/{own}/trash").status_code == 200
    assert rig.member.get(f"/api/cases/{own}/orthanc-series").status_code == 404
    rig.owner.post(f"/api/cases/{rig.case['id']}/orthanc-series", json=body)
    with rig.service.store.connection() as con:
        assert con.execute("SELECT 1 FROM audit_events WHERE action='case.import' AND case_id=?", (rig.case["id"],)).fetchone()


def test_view_and_seg_sends_need_the_archive_and_a_seg_file_does_not(rig, monkeypatch):
    own = rig.member.post("/api/cases/demo").json()
    prefix = f"/api/cases/{own['id']}/revisions/{own['active_revision_id']}"
    for response in (rig.member.get(f"{prefix}/dicom-view"), rig.member.post(f"{prefix}/dicom-view"), rig.member.post(f"{prefix}/dicom-seg", json={})):
        assert response.status_code == 403 and response.json() == {"detail": "This account cannot use the archive"}
    # A demo has no DICOM source, so a file is refused for that, and not for the archive.
    assert rig.member.post(f"{prefix}/dicom-seg", json={"delivery": "file"}).status_code == 400
    monkeypatch.setattr(rig.service.orthanc, "client", lambda: pytest.fail("Without the archive nothing contacts Orthanc"))
    answer = rig.member.get(f"{prefix}/dicom-seg")
    assert answer.status_code == 200
    body = answer.json()
    assert body["available"] is False and body["reason"] == ARCHIVE_OFF and body["destinations"] == [] and "file" in body


def test_lists_hold_only_your_own_cases(rig):
    assert rig.case["id"] in [case["id"] for case in rig.owner.get("/api/cases").json()]
    assert rig.other.get("/api/cases").json() == []
    assert rig.owner.get("/api/agreement").json()["cases"]["total"] >= 1
    assert rig.other.get("/api/agreement").json()["cases"]["total"] == 0
    assert rig.case["id"] in [case["id"] for case in rig.service.store.visible_cases(EVERY_CASE)]
    assert rig.service.store.visible_cases(rig.other.user_id) == []


def test_a_scope_that_is_neither_everything_nor_a_person_is_a_defect(rig):
    for scope in (None, ""):
        with pytest.raises(TypeError):
            rig.service.store.case_summaries(scope)
        with pytest.raises(TypeError):
            rig.service.store.storage_rows(scope)
    with pytest.raises(ValueError, match="scope"):
        rig.service.store.list("cases")


def test_the_gate_picks_the_route_the_router_picks_and_leaves_the_scope_alone(rig, tmp_path):
    def scope(app, method, path):
        return {"type": "http", "method": method, "path": path, "root_path": "", "headers": [], "query_string": b"", "app": app}

    # A method mismatch keeps looking, so with a viewer built the static mount answers it.
    mismatch = scope(rig.app, "PUT", "/api/cases")
    before = dict(mismatch)
    route, params = matched_route(mismatch)
    assert isinstance(route, Mount) and params == {} and mismatch == before
    assert rig.anonymous.put("/api/cases").status_code == 405
    assert rig.anonymous.request("HEAD", "/api/cases").status_code == 404
    # A later route can still match in full after an earlier partial one.
    route, params = matched_route(scope(rig.app, "PATCH", f"/api/cases/{'0' * 32}/revisions/{'1' * 32}"))
    assert route.endpoint.__name__ == "review" and params == {"case_id": "0" * 32, "revision_id": "1" * 32}
    # With no viewer there is no mount, so the same mismatch has no full match and the router says 405.
    bare = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", auth="password"))
    assert matched_route(scope(bare, "PUT", "/api/cases")) == (None, {})
    assert TestClient(bare, base_url=BASE).put("/api/cases").status_code == 405


def test_a_route_with_no_rule_stops_the_app_starting_and_never_runs(tmp_path):
    ran = []
    for auth in ("off", "password"):
        app = create_app(Settings(tmp_path / auth, (tmp_path,), web_dir=tmp_path / "dist", auth=auth))
        app.add_api_route("/api/unruled", lambda: ran.append(True))
        with pytest.raises(RuntimeError, match="/api/unruled"):
            check_policy(app.routes)
        assert TestClient(app, base_url=BASE).get("/api/unruled").status_code in (401, 403)
    assert TestClient(app, base_url=BASE).get("/api/unruled").json() == {"detail": "Sign in to continue"}
    assert account(app).get("/api/unruled").status_code == 403 and ran == []


def test_fastapi_pages_are_refused_in_password_mode_even_if_one_appears(tmp_path):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", auth="password"))
    app.add_api_route("/openapi.json", lambda: {"paths": "everything"})
    assert TestClient(app, base_url=BASE).get("/openapi.json").status_code == 401
    assert account(app).get("/openapi.json").status_code == 403


def test_the_policy_refuses_what_it_cannot_decide(tmp_path, monkeypatch):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    check_policy(app.routes)

    async def nothing(websocket):
        return None

    app.add_api_websocket_route("/api/socket", nothing)
    with pytest.raises(RuntimeError, match="Unknown route type"):
        check_policy(app.routes)
    app.router.routes.pop()
    monkeypatch.setitem(RULES, ("GET", "/api/gone"), Rule("public"))
    with pytest.raises(RuntimeError, match="/api/gone"):
        check_policy(app.routes)
    monkeypatch.delitem(RULES, ("GET", "/api/gone"))
    monkeypatch.setitem(RULES, ("GET", "/api/cases/{case_id}/images"), Rule("signed_in"))
    with pytest.raises(RuntimeError, match="path parameters"):
        check_policy(app.routes)


def test_the_policy_refuses_a_trashed_rule_on_a_route_with_no_case_id(tmp_path, monkeypatch):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    monkeypatch.setitem(RULES, ("GET", "/api/cases"), Rule("trashed"))
    with pytest.raises(RuntimeError, match="path parameters"):
        check_policy(app.routes)


def test_every_kind_a_rule_names_is_one_the_gate_decides():
    kinds = {"public", "off_only", "signed_in", "admin", "archive", "job", "upload", "case", "case_archive", "trashed", "trashed_admin"}
    assert {rule.kind for rule in RULES.values()} == kinds


def test_with_sign_in_off_everything_stays_open_and_audited_rules_still_write(tmp_path):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    http = TestClient(app, base_url=BASE)
    assert http.get("/api/session").json() == {
        "mode": "off",
        "user": {"id": "local", "username": "local", "display_name": "Local user", "role": "admin", "archive_access": True},
        "setup_required": False,
    }
    assert http.post("/api/session", json={"username": "someone", "password": "x" * 12}).status_code == 400
    assert all(http.get(path).status_code == 200 for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"))
    assert "gpu" in http.get("/api/health").json()
    case = http.post("/api/cases/demo").json()
    assert [entry["id"] for entry in http.get("/api/cases").json()] == [case["id"]]
    assert http.get(f"/api/cases/{case['id']}").status_code == http.get(f"/api/cases/{case['id']}").status_code == 200
    # Malformed IDs keep their existing answer, and raw input never reaches the log.
    assert http.get("/api/cases/not-a-record").status_code == 400
    with app.state.service.store.connection() as con:
        rows = [tuple(row) for row in con.execute("SELECT actor_id,action,case_id FROM audit_events ORDER BY seq")]
        assert con.execute("SELECT COUNT(*) FROM case_access").fetchone()[0] == 0
    assert rows == [("local", "case.import", None), ("local", "case.open", case["id"])]
    assert app.state.service.store.get("revisions", case["active_revision_id"])["case_id"] == case["id"]
