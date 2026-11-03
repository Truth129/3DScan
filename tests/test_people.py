"""People and one-time links: what an administrator may change, and what nobody may, on synthetic accounts."""

from datetime import datetime, timedelta, timezone
import secrets
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from scan3d import identity
from scan3d.accounts import Accounts, LinkUsed, token_hash
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
LINK_USED = {"detail": "This link has expired or was already used. Ask an administrator for a new one."}


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


def browser(rig):
    return TestClient(rig.app, base_url=BASE, headers=ORIGIN)


def redeem(client, code, password=None):
    return client.post("/api/session/redeem", json={"code": code, "password": password or secrets.token_urlsafe(16)})


def link_of(rig, user_id):
    return next(person for person in rig.accounts.people() if person["id"] == user_id)["link_expires_at"]


def test_an_admin_adds_a_person_who_sets_a_password_from_the_link(rig):
    made = add(rig, "newcomer", display_name="New Comer")
    person, link = made["person"], made["link"]
    assert (person["role"], person["state"], person["archive_access"], link["purpose"]) == ("member", "invited", True, "invite")
    assert person["link_expires_at"] == link["expires_at"]
    stored = rows(rig, "SELECT * FROM one_time_codes WHERE user_id=?", person["id"])
    assert [row[0] for row in stored] == [token_hash(link["code"])] and link["code"] not in str(stored)
    newcomer, password = browser(rig), secrets.token_urlsafe(16)
    response = redeem(newcomer, link["code"], password)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store" and "set-cookie" in response.headers
    assert response.json()["user"] == {"id": person["id"], "username": "newcomer", "display_name": "New Comer", "role": "member", "archive_access": True}
    assert newcomer.get("/api/cases").status_code == 200
    shown = next(item for item in rig.admin.get("/api/people").json() if item["id"] == person["id"])
    assert shown["state"] == "active" and shown["last_seen_at"] and shown["link_expires_at"] is None
    written = rows(rig, "SELECT actor_id,action,target FROM audit_events WHERE actor_id=? ORDER BY seq", person["id"])
    assert written == [(person["id"], "user.link_used", person["id"]), (person["id"], "session.sign_in", None)]
    assert not any(secret in str(rows(rig, "SELECT * FROM audit_events")) for secret in (link["code"], password))


def test_a_link_works_once(rig):
    code = add(rig, "once")["link"]["code"]
    assert redeem(browser(rig), code).status_code == 200
    sessions = rows(rig, "SELECT token_hash FROM sessions ORDER BY token_hash")
    again = redeem(browser(rig), code)
    assert again.status_code == 410 and again.json() == LINK_USED and again.headers["cache-control"] == "no-store"
    assert rows(rig, "SELECT token_hash FROM sessions ORDER BY token_hash") == sessions


def test_two_uses_of_one_link_at_once_sign_in_once(rig):
    made = add(rig, "twice")
    barrier, results = threading.Barrier(2), []

    def use():
        barrier.wait()
        try:
            results.append(rig.accounts.redeem(made["link"]["code"], secrets.token_urlsafe(16))[1].id)
        except LinkUsed:
            results.append(LinkUsed)

    threads = [threading.Thread(target=use) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(results, key=str) == sorted([made["person"]["id"], LinkUsed], key=str)
    assert len(rows(rig, "SELECT 1 FROM sessions WHERE user_id=?", made["person"]["id"])) == 1


def test_an_expired_or_replaced_link_reads_as_used(rig):
    made = add(rig, "expiring")
    person = made["person"]["id"]
    with rig.store.connection() as con:
        past = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat(timespec="microseconds")
        con.execute("UPDATE one_time_codes SET expires_at=? WHERE user_id=?", (past, person))
    assert redeem(browser(rig), made["link"]["code"]).json() == LINK_USED
    first = rig.admin.post(f"/api/people/{person}/link").json()["code"]
    second = rig.admin.post(f"/api/people/{person}/link").json()["code"]
    assert redeem(browser(rig), first).json() == LINK_USED
    assert redeem(browser(rig), second).status_code == 200


def test_a_link_dies_with_its_makers_admin_rights(rig):
    for take_away, give_back in (
        (lambda maker: rig.accounts.set_role(None, maker, "member"), lambda maker: rig.accounts.set_role(None, maker, "admin")),
        (lambda maker: rig.accounts.disable(None, maker), lambda maker: rig.accounts.enable(None, maker)),
    ):
        maker, person = account(rig.app, name="Maker"), account(rig.app, role="member")
        made = maker.post(f"/api/people/{person.user_id}/link")
        assert made.status_code == 200 and link_of(rig, person.user_id) == made.json()["expires_at"]
        take_away(maker.user_id)
        assert redeem(browser(rig), made.json()["code"]).json() == LINK_USED
        assert link_of(rig, person.user_id) is None
        # Rights given back revive nothing made before they went.
        give_back(maker.user_id)
        assert redeem(browser(rig), made.json()["code"]).status_code == 410 and link_of(rig, person.user_id) is None
    # A reset link the maker uses ends what was made with that account, as a new password from the command line does.
    maker, person = account(rig.app, name="Maker"), account(rig.app, role="member")
    maker.post(f"/api/people/{person.user_id}/link")
    assert redeem(browser(rig), rig.admin.post(f"/api/people/{maker.user_id}/link").json()["code"]).status_code == 200
    assert link_of(rig, person.user_id) is None


def test_a_new_password_from_the_command_line_ends_that_persons_links(rig):
    code = add(rig, "recovered")["link"]["code"]
    rig.accounts.set_password("recovered", secrets.token_urlsafe(16))
    assert redeem(browser(rig), code).json() == LINK_USED
    # And the links that person made, so an exposed admin's account can be recovered.
    maker = account(rig.app, name="Exposed")
    made = maker.post(f"/api/people/{account(rig.app, role='member').user_id}/link").json()["code"]
    rig.accounts.set_password(username(maker), secrets.token_urlsafe(16))
    assert redeem(browser(rig), made).json() == LINK_USED


def test_an_unknown_link_is_refused_without_hashing(rig, monkeypatch):
    def hashed(password):
        raise AssertionError("A dead link was hashed")

    monkeypatch.setattr(identity, "hash_password", hashed)
    refused = redeem(browser(rig), secrets.token_urlsafe(32))
    assert refused.status_code == 410 and refused.json() == LINK_USED


def test_a_bad_password_does_not_spend_the_link(rig):
    code = add(rig, "patient")["link"]["code"]
    short = redeem(browser(rig), code, "x" * 9)
    assert short.status_code == 400 and short.json() == {"detail": "A password needs 10 to 1,024 characters"}
    assert redeem(browser(rig), code).status_code == 200


def test_a_reset_ends_every_session_and_signs_in_here(rig):
    person = with_password(rig.app, account(rig.app, role="member"))
    link = rig.admin.post(f"/api/people/{person.user_id}/link").json()
    assert link["purpose"] == "reset"
    here = browser(rig)
    assert redeem(here, link["code"]).status_code == 200
    assert person.get("/api/cases").status_code == 401 and here.get("/api/cases").status_code == 200


def test_redeem_ends_the_session_this_browser_held_for_someone_else(rig):
    holder, person = account(rig.app, name="Holder"), account(rig.app, role="member")
    code = rig.admin.post(f"/api/people/{person.user_id}/link").json()["code"]
    assert redeem(holder, code).status_code == 200
    assert rows(rig, "SELECT 1 FROM sessions WHERE user_id=?", holder.user_id) == []
    assert rows(rig, "SELECT actor_id FROM audit_events WHERE action='session.sign_out' AND actor_id=?", holder.user_id) == [(holder.user_id,)]
    assert holder.get("/api/session").json()["user"]["id"] == person.user_id


def test_a_malformed_redeem_echoes_nothing_and_stays_small(rig):
    code, password = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
    response = browser(rig).post("/api/session/redeem", json={"code": code, "password": password, "extra": 1})
    assert response.status_code == 422 and response.json() == {"detail": "Invalid set-password request."}
    assert response.headers["cache-control"] == "no-store" and code not in response.text and password not in response.text
    assert browser(rig).post("/api/session/redeem", json={"code": code, "password": "x" * 5000}).status_code == 413


def test_redeem_needs_https_or_loopback_and_sign_in_on(rig, tmp_path):
    code = add(rig, "remote")["link"]["code"]
    plain = TestClient(rig.app, base_url="http://review.example", headers={"Origin": "http://review.example"})
    refused = redeem(plain, code)
    assert refused.status_code == 403 and refused.json() == {"detail": "Set your password through the HTTPS address"}
    _, http = off_mode(tmp_path)
    off = redeem(http, code)
    assert off.status_code == 400 and off.json() == {"detail": "Sign-in is off on this workstation"}


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
    code = rig.admin.post(f"{path}/link").json()["code"]
    disabled = rig.admin.post(f"{path}/disable")
    assert disabled.status_code == 200 and disabled.json()["state"] == "disabled" and disabled.json()["link_expires_at"] is None
    assert person.get("/api/cases").status_code == 401
    assert redeem(browser(rig), code).json() == LINK_USED
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
    assert redeem(http, secrets.token_urlsafe(32)).json() == {"detail": "Sign-in is off on this workstation"}
