"""People: what an administrator may change, and what nobody may, on synthetic accounts."""

import secrets
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from scan3d.accounts import Accounts, token_hash
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.errors import Conflict, Forbidden
from scan3d.store import Store
from test_access import BASE, ORIGIN, account
from test_accounts import rows
from test_library import off_mode
from test_purge import with_password

PERSON = {"id", "username", "display_name", "role", "archive_access", "state", "created_at", "last_seen_at", "link_expires_at"}
NOT_ADMIN = {"detail": "Administrator access is required"}


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    root = tmp_path_factory.mktemp("people")
    app = create_app(Settings(root / "data", (root,), web_dir=root / "dist", allowed_hosts=("127.0.0.1", "review.example"), auth="password"))
    service = app.state.service
    # First, so it is the oldest admin.
    admin = with_password(app, account(app, name="Admin"))
    return SimpleNamespace(
        app=app, service=service, accounts=service.accounts, store=service.store, admin=admin, member=account(app, role="member", name="Member")
    )


def username(client):
    """The name `account` gives a test person."""
    return f"user-{client.user_id[-8:]}"


def add(rig, name, **fields):
    response = rig.admin.post("/api/people", json={"username": name, **fields})
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store", response.text
    return response.json()


def test_people_lists_accounts_without_secrets(rig):
    code = add(rig, "listed")["link"]["code"]
    response = rig.admin.get("/api/people")
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    people = {person["id"]: person for person in response.json()}
    assert all(set(person) == PERSON for person in people.values())
    assert people[rig.admin.user_id]["state"] == "active" and people[rig.member.user_id]["role"] == "member"
    assert "scrypt$" not in response.text and code not in response.text and token_hash(code) not in response.text


def test_people_is_for_admins_only(rig):
    tried = 0
    for route in rig.app.routes:
        if getattr(route, "path", "").startswith("/api/people"):
            for method in route.methods:
                response = rig.member.request(method, route.path.replace("{user_id}", rig.admin.user_id), json={"role": "member"})
                assert response.status_code == 403 and response.json() == NOT_ADMIN, (method, route.path)
                tried += 1
    assert tried == 8
    assert rig.admin.get("/api/session").json()["user"]["role"] == "admin"


def test_a_people_change_checks_the_actor_again(rig):
    second = account(rig.app, name="Second")
    with rig.store.connection() as con:
        con.execute("UPDATE users SET role='member' WHERE id=?", (second.user_id,))
    with pytest.raises(Forbidden, match=NOT_ADMIN["detail"]):
        rig.accounts.set_role(second.user_id, rig.member.user_id, "admin")
    assert rows(rig, "SELECT role FROM users WHERE id=?", rig.member.user_id) == [("member",)]


def test_the_last_admin_who_can_sign_in_stays(tmp_path):
    accounts = Accounts(Store(tmp_path))
    real = accounts.add_user("real", secrets.token_urlsafe(16))
    accounts.invite(real, "invited", role="admin")
    for change in (lambda: accounts.set_role(None, real, "member"), lambda: accounts.disable(None, real)):
        with pytest.raises(Conflict, match="Keep at least one administrator who can sign in"):
            change()
    accounts.add_user("second", secrets.token_urlsafe(16))
    assert accounts.disable(None, real)["state"] == "disabled"
    assert accounts.set_role(None, real, "member")["role"] == "member"


def test_nobody_changes_their_own_role_or_disables_themselves(rig):
    mine = f"/api/people/{rig.admin.user_id}"
    for response in (rig.admin.post(f"{mine}/role", json={"role": "member"}), rig.admin.post(f"{mine}/disable")):
        assert response.status_code == 400 and response.json() == {"detail": "You cannot change your own role or disable your own account"}
    try:
        assert rig.admin.post(f"{mine}/archive", json={"archive_access": False}).json()["archive_access"] is False
    finally:
        rig.accounts.set_archive(None, rig.admin.user_id, True)
    assert rig.admin.post(f"{mine}/link").json()["purpose"] == "reset"
    assert rig.admin.get("/api/session").json()["user"]["role"] == "admin"


def test_disable_ends_sessions_and_links_and_enable_brings_neither_back(rig):
    person = with_password(rig.app, account(rig.app, role="member", name="Disabled"))
    path = f"/api/people/{person.user_id}"
    assert rig.admin.post(f"{path}/link").status_code == 200
    disabled = rig.admin.post(f"{path}/disable")
    assert disabled.status_code == 200 and disabled.json()["state"] == "disabled" and disabled.json()["link_expires_at"] is None
    assert person.get("/api/cases").status_code == 401
    assert rows(rig, "SELECT 1 FROM one_time_codes WHERE user_id=?", person.user_id) == []
    refused = rig.admin.post(f"{path}/link")
    assert refused.status_code == 409 and refused.json() == {"detail": "Enable this account before making a link"}
    signing_in = TestClient(rig.app, base_url=BASE, headers=ORIGIN).post("/api/session", json={"username": username(person), "password": person.password})
    assert signing_in.status_code == 401 and signing_in.json() == {"detail": "Wrong username or password"}
    assert rig.admin.post(f"{path}/enable").json()["state"] == "active"
    assert person.get("/api/cases").status_code == 401


def test_sign_out_everywhere_ends_every_session_of_that_person(rig):
    person = with_password(rig.app, account(rig.app, role="member"))
    again = TestClient(rig.app, base_url=BASE, headers=ORIGIN)
    assert again.post("/api/session", json={"username": username(person), "password": person.password}).status_code == 200
    response = rig.admin.post(f"/api/people/{person.user_id}/sign-out")
    assert response.status_code == 200 and response.json() == {"signed_out": 2}
    assert person.get("/api/cases").status_code == again.get("/api/cases").status_code == 401
    assert rig.admin.get("/api/cases").status_code == 200


def test_role_and_archive_changes_apply_on_the_next_request(rig):
    person = account(rig.app, role="member")
    path = f"/api/people/{person.user_id}"
    assert person.get("/api/storage").status_code == 403
    assert rig.admin.post(f"{path}/role", json={"role": "admin"}).json()["role"] == "admin"
    assert person.get("/api/storage").status_code == 200
    assert rig.admin.post(f"{path}/archive", json={"archive_access": False}).json()["archive_access"] is False
    refused = person.post("/api/orthanc/studies/query", json={})
    assert refused.status_code == 403 and refused.json() == {"detail": "This account cannot use the archive"}
    assert person.get("/api/session").json()["user"]["archive_access"] is False


def test_people_actions_are_audited_with_ids_only(rig):
    made = add(rig, "audit-target", display_name="Audit Target")
    person = made["person"]["id"]
    path = f"/api/people/{person}"
    code = rig.admin.post(f"{path}/link").json()["code"]
    for step, body in (("role", {"role": "admin"}), ("archive", {"archive_access": False}), ("disable", None), ("enable", None), ("sign-out", None)):
        assert rig.admin.post(f"{path}/{step}", json=body).status_code == 200, step
    admin = rig.admin.user_id
    assert rows(rig, "SELECT actor_id,action,detail FROM audit_events WHERE target=? ORDER BY seq", person) == [
        (admin, "user.invite", "{}"),
        (admin, "user.invite_link", "{}"),
        (admin, "user.make_admin", "{}"),
        (admin, "user.archive_off", "{}"),
        (admin, "user.disable", '{"count": 0}'),
        (admin, "user.enable", "{}"),
        (admin, "user.sign_out_everywhere", '{"count": 0}'),
    ]
    written = str(rows(rig, "SELECT * FROM audit_events"))
    for secret in ("audit-target", "Audit Target", made["link"]["code"], code, token_hash(code)):
        assert secret not in written


def test_people_and_redeem_with_sign_in_off(tmp_path):
    _, http = off_mode(tmp_path)
    assert http.get("/api/people").status_code == 404
