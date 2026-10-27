"""Isolated runtime validation; never opens the production case database.

Invoke with the runtime being tested and PYTHONPATH pointing at this checkout.
GPU commands share a harness lock, but the live worker must be idle separately.
Output directories must be new. Source CT/mask files are read only. Reports use
content hashes rather than patient filenames; local logs/artifacts remain private.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import platform
import resource
import re
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
PACKAGES = ("torch", "torchvision", "triton", "pytorch-triton-rocm", "TotalSegmentator",
            "nnunetv2", "nnInteractive", "nninteractive-client", "numpy", "scipy",
            "nibabel", "pydicom", "python-gdcm", "SimpleITK", "vtk", "fastapi", "pydantic",
            "rocm", "rocm-sdk", "rocm-sdk-core", "rocm-sdk-libraries", "rocm-sdk-device-gfx1201")


def check_output_path(path):
    """Keep benchmark artifacts outside the live store and model trees."""
    path = Path(path).resolve()
    protected = [ROOT / "data", ROOT / ".runtime" / "nninteractive", ROOT / ".runtime" / "totalsegmentator"]
    protected += [Path(os.environ[name]).expanduser().resolve() for name in
                  ("SCAN3D_NNINTERACTIVE_MODEL", "TOTALSEG_WEIGHTS_PATH", "TOTALSEG_HOME_DIR") if os.environ.get(name)]
    for directory in protected:
        directory = directory.resolve()
        if path == directory or directory in path.parents:
            raise ValueError("Benchmark output must be outside production data and model/config directories")
    return path


def sha256(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def write_json(path, value):
    path = check_output_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".writing")
    # No sensitive intermediate is created with a permissive inherited umask.
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps(value, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)


def provenance():
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = None
    # TheRock splits core, per-device libraries, and development components into
    # several distributions. Discover their exact published names dynamically.
    for distribution in metadata.distributions():
        name = distribution.metadata["Name"]
        if name and re.match(r"^(rocm|amd[-_]|hip[-_]|miopen|rccl|aotriton)", name, re.IGNORECASE):
            packages[name] = distribution.version
    return {"schema_version": 1, "checked_at": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(), "python_implementation": platform.python_implementation(),
            "kernel": platform.release(), "packages": packages,
            "harness_sha256": sha256(__file__),
            "sources_sha256": {p.name: sha256(p) for p in sorted((ROOT / "scan3d").glob("*.py"))}}


def new_output(path):
    path = check_output_path(path)
    # New directories avoid modifying artifacts from any previous runtime/run.
    path.mkdir(mode=0o700, parents=True, exist_ok=False)
    return path


def loaded_libraries():
    """Observe mapped libraries, rather than inferring which ROCm was loaded."""
    try:
        paths = set()
        for line in Path("/proc/self/maps").read_text().splitlines():
            fields = line.split(maxsplit=5)
            if len(fields) == 6 and fields[5].startswith("/") and ".so" in fields[5]:
                paths.add(fields[5])
        return {"loaded_native_libraries": sorted(paths),
                "loaded_opt_rocm_libraries": sorted(path for path in paths if path.startswith("/opt/rocm")),
                "library_environment": {key: os.environ[key] for key in
                    ("LD_LIBRARY_PATH", "LD_PRELOAD", "ROCM_PATH", "HIP_PATH", "ROCR_VISIBLE_DEVICES",
                     "HIP_VISIBLE_DEVICES", "HSA_OVERRIDE_GFX_VERSION") if key in os.environ}}
    except OSError as exc:
        return {"loaded_native_libraries": [], "library_map_error": str(exc)}


def progress(message):
    print(message, flush=True)


@contextmanager
def gpu_lock():
    path = ROOT / ".runtime" / "stack-validation-gpu.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError("Another stack-validation GPU command is running") from exc
        yield


def gpu_smoke(args):
    output = new_output(args.output)
    report = {"provenance": provenance(), "status": "running"}
    write_json(output / "report.json", report)
    try:
        import torch
        from scan3d.segmentation import _device_info
        with gpu_lock():
            info = _device_info(os.environ.get("SCAN3D_DEVICE", "gpu"))
            if "R9700" not in info.get("gpu_name", "").upper() or not info.get("hip"):
                raise RuntimeError("Refusing to test a CPU, integrated GPU, or non-ROCm runtime")
            device = torch.device("cuda:" + str(info["gpu_index"]))
            report["runtime"] = info
            try:
                report["device_arch"] = str(torch.cuda.get_device_properties(device).gcnArchName)
            except AttributeError:
                report["device_arch"] = "Not exposed by this PyTorch device-properties API"
            report["checks"] = []
            with torch.inference_mode(), torch.cuda.device(device):
                torch.manual_seed(431)
                conv = torch.nn.Conv3d(1, 8, 3, padding=1).to(device)
                values = torch.randn((1, 1, 48, 48, 48), device=device)
                for precision in ("fp32", "fp16"):
                    for iteration in range(2):
                        torch.cuda.synchronize(device)
                        started = time.perf_counter()
                        with torch.autocast("cuda", dtype=torch.float16, enabled=precision == "fp16"):
                            result = conv(values)
                        torch.cuda.synchronize(device)
                        finite = bool(torch.isfinite(result).all())
                        if not finite:
                            raise RuntimeError("Convolution produced nonfinite values")
                        report["checks"].append({"precision": precision, "iteration": iteration,
                            "seconds": time.perf_counter() - started, "finite": finite,
                            "allocated_bytes": torch.cuda.memory_allocated(device),
                            "reserved_bytes": torch.cuda.memory_reserved(device)})
            report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        report.update(loaded_libraries())
        write_json(output / "report.json", report)


def _native_cli(ct, output, profile, options, runtime):
    from scan3d import imaging, segmentation
    ct_image, affine = imaging._image(ct)
    records = []
    structures = []
    for task in options["tasks"]:
        task_dir = output / task
        task_dir.mkdir()
        native_path = output / (task + "-report.json")
        command = [sys.executable, "-u", "-m", "totalsegmentator.bin.TotalSegmentator", "-i", str(ct),
                   "-o", str(task_dir), "--task", task, "--device", runtime["selected_device"],
                   "--report", str(native_path)]
        command += ["--" + key for key in ("robust_crop", "body_seg", "remove_small_blobs") if options[key]]
        progress("Native CLI: " + task)
        started = time.perf_counter()
        resources = segmentation._Resources(track_r9700=True)
        with (output / (task + ".log")).open("wb") as log:
            process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            try:
                while process.poll() is None:
                    resources.sample(process.pid)
                    time.sleep(.2)
            except BaseException:
                segmentation._stop(process)
                raise
            finally:
                resources.sample(process.pid)
        elapsed = time.perf_counter() - started
        if process.returncode:
            raise RuntimeError(f"Native {task} failed (exit {process.returncode}); inspect its private log")
        native = json.loads(native_path.read_text())
        if native.get("device") != "gpu" or native.get("fast") or native.get("fastest") or native.get("save_lowres"):
            raise RuntimeError("Native report does not prove the requested full-resolution GPU execution")
        paths = sorted(task_dir.glob("*.nii.gz"))
        if not paths or set(native.get("classes", {}).values()) - {p.name[:-7] for p in paths}:
            raise RuntimeError("Missing native class outputs")
        validate_started = time.perf_counter()
        for path in paths:
            if imaging._binary_nonempty(path, ct_image, affine):
                name = path.name[:-7]
                group, color = segmentation._appearance(name, task)
                structures.append({"id": task + ":" + name, "task": task, "name": name.replace("_", " ").capitalize(),
                                   "group": group, "color": color, "mask_file": str(path)})
        records.append({"task": task, "elapsed_seconds": elapsed, "runtime_seconds": native.get("runtime_seconds"),
                        "mask_validation_seconds": time.perf_counter() - validate_started,
                        "resources": resources.result()})
    return structures, records


def segment(args):
    output = new_output(args.output)
    report = {"provenance": provenance(), "status": "running", "mode": args.mode,
              "profile": args.profile, "modality": args.modality}
    write_json(output / "report.json", report)
    try:
        from scan3d import segmentation
        from scan3d.segmentation_options import normalize_options
        ct = args.ct.resolve()
        source_hash = sha256(ct)
        args.tasks = args.tasks or (["total_mr"] if args.modality == "MR" else ["total", "lung_vessels"])
        options = normalize_options(args.profile, {"tasks": args.tasks, "robust_crop": args.robust_crop,
                      "body_seg": args.body_seg, "remove_small_blobs": args.remove_small_blobs}, modality=args.modality)
        report.update(source_ct_sha256=source_hash, options=options)
        with gpu_lock():
            runtime = segmentation._device_info(os.environ.get("SCAN3D_DEVICE", "gpu"))
            if "R9700" not in runtime.get("gpu_name", "").upper() or not runtime.get("hip"):
                raise RuntimeError("Real inference requires the explicitly selected R9700 ROCm device")
            report["runtime"] = runtime
            started = time.perf_counter()
            if args.mode == "wrapper":
                structures = segmentation.segment(ct, output / "inference", args.profile, progress, lambda: False, options,
                                                  modality=args.modality)
                manifest = json.loads((output / "inference" / "inference.json").read_text())
                mask_root = output / "inference" / manifest["run_id"]
                report["tasks"] = manifest["tasks"]
            else:
                mask_root = output / "inference"
                mask_root.mkdir()
                structures, report["tasks"] = _native_cli(ct, mask_root, args.profile, options, runtime)
            report["end_to_end_seconds"] = time.perf_counter() - started
            report["model_fingerprint"] = segmentation.model_fingerprint(args.profile, options, modality=args.modality)
            if not report["model_fingerprint"]["weights_complete"]:
                raise RuntimeError("The exact model checkpoints could not be identified")
            report["masks"] = {task + ":" + p.name[:-7]: str(p.relative_to(output))
                               for task in args.tasks for p in sorted((mask_root / task).glob("*.nii.gz"))}
            report["structures"] = [{**s, "mask_file": str(Path(s["mask_file"]).relative_to(output))} for s in structures]
            report["source_unchanged"] = sha256(ct) == source_hash
            if not report["source_unchanged"]:
                raise RuntimeError("Source CT content changed during validation")
            report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        report.update(loaded_libraries())
        report["peak_process_rss_kib"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        write_json(output / "report.json", report)


def compare_masks(left, right):
    import numpy as np
    from scan3d.imaging import _binary_values
    a, ai, affine, _ = _binary_values(Path(left))
    b, bi, other, _ = _binary_values(Path(right))
    if ai.shape != bi.shape or not np.allclose(affine, other, rtol=0, atol=1e-4):
        raise ValueError("Compared mask grids differ")
    count_a = count_b = intersection = changed = 0
    for offset in range(0, a.shape[0], 16):
        x, y = a[offset:offset + 16], b[offset:offset + 16]
        count_a += int(np.count_nonzero(x))
        count_b += int(np.count_nonzero(y))
        intersection += int(np.count_nonzero(np.logical_and(x, y)))
        changed += int(np.count_nonzero(x != y))
    voxel_ml = abs(float(np.linalg.det(affine[:3, :3]))) / 1000
    return {"left_voxels": count_a, "right_voxels": count_b, "changed_voxels": changed,
            "dice": 2 * intersection / (count_a + count_b) if count_a + count_b else 1.,
            "left_volume_ml": count_a * voxel_ml, "right_volume_ml": count_b * voxel_ml,
            "volume_difference_ml": (count_b - count_a) * voxel_ml,
            "relative_volume_difference": (count_b - count_a) / count_a if count_a else None,
            "grid_matches": True}


def compare(args):
    left = json.loads((args.left / "report.json").read_text())
    right = json.loads((args.right / "report.json").read_text())
    if left.get("modality", "CT") != right.get("modality", "CT"):
        raise ValueError("Cannot compare different source modalities")
    for field in ("source_ct_sha256", "options", "profile"):
        if left[field] != right[field]:
            raise ValueError("Cannot compare different " + field)
    if left["status"] != "passed" or right["status"] != "passed":
        raise ValueError("Both inference runs must have passed")
    # Runtime/package versions intentionally differ; checkpoint bytes must not.
    def weights(report):
        return report["model_fingerprint"]["models"]
    if weights(left) != weights(right):
        raise ValueError("Model configurations/checkpoint hashes differ")
    if left["masks"].keys() != right["masks"].keys():
        raise ValueError("Class label sets differ")
    entries = {name: compare_masks(args.left / path, args.right / right["masks"][name])
               for name, path in left["masks"].items()}
    report = {"schema_version": 1, "status": "completed", "source_ct_sha256": left["source_ct_sha256"],
              "left_provenance": left["provenance"], "right_provenance": right["provenance"],
              "left_seconds": left["end_to_end_seconds"], "right_seconds": right["end_to_end_seconds"],
              "labels": entries, "all_voxels_identical": all(e["changed_voxels"] == 0 for e in entries.values()),
              "minimum_dice": min(e["dice"] for e in entries.values()),
              "maximum_absolute_volume_difference_ml": max(abs(e["volume_difference_ml"]) for e in entries.values()),
              "interpretation": "Numerical agreement only; no automatic clinical accuracy or promotion threshold."}
    write_json(args.output, report)


def seed_points(mask, affine):
    """Find two reproducible foreground seeds without a full-volume argwhere."""
    import numpy as np
    from scipy.ndimage import distance_transform_edt
    found = np.flatnonzero(mask.any(axis=(0, 1)))
    if not len(found):
        raise ValueError("A nonempty baseline mask is required")
    seeds = []
    for fraction in (.4, .6):
        z = int(found[round((len(found) - 1) * fraction)])
        distance = distance_transform_edt(mask[:, :, z], sampling=np.linalg.norm(affine[:3, :2], axis=0))
        x, y = np.unravel_index(np.argmax(distance), distance.shape)
        seeds.append([int(x), int(y), z])
    return seeds


def interactive(args):
    output = new_output(args.output)
    report = {"status": "running", "provenance": provenance(), "steps": []}
    write_json(output / "report.json", report)
    runtime = None
    try:
        import numpy as np
        from scan3d import imaging
        from scan3d.interactive import InteractiveRuntime
        ct, baseline = args.ct.resolve(), args.mask.resolve()
        hashes = [sha256(ct), sha256(baseline)]
        image, affine = imaging._image(ct)
        mask, _, _ = imaging._binary(baseline, image, affine)
        seeds = seed_points(mask, affine)
        del mask
        prompts = [{"id": "runtime-seed-" + str(i), "kind": "point", "include": True,
                    "ras": (affine @ np.r_[seed, 1])[:3].tolist()} for i, seed in enumerate(seeds)]
        report.update(source_ct_sha256=hashes[0], baseline_mask_sha256=hashes[1], seed_voxels=seeds)
        runtime = InteractiveRuntime()
        with gpu_lock():
            for name, selected in (("first", prompts[:1]), ("repeat", prompts),
                                   ("unchanged", prompts), ("undo_all", [])):
                started = time.perf_counter()
                result = runtime.predict(ct, baseline, selected, output / (name + ".nii.gz"),
                                         progress, lambda: False, "isolated-runtime-validation")
                report["steps"].append({"name": name, "wall_seconds": time.perf_counter() - started,
                                        "provenance": result})
                write_json(output / "report.json", report)
            unchanged = compare_masks(output / "repeat.nii.gz", output / "unchanged.nii.gz")
            undo = compare_masks(baseline, output / "undo_all.nii.gz")
            if unchanged["changed_voxels"] or undo["changed_voxels"]:
                raise RuntimeError("Repeated save or undo did not preserve exact mask voxels")
            if (not report["steps"][1]["provenance"]["reused_prompt_prefix"] or
                    report["steps"][1]["provenance"]["predictions_this_request"] != 1):
                raise RuntimeError("Second prompt did not reuse the warm session")
            if report["steps"][2]["provenance"]["predictions_this_request"] != 0:
                raise RuntimeError("An unchanged prompt list unnecessarily repeated inference")
            report.update(unchanged_save=unchanged, exact_undo=undo, source_hashes_unchanged=hashes == [sha256(ct), sha256(baseline)])
            if not report["source_hashes_unchanged"]:
                raise RuntimeError("Source CT/baseline mask changed")
            report["status"] = "passed"
    except BaseException as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        active_error = sys.exc_info()[0] is not None
        try:
            if runtime is not None:
                runtime.release()
        except Exception as exc:
            report.update(status="failed", cleanup_error=str(exc))
            if not active_error:
                raise
        finally:
            report.update(loaded_libraries())
            write_json(output / "report.json", report)


def postprocess(args):
    output = new_output(args.output)
    report = {"status": "running", "provenance": provenance(), "builds": []}
    write_json(output / "report.json", report)
    try:
        from scan3d.config import Settings
        from scan3d.service import Service
        source = json.loads((args.segment / "report.json").read_text())
        if source["status"] != "passed" or source["source_ct_sha256"] != sha256(args.ct):
            raise ValueError("The completed segmentation and CT do not match")
        structures = [{**s, "mask_file": str((args.segment / s["mask_file"]).resolve())} for s in source["structures"]]
        hashes = {s["id"]: sha256(s["mask_file"]) for s in structures}
        settings = Settings(output / "store", (ROOT,), web_dir=args.web_dir.resolve())
        service = Service(settings)
        case = service.new_case("Isolated runtime verification")
        report["source_ct_sha256"] = source["source_ct_sha256"]
        for phase in ("cold", "warm"):
            revision = service.build_revision(case["id"], args.ct.resolve(), structures, "runtime_validation")
            service.store.commit_revision(revision)
            report["builds"].append({"phase": phase, "timings": revision["processing_timings"],
                                    "mesh_statistics": revision["mesh_statistics"],
                                    "measurements": {s["id"]: {k: s[k] for k in ("voxel_count", "volume_ml")} for s in revision["structures"]},
                                    "model_bytes": (Path(revision["directory"]) / "model.glb").stat().st_size})
            write_json(output / "report.json", report)
        if report["builds"][0]["measurements"] != report["builds"][1]["measurements"]:
            raise RuntimeError("Cold/warm voxel measurements differ")
        if args.detailed:
            started = time.perf_counter()
            target = service.ensure_detailed(case["id"], revision["id"])
            report["detailed"] = {"seconds": time.perf_counter() - started, "bytes": target.stat().st_size}
        if args.export_html:
            from scan3d.portable_export import ExportRequest, prepare_export
            started = time.perf_counter()
            plan = prepare_export(revision, case["id"], settings, ExportRequest())
            exported = output / "viewer.html"
            with exported.open("wb") as stream:
                for chunk in plan.chunks():
                    stream.write(chunk)
            if exported.stat().st_size != plan.byte_length:
                raise RuntimeError("Portable HTML size differs from its export plan")
            report["html_export"] = {"seconds": time.perf_counter() - started,
                                     "bytes": exported.stat().st_size, "layers": ["segmentation"]}
        if hashes != {s["id"]: sha256(s["mask_file"]) for s in structures}:
            raise RuntimeError("Postprocessing changed a source mask")
        if source["source_ct_sha256"] != sha256(args.ct):
            raise RuntimeError("Postprocessing changed the source CT")
        report.update(status="passed", source_masks_unchanged=True)
    except BaseException as exc:
        report.update(status="failed", error=str(exc))
        raise
    finally:
        report.update(loaded_libraries())
        write_json(output / "report.json", report)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("provenance")
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(run=lambda a: write_json(a.output, provenance()))
    p = commands.add_parser("gpu")
    p.add_argument("--output", type=Path, required=True)
    p.set_defaults(run=gpu_smoke)
    p = commands.add_parser("segment")
    p.add_argument("--ct", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--mode", choices=("wrapper", "cli"), default="wrapper")
    p.add_argument("--profile", default="chest_abdomen")
    p.add_argument("--modality", choices=("CT", "MR"), default="CT")
    p.add_argument("--tasks", nargs="+", help="Default: total + lung_vessels for CT, total_mr for MRI")
    for flag in ("robust-crop", "body-seg", "remove-small-blobs"):
        p.add_argument("--" + flag, action="store_true")
    p.set_defaults(run=segment)
    p = commands.add_parser("compare")
    for flag in ("left", "right", "output"):
        p.add_argument("--" + flag, type=Path, required=True)
    p.set_defaults(run=compare)
    p = commands.add_parser("interactive")
    for flag in ("ct", "mask", "output"):
        p.add_argument("--" + flag, type=Path, required=True)
    p.set_defaults(run=interactive)
    p = commands.add_parser("postprocess")
    for flag in ("ct", "segment", "output"):
        p.add_argument("--" + flag, type=Path, required=True)
    p.add_argument("--detailed", action="store_true")
    p.add_argument("--export-html", action="store_true")
    p.add_argument("--web-dir", type=Path, default=ROOT / "web" / "dist")
    p.set_defaults(run=postprocess)
    args = parser.parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
