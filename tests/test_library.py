"""The case list, on synthetic records: last-opened order for each person."""

from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.store import now
from test_access import BASE, account


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
