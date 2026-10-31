import json
from importlib import metadata
import os
from pathlib import Path
import platform
import shutil

from .config import ROOT, device_request


def proof_path():
    """The default path's proof. An added path keeps its own, so neither overwrites the other."""
    return Path(os.environ.get("SCAN3D_GPU_PROOF", str(ROOT / ".runtime" / "gpu-proof.json")))


def _matching_versions(versions, lookup=None):
    """Proofs belong to an exact installed package family, not a wheel label.

    `lookup` reads another environment's packages, because a healthy second runtime
    holds a different family from this process. The default is resolved per call.
    """
    if not isinstance(versions, dict):
        return False
    lookup = lookup or metadata.version
    try:
        return all(isinstance(name, str) and isinstance(version, str) and lookup(name) == version for name, version in versions.items())
    except metadata.PackageNotFoundError:
        return False


def diagnose(live=True):
    """Describe the inference runtime. `ready` means a GPU, so a CPU install reports False by design."""
    result = {
        "ready": False,
        "device": device_request(),
        "dcm2niix": bool(os.environ.get("SCAN3D_DCM2NIIX") or shutil.which("dcm2niix")),
        "reason": "PyTorch is not installed",
    }
    if live:
        try:
            import torch

            result.update(torch_version=torch.__version__, hip_version=torch.version.hip, cuda_version=torch.version.cuda)
            if not torch.cuda.is_available():
                result["reason"] = "PyTorch cannot access a GPU in this process, so AI tasks can only run on the CPU, which is slow"
            else:
                devices = [torch.cuda.get_device_name(i) for i in range(torch.cuda.device_count())]
                result.update(ready=True, devices=devices, reason="GPU available; full inference must still be verified")
        except ImportError:
            pass
        except Exception as exc:
            result["reason"] = str(exc)
    else:
        result["reason"] = "AI tasks are set to run on the CPU" if result["device"] == "cpu" else "Run scripts/check_gpu.py to verify the GPU runtime"
    proof_file = proof_path()
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
                    result.update(
                        hip_version=checks.get("hip_version"),
                        cuda_version=checks.get("cuda_version"),
                        devices=[d["name"] for d in checks.get("devices", [])],
                        checked_at=proof.get("checked_at"),
                    )
                    result["reason"] = "GPU convolution verified; model inference is tracked separately"
                elif not (matches and proof_matches):
                    result["reason"] = "GPU validation proof does not match the installed runtime"
                elif proof.get("errors"):
                    result["reason"] = "; ".join(proof["errors"])
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return result
