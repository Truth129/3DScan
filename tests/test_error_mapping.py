"""Only explicit missing records are 404s; other KeyErrors are server defects."""

import sqlite3

from fastapi.testclient import TestClient

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.errors import NotFound


def test_missing_records_and_purged_writes_are_404_and_other_errors_are_500_without_detail(tmp_path, monkeypatch):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    http = TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)
    assert http.get(f"/api/cases/{'0' * 32}").json() == {"detail": "Case not found"}
    assert http.get(f"/api/jobs/{'0' * 32}").json() == {"detail": "Job not found"}
    assert isinstance(NotFound(), KeyError)

    def fails_with(error):
        def case_summaries(*args, **kwargs):
            raise error

        monkeypatch.setattr(app.state.service.store, "case_summaries", case_summaries)
        return http.get("/api/cases")

    purged = fails_with(sqlite3.IntegrityError("purged"))
    assert purged.status_code == 404 and purged.json() == {"detail": "Case not found"}
    for error, secret in ((KeyError("internal_field"), "internal_field"), (sqlite3.IntegrityError("UNIQUE constraint failed: cases.id"), "UNIQUE")):
        broken = fails_with(error)
        assert broken.status_code == 500 and secret not in broken.text
