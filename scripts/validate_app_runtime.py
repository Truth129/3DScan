"""Run browser regressions with this interpreter and disposable synthetic data.

Example:
  .venv-rocm10-py314/bin/python scripts/validate_app_runtime.py \
      --stage .runtime/image-foundation-release-dist \
      --output .runtime/stack-upgrade/app-qa

This does not load neural networks, alter the live service, or validate clinical
segmentation quality. Browser renderer details are retained by each workflow.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import tempfile
import time
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = {
    "acquisition": ("acquisition_smoke.py", []),
    "gpu-volume-navigation": ("main_plane_smoke.py", ["--gpu-volume", "--check-context-recovery"]),
    "gpu-volume-fallback": ("main_plane_smoke.py", ["--gpu-volume", "--check-context-recovery", "--force-gpu-fallback"]),
    "direct-edit": ("direct_edit_smoke.py", []),
    "direct-edit-3d": ("direct_edit_smoke.py", ["--three-dimensional"]),
    "portable-stack": ("portable_stack_smoke.py", []),
}


def save(path: Path, report: dict) -> None:
    pending = path.with_suffix(".writing")
    pending.write_text(json.dumps(report, indent=2) + "\n")
    pending.replace(path)


def stop(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    os.killpg(process.pid, signal.SIGTERM)
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=15)


@contextmanager
def disposable_store(output: Path, processes: list[subprocess.Popen]):
    with tempfile.TemporaryDirectory(prefix="scan3d-review-regressions-", dir=output) as folder:
        try:
            yield folder
        finally:
            # Close SQLite/worker handles before removing their temporary files.
            for process in processes:
                stop(process)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--stage", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--port", type=int, default=9011)
    parser.add_argument("--driver-port", type=int, default=9521)
    parser.add_argument("--binary", default="/opt/helium-browser-bin/helium")
    parser.add_argument("--driver", default="/opt/helium-browser-bin/chromedriver")
    parser.add_argument("--workflows", nargs="+", choices=WORKFLOWS, default=list(WORKFLOWS))
    args = parser.parse_args()
    stage, output = args.stage.resolve(), args.output.resolve()
    if args.port == 9010 or args.driver_port == 9010 or args.port == args.driver_port:
        parser.error("Use distinct test ports; production port 9010 is forbidden")
    if not all((stage / name).is_file() for name in ("index.html", "offline-viewer.js")):
        parser.error("Stage must contain the main and offline builds")
    if not (output.is_relative_to(ROOT / ".runtime") or output.is_relative_to(Path(tempfile.gettempdir()))):
        parser.error("Write evidence beneath .runtime or the temporary directory")
    if output.exists() and any(output.iterdir()):
        parser.error("Output must be empty to preserve previous evidence")
    for port in (args.port, args.driver_port):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError:
                parser.error(f"Test port {port} is already in use")
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "python": sys.version,
        "executable": sys.executable,
        "stage": str(stage.relative_to(ROOT)) if stage.is_relative_to(ROOT) else str(stage),
        "build_hashes": {
            str(path.relative_to(stage)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(stage.rglob("*")) if path.is_file()
        },
        "packages": {name: metadata.version(name) for name in ("fastapi", "pydantic", "numpy", "vtk", "SimpleITK")},
        "scope": "Disposable synthetic DICOM and masks; no production calls or neural inference",
        "status": "running",
        "workflows": {},
    }
    save(output / "report.json", report)
    processes, handles = [], []
    try:
        with disposable_store(output, processes) as folder:
            env = dict(os.environ,
                       SCAN3D_DATA_DIR=folder,
                       SCAN3D_TEST_WEB_DIR=str(stage),
                       SCAN3D_IMPORT_ROOTS=str(output),
                       TOTALSEG_HOME_DIR=str(output / "totalsegmentator"),
                       XDG_CACHE_HOME=str(output / "cache"),
                       PYTHONNOUSERSITE="1",
                       PYTHONPATH=str(ROOT),
                       PATH=str(Path(sys.executable).parent) + os.pathsep + str(ROOT / ".tools/bin") + os.pathsep + os.environ.get("PATH", ""))
            for name, command in (
                ("server", [sys.executable, "-m", "uvicorn", "web.tests.interactive_test_server:create_test_app", "--factory",
                            "--host", "127.0.0.1", "--port", str(args.port)]),
                ("driver", [args.driver, f"--port={args.driver_port}", "--allowed-ips=127.0.0.1"]),
            ):
                handle = (output / f"{name}.log").open("w")
                handles.append(handle)
                processes.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=handle,
                                                  stderr=subprocess.STDOUT, start_new_session=True))
            base, driver = f"http://127.0.0.1:{args.port}", f"http://127.0.0.1:{args.driver_port}"
            for url in (base + "/api/health", driver + "/status"):
                deadline = time.monotonic() + 90
                while time.monotonic() < deadline:
                    if any(process.poll() is not None for process in processes):
                        raise RuntimeError("Test process exited; inspect server/driver logs")
                    try:
                        with urlopen(url, timeout=1) as response:
                            if url.endswith("/api/health"):
                                assert response.headers.get("X-Interactive-Test-Engine") == "true", "Refusing production API"
                        break
                    except OSError:
                        time.sleep(.25)
                else:
                    raise TimeoutError(f"Test process not ready: {url}")
            for name in args.workflows:
                script, extra = WORKFLOWS[name]
                # The direct editor appends -3d itself for its second mode.
                destination = output / ("direct-edit" if name == "direct-edit-3d" else name)
                command = [sys.executable, str(ROOT / "web/tests" / script), "--base-url", base,
                           "--driver-url", driver, "--binary", args.binary, "--output", str(destination), *extra]
                print(f"Running {name}", flush=True)
                began = time.monotonic()
                with (output / f"{name}.log").open("w") as handle:
                    result = subprocess.run(command, cwd=ROOT, env=env, stdout=handle,
                                            stderr=subprocess.STDOUT, timeout=1800)
                details = output / name / "report.json"
                report["workflows"][name] = {
                    "returncode": result.returncode,
                    "seconds": round(time.monotonic() - began, 3),
                    "report": str(details.relative_to(output)),
                }
                if details.exists():
                    report["workflows"][name]["result"] = json.loads(details.read_text())
                save(output / "report.json", report)
                result.check_returncode()
        report["status"] = "passed"
        return 0
    except Exception as exc:
        report.update(status="failed", error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        for handle in handles:
            handle.close()
        report["processes_stopped"] = all(process.poll() is not None for process in processes)
        save(output / "report.json", report)


if __name__ == "__main__":
    raise SystemExit(main())
