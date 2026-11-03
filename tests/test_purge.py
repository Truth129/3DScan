"""Delete forever on synthetic cases: what a purge waits for and what it must find."""

import json
import os
import secrets
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from scan3d import identity
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.disk_usage import REBUILDABLE, REBUILT_ON_A_MISS, tree_bytes
from scan3d.janitor import maintenance_lock, review_entries
from scan3d.store import identifier
from test_access import BASE, account
from test_library import fill, finished_upload, off_mode

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


@pytest.fixture
def started(tmp_path):
    """An off-mode app whose lanes hold `worker.lock`. No janitor thread runs: each test drives the passes."""
    service, http = off_mode(tmp_path)
    service.start()
    yield service, http
    service.stop()


def demo(http):
    """A demo case with a preview CT read, so the review cache holds an entry naming it."""
    case = http.post("/api/cases/demo").json()
    assert http.get(f"/api/cases/{case['id']}/revisions/{case['active_revision_id']}/ct-volume?quality=preview").status_code == 200
    return case["id"]


def deleted(http, case_id):
    assert http.post(f"/api/cases/{case_id}/trash").status_code == 200
    assert http.post(f"/api/trash/{case_id}/purge", json={"confirm": True}).status_code == 200


def entries(service, case_id):
    return sorted(entry for entry, record in review_entries(service.settings.data_dir) if record and record.get("case_id") == case_id)


def test_janitor_removes_the_case_files_uploads_and_marked_review_cache(started):
    service, http = started
    data = service.settings.data_dir
    first, second = demo(http), demo(http)
    upload = finished_upload(service, first)
    kept = entries(service, second)
    assert entries(service, first) and kept
    deleted(http, first)
    assert service.janitor.run_pass() is False
    assert not (data / "cases" / first).exists() and not (data / "import-uploads" / upload).exists()
    assert entries(service, first) == [] and entries(service, second) == kept and (data / "cases" / second).is_dir()
    assert trash_state(service, first) == "purged"


def test_janitor_leaves_no_copy_of_a_purged_record_in_the_database_files(started):
    service, http = started
    marker = f"synthetic-{secrets.token_hex(16)}"
    case_id, other = service.new_case(marker)["id"], service.new_case("Synthetic")["id"]
    deleted(http, case_id)
    # A request on another case, so the pass stops before the shared caches.
    service.activity.enter(other)
    try:
        assert service.janitor.run_pass() is True
    finally:
        service.activity.leave(other)
    assert trash_state(service, case_id) == "purging"
    for name in ("scan3d.sqlite3", "scan3d.sqlite3-wal"):
        path = service.settings.data_dir / name
        assert marker.encode() not in (path.read_bytes() if path.exists() else b""), name


def test_one_stuck_case_does_not_hold_up_another(started):
    service, http = started
    data = service.settings.data_dir
    stuck, freed = demo(http), demo(http)
    for case_id in (stuck, freed):
        deleted(http, case_id)
    locked = data / "cases" / stuck / "synthetic"
    locked.chmod(0o500)
    try:
        assert service.janitor.run_pass() is False
    finally:
        locked.chmod(0o700)
    assert trash_state(service, freed) == "purged" and not (data / "cases" / freed).exists()
    assert trash_state(service, stuck) == "purging"


def test_janitor_waits_for_idle_before_the_shared_caches(started, monkeypatch):
    service, http = started
    data = service.settings.data_dir
    # The running GPU lane calls this once a purge sets `release_engine`.
    released = threading.Event()
    monkeypatch.setattr(service, "_release_interactive", released.set)
    case_id, other = service.new_case("Synthetic")["id"], service.new_case("Synthetic")["id"]
    fill(data / "cases" / case_id / "scan.bin", 1024)
    cache = data / "analysis-cache" / "aa" / "result.json"
    fill(cache, 1024)
    deleted(http, case_id)
    service.activity.enter(other)
    try:
        assert service.janitor.run_pass() is True
        assert not (data / "cases" / case_id).exists() and cache.exists() and trash_state(service, case_id) == "purging"
        assert not service.release_engine.is_set() and not released.is_set()
    finally:
        service.activity.leave(other)
    assert service.janitor.run_pass() is False
    assert not cache.exists() and trash_state(service, case_id) == "purged" and released.wait(2)


def test_janitor_sweeps_unmarked_and_orphaned_review_entries_only(started):
    service, http = started
    review = service.settings.data_dir / "review-cache"
    shard = review / "volumes" / "aa"
    manifests = {"live": {"case_id": service.new_case("Synthetic")["id"]}, "unmarked": {}, "unknown": {"case_id": identifier()}}
    for name, record in manifests.items():
        (shard / f"aa-{name}").mkdir(parents=True)
        (shard / f"aa-{name}" / "manifest.json").write_text(json.dumps(record))
    fill(shard / "aa-unreadable" / "manifest.json", 16)
    fill(shard / ".aa-left.x" / "chunk", 16)
    fill(review / "generation-full.lock", 0)
    deleted(http, service.new_case("Synthetic")["id"])
    assert service.janitor.run_pass() is False
    left = sorted(path.relative_to(review).as_posix() for path in review.rglob("*") if path.is_file())
    assert left == ["generation-full.lock", "generation-preview.lock", "generation-volume-mask-union.lock", "volumes/aa/aa-live/manifest.json"]


def test_surface_sweep_keeps_live_assets_and_every_sibling_of_them(started):
    service, http = started
    data = service.settings.data_dir
    live, draft, dead = "a" * 64, "c" * 64, "b" * 64
    case_id = service.new_case("Synthetic")["id"]
    service.store.save("revisions", dict(id=identifier(), case_id=case_id, structures=[], surface_assets=[{"id": "x", "key": live, "triangles": 1}]))
    with service.store.connection() as con:
        con.execute("INSERT INTO review_sessions VALUES (?,?,?)", (identifier(), case_id, json.dumps({"candidate": {"surface_assets": [{"key": draft}]}})))

    def files(key):
        names = (f"{key}.glb", f"{key}.glb.json", f"{key}.glb.gz", f"{key}.glb.gz.json", f"{key}.glb.gz.lock", f".{key}.glb.abc.writing")
        return [data / "surface-cache" / "assets" / key[:2] / name for name in names]

    for path in files(live) + files(draft) + files(dead):
        fill(path, 16)
    lock = data / "surface-cache" / "locks" / f"{dead}.lock"
    fill(lock, 0)
    deleted(http, service.new_case("Synthetic")["id"])
    assert service.janitor.run_pass() is False
    assert all(path.exists() for path in files(live) + files(draft)) and lock.exists()
    assert not any(path.exists() for path in files(dead))


def test_janitor_leaves_files_alone_while_a_backup_holds_the_lock(started):
    service, http = started
    case_id = service.new_case("Synthetic")["id"]
    folder = service.settings.data_dir / "cases" / case_id
    fill(folder / "scan.bin", 1024)
    deleted(http, case_id)
    with maintenance_lock(service.settings.data_dir, exclusive=False) as held:
        assert held
        assert service.janitor.run_pass() is True
        assert folder.exists()
    assert service.janitor.run_pass() is False and not folder.exists()


def test_a_root_run_gives_maintenance_lock_to_the_data_folder_owner(tmp_path, monkeypatch):
    given = []
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "fchown", lambda fd, uid, gid: given.append((uid, gid)))
    with maintenance_lock(tmp_path, exclusive=False) as held:
        assert held
    owner = tmp_path.stat()
    assert given == [(owner.st_uid, owner.st_gid)]


def test_a_purge_resumes_after_a_restart_and_a_returning_folder_goes(tmp_path):
    service, http = off_mode(tmp_path)
    case_id = service.new_case("Synthetic")["id"]
    folder = service.settings.data_dir / "cases" / case_id
    fill(folder / "scan.bin", 1024)
    deleted(http, case_id)

    def soon(check):
        deadline = time.monotonic() + 10
        while not check():
            assert time.monotonic() < deadline
            time.sleep(0.05)

    with TestClient(create_app(service.settings)) as restarted:
        soon(lambda: trash_state(restarted.app.state.service, case_id) == "purged" and not folder.exists())
    # A command-line write or a restored backup can make the folder again.
    fill(folder / "x", 16)
    with TestClient(create_app(service.settings)):
        soon(lambda: not folder.exists())


def test_start_removes_orthanc_staging_leftovers(tmp_path):
    service, _ = off_mode(tmp_path)
    leftover = service.settings.data_dir / "orthanc-staging" / "transfer-abc" / "f"
    fill(leftover, 16)
    service.start()
    try:
        assert not leftover.parent.exists()
    finally:
        service.stop()


def test_an_idle_gpu_lane_releases_the_warm_engine_after_a_purge(tmp_path, monkeypatch):
    service, _ = off_mode(tmp_path)
    released = threading.Event()
    monkeypatch.setattr(service, "_release_interactive", released.set)
    service.release_engine.set()
    service.start()
    try:
        assert released.wait(2) and not service.release_engine.is_set()
    finally:
        service.stop()


def test_clear_caches_empties_rebuildable_caches_and_keeps_surfaces(started):
    service, http = started
    data = service.settings.data_dir
    for name in REBUILDABLE:
        fill(data / name / "aa" / "cached.bin", 8192)
    fill(data / "review-cache" / "generation-full.lock", 0)
    kept = [data / "surface-cache" / "assets" / "aa" / "x.glb", data / "cases" / service.new_case("Synthetic")["id"] / "scan.bin"]
    for path in kept:
        fill(path, 8192)
    expected = sum(tree_bytes(data / name) for name in REBUILDABLE)
    response = http.post("/api/storage/clear-caches")
    assert response.status_code == 200, response.text
    freed = response.json()["freed"]
    assert freed == expected >= 4 * 8192
    assert all(child.name.startswith("generation-") and child.name.endswith(".lock") for child in (data / "review-cache").iterdir())
    assert not any((data / name).exists() for name in REBUILT_ON_A_MISS)
    assert all(path.exists() for path in kept)
    assert response.json()["storage"]["totals"]["rebuildable"] < freed
    with service.store.connection() as con:
        row = con.execute("SELECT detail FROM audit_events WHERE action='cache.clear' ORDER BY seq DESC").fetchone()
    assert json.loads(row["detail"]) == {"bytes": freed}


def test_clear_caches_refuses_while_anything_is_active(tmp_path):
    service, http = off_mode(tmp_path)

    def refusal():
        response = http.post("/api/storage/clear-caches")
        assert response.status_code == 409, response.text
        return response.json()["detail"]

    assert refusal() == "Another 3DScan command was running when 3DScan started. Restart 3DScan, then try again."
    service.start()
    try:
        # Written as running in one step, so no lane claims it first.
        job = service.store.save("jobs", dict(id=identifier(), case_id=service.new_case("Synthetic")["id"], kind="include_all", status="running"))
        assert refusal() == "Jobs are running. Try again when they finish."
        service.store.update_job(job["id"], status="completed")
        service.activity.enter()
        try:
            assert refusal() == "Images are still loading. Try again in a minute."
        finally:
            service.activity.leave()
        with maintenance_lock(service.settings.data_dir, exclusive=False):
            assert refusal() == "A backup, a deletion or another 3DScan command is running. Try again when it finishes."
        assert http.post("/api/storage/clear-caches").status_code == 200
    finally:
        service.stop()


def test_a_cache_file_the_service_cannot_remove_holds_up_nothing_else(started):
    service, http = started
    data = service.settings.data_dir
    locked = data / "analysis-cache" / "aa"
    fill(locked / "result.json", 16)
    unnamed = data / "surface-cache" / "assets" / "bb" / f"{'b' * 64}.glb"
    fill(unnamed, 16)
    case_id = service.new_case("Synthetic")["id"]
    deleted(http, case_id)
    locked.chmod(0o500)
    try:
        # No exception, so no traceback every 5 seconds: the case waits a minute, and later steps still run.
        assert service.janitor.run_pass() is False
        assert trash_state(service, case_id) == "purging" and not unnamed.exists()
        response = http.post("/api/storage/clear-caches")
        assert response.status_code == 409, response.text
        # Python names the folder it could not empty.
        assert response.json()["detail"] == "3DScan cannot remove aa in its caches. Check its owner and permissions, then try again."
    finally:
        locked.chmod(0o700)
    assert service.janitor.run_pass() is False and trash_state(service, case_id) == "purged"
