import json
from importlib import metadata
from pathlib import Path
import shutil

from .config import ROOT


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
        result["reason"] = "Run scripts/check_gpu.sh to verify the native inference environment"
    proof_file = ROOT / ".runtime" / "gpu-proof.json"
    if proof_file.exists():
        try:
            proof = json.loads(proof_file.read_text())
            if not live:
                try:
                    current = metadata.version("torch")
                except metadata.PackageNotFoundError:
                    current = None
                checks = proof.get("checks", {})
                matches = current and current == checks.get("torch_distribution_version", checks.get("torch_version"))
                ready = bool(matches and proof.get("gpu_ready"))
                result.update(ready=ready, torch_version=current, hip_version=checks.get("hip_version"),
                              devices=[d["name"] for d in checks.get("devices", [])], checked_at=proof.get("checked_at"))
                if ready:
                    result["reason"] = "Native ROCm convolution verified; model inference is tracked separately"
                elif proof.get("errors"):
                    result["reason"] = "; ".join(proof["errors"])
            versions = proof.get("versions", {})
            try:
                matching = all(versions.get(package) == metadata.version(package)
                               for package in ("torch", "nnunetv2", "TotalSegmentator"))
            except metadata.PackageNotFoundError:
                matching = False
            result["inference_verified"] = bool(result["ready"] and matching and proof.get("inference_verified"))
            if result["inference_verified"]:
                result.update(completed_tasks=proof.get("completed_tasks", []), validated_at=proof.get("validated_at"))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return result
