import json
from importlib import metadata
import os
from pathlib import Path
import platform
import shutil

from .config import ROOT


def _matching_versions(versions, required=()):
    """Proofs belong to an exact installed package family, not a wheel label."""
    if not isinstance(versions, dict) or any(name not in versions for name in required):
        return False
    try:
        return all(isinstance(name, str) and isinstance(version, str) and metadata.version(name) == version
                   for name, version in versions.items())
    except metadata.PackageNotFoundError:
        return False


def diagnose(live=True):
    result = {"ready": False, "inference_verified": False, "kfd": Path("/dev/kfd").exists(),
              "dcm2niix": bool(shutil.which("dcm2niix")), "reason": "ROCm PyTorch is not installed"}
    if live:
        try:
            import torch
            result.update(torch_version=torch.__version__, hip_version=torch.version.hip)
            if not torch.version.hip:
                result["reason"] = "The installed PyTorch build does not have ROCm support"
            elif not torch.cuda.is_available():
                result["reason"] = "ROCm cannot access a GPU in this process"
            else:
                devices = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
                result.update(ready=True, devices=devices, reason="ROCm available; full inference must still be verified")
        except Exception as exc:
            result["reason"] = str(exc)
    else:
        command = "scripts/check-production-gpu.sh" if os.environ.get("SCAN3D_GPU_PROOF") else "scripts/check_gpu.sh"
        result["reason"] = f"Run {command} to verify the native inference environment"
    proof_file = Path(os.environ.get("SCAN3D_GPU_PROOF", str(ROOT / ".runtime" / "gpu-proof.json")))
    if proof_file.exists():
        try:
            proof = json.loads(proof_file.read_text())
            if not isinstance(proof, dict):
                raise ValueError("Invalid GPU proof record")
            checks = proof.get("checks", {})
            if not isinstance(checks, dict):
                raise ValueError("Invalid GPU proof checks")
            python_matches = proof.get("python_version", platform.python_version()) == platform.python_version()
            runtime_matches = _matching_versions(checks.get("runtime_versions", {}))
            proof_matches = python_matches and runtime_matches
            if not live:
                try:
                    current = metadata.version("torch")
                except metadata.PackageNotFoundError:
                    current = None
                matches = current and current == checks.get("torch_distribution_version", checks.get("torch_version"))
                ready = bool(matches and proof_matches and proof.get("gpu_ready"))
                result.update(ready=ready, torch_version=current)
                if ready:
                    result.update(hip_version=checks.get("hip_version"),
                                  devices=[d["name"] for d in checks.get("devices", [])], checked_at=proof.get("checked_at"))
                    result["reason"] = "Native ROCm convolution verified; model inference is tracked separately"
                elif not (matches and proof_matches):
                    result["reason"] = "GPU validation proof does not match the installed runtime"
                elif proof.get("errors"):
                    result["reason"] = "; ".join(proof["errors"])
            versions = proof.get("versions", {})
            matching = _matching_versions(versions, ("torch", "nnunetv2", "TotalSegmentator"))
            result["inference_verified"] = bool(result["ready"] and proof_matches and matching and proof.get("inference_verified"))
            if result["inference_verified"]:
                result.update(completed_tasks=proof.get("completed_tasks", []), validated_at=proof.get("validated_at"))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return result
