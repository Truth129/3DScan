"""Archive launch resolution uses synthetic sources and never starts work."""

from contextlib import contextmanager
from pathlib import Path

import pytest

from scan3d import identity
from scan3d.errors import NotFound
from scan3d.identity import EVERY_CASE
from scan3d.orthanc import OrthancClient, OrthancError, series_summary
from scan3d.orthanc_models import snapshot_fingerprint
from test_orthanc import Archive, STUDY as TRANSPORT_STUDY
from test_orthanc_imports import SERIES, STUDY, STUDY_UID, execute_cpu, rig, start, start_as


@pytest.fixture
def launch_rig(rig, monkeypatch):
    archive = rig.archive
    with OrthancClient(rig.settings.orthanc_url) as client:
        archive.source_key = client.source_key
    archive.snapshot_doc["source_key"] = archive.source_key
    refresh_snapshot(rig)
    archive.calls = []
    snapshot = archive.snapshot

    def read_snapshot(series_id):
        archive.calls.append(("snapshot", series_id))
        return snapshot(series_id)

    def read_study(study_id):
        archive.calls.append(("study", study_id))
        assert study_id == STUDY
        return {
            "id": STUDY,
            "study_uid": STUDY_UID,
            "patient_name": "Synthetic^Orthanc",
            "patient_id": "SYNTHETIC-ORTHANC",
            "description": "Synthetic study",
            "study_date": "20261030",
            "accession_number": "SYNTHETIC",
            "series_count": 1,
            "modalities": "CT",
            "stable": True,
        }

    monkeypatch.setattr(archive, "snapshot", read_snapshot)
    monkeypatch.setattr(archive, "study", read_study, raising=False)
    return rig


def refresh_snapshot(rig):
    from scan3d.orthanc_models import parse_snapshot

    rig.archive.snapshot_doc["snapshot"] = snapshot_fingerprint(parse_snapshot(rig.archive.snapshot_doc))


def launch(rig, **overrides):
    return rig.http.post("/api/orthanc/launch", json={"series_id": SERIES, "source_key": rig.archive.source_key, **overrides})


def files_state(root):
    # SQLite's shared-memory index records read marks, so even a plain read changes it.
    return {str(item.relative_to(root)): item.read_bytes() if item.is_file() else None for item in root.rglob("*") if item.name != "scan3d.sqlite3-shm"}


@contextmanager
def read_only(rig, monkeypatch):
    before = files_state(rig.settings.data_dir)
    queries = []
    connection = rig.service.store.connection

    @contextmanager
    def checked_connection():
        with connection() as con:
            con.execute("PRAGMA query_only=ON")
            con.set_trace_callback(queries.append)
            yield con
            assert con.total_changes == 0

    def unexpected(*args, **kwargs):
        pytest.fail("Launch attempted to create or save local state")

    audited = []
    with monkeypatch.context() as patch:
        # The gate's audit row says a launch was allowed to start, and is the one write it may cause.
        patch.setattr(rig.service.accounts, "audit", lambda action, *args: audited.append(action))
        patch.setattr(rig.service.store, "connection", checked_connection)
        patch.setattr(rig.service.store, "case_dir", unexpected)
        patch.setattr(rig.service.store, "save", unexpected)
        patch.setattr(rig.service, "new_case", unexpected)
        yield
    assert audited == ["archive.launch"]
    assert all(query.lstrip().startswith("SELECT") for query in queries), queries
    assert files_state(rig.settings.data_dir) == before


def test_launch_returns_only_public_current_selection_without_creating_work(launch_rig, monkeypatch):
    rig = launch_rig
    with read_only(rig, monkeypatch):
        response = launch(rig)
    assert response.status_code == 200 and response.headers["cache-control"] == "no-store"
    result = response.json()
    assert set(result) == {"study", "series", "case_id", "job"}
    assert result["study"]["id"] == STUDY
    assert result["series"] == {**series_summary(rig.archive.snapshot(SERIES)), "supported": True, "unsupported_reason": None}
    assert result["case_id"] is result["job"] is None
    assert rig.service.store.visible_cases(EVERY_CASE) == rig.service.store.list("jobs") == []
    for private in ('"instances"', "file_uuid", "source_key", "synthetic-user", "private-test-password", "https://synthetic.example.test"):
        assert private not in response.text


def test_launch_finds_an_import_whoever_made_it(launch_rig):
    rig = launch_rig
    first, second = "a" * 32, "b" * 32

    def launch_as(user_id):
        identity.activate(identity.Principal(user_id, "name", "Name", "admin", True))
        try:
            return rig.service.orthanc.launch(SERIES, rig.archive.source_key)
        finally:
            identity.activate(None)

    job = start_as(rig, first)
    assert launch_as(first)["job"]["id"] == launch_as(second)["job"]["id"] == job["id"]
    assert execute_cpu(rig, job["id"])["status"] == "completed"
    assert launch_as(first)["case_id"] == launch_as(second)["case_id"] == job["case_id"]


def test_launch_treats_a_trashed_case_as_absent(launch_rig):
    rig = launch_rig
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "completed"
    assert launch(rig).json()["case_id"] == job["case_id"]
    rig.service.store.trash_case(job["case_id"], None)
    result = launch(rig).json()
    assert result["case_id"] is result["job"] is None


def test_archive_mismatch_is_rejected_before_any_upstream_request(launch_rig, monkeypatch):
    rig = launch_rig
    with read_only(rig, monkeypatch):
        response = launch(rig, source_key="a" * 64)
    assert response.status_code == 400 and response.headers["cache-control"] == "no-store"
    assert "different Orthanc archive" in response.json()["detail"]
    assert rig.archive.calls == []


@pytest.mark.parametrize(
    "body",
    [
        {"series_id": SERIES},
        {"series_id": "not-an-id", "source_key": "a" * 64},
        {"series_id": SERIES, "source_key": "A" * 64},
        {"series_id": SERIES, "source_key": "a" * 64, "orthanc_url": "https://untrusted.example.test"},
    ],
)
def test_invalid_launch_shape_never_contacts_archive(launch_rig, body):
    rig = launch_rig
    response = rig.http.post("/api/orthanc/launch", json=body)
    assert response.status_code == 422 and response.headers["cache-control"] == "no-store"
    assert rig.archive.calls == []
    assert rig.service.store.visible_cases(EVERY_CASE) == rig.service.store.list("jobs") == []


@pytest.mark.parametrize("status", ["queued", "running"])
def test_matching_pending_import_is_returned_without_starting_or_mutating_it(launch_rig, monkeypatch, status):
    rig = launch_rig
    job = start(rig).json()
    if status == "running":
        assert rig.service.store.claim("cpu")["id"] == job["id"]
    with read_only(rig, monkeypatch):
        result = launch(rig).json()
    assert result["case_id"] is None
    assert result["job"]["id"] == job["id"] and result["job"]["case_id"] == job["case_id"] and result["job"]["status"] == status
    assert "payload" not in result["job"] and "cancel_requested" not in result["job"]
    assert rig.archive.downloaded == 0


def test_matching_completed_import_opens_exact_case_even_after_source_metadata_changes(launch_rig, monkeypatch):
    rig = launch_rig
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "completed"
    unrelated = rig.service.new_case("Unrelated local case")
    rig.archive.snapshot_doc["description"] = "Updated synthetic description"
    with read_only(rig, monkeypatch):
        result = launch(rig).json()
    assert result["case_id"] == job["case_id"] != unrelated["id"] and result["job"] is None
    assert result["series"]["description"] == "Updated synthetic description"
    assert rig.archive.downloaded == 1


@pytest.mark.parametrize("completed", [False, True])
def test_changed_snapshot_does_not_reopen_or_follow_old_import(launch_rig, monkeypatch, completed):
    rig = launch_rig
    job = start(rig).json()
    before_snapshot = rig.archive.snapshot_doc["snapshot"]
    if completed:
        assert execute_cpu(rig, job["id"])["status"] == "completed"
    rig.archive.snapshot_doc["instances"][0]["file_uuid"] = "new-synthetic-file-version"
    refresh_snapshot(rig)
    with read_only(rig, monkeypatch):
        result = launch(rig).json()
    assert result["case_id"] is result["job"] is None
    assert result["series"]["snapshot"] != before_snapshot
    assert len(rig.service.store.list("jobs")) == 1


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_or_cancelled_import_is_only_resolved_for_explicit_retry(launch_rig, monkeypatch, status):
    rig = launch_rig
    job = start(rig).json()
    rig.service.store.update_job(job["id"], status=status)
    with read_only(rig, monkeypatch):
        result = launch(rig).json()
    assert result["case_id"] is result["job"] is None
    assert result["series"]["supported"] is True


@pytest.mark.parametrize("mutation", ["receiving", "incomplete", "MR"])
def test_blocked_series_is_shown_without_importing_or_reusing(launch_rig, monkeypatch, mutation):
    rig = launch_rig
    if mutation in {"receiving", "incomplete"}:
        rig.archive.snapshot_doc["completeness"] = mutation
        rig.archive.snapshot_doc["stable"] = mutation != "receiving"
    else:
        rig.archive.snapshot_doc["modality"] = mutation
    refresh_snapshot(rig)
    with read_only(rig, monkeypatch):
        response = launch(rig)
    assert response.status_code == 200
    result = response.json()
    assert result["case_id"] is result["job"] is None
    if mutation == "MR":
        assert result["series"]["supported"] is False and result["series"]["unsupported_reason"]
    else:
        assert result["series"]["completeness"] == mutation


@pytest.mark.parametrize("missing", ["case", "manifest", "source", "truncated_file", "binding", "null_provenance", "job_snapshot"])
def test_missing_or_inconsistent_local_binding_never_opens_case(launch_rig, monkeypatch, missing):
    rig = launch_rig
    job = start(rig).json()
    assert execute_cpu(rig, job["id"])["status"] == "completed"
    case = rig.service.store.get("cases", job["case_id"])
    directory = Path(case["source_dir"])
    if missing == "case":
        with rig.service.store.connection() as con:
            con.execute("DELETE FROM cases WHERE id=?", (case["id"],))
    elif missing == "manifest":
        (directory.parent / "source-import.json").unlink()
    elif missing == "source":
        directory.rename(directory.with_name("retained-synthetic-source"))
    elif missing == "truncated_file":
        (directory / case["references"]["orthanc"]["files"][0]["path"]).write_bytes(b"truncated synthetic source")
    elif missing == "job_snapshot":
        private = rig.service.store.get("jobs", job["id"])
        private["payload"]["snapshot"]["source_key"] = "c" * 64
        rig.service.store.save("jobs", private)
    else:
        if missing == "binding":
            case["references"]["orthanc"]["snapshot"]["series_uid"] = "1.2.3.999"
        else:
            case["import_source"] = None
        rig.service.store.save("cases", case)
    with read_only(rig, monkeypatch):
        response = launch(rig)
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"
    assert "previous import cannot be verified" in response.json()["detail"]
    assert "case_id" not in response.json() and "job" not in response.json()


@pytest.mark.parametrize(("error", "status"), [(NotFound("Synthetic absent series"), 404), (OrthancError("Synthetic unavailable"), 502)])
def test_upstream_failures_preserve_status_and_privacy(launch_rig, monkeypatch, error, status):
    rig = launch_rig
    rig.archive.error = error
    with read_only(rig, monkeypatch):
        response = launch(rig)
    assert response.status_code == status and response.headers["cache-control"] == "no-store"
    assert "private-test-password" not in response.text and rig.settings.orthanc_url not in response.text


def test_cross_origin_launch_is_rejected_before_archive_or_local_resolution(launch_rig, monkeypatch):
    rig = launch_rig
    body = {"series_id": SERIES, "source_key": rig.archive.source_key}
    response = rig.http.post("/api/orthanc/launch", json=body, headers={"Origin": "https://unrelated.example.test"})
    assert response.status_code == 403 and response.headers["cache-control"] == "no-store"
    assert rig.archive.calls == []
    with read_only(rig, monkeypatch):
        response = rig.http.post("/api/orthanc/launch", json=body, headers={"Origin": "http://127.0.0.1"})
    assert response.status_code == 200 and response.json()["case_id"] is response.json()["job"] is None


@pytest.mark.parametrize(("field", "value"), [("id", SERIES), ("study_uid", "1.2.3.999")])
def test_study_change_between_snapshot_and_summary_is_refused(launch_rig, monkeypatch, field, value):
    rig = launch_rig
    original = rig.archive.study
    monkeypatch.setattr(rig.archive, "study", lambda study_id: {**original(study_id), field: value})
    with read_only(rig, monkeypatch):
        response = launch(rig)
    assert response.status_code == 409 and response.headers["cache-control"] == "no-store"


def test_public_study_lookup_validates_identity_without_enumerating_series():
    upstream = Archive()
    with OrthancClient("https://archive.example.test/orthanc", session=upstream) as client:
        study = client.study(TRANSPORT_STUDY)
        assert study["id"] == TRANSPORT_STUDY and study["patient_name"] == "Synthetic^Patient"
        assert len(upstream.calls) == 1 and upstream.calls[0][0] == "GET"
        upstream.study["ID"] = SERIES
        with pytest.raises(OrthancError, match="different study"):
            client.study(TRANSPORT_STUDY)
