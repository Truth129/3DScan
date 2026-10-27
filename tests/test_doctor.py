"""Runtime proofs cannot silently certify a different SDK, Python or model set."""

import copy
import builtins
import json
from importlib import metadata

import pytest

from scan3d import doctor


VERSIONS = {
    "torch": "2.14.0+rocm10.1.0",
    "rocm": "10.1.0",
    "rocm-sdk-core": "10.1.0",
    "rocm-sdk-libraries": "10.1.0",
    "nnunetv2": "2.8.1",
    "TotalSegmentator": "2.18.0",
    "nnInteractive": "2.6.0",
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
    target = tmp_path / "rocm10-proof.json"
    monkeypatch.setenv("SCAN3D_GPU_PROOF", str(target))
    record = {
        "gpu_ready": True,
        "inference_verified": True,
        "python_version": "3.14.8",
        "checked_at": "gpu-check",
        "validated_at": "model-check",
        "completed_tasks": ["total", "lung_vessels"],
        "versions": dict(VERSIONS),
        "checks": {
            "torch_distribution_version": VERSIONS["torch"],
            "hip_version": "7.16.26385",
            "runtime_versions": {key: VERSIONS[key] for key in ("torch", "rocm", "rocm-sdk-core", "rocm-sdk-libraries")},
            "devices": [{"name": "AMD Radeon AI PRO R9700"}],
        },
    }

    def save(data=record, path=target):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    return target, record, save


def test_selected_runtime_proof_certifies_completed_tasks_without_importing_torch(proof, monkeypatch):
    original_import = builtins.__import__

    def guard(name, *args, **kwargs):
        assert name != "torch", "Non-live health must not initialize the GPU runtime"
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    _, _, save = proof
    save()
    result = doctor.diagnose(live=False)
    assert result["ready"] and result["inference_verified"]
    assert result["hip_version"] == "7.16.26385"
    assert result["completed_tasks"] == ["total", "lung_vessels"]
    assert result["validated_at"] == "model-check"


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
    assert result["ready"] is False and result["inference_verified"] is False
    assert "does not match" in result["reason"]
    assert "hip_version" not in result and "devices" not in result


def test_model_version_mismatch_keeps_gpu_proof_but_rejects_inference(proof):
    _, record, save = proof
    record["versions"]["nnInteractive"] = "2.5.0"
    save()
    result = doctor.diagnose(live=False)
    assert result["ready"] is True
    assert result["inference_verified"] is False
    assert "completed_tasks" not in result


def test_missing_selected_proof_never_falls_back_to_legacy_proof(proof):
    target, record, save = proof
    save(record, target.parent / ".runtime/gpu-proof.json")
    result = doctor.diagnose(live=False)
    assert result["ready"] is False and result["inference_verified"] is False
    assert "check-production-gpu.sh" in result["reason"]


def test_default_legacy_proof_remains_supported(proof, monkeypatch):
    target, record, save = proof
    monkeypatch.delenv("SCAN3D_GPU_PROOF")
    record.pop("python_version")
    record["checks"].pop("runtime_versions")
    save(record, target.parent / ".runtime/gpu-proof.json")
    result = doctor.diagnose(live=False)
    assert result["ready"] and result["inference_verified"]


@pytest.mark.parametrize("malformed", [[], {"checks": []}, {"checks": {"runtime_versions": []}}])
def test_malformed_proof_does_not_break_health(proof, malformed):
    _, _, save = proof
    save(malformed)
    result = doctor.diagnose(live=False)
    assert result["ready"] is False and result["inference_verified"] is False
