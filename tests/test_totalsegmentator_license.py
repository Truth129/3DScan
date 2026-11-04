"""License configuration with fake keys and no vendor or patient network traffic."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import stat
import subprocess
import sys
import threading
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
import requests

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d import totalsegmentator_license as licenses
from scan3d.segmentation_options import segmentation_catalog
from scan3d.store import identifier, now


KEY_A = "aca_" + "A" * 14
KEY_B = "aca_" + "B" * 14
ENDPOINT = "/api/totalsegmentator/license"


@pytest.fixture
def license_home(tmp_path, monkeypatch):
    home = tmp_path / "totalsegmentator"
    monkeypatch.setenv("TOTALSEG_HOME_DIR", str(home))

    def unexpected_network(*args, **kwargs):
        raise AssertionError("License checks must not use real network requests")

    monkeypatch.setattr(licenses.requests, "post", unexpected_network)
    return home


@pytest.fixture
def http(license_home, tmp_path):
    app = create_app(Settings(tmp_path / "cases", (tmp_path,), web_dir=tmp_path / "dist"))
    # These endpoints do not need a worker. Keeping lifespan stopped also makes
    # queued/running-job race checks independent of model setup or inference.
    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        yield SimpleNamespace(client=client, service=app.state.service, home=license_home)
    finally:
        client.close()


def accepted(monkeypatch):
    calls = []

    def post(url, **kwargs):
        calls.append((url, kwargs))
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"status": "valid_license"}, close=lambda: None)

    monkeypatch.setattr(licenses.requests, "post", post)
    return calls


def write_config(home, value):
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.json"
    path.write_text(json.dumps(value))
    return path


def assert_private(response, *keys):
    for key in keys:
        assert key not in response.text
    assert response.headers.get("cache-control") == "no-store"


def test_status_is_local_and_does_not_import_torch(license_home):
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from scan3d.totalsegmentator_license import license_status; "
            "s=license_status(); assert not s['configured']; assert 'torch' not in sys.modules",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert not result.stdout and not result.stderr
    assert licenses.license_status() == {"configured": False, "validated": None, "validated_at": None}


def test_save_update_remove_preserve_upstream_config_and_use_private_permissions(license_home, monkeypatch):
    unrelated = {"totalseg_id": "fixture", "send_usage_stats": False, "prediction_counter": 17, "unknown_upstream_field": {"preserve": [1, 2, 3]}}
    path = write_config(license_home, unrelated)
    calls = accepted(monkeypatch)
    for key in [KEY_A, KEY_B]:
        result = licenses.save_license(f"  {key}\n")
        assert result["configured"] is True and result["validated"] is True
        assert result["validated_at"]
        assert key not in json.dumps(result)
        stored = json.loads(path.read_text())
        assert stored["license_number"] == key
        assert {name: stored[name] for name in unrelated} == unrelated
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert licenses.license_status() == result
    assert len(calls) == 2
    for (url, options), key in zip(calls, [KEY_A, KEY_B]):
        assert url == "https://backend.totalsegmentator.com:443/is_valid_license_number"
        assert options == {"json": {"license_number": key}, "timeout": (3.05, 5), "allow_redirects": False}
    removed = licenses.remove_license()
    assert removed == {"configured": False, "validated": None, "validated_at": None}
    assert json.loads(path.read_text()) == unrelated
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert licenses.remove_license() == removed


def test_first_save_creates_a_usable_upstream_config_without_changing_usage_preferences(license_home, monkeypatch):
    accepted(monkeypatch)
    licenses.save_license(KEY_A)
    stored = json.loads((license_home / "config.json").read_text())
    assert isinstance(stored["totalseg_id"], str) and stored["totalseg_id"]
    assert isinstance(stored["prediction_counter"], int)
    assert stored["license_number"] == KEY_A
    assert stat.S_IMODE((license_home / "config.json").stat().st_mode) == 0o600


@pytest.mark.parametrize("key", ["", "x" * 18, "aca_" + "a" * 15, "aca_" + "a" * 13 + "é"])
def test_invalid_shape_never_calls_vendor_or_replaces_existing_key(license_home, monkeypatch, key):
    path = write_config(license_home, {"license_number": KEY_A, "prediction_counter": 4})
    before = path.read_bytes()
    with pytest.raises(licenses.LicenseError) as caught:
        licenses.save_license(key)
    assert caught.value.status_code == 400
    if key:
        assert key not in str(caught.value)
    assert path.read_bytes() == before


def test_rejected_license_keeps_previous_key_and_redacts_vendor_response(license_home, monkeypatch, capsys, caplog):
    reply = {"status": "invalid_license", "message": KEY_B}
    path = write_config(license_home, {"license_number": KEY_A, "prediction_counter": 4})
    before = path.read_bytes()
    monkeypatch.setattr(licenses.requests, "post", lambda *args, **kwargs: SimpleNamespace(ok=True, status_code=200, json=lambda: reply, close=lambda: None))
    with pytest.raises(licenses.LicenseError) as caught:
        licenses.save_license(KEY_B)
    assert caught.value.status_code == 400
    assert KEY_A not in str(caught.value) and KEY_B not in str(caught.value)
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert KEY_B not in output.out + output.err + caplog.text


@pytest.mark.parametrize("mode,code", [("timeout", 503), ("server", 502), ("redirect", 502), ("malformed", 502)])
def test_validation_failure_does_not_replace_existing_key(license_home, monkeypatch, mode, code, capsys, caplog):
    path = write_config(license_home, {"license_number": KEY_A, "prediction_counter": 4})
    before = path.read_bytes()

    def post(*args, **kwargs):
        if mode in {"timeout", "connection"}:
            error = requests.Timeout if mode == "timeout" else requests.ConnectionError
            raise error("private detail " + KEY_B)

        def body():
            if mode == "malformed":
                raise ValueError("private detail " + KEY_B)
            return {"message": KEY_B} if mode == "unexpected" else {"status": "valid_license", "message": KEY_B}

        status = 500 if mode == "server" else 302 if mode == "redirect" else 200
        return SimpleNamespace(ok=status < 400, status_code=status, json=body, close=lambda: None)

    monkeypatch.setattr(licenses.requests, "post", post)
    with pytest.raises(licenses.LicenseError) as caught:
        licenses.save_license(KEY_B)
    assert caught.value.status_code == code
    assert KEY_A not in str(caught.value) and KEY_B not in str(caught.value)
    assert path.read_bytes() == before
    output = capsys.readouterr()
    assert KEY_B not in output.out + output.err + caplog.text


def test_external_key_replacement_cannot_inherit_previous_validation(license_home, monkeypatch):
    accepted(monkeypatch)
    licenses.save_license(KEY_A)
    path = license_home / "config.json"
    config = json.loads(path.read_text())
    config["license_number"] = KEY_B
    path.write_text(json.dumps(config))
    status = licenses.license_status()
    assert status == {"configured": True, "validated": None, "validated_at": None}
    assert KEY_A not in json.dumps(status) and KEY_B not in json.dumps(status)


def test_existing_legacy_license_status_is_available_but_not_falsely_validated(license_home):
    legacy = "private-key-123456"
    assert len(legacy) == 18
    write_config(license_home, {"license_number": legacy, "send_usage_stats": False})
    status = licenses.license_status()
    assert status == {"configured": True, "validated": None, "validated_at": None}
    assert legacy not in json.dumps(status)
    assert all(item["license_available"] for item in segmentation_catalog()["tasks"] if item["license_required"])


def test_corrupt_existing_config_is_not_overwritten_or_sent_for_validation(http):
    http.home.mkdir()
    path = http.home / "config.json"
    before = ('{"license_number":"' + KEY_A + '"').encode()
    path.write_bytes(before)
    for method in ["get", "put", "delete"]:
        options = {"json": {"license_key": KEY_B}} if method == "put" else {}
        response = getattr(http.client, method)(ENDPOINT, **options)
        assert response.status_code == 500
        assert_private(response, KEY_A, KEY_B)
        assert path.read_bytes() == before


def test_atomic_write_failure_preserves_existing_config_and_cleans_secret_temporary_file(license_home, monkeypatch):
    path = write_config(license_home, {"license_number": KEY_A, "prediction_counter": 4})
    before = path.read_bytes()
    accepted(monkeypatch)

    def fail_replace(*args, **kwargs):
        raise OSError("private filesystem detail " + KEY_B)

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(licenses.LicenseError) as caught:
        licenses.save_license(KEY_B)
    assert caught.value.status_code == 500
    assert KEY_B not in str(caught.value)
    assert path.read_bytes() == before
    assert {p.name for p in license_home.iterdir()} == {"config.json"}


def test_api_add_update_remove_unlocks_catalog_without_returning_key(http, monkeypatch):
    accepted(monkeypatch)
    response = http.client.get(ENDPOINT)
    assert response.status_code == 200 and response.json()["configured"] is False
    assert_private(response, KEY_A, KEY_B)
    for key in [KEY_A, KEY_B]:
        response = http.client.put(ENDPOINT, json={"license_key": key})
        assert response.status_code == 200
        assert response.json()["configured"] is True and response.json()["validated"] is True
        assert_private(response, KEY_A, KEY_B)
        status = http.client.get(ENDPOINT)
        assert status.json() == response.json()
        assert_private(status, KEY_A, KEY_B)
        catalog = http.client.get("/api/segmentation-options")
        assert KEY_A not in catalog.text and KEY_B not in catalog.text
        licensed = [item for item in catalog.json()["tasks"] if item["license_required"]]
        assert licensed and all(item["license_available"] for item in licensed)
    response = http.client.delete(ENDPOINT)
    assert response.status_code == 200 and response.json()["configured"] is False
    assert_private(response, KEY_A, KEY_B)
    assert all(not item["license_available"] for item in segmentation_catalog()["tasks"] if item["license_required"])


@pytest.mark.parametrize(
    "body",
    [
        {"license_key": 42},
        {"license_key": KEY_A, "extra": KEY_B},
        {"wrong_field": KEY_A},
        KEY_A,
    ],
)
def test_api_validation_errors_never_echo_key_or_extra_fields(http, body):
    response = http.client.put(ENDPOINT, json=body)
    assert response.status_code == 422
    assert_private(response, KEY_A, KEY_B)
    assert "input" not in response.text


def test_invalid_json_license_request_is_redacted(http):
    response = http.client.put(ENDPOINT, content='{"license_key":"' + KEY_A + '"', headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert_private(response, KEY_A)


@pytest.mark.parametrize("method", ["put", "delete"])
def test_cross_origin_license_changes_are_rejected(http, method):
    call = getattr(http.client, method)
    options = {"json": {"license_key": KEY_A}} if method == "put" else {}
    response = call(ENDPOINT, headers={"Origin": "https://unrelated.example"}, **options)
    assert response.status_code == 403
    assert KEY_A not in response.text
    assert not (http.home / "config.json").exists()


@pytest.mark.parametrize("status, method", [("queued", "put"), ("running", "delete")])
def test_busy_jobs_block_license_writes_before_network_or_config_mutation(http, status, method):
    path = write_config(http.home, {"license_number": KEY_A, "prediction_counter": 3})
    before = path.read_bytes()
    case_id = identifier()
    job = {"id": identifier(), "case_id": case_id, "status": status, "created_at": now(), "kind": "segment"}
    http.service.store.save("jobs", job)
    options = {"json": {"license_key": KEY_B}} if method == "put" else {}
    response = getattr(http.client, method)(ENDPOINT, **options)
    assert response.status_code == 409
    assert_private(response, KEY_A, KEY_B)
    assert path.read_bytes() == before


def test_concurrent_updates_preserve_unrelated_config_and_commit_whole_valid_license(http, monkeypatch):
    path = write_config(http.home, {"send_usage_stats": False, "prediction_counter": 6})
    calls = accepted(monkeypatch)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(lambda key: http.client.put(ENDPOINT, json={"license_key": key}), [KEY_A, KEY_B]))
    assert all(response.status_code == 200 for response in responses)
    config = json.loads(path.read_text())
    assert config["license_number"] in {KEY_A, KEY_B}
    assert config["send_usage_stats"] is False and config["prediction_counter"] == 6
    assert len(calls) == 2 and licenses.license_status()["validated"] is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_license_update_serializes_with_new_job_enqueue(http, monkeypatch):
    validating, release, enqueued = threading.Event(), threading.Event(), threading.Event()

    def validate(*args, **kwargs):
        validating.set()
        assert release.wait(5), "Test did not release fake validation"
        return SimpleNamespace(ok=True, status_code=200, json=lambda: {"status": "valid_license"}, close=lambda: None)

    monkeypatch.setattr(licenses.requests, "post", validate)
    job = {"id": identifier(), "case_id": identifier(), "status": "queued", "created_at": now(), "kind": "segment"}

    def enqueue():
        http.service.store.save("jobs", job)
        enqueued.set()

    with ThreadPoolExecutor(max_workers=2) as pool:
        updating = pool.submit(http.client.put, ENDPOINT, json={"license_key": KEY_A})
        assert validating.wait(3)
        saving = pool.submit(enqueue)
        try:
            assert not enqueued.wait(0.15), "A job was enqueued while license configuration was being validated"
        finally:
            release.set()
        response = updating.result(timeout=5)
        saving.result(timeout=5)
    assert response.status_code == 200
    assert enqueued.is_set() and http.service.store.get("jobs", job["id"])["status"] == "queued"
