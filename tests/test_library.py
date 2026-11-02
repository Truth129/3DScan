"""The Library's backend on synthetic records: last-opened order, rename, Trash and sizes."""

import json
import os
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.disk_usage import tree_bytes
from scan3d.store import identifier, now
from test_access import BASE, account, filled, ruled_routes


@pytest.fixture(scope="module")
def rig(tmp_path_factory):
    root = tmp_path_factory.mktemp("library")
    app = create_app(Settings(root / "data", (root,), web_dir=root / "dist", auth="password"))
    # First, so it is the oldest admin and owns what `service.new_case` makes.
    owner = account(app)
    return SimpleNamespace(app=app, service=app.state.service, owner=owner, other=account(app), member=account(app, role="member", archive_access=0))


def off_mode(tmp_path):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    return app.state.service, TestClient(app, base_url=BASE)


def actions(service, case_id):
    with service.store.connection() as con:
        return [row["action"] for row in con.execute("SELECT action FROM audit_events WHERE case_id=? ORDER BY seq", (case_id,))]


def finished_upload(service, case_id):
    """A staged upload that has become this case, as `Uploads.finish` leaves it."""
    folder = service.settings.data_dir / "import-uploads" / identifier()
    folder.mkdir(parents=True)
    (folder / "state.json").write_text(json.dumps({"case_id": case_id, "owner_id": None}))
    return folder.name


def imported(service):
    """A bare case past its import, which is what Rename accepts."""
    return service.store.save("cases", dict(service.new_case("Synthetic"), status="imported"))["id"]


def fill(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)


def absent(response, detail):
    assert response.status_code == 404, response.text
    assert response.json() == {"detail": detail}


def test_cases_list_last_opened_first_for_each_person(rig):
    older, newer = rig.service.new_case("Synthetic")["id"], rig.service.new_case("Synthetic")["id"]
    with rig.service.store.connection() as con:
        for case_id in (older, newer):
            con.execute("INSERT INTO case_access(case_id,user_id,role,granted_at) VALUES (?,?,'viewer',?)", (case_id, rig.other.user_id, now()))

    def listed(client):
        return [case for case in client.get("/api/cases").json() if case["id"] in (older, newer)]

    assert [case["id"] for case in listed(rig.owner)] == [case["id"] for case in listed(rig.other)] == [newer, older]
    assert listed(rig.owner)[0]["opened_at"] is None
    assert rig.owner.post(f"/api/cases/{older}/open").json() == {"id": older}
    assert [case["id"] for case in listed(rig.owner)] == [older, newer]
    assert listed(rig.owner)[0]["opened_at"]
    assert [case["id"] for case in listed(rig.other)] == [newer, older]


def test_opening_a_missing_case_is_not_found_with_sign_in_off(tmp_path):
    _, http = off_mode(tmp_path)
    response = http.post(f"/api/cases/{'0' * 32}/open")
    assert response.status_code == 404 and response.json() == {"detail": "Case not found"}


def test_trash_hides_a_case_from_every_route_and_list(rig):
    case_id = rig.service.new_case("Synthetic")["id"]
    kept = rig.service.store.case_dir(case_id) / "kept.bin"
    kept.write_bytes(b"synthetic")
    counted = rig.owner.get("/api/agreement").json()["cases"]["total"]
    assert rig.owner.post(f"/api/cases/{case_id}/trash").json() == {"id": case_id, "jobs_stopped": 0, "sends_continuing": 0}
    walked = 0
    for route, method, rule in ruled_routes(rig.app):
        if rule.kind == "case":
            absent(rig.owner.request(method, filled(route.path.replace("{case_id}", case_id))), "Case not found")
            walked += 1
    assert walked > 40
    assert case_id not in [case["id"] for case in rig.owner.get("/api/cases").json()]
    assert rig.owner.get("/api/agreement").json()["cases"]["total"] == counted - 1
    listed = next(case for case in rig.owner.get("/api/trash").json() if case["id"] == case_id)
    assert listed["trashed_at"] and "source_dir" not in listed
    assert kept.read_bytes() == b"synthetic"


def test_a_trashed_case_reads_as_absent_with_sign_in_off(tmp_path):
    service, http = off_mode(tmp_path)
    case_id = service.new_case("Synthetic")["id"]
    job = service.store.enqueue(case_id, "include_all", {})
    upload_id = finished_upload(service, case_id)
    assert http.post(f"/api/cases/{case_id}/trash").json() == {"id": case_id, "jobs_stopped": 1, "sends_continuing": 0}
    absent(http.get(f"/api/cases/{case_id}"), "Case not found")
    absent(http.post(f"/api/cases/{case_id}/trash"), "Case not found")
    absent(http.post(f"/api/cases/{'0' * 32}/trash"), "Case not found")
    absent(http.get(f"/api/jobs/{job['id']}"), "Job not found")
    absent(http.post(f"/api/imports/{upload_id}/finish"), "Upload not found")
    assert http.get("/api/cases").json() == []
    assert [case["id"] for case in http.get("/api/trash").json()] == [case_id]
    # The gate refuses before its audit row, so a refused open leaves no sign it was allowed.
    assert actions(service, case_id) == ["case.trash"]
    assert http.get("/api/cases/not-a-record").status_code == 400
    assert http.post(f"/api/trash/{case_id}/restore").status_code == 200
    assert http.get(f"/api/cases/{case_id}").status_code == 200


def test_a_trashed_cases_job_and_upload_read_as_absent(rig):
    case_id = rig.service.new_case("Synthetic")["id"]
    job_id = rig.service.store.enqueue(case_id, "include_all", {})["id"]
    upload_id = finished_upload(rig.service, case_id)
    rig.owner.post(f"/api/cases/{case_id}/trash")
    for response in (rig.owner.get(f"/api/jobs/{job_id}"), rig.owner.get(f"/api/jobs/{job_id}/measurements"), rig.owner.post(f"/api/jobs/{job_id}/cancel")):
        absent(response, "Job not found")
    for response in (
        rig.owner.post(f"/api/imports/{upload_id}/parts"),
        rig.owner.post(f"/api/imports/{upload_id}/finish"),
        rig.owner.delete(f"/api/imports/{upload_id}"),
    ):
        absent(response, "Upload not found")


def test_restore_returns_the_case_unchanged_and_jobs_stay_cancelled(rig):
    case_id = rig.service.new_case("Synthetic")["id"]
    job_id = rig.service.store.enqueue(case_id, "include_all", {})["id"]
    before = rig.owner.get(f"/api/cases/{case_id}").json()
    rig.owner.post(f"/api/cases/{case_id}/trash")
    restored = rig.owner.post(f"/api/trash/{case_id}/restore")
    assert restored.status_code == 200 and restored.json() == before
    assert rig.owner.get(f"/api/cases/{case_id}").json() == before
    assert case_id not in [case["id"] for case in rig.owner.get("/api/trash").json()]
    assert rig.service.store.get("jobs", job_id)["status"] == "cancelled"


def test_restore_is_for_the_owner_and_only_for_a_trashed_case(rig):
    case_id = rig.service.new_case("Synthetic")["id"]
    absent(rig.owner.post(f"/api/trash/{case_id}/restore"), "Case not found")
    rig.owner.post(f"/api/cases/{case_id}/trash")
    absent(rig.other.post(f"/api/trash/{case_id}/restore"), "Case not found")
    assert case_id in [case["id"] for case in rig.owner.get("/api/trash").json()]
    assert case_id not in [case["id"] for case in rig.other.get("/api/trash").json()]
    assert rig.owner.post(f"/api/trash/{case_id}/restore").status_code == 200


def test_trash_stops_each_active_job_and_counts_a_send_apart(rig):
    store = rig.service.store
    case_id = rig.service.new_case("Synthetic")["id"]
    queued = store.enqueue(case_id, "include_all", {})["id"]
    running = store.enqueue(case_id, "include_all", {})["id"]
    sending = store.enqueue(case_id, "orthanc_export", {"delivery_state": "pending"})["id"]
    for job_id in (running, sending):
        store.update_job(job_id, status="running")
    assert rig.owner.post(f"/api/cases/{case_id}/trash").json() == {"id": case_id, "jobs_stopped": 2, "sends_continuing": 1}
    assert store.get("jobs", queued)["status"] == "cancelled"
    assert store.get("jobs", running)["cancel_requested"] is True
    assert store.get("jobs", sending)["status"] == "running"


def test_a_job_that_cannot_be_stopped_does_not_fail_trash(rig, monkeypatch):
    store = rig.service.store
    case_id = rig.service.new_case("Synthetic")["id"]
    running = store.enqueue(case_id, "include_all", {})["id"]
    store.update_job(running, status="running")

    def refuse(_job_id):
        raise RuntimeError("Synthetic failure")

    monkeypatch.setattr(rig.service, "cancel_job", refuse)
    assert rig.owner.post(f"/api/cases/{case_id}/trash").json() == {"id": case_id, "jobs_stopped": 0, "sends_continuing": 0}
    assert case_id in [case["id"] for case in rig.owner.get("/api/trash").json()]
    assert store.get("jobs", running)["cancel_requested"] is True


def test_trashing_twice_leaves_one_row_and_one_audit_event(rig):
    store = rig.service.store
    case_id = rig.service.new_case("Synthetic")["id"]
    store.trash_case(case_id, rig.owner.user_id)
    store.trash_case(case_id, rig.owner.user_id)
    with store.connection() as con:
        assert con.execute("SELECT COUNT(*) FROM trash WHERE case_id=?", (case_id,)).fetchone()[0] == 1
    assert actions(rig.service, case_id) == ["case.trash"]


def test_a_job_that_starts_after_trash_ends_cancelled(tmp_path):
    service, http = off_mode(tmp_path)
    case = http.post("/api/cases/demo").json()
    revision = service.store.get("revisions", case["active_revision_id"])
    assert http.post(f"/api/cases/{case['id']}/trash").status_code == 200
    operation = dict(type="erase_region", seed_ras=None, bounds_ras=[[-5, -5, -5], [5, 5, 5]])
    job = service.store.enqueue(case["id"], "refine", dict(revision_id=revision["id"], structure_id=revision["structures"][0]["id"], operation=operation))
    service.execute(service.store.claim())
    assert service.store.get("jobs", job["id"])["status"] == "cancelled"
    assert len(service.store.list("revisions", case["id"])) == 1


def test_trash_restore_and_rename_audit_ids_only(rig):
    case_id = imported(rig.service)
    rig.owner.patch(f"/api/cases/{case_id}", json={"label": "Synthetic private name"})
    rig.owner.post(f"/api/cases/{case_id}/trash")
    rig.owner.post(f"/api/trash/{case_id}/restore")
    with rig.service.store.connection() as con:
        rows = [tuple(row) for row in con.execute("SELECT actor_id,action,case_id,target,detail FROM audit_events WHERE case_id=? ORDER BY seq", (case_id,))]
    assert rows == [(rig.owner.user_id, action, case_id, None, "{}") for action in ("case.rename", "case.trash", "case.restore")]


def test_rename_updates_the_list_and_the_record(rig):
    case_id = imported(rig.service)
    response = rig.owner.patch(f"/api/cases/{case_id}", json={"label": "  Synthetic renamed  "})
    assert response.status_code == 200 and response.json()["label"] == "Synthetic renamed"
    assert rig.service.store.get("cases", case_id)["label"] == "Synthetic renamed"
    assert next(case for case in rig.owner.get("/api/cases").json() if case["id"] == case_id)["label"] == "Synthetic renamed"


def test_rename_refuses_an_empty_or_unprintable_name(rig):
    case_id = imported(rig.service)
    for label in ("", "   ", "Synthetic\x07name", "Synthetic\x7fname"):
        response = rig.owner.patch(f"/api/cases/{case_id}", json={"label": label})
        assert response.status_code == 400 and response.json() == {"detail": "A case name must contain 1 to 128 printable characters"}
    assert rig.owner.patch(f"/api/cases/{case_id}", json={"label": "x" * 129}).status_code == 422
    with pytest.raises(ValueError, match="1 to 128"):
        rig.service.store.rename_case(case_id, "x" * 129)
    assert rig.service.store.get("cases", case_id)["label"] == "Synthetic"


def test_rename_waits_for_an_import(rig):
    case_id = rig.service.new_case("Synthetic")["id"]
    response = rig.owner.patch(f"/api/cases/{case_id}", json={"label": "Synthetic renamed"})
    assert response.status_code == 409 and response.json() == {"detail": "This case is still importing. Rename it when the import finishes."}


def test_tree_bytes_counts_a_link_once_and_skips_what_is_missing(tmp_path):
    fill(tmp_path / "outside.bin", 100_000)
    fill(tmp_path / "case" / "mask.bin", 100_000)
    alone = tree_bytes(tmp_path / "case")
    assert alone >= 100_000
    os.link(tmp_path / "case" / "mask.bin", tmp_path / "case" / "same-mask.bin")
    (tmp_path / "case" / "scan-link").symlink_to(tmp_path / "outside.bin")
    assert tree_bytes(tmp_path / "case") < alone + 100_000
    assert tree_bytes(tmp_path / "missing") == 0


def test_sizes_cover_your_own_cases_including_trash(rig):
    kept, trashed = rig.service.new_case("Synthetic")["id"], rig.service.new_case("Synthetic")["id"]
    for case_id in (kept, trashed):
        fill(rig.service.store.case_dir(case_id) / "scan.bin", 8192)
    rig.owner.post(f"/api/cases/{trashed}/trash")
    sizes = rig.owner.get("/api/sizes").json()["sizes"]
    assert sizes[kept] >= 8192 and sizes[trashed] >= 8192
    assert kept not in rig.other.get("/api/sizes").json()["sizes"]


def test_storage_lists_every_case_and_labels_only_your_own(rig):
    case = rig.service.new_case("Synthetic")
    fill(rig.service.store.case_dir(case["id"]) / "scan.bin", 8192)
    before = rig.other.get("/api/storage").json()["totals"]["trash"]
    rig.owner.post(f"/api/cases/{case['id']}/trash")
    mine = next(row for row in rig.owner.get("/api/storage").json()["cases"] if row.get("id") == case["id"])
    assert mine["label"] == "Synthetic" and mine["trashed_at"] and mine["size"] >= 8192
    report = rig.other.get("/api/storage").json()
    theirs = next(row for row in report["cases"] if row["created_at"] == case["created_at"])
    assert theirs == {"label": None, "owner": "Someone", "created_at": case["created_at"], "size": mine["size"]}
    assert report["totals"]["trash"] == before + mine["size"]


def test_storage_refuses_a_member(rig):
    response = rig.member.get("/api/storage")
    assert response.status_code == 403 and response.json() == {"detail": "Administrator access is required"}


def test_storage_totals_cover_cases_trash_caches_and_the_rest(tmp_path):
    service, http = off_mode(tmp_path)
    root = service.settings.data_dir
    kept, trashed = service.new_case("Synthetic")["id"], service.new_case("Synthetic")["id"]
    service.store.trash_case(trashed, "local")
    fill(root / "cases" / kept / "scan.bin", 8192)
    fill(root / "cases" / trashed / "scan.bin", 16384)
    fill(root / "review-cache" / "chunk.bin", 32768)
    fill(root / "cases" / "stray" / "left.bin", 65536)
    fill(root / "elsewhere" / "left.bin", 131072)
    report = http.get("/api/storage").json()
    assert report["totals"]["cases"] == tree_bytes(root / "cases" / kept) >= 8192
    assert report["totals"]["trash"] == tree_bytes(root / "cases" / trashed) >= 16384
    assert report["totals"]["shared_caches"] == tree_bytes(root / "review-cache") >= 32768
    assert report["totals"]["other"] >= 65536 + 131072
    assert [row["label"] for row in report["cases"]] == ["Synthetic", "Synthetic"]
    assert 0 < report["disk"]["free"] <= report["disk"]["total"]
