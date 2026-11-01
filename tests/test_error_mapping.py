"""Only explicit missing records are 404s; other KeyErrors are server defects."""

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
