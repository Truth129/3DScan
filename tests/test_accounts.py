"""Accounts, passwords, sessions, cookies, the audit log and the sign-in mode."""

import asyncio
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from scan3d import __main__ as cli
from scan3d import config, identity
from scan3d.accounts import Accounts, TooManyAttempts, WrongPassword, token_hash
from scan3d.api import create_app
from scan3d.backup import backup
from scan3d.config import Settings, resolve_auth
from scan3d.identity import LOCAL, Principal
from scan3d.service import Service
from scan3d.store import Store, grant_owner, identifier, now

BASE = "http://127.0.0.1"
ORIGIN = {"Origin": BASE}


def generated_password():
    return secrets.token_urlsafe(16)


@pytest.fixture
def rig(tmp_path):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", allowed_hosts=("127.0.0.1", "review.example"), auth="password"))
    service = app.state.service
    password = generated_password()
    user_id = service.accounts.add_user("Reader", password, "A Reader")
    return SimpleNamespace(
        app=app,
        service=service,
        accounts=service.accounts,
        store=service.store,
        http=TestClient(app, base_url=BASE, headers=ORIGIN),
        password=password,
        user_id=user_id,
    )


def sign_in(rig, client=None, **fields):
    client = client or rig.http
    return client.post("/api/session", json={"username": "reader", "password": rig.password, **fields})


def rows(rig, query, *values):
    with rig.store.connection() as con:
        return [tuple(row) for row in con.execute(query, values)]


def session_time(rig, **fields):
    assignments = ",".join(f"{name}=?" for name in fields)
    with rig.store.connection() as con:
        con.execute(f"UPDATE sessions SET {assignments}", tuple(value.isoformat(timespec="microseconds") for value in fields.values()))


def ago(**amount):
    return datetime.now(timezone.utc) - timedelta(**amount)


def test_a_hash_holds_its_parameters_and_verifies_only_its_own_password():
    password = generated_password()
    stored = identity.hash_password(password)
    scheme, n, r, p, salt, digest = stored.split("$")
    assert (scheme, n, r, p) == ("scrypt", str(2**17), "8", "1") and len(bytes.fromhex(salt)) == 16 and len(bytes.fromhex(digest)) == 32
    assert identity.verify_password(password, stored) and not identity.verify_password(password + "x", stored)
    assert stored != identity.hash_password(password)
    # U+FB01 is the ligature fi, which NFKC folds to the two letters.
    assert identity.verify_password("conﬁdential", identity.hash_password("confidential"))
    # A lone surrogate must not raise an error that quotes the password.
    assert identity.verify_password("\ud800" + password, stored) is False


@pytest.mark.parametrize("stored", [None, "scrypt$1$2", "bcrypt$131072$8$1$00$00", "scrypt$131072$8$1$zz$00", f"scrypt${2**40}$8$1$00$00"])
def test_a_malformed_stored_hash_is_a_wrong_password(stored):
    assert identity.verify_password(generated_password(), stored) is False


def test_password_and_username_limits():
    assert identity.hash_password("x" * 10) and identity.hash_password("x" * 1024)
    for password in ("x" * 9, "x" * 1025):
        with pytest.raises(ValueError, match="password"):
            identity.hash_password(password)
    assert all(identity.valid_username(name) for name in ("a", "first.last@site-1_x", "a" * 64))
    assert not any(identity.valid_username(name) for name in ("", "a" * 65, "two words", "new\n", "é"))


def test_a_case_scope_is_the_whole_library_for_everyone():
    for principal in (LOCAL, Principal("abc", "name", "Name", "admin", True), Principal("def", "name", "Name", "member", False)):
        assert identity.case_scope(principal) is identity.EVERY_CASE
    with pytest.raises(RuntimeError):
        identity.case_scope(None)


def test_sign_in_sets_a_session_and_stores_only_a_hash(rig):
    response = sign_in(rig)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "mode": "password",
        "user": {"id": rig.user_id, "username": "Reader", "display_name": "A Reader", "role": "admin", "archive_access": True},
        "setup_required": False,
    }
    cookie = response.headers["set-cookie"]
    token = cookie.split(";")[0].split("=", 1)[1]
    assert cookie == f"scan3d_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
    assert rows(rig, "SELECT token_hash,user_id FROM sessions") == [(token_hash(token), rig.user_id)]
    assert rig.http.get("/api/session").json()["user"]["id"] == rig.user_id
    assert rig.http.get("/api/cases").status_code == 200


def test_every_failed_sign_in_gives_the_same_answer_and_names_an_existing_user_only(rig):
    with rig.store.connection() as con:
        con.execute("INSERT INTO users(id,username,display_name,role,created_at) VALUES (?,?,?,?,?)", (identifier(), "nopassword", "x", "admin", now()))
        con.execute(
            "INSERT INTO users(id,username,display_name,role,created_at,disabled_at,password_hash) VALUES (?,?,?,?,?,?,?)",
            (identifier(), "disabled", "x", "admin", now(), now(), identity.hash_password(rig.password)),
        )
    for name, password in (
        ("reader", generated_password()),
        ("nobody", rig.password),
        ("nopassword", rig.password),
        ("disabled", rig.password),
        ("not a name", rig.password),
    ):
        response = rig.http.post("/api/session", json={"username": name, "password": password})
        assert response.status_code == 401 and response.json() == {"detail": "Wrong username or password"} and "set-cookie" not in response.headers
    assert rows(rig, "SELECT actor_id,target FROM audit_events WHERE action='session.sign_in_failed' ORDER BY seq")[:2] == [(None, rig.user_id), (None, None)]
    assert not any("nobody" in str(row) for row in rows(rig, "SELECT * FROM audit_events"))


def test_a_failed_sign_in_burns_the_same_work_for_an_unknown_name(rig, monkeypatch):
    burned = []
    monkeypatch.setattr(identity, "burn_password", burned.append)
    with pytest.raises(WrongPassword):
        rig.accounts.sign_in("nobody", "guess")
    assert burned == ["guess"]


def test_failures_wait_longer_and_never_lock(rig, monkeypatch):
    clock = {"now": 100.0}
    rig.accounts.throttle.clock = lambda: clock["now"]
    hashed = []
    monkeypatch.setattr(identity, "verify_password", lambda password, stored: hashed.append(password))
    for wait in (0, 0, 0, 1, 2, 4, 8, 16, 30, 30):
        with pytest.raises(WrongPassword):
            rig.accounts.sign_in("READER", "guess")
        assert rig.accounts.throttle.remaining("reader") == wait
        if wait:
            count = len(hashed)
            with pytest.raises(TooManyAttempts) as refused:
                rig.accounts.sign_in("reader", "guess")
            assert refused.value.seconds == wait and len(hashed) == count
        clock["now"] += wait
    monkeypatch.undo()
    assert sign_in(rig).status_code == 200
    assert rig.accounts.throttle.entries == {}


def test_a_wait_answers_429_with_retry_after(rig):
    rig.accounts.throttle.clock = lambda: 100.0
    for _ in range(4):
        rig.accounts.throttle.failed("reader")
    response = sign_in(rig)
    assert response.status_code == 429 and response.headers["retry-after"] == "1" and response.headers["cache-control"] == "no-store"
    assert response.json() == {"detail": "Too many attempts. Try again in 1 seconds."}


def test_the_throttle_table_is_bounded_oldest_out(rig):
    for index in range(4097):
        rig.accounts.throttle.failed(f"name{index}")
    assert len(rig.accounts.throttle.entries) == 4096 and "name0" not in rig.accounts.throttle.entries and "name4096" in rig.accounts.throttle.entries


def test_two_hashes_run_at_once_and_any_number_wait_their_turn(rig, monkeypatch):
    lock, running, peaks = threading.Lock(), [], []

    def verify(password, stored):
        with lock:
            running.append(password)
            peaks.append(len(running))
        time.sleep(0.005)
        with lock:
            running.remove(password)
        return True

    monkeypatch.setattr(identity, "verify_password", verify)

    async def burst():
        return await asyncio.gather(*(rig.accounts.queued_sign_in("reader", "any") for _ in range(30)))

    assert len(asyncio.run(burst())) == 30 and max(peaks) == 2


def test_no_free_slot_refuses_only_after_a_wait(rig):
    # At once, a refused attacker could retry at once and keep every correct password out.
    rig.accounts.hashing = asyncio.Semaphore(0)
    started = time.monotonic()
    response = sign_in(rig)
    assert response.status_code == 429 and response.headers["retry-after"] == "1"
    assert time.monotonic() - started >= 1


def test_an_expired_session_is_refused_and_removed_at_the_next_sign_in(rig):
    sign_in(rig)
    session_time(rig, expires_at=ago(seconds=1))
    assert rig.http.get("/api/cases").status_code == 401
    assert rig.http.get("/api/session").json()["user"] is None
    assert len(rows(rig, "SELECT 1 FROM sessions")) == 1
    sign_in(rig, TestClient(rig.app, base_url=BASE, headers=ORIGIN))
    assert len(rows(rig, "SELECT 1 FROM sessions")) == 1


def test_a_session_slides_at_most_every_ten_minutes_and_keeps_its_cookie_name(rig):
    response = sign_in(rig)
    token = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    before = rows(rig, "SELECT last_seen_at,expires_at FROM sessions")
    ((user_id, seen),) = rows(rig, "SELECT user_id,seen_at FROM last_seen")
    still = rig.http.get("/api/cases")
    assert "set-cookie" not in still.headers and rows(rig, "SELECT last_seen_at,expires_at FROM sessions") == before
    session_time(rig, last_seen_at=ago(minutes=11), expires_at=datetime.now(timezone.utc) + timedelta(days=1))
    slid = rig.http.get("/api/cases")
    assert slid.headers["set-cookie"] == f"scan3d_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000"
    assert datetime.fromisoformat(rows(rig, "SELECT expires_at FROM sessions")[0][0]) > datetime.now(timezone.utc) + timedelta(days=29)
    # A slide also records when the person was last seen.
    assert user_id == rig.user_id and rows(rig, "SELECT seen_at FROM last_seen")[0][0] > seen


def test_a_burst_of_reads_slides_a_session_once(rig, monkeypatch):
    token = sign_in(rig).headers["set-cookie"].split(";")[0].split("=", 1)[1]
    session_time(rig, last_seen_at=ago(minutes=11))
    slide = rig.accounts._slide

    def behind_another_read(*arguments):
        # Both reads saw the old time, and the other one writes first.
        monkeypatch.setattr(rig.accounts, "_slide", slide)
        assert rig.accounts.principal(token).slid
        return slide(*arguments)

    monkeypatch.setattr(rig.accounts, "_slide", behind_another_read)
    assert not rig.accounts.principal(token).slid


def test_a_held_write_lock_neither_delays_nor_fails_a_read(rig):
    token = sign_in(rig).headers["set-cookie"].split(";")[0].split("=", 1)[1]
    session_time(rig, last_seen_at=ago(minutes=11))
    with rig.store.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        principal = rig.accounts.principal(token)
        assert time.monotonic() - started < 5
    assert principal.id == rig.user_id and not principal.slid
    assert rig.accounts.principal(token).slid


def test_signing_out_on_a_request_that_slid_still_clears_the_cookie(rig):
    sign_in(rig)
    session_time(rig, last_seen_at=ago(minutes=11))
    response = rig.http.delete("/api/session")
    assert response.headers.get_list("set-cookie") == ["scan3d_session=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"]


def test_a_shared_device_is_capped_never_slides_and_is_never_stored(rig, tmp_path):
    response = sign_in(rig, shared_device=True)
    assert "Max-Age" not in response.headers["set-cookie"]
    expires = datetime.fromisoformat(rows(rig, "SELECT expires_at FROM sessions")[0][0])
    assert timedelta(hours=11) < expires - datetime.now(timezone.utc) <= timedelta(hours=12)
    case = rig.http.post("/api/cases/demo").json()
    prefix = f"/api/cases/{case['id']}/revisions/{case['active_revision_id']}"
    assert rig.http.get(f"{prefix}/slicer.zip").headers["cache-control"] == "no-store"
    assert rig.http.get(f"{prefix}/ct-volume").headers["cache-control"] == "no-store"
    session_time(rig, last_seen_at=ago(hours=1))
    assert "set-cookie" not in rig.http.get("/api/cases").headers
    assert datetime.fromisoformat(rows(rig, "SELECT expires_at FROM sessions")[0][0]) == expires


def test_https_uses_the_host_prefixed_secure_cookie_and_the_gate_reads_it(rig):
    origin = "https://review.example"
    http = TestClient(rig.app, base_url=origin, headers={"Origin": origin})
    response = sign_in(rig, http)
    assert response.status_code == 200
    token = response.headers["set-cookie"].split(";")[0].split("=", 1)[1]
    assert response.headers["set-cookie"] == f"__Host-scan3d_session={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=2592000; Secure"
    assert http.get("/api/cases").status_code == 200
    session_time(rig, last_seen_at=ago(minutes=11))
    assert http.get("/api/cases").headers["set-cookie"].startswith("__Host-scan3d_session=")
    assert http.delete("/api/session").headers["set-cookie"] == "__Host-scan3d_session=; HttpOnly; SameSite=Strict; Path=/; Secure; Max-Age=0"


def test_a_password_is_refused_over_plain_http_to_another_address(rig):
    http = TestClient(rig.app, base_url="http://review.example", headers={"Origin": "http://review.example"})
    response = sign_in(rig, http)
    assert response.status_code == 403 and response.json() == {"detail": "Sign in through the HTTPS address"}
    assert rows(rig, "SELECT 1 FROM sessions") == []


def test_a_mutation_with_a_cookie_and_no_origin_is_refused(rig):
    sign_in(rig)
    bare = TestClient(rig.app, base_url=BASE)
    bare.cookies = rig.http.cookies
    for response in (bare.post("/api/cases/demo"), bare.delete("/api/session")):
        assert (
            response.status_code == 403
            and response.json() == {"detail": "Cross-origin mutations are not allowed"}
            and response.headers["cache-control"] == "no-store"
        )
    assert bare.get("/api/cases").status_code == 200 and rows(rig, "SELECT 1 FROM sessions") == [(1,)]


def test_a_signed_in_health_check_has_the_gpu_and_an_anonymous_one_has_not(rig):
    anonymous = rig.http.get("/api/health").json()
    assert "gpu" not in anonymous and {"status", "worker", "lanes", "modalities", "version"} <= set(anonymous)
    assert all(rig.http.get(path).status_code == 404 for path in ("/docs", "/docs/oauth2-redirect", "/redoc", "/openapi.json"))
    sign_in(rig)
    assert "gpu" in rig.http.get("/api/health").json()


def test_setup_is_required_until_an_account_exists(tmp_path):
    http = TestClient(create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", auth="password")), base_url=BASE)
    assert http.get("/api/session").json() == {"mode": "password", "user": None, "setup_required": True}


def test_signing_out_others_then_this_session_and_audits_ids_only(rig):
    sign_in(rig)
    other = TestClient(rig.app, base_url=BASE, headers=ORIGIN)
    sign_in(rig, other)
    assert rig.http.post("/api/session/sign-out-others").json() == {"signed_out": 1}
    assert rig.http.get("/api/cases").status_code == 200 and other.get("/api/cases").status_code == 401
    response = rig.http.delete("/api/session")
    assert response.status_code == 200 and response.headers["clear-site-data"] == '"cache"'
    assert response.json() == {"mode": "password", "user": None, "setup_required": False}
    assert rows(rig, "SELECT 1 FROM sessions") == [] and rig.http.get("/api/cases").status_code == 401
    assert rows(rig, "SELECT actor_id,action,detail FROM audit_events WHERE action LIKE 'session.%' ORDER BY seq") == [
        (rig.user_id, "session.sign_in", "{}"),
        (rig.user_id, "session.sign_in", "{}"),
        (rig.user_id, "session.sign_out_others", '{"count": 1}'),
        (rig.user_id, "session.sign_out", "{}"),
    ]


def test_a_new_password_or_a_disabled_account_ends_every_session(rig):
    sign_in(rig)
    password = generated_password()
    rig.accounts.set_password("READER", password)
    assert rig.http.get("/api/cases").status_code == 401
    assert sign_in(rig).status_code == 401
    rig.password = password
    assert sign_in(rig).status_code == 200
    with rig.store.connection() as con:
        con.execute("UPDATE users SET disabled_at=?", (now(),))
    assert rig.http.get("/api/cases").status_code == 401


@pytest.mark.parametrize("path", ["/api/session", "/api/session%0A"])
def test_the_session_route_keeps_its_limits_however_the_path_is_spelled(rig, path):
    secret = generated_password()
    malformed = rig.http.post(path, json={"username": "reader", "password": secret, "extra": secret})
    assert malformed.status_code == 422 and malformed.json() == {"detail": "Invalid sign-in request."} and secret not in malformed.text
    assert malformed.headers["cache-control"] == "no-store"
    assert rig.http.post(path, json={"username": "reader", "password": "x" * 5000}).status_code == 413
    crafted = TestClient(rig.app, base_url=BASE, headers={"Host": "127.0.0.1:/elsewhere"})
    assert crafted.post(path, json={"username": "reader", "password": "x" * 5000}).status_code == 413

    def chunks():
        yield b"{}"

    # A body must declare its length.
    assert rig.http.post(path, content=chunks()).status_code == 411


def test_a_case_and_its_owner_are_one_write(rig, monkeypatch):
    identity.activate(identity.Principal(rig.user_id, "reader", "Reader", "admin", True))
    try:
        case = rig.service.new_case("Synthetic")
    finally:
        identity.activate(None)
    assert rows(rig, "SELECT user_id,role FROM case_access WHERE case_id=?", case["id"]) == [(rig.user_id, "owner")]

    def fail(con, case_id):
        raise sqlite3.OperationalError("no grant")

    monkeypatch.setattr("scan3d.store.grant_owner", fail)
    with pytest.raises(sqlite3.OperationalError):
        rig.service.new_case("Synthetic")
    assert len(rows(rig, "SELECT 1 FROM cases")) == 1


def test_work_with_no_person_goes_to_the_oldest_active_admin(rig):
    with rig.store.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        grant_owner(con, "0" * 32)
    assert rows(rig, "SELECT user_id FROM case_access") == [(rig.user_id,)]
    case = {"id": identifier()}
    identity.activate(LOCAL)
    try:
        rig.store.create_case(case)
    finally:
        identity.activate(None)
    assert rows(rig, "SELECT user_id FROM case_access WHERE case_id=?", case["id"]) == [(rig.user_id,)]


def test_a_case_never_gains_a_second_owner(rig):
    younger, case_id = identifier(), identifier()

    def grant(principal):
        identity.activate(principal)
        try:
            with rig.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                grant_owner(con, case_id)
        finally:
            identity.activate(None)

    grant(Principal(younger, "younger", "Younger", "admin", True))
    # With no person, a retry falls to the oldest admin, who must not join the owner.
    for retried_by in (Principal(younger, "younger", "Younger", "admin", True), LOCAL, None, Principal(identifier(), "other", "Other", "admin", True)):
        grant(retried_by)
    assert rows(rig, "SELECT user_id FROM case_access") == [(younger,)]


def test_with_no_admin_a_new_case_has_no_owner_until_one_exists(tmp_path):
    store = Store(tmp_path)
    store.create_case({"id": identifier()})
    assert Accounts(store).adopt_orphans() == 0
    with store.connection() as con:
        assert con.execute("SELECT (SELECT COUNT(*) FROM case_access) + (SELECT COUNT(*) FROM audit_events)").fetchone()[0] == 0


def test_adopting_orphans_touches_only_unowned_cases_and_audits_the_count(rig):
    owned = rig.store.create_case({"id": identifier()})
    other = identifier()
    with rig.store.connection() as con:
        con.execute("UPDATE case_access SET user_id=? WHERE case_id=?", (other, owned["id"]))
        for _ in range(2):
            con.execute("INSERT INTO cases VALUES (?,?)", (identifier(), "{}"))
    before = rows(rig, "SELECT data FROM cases")
    assert rig.accounts.adopt_orphans() == 2
    assert rig.accounts.adopt_orphans() == 0
    assert rows(rig, "SELECT user_id FROM case_access WHERE case_id=?", owned["id"]) == [(other,)]
    assert len(rows(rig, "SELECT 1 FROM case_access WHERE user_id=?", rig.user_id)) == 2
    assert rows(rig, "SELECT action,detail FROM audit_events WHERE action='case.adopt'") == [("case.adopt", '{"count": 2}')]
    assert rows(rig, "SELECT data FROM cases") == before


def test_password_mode_adopts_orphans_before_workers_start(tmp_path):
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", auth="password")
    service = Service(settings)
    service.accounts.add_user("first", generated_password())
    service.store.save("cases", {"id": identifier()})
    service.store.close()
    with TestClient(create_app(settings), base_url=BASE) as http:
        with http.app.state.service.store.connection() as con:
            assert con.execute("SELECT COUNT(*) FROM case_access").fetchone()[0] == 1


def run_cli(monkeypatch, capsys, tmp_path, *arguments, passwords=()):
    monkeypatch.setenv("SCAN3D_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SCAN3D_CONFIG", str(tmp_path / "scan3d.env"))
    (tmp_path / "scan3d.env").touch()
    monkeypatch.setenv("TOTALSEG_HOME_DIR", str(tmp_path))
    monkeypatch.setattr("os.umask", lambda mask: None)
    monkeypatch.setattr("sys.argv", ["scan3d", "users", *arguments])
    answers = iter(passwords)
    monkeypatch.setattr("getpass.getpass", lambda prompt: next(answers))
    cli.main()
    return capsys.readouterr().out


def test_the_cli_adds_changes_and_lists_accounts(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path / "data")
    store.save("cases", {"id": identifier()})
    password = generated_password()
    added = run_cli(monkeypatch, capsys, tmp_path, "add", "first", "--admin", "--display-name", "First Person", passwords=[password, password])
    assert "first" in added and str(tmp_path / "data") in added and added.rstrip().endswith(": 1") and password not in added
    listing = run_cli(monkeypatch, capsys, tmp_path, "list")
    user_id, username, role, archive, state = listing.strip().split("\t")
    assert (username, role, archive, state) == ("first", "admin", "archive", "active") and "scrypt" not in listing
    changed = generated_password()
    assert "first" in run_cli(monkeypatch, capsys, tmp_path, "passwd", "first", passwords=[changed, changed])
    accounts = Accounts(store)
    assert accounts.sign_in("first", changed)[1].id == user_id
    with store.connection() as con:
        assert [row[0] for row in con.execute("SELECT action FROM audit_events ORDER BY seq")] == ["user.add", "case.adopt", "user.password", "session.sign_in"]
        assert con.execute("SELECT archive_access FROM users").fetchone()[0] == 1


def test_the_cli_changes_a_role_and_disables_and_enables_an_account(tmp_path, monkeypatch, capsys):
    password = generated_password()

    def listed():
        return {fields[1]: fields[2:] for fields in (line.split("\t") for line in run_cli(monkeypatch, capsys, tmp_path, "list").splitlines())}

    run_cli(monkeypatch, capsys, tmp_path, "add", "first", "--admin", passwords=[password, password])
    with pytest.raises(SystemExit, match="Keep at least one administrator who can sign in"):
        run_cli(monkeypatch, capsys, tmp_path, "role", "first", "member")
    run_cli(monkeypatch, capsys, tmp_path, "add", "second", "--admin", passwords=[password, password])
    assert run_cli(monkeypatch, capsys, tmp_path, "role", "first", "member") == "first is now a member\n"
    assert run_cli(monkeypatch, capsys, tmp_path, "disable", "first") == "Disabled first and signed it out everywhere\n"
    assert listed() == {"first": ["member", "archive", "disabled"], "second": ["admin", "archive", "active"]}
    assert run_cli(monkeypatch, capsys, tmp_path, "enable", "first") == "Enabled first\n"
    assert listed()["first"] == ["member", "archive", "active"]


def test_the_cli_refuses_bad_input_before_it_makes_a_data_folder(tmp_path, monkeypatch, capsys):
    for arguments, passwords, message in (
        (["add", "first"], (), None),
        (["add", "two words", "--admin"], (), "username needs"),
        (["add", "first", "--admin"], ["a" * 12, "b" * 12], "do not match"),
        (["add", "first", "--admin"], ["short", "short"], "password needs"),
        (["list"], (), str(tmp_path / "data")),
        (["passwd", "first"], (), str(tmp_path / "data")),
    ):
        with pytest.raises(SystemExit) as refused:
            run_cli(monkeypatch, capsys, tmp_path, *arguments, passwords=passwords)
        assert message is None or message in str(refused.value)
    assert not (tmp_path / "data").exists()
    Store(tmp_path / "data")
    with pytest.raises(SystemExit, match="User not found"):
        run_cli(monkeypatch, capsys, tmp_path, "passwd", "nobody")


@pytest.mark.parametrize("interruption", [EOFError, KeyboardInterrupt])
def test_an_interrupted_prompt_changes_nothing(tmp_path, monkeypatch, capsys, interruption):
    def answers():
        yield "a" * 12
        raise interruption

    with pytest.raises(SystemExit, match="Nothing was changed"):
        run_cli(monkeypatch, capsys, tmp_path, "add", "first", "--admin", passwords=answers())
    assert not (tmp_path / "data").exists()


def test_the_cli_refuses_a_data_folder_it_cannot_use(tmp_path, monkeypatch, capsys):
    (tmp_path / "data").touch()
    with pytest.raises(SystemExit, match="Cannot use"):
        run_cli(monkeypatch, capsys, tmp_path, "add", "first", "--admin", passwords=["a" * 12, "a" * 12])


def test_a_username_is_unique_whatever_its_case(rig):
    with pytest.raises(ValueError, match="already in use"):
        rig.accounts.add_user("READER", generated_password())


def test_audited_routes_record_who_and_which_case_once_per_session(rig):
    sign_in(rig)
    case = rig.http.post("/api/cases/demo").json()
    for _ in range(3):
        rig.http.get(f"/api/cases/{case['id']}")
    assert rig.http.get(f"/api/cases/{case['id']}/revisions/{case['active_revision_id']}/slicer.zip").status_code == 200
    assert rows(rig, "SELECT actor_id,action,case_id FROM audit_events WHERE action IN ('case.import','case.open','export.slicer') ORDER BY seq") == [
        (rig.user_id, "case.import", None),
        (rig.user_id, "case.open", case["id"]),
        (rig.user_id, "export.slicer", case["id"]),
    ]


def test_the_case_open_memory_is_bounded(rig, monkeypatch):
    monkeypatch.setattr("scan3d.access._OPENED_LIMIT", 2)
    gate = next(entry for entry in rig.app.user_middleware if entry.cls.__name__ == "SessionGate")
    instance = gate.cls(None, rig.service)
    assert [instance._first_open(key) for key in ("a", "b", "a", "c", "a")] == [True, True, False, True, True]
    assert len(instance.opened) == 2


@pytest.mark.parametrize("statement", ["UPDATE audit_events SET action='x'", "DELETE FROM audit_events"])
def test_the_audit_log_is_append_only(rig, statement):
    rig.accounts.audit("case.open", rig.user_id)
    with pytest.raises(sqlite3.IntegrityError, match="append-only"), rig.store.connection() as con:
        con.execute(statement)


def test_a_failed_audit_write_fails_the_request_before_its_handler_runs(rig, monkeypatch):
    sign_in(rig)
    ran = []
    monkeypatch.setattr("scan3d.demo.create_demo", lambda service: ran.append(True))
    with rig.store.connection() as con:
        con.execute("CREATE TRIGGER audit_broken BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'disk full'); END")
    http = TestClient(rig.app, base_url=BASE, headers=ORIGIN, raise_server_exceptions=False)
    http.cookies = rig.http.cookies
    assert http.post("/api/cases/demo").status_code == 500 and ran == []


def test_a_backup_drops_sessions_and_links_but_keeps_accounts_and_audit(rig, tmp_path):
    sign_in(rig)
    hashed = rows(rig, "SELECT token_hash FROM sessions")[0][0]
    link = token_hash(secrets.token_urlsafe(32))
    with rig.store.connection() as con:
        con.execute(
            "INSERT INTO one_time_codes(token_hash,purpose,user_id,created_by,expires_at) VALUES (?,?,?,?,?)",
            (link, "reset", rig.user_id, rig.user_id, (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="microseconds")),
        )
    manifest = backup(rig.service.settings.data_dir, tmp_path / "backups", log=lambda message: None)
    snapshot = tmp_path / "backups" / manifest["snapshot"] / "scan3d.sqlite3"
    copy = sqlite3.connect(snapshot)
    assert copy.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
    assert copy.execute("SELECT COUNT(*) FROM one_time_codes").fetchone()[0] == 0
    assert copy.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    assert copy.execute("SELECT COUNT(*) FROM audit_events").fetchone()[0] >= 2
    copy.close()
    assert hashed.encode() not in snapshot.read_bytes() and link.encode() not in snapshot.read_bytes()
    assert rows(rig, "SELECT token_hash FROM sessions") == [(hashed,)]
    assert rows(rig, "SELECT token_hash FROM one_time_codes") == [(link,)]


def test_a_backup_of_a_database_with_no_sessions_table_still_works(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    sqlite3.connect(data / "scan3d.sqlite3").execute("CREATE TABLE cases(id TEXT PRIMARY KEY, data TEXT NOT NULL)").connection.close()
    assert backup(data, tmp_path / "backups", log=lambda message: None)["database"]["integrity_check"] == "ok"


@pytest.mark.parametrize(
    "value, hosts, expected",
    [
        (None, ("127.0.0.1", "::1", "[::1]", "localhost"), "off"),
        (None, ("127.0.0.1", "review.example"), "password"),
        (None, ("*",), "password"),
        (None, ("",), "password"),
        (" OFF ", ("127.0.0.1",), "off"),
        ("Password", ("127.0.0.1",), "password"),
    ],
)
def test_the_sign_in_mode_follows_the_addresses(value, hosts, expected):
    assert resolve_auth(value, hosts) == expected


@pytest.mark.parametrize("value, hosts", [("off", ("review.example",)), ("off", ("0.0.0.0",)), ("basic", ("127.0.0.1",))])
def test_an_open_published_host_or_unknown_mode_refuses_to_start(value, hosts):
    with pytest.raises(ValueError, match="SCAN3D_AUTH"):
        resolve_auth(value, hosts)


def test_settings_read_the_host_as_well_as_the_allowed_hosts(monkeypatch):
    monkeypatch.delenv("SCAN3D_AUTH", raising=False)
    monkeypatch.delenv("SCAN3D_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("SCAN3D_HOST", "127.0.0.1")
    assert Settings.from_env().auth == "off"
    monkeypatch.setenv("SCAN3D_HOST", "0.0.0.0")
    assert Settings.from_env().auth == "password"
    monkeypatch.setenv("SCAN3D_AUTH", "off")
    with pytest.raises(ValueError, match="SCAN3D_AUTH"):
        Settings.from_env()


def test_serve_counts_its_host_flag_before_it_creates_the_app(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setenv("SCAN3D_CONFIG", str(tmp_path / "scan3d.env"))
    (tmp_path / "scan3d.env").touch()
    monkeypatch.setenv("SCAN3D_HOST", "127.0.0.1")
    monkeypatch.setenv("TOTALSEG_HOME_DIR", str(tmp_path))
    monkeypatch.delenv("SCAN3D_AUTH", raising=False)
    monkeypatch.setattr("sys.argv", ["scan3d", "serve", "--host", "0.0.0.0"])
    monkeypatch.setattr("os.umask", lambda mask: None)
    monkeypatch.setattr("scan3d.api.create_app", lambda: seen.update(auth=config.Settings.from_env().auth))
    monkeypatch.setattr("uvicorn.run", lambda app, **options: None)
    cli.main()
    assert seen == {"auth": "password"}


def test_an_old_database_gains_the_new_tables_without_touching_a_case(tmp_path):
    store = Store(tmp_path)
    record = store.save("cases", {"id": identifier(), "label": "Synthetic"})
    with store.connection() as con:
        for table in ("users", "sessions", "case_access", "audit_events", "one_time_codes", "last_seen"):
            con.execute(f"DROP TABLE {table}")
        con.execute("PRAGMA user_version=4")
    Store(tmp_path)
    with store.connection() as con:
        assert con.execute("PRAGMA user_version").fetchone()[0] == 8
        assert con.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        assert con.execute("SELECT COUNT(*) FROM one_time_codes").fetchone()[0] == con.execute("SELECT COUNT(*) FROM last_seen").fetchone()[0] == 0
        assert json.loads(con.execute("SELECT data FROM cases").fetchone()[0]) == record
