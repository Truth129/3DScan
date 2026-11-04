"""Runtime proofs cannot silently certify a different SDK or Python."""

import copy
import builtins
import json
from importlib import metadata
import os

import pytest

from inference_path_stub import GPU_NAME, activate, stand_in
from scan3d import __main__ as cli
from scan3d import doctor


VERSIONS = {
    "torch": "2.14.0+rocm10.1.0",
    "rocm": "10.1.0",
    "rocm-sdk-core": "10.1.0",
    "rocm-sdk-libraries": "10.1.0",
}


@pytest.fixture
def proof(tmp_path, monkeypatch):
    monkeypatch.setattr(doctor, "ROOT", tmp_path)
    monkeypatch.setattr(doctor.platform, "python_version", lambda: "3.14.8")

    def version(package):
        if package not in VERSIONS:
            raise metadata.PackageNotFoundError(package)
        return VERSIONS[package]

    monkeypatch.setattr(doctor.metadata, "version", version)
    target = tmp_path / "selected-proof.json"
    monkeypatch.setenv("SCAN3D_GPU_PROOF", str(target))
    record = {
        "gpu_ready": True,
        "python_version": "3.14.8",
        "checked_at": "gpu-check",
        "checks": {
            "torch_distribution_version": VERSIONS["torch"],
            "hip_version": "7.16.26385",
            "runtime_versions": {key: VERSIONS[key] for key in ("torch", "rocm", "rocm-sdk-core", "rocm-sdk-libraries")},
            "devices": [{"name": "Test GPU"}],
        },
    }

    def save(data=record, path=target):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    return target, record, save


def test_selected_runtime_proof_is_read_without_importing_torch(proof, monkeypatch):
    original_import = builtins.__import__

    def guard(name, *args, **kwargs):
        assert name != "torch", "Non-live health must not initialize the GPU runtime"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    _, _, save = proof
    save()
    result = doctor.diagnose(live=False)
    assert result["ready"]
    assert result["hip_version"] == "7.16.26385"
    assert result["devices"] == ["Test GPU"]
    assert result["checked_at"] == "gpu-check"


def test_a_cuda_proof_needs_no_rocm_packages(proof):
    _, record, save = proof
    record = copy.deepcopy(record)
    record["checks"]["runtime_versions"] = {"torch": VERSIONS["torch"]}
    record["checks"]["hip_version"] = None
    record["checks"]["cuda_version"] = "13.0"
    save(record)
    result = doctor.diagnose(live=False)
    assert result["ready"] and result["cuda_version"] == "13.0"


@pytest.mark.parametrize("field", ["python", "sdk", "torch"])
def test_stale_runtime_proof_cannot_reuse_matching_other_versions(proof, field):
    _, record, save = proof
    record = copy.deepcopy(record)
    if field == "python":
        record["python_version"] = "3.12.12"
    elif field == "sdk":
        record["checks"]["runtime_versions"]["rocm-sdk-libraries"] = "10.0.0"
    else:
        record["checks"]["torch_distribution_version"] = "2.8.0+rocm7.2.4"
    save(record)
    result = doctor.diagnose(live=False)
    assert result["ready"] is False
    assert "does not match" in result["reason"]
    assert "hip_version" not in result and "devices" not in result


def test_missing_selected_proof_never_falls_back_to_the_default_location(proof):
    target, record, save = proof
    save(record, target.parent / ".runtime/gpu-proof.json")
    result = doctor.diagnose(live=False)
    assert result["ready"] is False
    assert "check_gpu.py" in result["reason"]


def test_proof_in_the_default_location_is_read(proof, monkeypatch):
    target, record, save = proof
    monkeypatch.delenv("SCAN3D_GPU_PROOF")
    record.pop("python_version")
    record["checks"].pop("runtime_versions")
    save(record, target.parent / ".runtime/gpu-proof.json")
    assert doctor.diagnose(live=False)["ready"]


@pytest.mark.parametrize("malformed", [[], {"checks": []}, {"checks": {"runtime_versions": []}}])
def test_malformed_proof_does_not_break_health(proof, malformed):
    _, _, save = proof
    save(malformed)
    assert doctor.diagnose(live=False)["ready"] is False


@pytest.mark.parametrize("request_value, expected", [("", "auto"), ("gpu:1", "gpu:1")])
def test_health_reports_the_configured_device(proof, monkeypatch, request_value, expected):
    monkeypatch.setenv("SCAN3D_DEVICE", request_value)
    assert doctor.diagnose(live=False)["device"] == expected


def test_the_proof_location_follows_the_setting_per_call(proof, monkeypatch, tmp_path):
    target, _, _ = proof
    assert doctor.proof_path() == target
    monkeypatch.setenv("SCAN3D_GPU_PROOF", str(tmp_path / "moved.json"))
    assert doctor.proof_path() == tmp_path / "moved.json"
    monkeypatch.delenv("SCAN3D_GPU_PROOF")
    assert doctor.proof_path() == tmp_path / ".runtime" / "gpu-proof.json"


def test_the_suite_reads_no_proof_this_checkout_holds():
    # A proof names this machine's card, and tests/conftest.py keeps every test and child off it.
    assert doctor.proof_path() != doctor.ROOT / ".runtime" / "gpu-proof.json" and not doctor.proof_path().exists()


def test_the_doctor_command_adds_every_inference_path(proof, monkeypatch, capsys, tmp_path):
    stub = stand_in(tmp_path)
    activate(monkeypatch, stub)
    settings = tmp_path / "no-settings.env"
    settings.touch()
    monkeypatch.setenv("SCAN3D_CONFIG", str(settings))
    monkeypatch.setenv("TOTALSEG_HOME_DIR", str(tmp_path))
    monkeypatch.setenv("PATH", os.environ["PATH"])  # load_environment extends it in place.
    monkeypatch.setattr(os, "umask", lambda mask: None)
    monkeypatch.setattr(doctor, "diagnose", lambda: {"ready": False, "reason": "A live check imports PyTorch"})
    monkeypatch.setattr("sys.argv", ["scan3d", "doctor"])
    cli.main()
    output = json.loads(capsys.readouterr().out)
    assert output["ready"] is False and output["reason"] == "A live check imports PyTorch"
    assert [(row["id"], row["label"]) for row in output["inference_paths"]] == [("default", "Default device"), ("nvidia", GPU_NAME)]
