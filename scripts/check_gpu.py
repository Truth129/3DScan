"""Exercise a GPU with a 3D convolution. This does not certify model inference."""

import argparse
from datetime import datetime, timezone
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=Path(os.environ.get("SCAN3D_GPU_PROOF", str(Path(__file__).resolve().parents[1] / ".runtime/gpu-proof.json")))
    )
    args = parser.parse_args()
    result = {
        "gpu_ready": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "checks": {},
        "errors": [],
    }
    try:
        import torch

        result["checks"].update(
            torch_version=torch.__version__,
            torch_distribution_version=metadata.version("torch"),
            hip_version=torch.version.hip,
            cuda_version=torch.version.cuda,
            gpu_available=torch.cuda.is_available(),
        )
        runtime_versions = {"torch": metadata.version("torch")}
        for package in ("rocm", "rocm-sdk-core", "rocm-sdk-libraries"):
            try:
                runtime_versions[package] = metadata.version(package)
            except metadata.PackageNotFoundError:
                pass
        result["checks"]["runtime_versions"] = runtime_versions
        if not torch.cuda.is_available():
            raise RuntimeError("PyTorch cannot access a GPU. Check the driver, device permissions and PyTorch build.")
        devices = [
            {"index": i, "name": torch.cuda.get_device_name(i), "memory_bytes": torch.cuda.get_device_properties(i).total_memory}
            for i in range(torch.cuda.device_count())
        ]
        result["checks"]["devices"] = devices
        from scan3d.segmentation import _device_info

        selected = _device_info("gpu")["gpu_index"]
        device = torch.device(f"cuda:{selected}")
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
        result["checks"]["recommended_device"] = f"gpu:{selected}"
        result["gpu_ready"] = True
    except Exception as exc:
        result["errors"].append(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    pending = args.output.with_suffix(".json.writing")
    pending.write_text(json.dumps(result, indent=2) + "\n")
    pending.replace(args.output)
    print(json.dumps(result, indent=2))
    return 0 if result["gpu_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
