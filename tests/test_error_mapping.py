"""Only explicit missing records are 404s; other KeyErrors are server defects."""

import sqlite3

from fastapi.testclient import TestClient

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.errors import NotFound


def test_missing_records_are_404_and_other_key_errors_are_500_without_detail(tmp_path, monkeypatch):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    http = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    missing = http.get(f"/api/cases/{'0' * 32}")
    assert missing.status_code == 404 and missing.json() == {"detail": "Case not found"}
    assert http.get(f"/api/jobs/{'0' * 32}").json() == {"detail": "Job not found"}
    monkeypatch.setattr(app.state.service.store, "case_summaries", lambda *args, **kwargs: {}["internal_field"])
    broken = http.get("/api/cases")
    assert broken.status_code == 500 and "internal_field" not in broken.text
    assert str(NotFound("Upload not found")) == "Upload not found" and isinstance(NotFound(), KeyError)


def test_only_a_purged_case_write_maps_to_not_found(tmp_path, monkeypatch):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    http = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)

    def refuse(message):
        def case_summaries(*args, **kwargs):
            raise sqlite3.IntegrityError(message)

        monkeypatch.setattr(app.state.service.store, "case_summaries", case_summaries)

    refuse("purged")
    purged = http.get("/api/cases")
    assert purged.status_code == 404 and purged.json() == {"detail": "Case not found"}
    refuse("UNIQUE constraint failed: cases.id")
    broken = http.get("/api/cases")
    assert broken.status_code == 500 and "UNIQUE" not in broken.text
