"""Exercise HIP and 3D convolution. This does not certify model inference."""
import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
from pathlib import Path
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / ".runtime/gpu-proof.json")
    args = parser.parse_args()
    result = {"gpu_ready": False, "inference_verified": False, "model_inference_proven": False,
              "checked_at": datetime.now(timezone.utc).isoformat(), "checks": {}, "errors": []}
    result["checks"]["kfd"] = Path("/dev/kfd").exists()
    result["checks"]["render_nodes"] = [str(p) for p in Path("/dev/dri").glob("renderD*")]
    try:
        import torch
        result["checks"].update(torch_version=torch.__version__, torch_distribution_version=metadata.version("torch"),
                                hip_version=torch.version.hip,
                                gpu_available=torch.cuda.is_available())
        if not torch.version.hip:
            raise RuntimeError("This environment does not contain a ROCm PyTorch build.")
        if not torch.cuda.is_available():
            raise RuntimeError("ROCm PyTorch cannot access an AMD GPU. Check host device access and runtime.")
        devices = [{"index": i, "name": torch.cuda.get_device_name(i),
                    "memory_bytes": torch.cuda.get_device_properties(i).total_memory}
                   for i in range(torch.cuda.device_count())]
        result["checks"]["devices"] = devices
        discrete = next((d for d in devices if "R9700" in d["name"]), None)
        if discrete is None:
            raise RuntimeError("R9700 not found; refusing to validate on the integrated GPU.")
        device = torch.device(f"cuda:{discrete['index']}")
        started = time.monotonic()
        with torch.inference_mode():
            conv = torch.nn.Conv3d(1, 4, 3, padding=1).to(device)
            values = torch.randn((1, 1, 32, 32, 32), device=device)
            full = conv(values)
            with torch.autocast(device_type="cuda", dtype=torch.float16):
                mixed = conv(values)
            torch.cuda.synchronize(device)
            if not bool(torch.isfinite(full).all()) or not bool(torch.isfinite(mixed).all()):
                raise RuntimeError("3D convolution produced nonfinite values.")
        result["checks"]["conv3d_seconds"] = round(time.monotonic() - started, 3)
        result["checks"]["recommended_device"] = f"gpu:{discrete['index']}"
        result["gpu_ready"] = True
    except Exception as exc:
        result["errors"].append(str(exc))
        if isinstance(exc, ImportError) and ".so" in str(exc):
            result["native_dependency_action"] = "sudo pacman -S --needed rocm-ml-libraries hipsparselt rocm-smi-lib"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pending = args.output.with_suffix(".json.writing")
    pending.write_text(json.dumps(result, indent=2) + "\n")
    pending.replace(args.output)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(json.dumps(result, indent=2))
        print("Model inference remains unproven until a representative CT completes segmentation and QC.")
    return 0 if result["gpu_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
