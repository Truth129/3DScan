"""Exercise a GPU with a 3D convolution. This does not certify model inference."""

import argparse
from datetime import datetime, timezone
from importlib import import_module, metadata
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys
import sysconfig
import time

# An added runtime holds no scan3d install, and Python puts only scripts/ on the path. Without
# this a healthy runtime would write a failing proof.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scan3d.config import ROOT, Settings, device_request, load_environment
from scan3d.doctor import proof_path
from scan3d.inference_paths import _WITHHELD, DEFAULT, GPU_REQUEST, ID_PATTERN, directory, gpu_cards, installed

# What a child on this runtime imports at the top of its entry point, and the two engines. They
# fail here, in a check, and not at the first job.
RUNTIME_IMPORTS = (
    "scan3d.segmentation",
    "scan3d.interactive",
    "scan3d.interactive_worker",
    "totalsegmentator.python_api",
    "nnInteractive.inference.inference_session",
)


def jobs_waiting():
    """Whether any job is queued or running, by the same rule as the license route.

    A proof taken beside inference proves little and competes for the card. The connection is
    read-only and selects no case content. A library with no database has no jobs, and nothing
    else counts as none: this answer guards a rebuild, so a database that is there and cannot be
    read has to raise. `Path.is_file()` would call that absent, and so would following a link.

    A plain missing directory is a fresh install. A link on the way to it whose target has gone
    is a library this shell cannot see, such as an unmounted disk, and a service may still hold it
    open. Settings has already resolved that into a plain missing directory, so read it as written.
    """
    written = Path(os.environ.get("SCAN3D_DATA_DIR", ROOT / "data"))
    for place in (written, *written.parents):
        if place.is_symlink() and not place.exists():
            raise FileNotFoundError(f"{place} is a link whose target has gone")
    database = Settings.from_env().data_dir / "scan3d.sqlite3"
    try:
        database.lstat()
    except FileNotFoundError:
        return False
    connection = sqlite3.connect(f"{database.as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT 1 FROM jobs WHERE status IN ('queued','running') LIMIT 1").fetchone() is not None
    finally:
        connection.close()


def main():
    load_environment()
    parser = argparse.ArgumentParser(description=__doc__)
    destination = parser.add_mutually_exclusive_group()
    destination.add_argument("--output", type=Path, help="proof file for the default path; the default is SCAN3D_GPU_PROOF")
    destination.add_argument("--path", metavar="ID", help="record this interpreter as the added inference path ID, instead of the default path")
    destination.add_argument("--queue", action="store_true", help="only read the job queue: exit 0 when it is empty, and 1 when it is busy or unreadable")
    parser.add_argument("--device", help="gpu or gpu:N; N counts within this PyTorch build, so it names a different card per build")
    args = parser.parse_args()
    if args.path is not None and (re.fullmatch(ID_PATTERN, args.path) is None or args.path == DEFAULT):
        parser.error(f"--path must match {ID_PATTERN} and cannot be {DEFAULT}")
    if args.device is not None and re.fullmatch(GPU_REQUEST, args.device) is None:
        parser.error("--device must be gpu or gpu:N")
    try:
        waiting = jobs_waiting()
    except (OSError, ValueError, sqlite3.Error) as exc:  # ValueError is a setting Settings refuses.
        print(f"Could not read the job queue, so no GPU check ran: {exc}", file=sys.stderr)
        return 1
    if waiting:
        print("Jobs are queued or running, so no GPU check ran. Run it again when the queue is empty.", file=sys.stderr)
        return 1
    # The install script asks this with a bare Python, which has nothing the rest needs.
    if args.queue:
        return 0
    # A job on this path never gets these, so a proof taken with them measures something no job
    # sees. The loader has read the two LD_ names already, so for those this reaches only children.
    if args.path:
        for name in _WITHHELD:
            os.environ.pop(name, None)
    # SCAN3D_DEVICE belongs to the default path: gpu:N counts within one build, so it says nothing
    # about an added runtime. A GPU check needs a GPU, so auto and cpu mean the best one.
    configured = "gpu" if args.path else device_request()
    request = args.device or (configured if configured.startswith("gpu") else "gpu")
    # Read the packages the way discovery re-reads them, from this site-packages alone.
    site = sysconfig.get_path("purelib")

    def version(package):
        return installed(site, package) if args.path else metadata.version(package)

    result = {
        "gpu_ready": False,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "python_version": platform.python_version(),
        "checks": {},
        "errors": [],
    }
    # A failed check still leaves a record, so the app can show the path with its reason.
    path_record = {"id": args.path, "python": sys.executable, "site_packages": site, "device": request, "kind": None, "gpu_name": None, "memory_bytes": None}
    if args.path:
        result["path"] = path_record
    try:
        import torch

        path_record["kind"] = "cuda" if torch.version.cuda else "rocm" if torch.version.hip else None
        result["checks"].update(
            torch_version=torch.__version__,
            torch_distribution_version=version("torch"),
            hip_version=torch.version.hip,
            cuda_version=torch.version.cuda,
            gpu_available=torch.cuda.is_available(),
            hardware=gpu_cards(path_record["kind"]),
        )
        runtime_versions = {"torch": version("torch")}
        for package in ("rocm", "rocm-sdk-core", "rocm-sdk-libraries", "cuda-toolkit", "nvidia-cudnn-cu13"):
            try:
                runtime_versions[package] = version(package)
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

        chosen = _device_info(request)
        selected = chosen["gpu_index"]
        path_record.update(gpu_name=chosen["gpu_name"], memory_bytes=devices[selected]["memory_bytes"])
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
        if args.path:
            for module in RUNTIME_IMPORTS:
                import_module(module)
        result["checks"]["recommended_device"] = f"gpu:{selected}"
        result["gpu_ready"] = True
    except Exception as exc:
        result["errors"].append(str(exc))
    output = directory() / f"{args.path}.json" if args.path else args.output or proof_path()
    output.parent.mkdir(parents=True, exist_ok=True)
    pending = output.with_suffix(".json.writing")
    pending.write_text(json.dumps(result, indent=2) + "\n")
    pending.replace(output)
    print(json.dumps(result, indent=2))
    return 0 if result["gpu_ready"] else 1


if __name__ == "__main__":
    sys.exit(main())
