"""Full-resolution, explicitly selected-device TotalSegmentator worker adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from importlib import metadata
from functools import lru_cache
from pathlib import Path
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
import uuid

import numpy as np

from .imaging import _binary, _image


class SegmentationCancelled(InterruptedError):
    pass


_LOBES = ("lung_lower_lobe_left", "lung_lower_lobe_right", "lung_middle_lobe_right",
          "lung_upper_lobe_left", "lung_upper_lobe_right")
_LUNG = {"lung_arteries", "lung_veins", "lung_airways", "lung_airways_wall"}


def _appearance(name):
    if "vein" in name or "vena" in name:
        return "Veins", "#4d82e8"
    if "arter" in name or "aorta" in name or name == "brachiocephalic_trunk":
        return "Arteries", "#eb655d"
    if "airway" in name or name == "trachea":
        return "Airways", "#f1dfb1"
    if name.startswith("lung_"):
        colors = ["#dca6d0", "#ad9ed8", "#e7ad8f", "#b5d59a", "#8fc3d7"]
        return "Lung lobes", colors[_LOBES.index(name)] if name in _LOBES else "#91cbd9"
    if name.startswith(("rib_", "vertebrae_", "clavicula", "scapula", "hip_", "humerus_", "femur_")) or name in {"skull", "sacrum", "sternum", "costal_cartilages"}:
        return "Bones", "#d8cdb1"
    if name in {"heart", "atrial_appendage_left", "spinal_cord", "esophagus", "thyroid_gland"}:
        return "Chest organs", "#ce94a7"
    colors = ["#d88a72", "#ad91d6", "#a6c682", "#e3bb78", "#7ebbb7", "#c092b8"]
    index = int.from_bytes(hashlib.sha256(name.encode()).digest()[:2], "little") % len(colors)
    if name == "brain":
        return "Head organs", colors[index]
    if name.startswith(("gluteus_", "autochthon_", "iliopsoas_")):
        return "Muscles", colors[index]
    return "Abdominal organs", colors[index]


def _device_info(device):
    import torch

    if device == "cpu":
        return {"device": "cpu", "selected_device": "cpu", "torch": torch.__version__, "explicit_cpu": True}
    if device != "gpu" and not (device.startswith("gpu:") and device[4:].isdigit()):
        raise ValueError("SCAN3D_DEVICE must be gpu, gpu:N, or an explicitly selected cpu")
    if not torch.cuda.is_available():
        raise RuntimeError("GPU inference was requested but PyTorch cannot access a GPU; verify the ROCm/CUDA setup")
    if ":" in device:
        index = int(device[4:])
    else:
        candidates = [i for i in range(torch.cuda.device_count())
                      if "R9700" in torch.cuda.get_device_name(i).upper()]
        if not candidates:
            raise RuntimeError("Default R9700 GPU was not enumerated; verify ROCm or explicitly configure SCAN3D_DEVICE=gpu:N")
        index = candidates[0]
    if index >= torch.cuda.device_count():
        raise ValueError("Requested GPU index is unavailable")
    return {"device": device, "selected_device": f"gpu:{index}", "torch": torch.__version__, "gpu_index": index,
            "gpu_name": torch.cuda.get_device_name(index), "hip": torch.version.hip,
            "cuda": torch.version.cuda}


def _digest(path):
    sha = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            sha.update(block)
    return sha.hexdigest()


@lru_cache(maxsize=256)
def _checkpoint_digest(path, size, modified_ns, changed_ns):
    # The stat tuple invalidates this small in-process cache when a checkpoint
    # is downloaded/replaced. Checkpoint bytes are hashed once per file version.
    return _digest(Path(path))


def model_fingerprint(profile: str) -> dict:
    """Resolved model/checkpoint identity for provenance and safe cache reuse.

    This does not download or import a model or initialize a GPU. An incomplete
    fingerprint cannot authorize a cache hit. Recompute after the first run,
    once TotalSegmentator has downloaded its model files.
    """
    if profile not in {"chest", "abdomen", "chest_abdomen"}:
        raise ValueError("Unknown segmentation profile")
    from totalsegmentator.map_tasks_config import DEFAULT_CONFIG, TASK_CONFIGS

    versions = {}
    for package in ("TotalSegmentator", "nnunetv2", "torch"):
        try:
            versions[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            versions[package] = "unavailable"
    if os.environ.get("TOTALSEG_WEIGHTS_PATH"):
        base = Path(os.environ["TOTALSEG_WEIGHTS_PATH"]).expanduser()
    else:
        home = Path(os.environ.get("TOTALSEG_HOME_DIR", str(Path.home() / ".totalsegmentator")))
        base = home / "nnunet" / "results"
    tasks = ["total"] + (["lung_vessels"] if profile != "abdomen" else [])
    configurations = []
    for task in tasks:
        raw = TASK_CONFIGS[task]
        config = dict(DEFAULT_CONFIG)
        config.update(raw["sub_modes"]["default"] if "sub_modes" in raw else raw)
        config["plans"] = config.get("plans") or "nnUNetPlans"
        configurations.append((task, config))
        if config.get("crop") and config.get("crop_model") is None:
            mode = "fast" if config.get("robust_crop") else "fastest"
            crop = dict(DEFAULT_CONFIG)
            crop.update(TASK_CONFIGS["total"]["sub_modes"][mode])
            crop["plans"] = crop.get("plans") or "nnUNetPlans"
            configurations.append((task + ":crop", crop))
    models = []
    for task, config in configurations:
        ids = config["task_id"] if isinstance(config["task_id"], list) else [config["task_id"]]
        for dataset_id in ids:
            descriptor = {"task": task, "dataset_id": dataset_id, "trainer": config["trainer"],
                          "configuration": config["model"], "plans": config["plans"],
                          "folds": config["folds"], "resample_mm": config.get("resample"), "files": []}
            datasets = [p for p in base.glob(f"Dataset{dataset_id:03d}*")
                        if p.is_dir() and p.name.split("_", 1)[0] == f"Dataset{dataset_id:03d}"]
            if len(datasets) == 1:
                folder = datasets[0] / f"{config['trainer']}__{config['plans']}__{config['model']}"
                required = [folder / "plans.json", folder / "dataset.json"]
                required += [folder / f"fold_{fold}" / "checkpoint_final.pth" for fold in config["folds"]]
                for path in required:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                    descriptor["files"].append({"name": path.relative_to(base).as_posix(), "bytes": stat.st_size,
                                                "sha256": _checkpoint_digest(str(path.resolve()), stat.st_size,
                                                                             stat.st_mtime_ns, stat.st_ctime_ns)})
                descriptor["complete"] = len(descriptor["files"]) == len(required)
            else:
                descriptor["complete"] = False
            models.append(descriptor)
    result = {"schema_version": 1, "versions": versions, "profile": profile,
              "device_request": os.environ.get("SCAN3D_DEVICE", "gpu"), "fast": False, "fastest": False,
              "wrapper_sha256": _digest(Path(__file__)), "models": models,
              "weights_complete": bool(models) and all(model["complete"] for model in models)}
    result["digest"] = hashlib.sha256(json.dumps(result, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return result


def _stop(process):
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
    except ProcessLookupError:
        pass


class _Resources:
    """Optional Linux process-tree RSS and whole-discrete-device VRAM samples."""

    def __init__(self, track_r9700=False):
        self.rss_peak = None
        self.vram_path = None
        self.vram_baseline = None
        self.vram_peak = None
        self.pci_device = None
        if track_r9700:
            for device in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
                try:
                    if (device / "device").read_text().strip().lower() == "0x7551":
                        candidate = device / "mem_info_vram_used"
                        value = int(candidate.read_text().strip())
                        self.vram_path = candidate
                        self.pci_device = device.resolve().name
                        self.vram_baseline = self.vram_peak = value
                        break
                except (OSError, ValueError):
                    continue

    def sample(self, pid=None):
        if pid is not None:
            try:
                pending = [pid]
                visited = set()
                resident = 0
                while pending:
                    child = pending.pop()
                    if child in visited:
                        continue
                    visited.add(child)
                    try:
                        resident += int((Path("/proc") / str(child) / "statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
                        pending.extend(int(value) for value in (Path("/proc") / str(child) / "task" / str(child) / "children").read_text().split())
                    except (OSError, ValueError, IndexError):
                        continue
                if resident:
                    self.rss_peak = max(self.rss_peak or 0, resident)
            except (OSError, ValueError, AttributeError):
                pass
        if self.vram_path:
            try:
                self.vram_peak = max(self.vram_peak, int(self.vram_path.read_text().strip()))
            except (OSError, ValueError):
                pass

    def result(self):
        return {"sample_interval_seconds": 0.2, "worker_tree_rss_peak_bytes": self.rss_peak,
                "rss_scope": "Sampled sum of child process-tree RSS; shared pages may be counted more than once; excludes API parent",
                "gpu_pci_device": self.pci_device, "whole_device_vram_baseline_bytes": self.vram_baseline,
                "whole_device_vram_peak_bytes": self.vram_peak,
                "whole_device_vram_peak_above_baseline_bytes": (self.vram_peak - self.vram_baseline
                                                               if self.vram_peak is not None else None),
                "vram_scope": "Sampled whole R9700 device use via AMD sysfs; includes graphics and other processes; not per-process allocation"}


def segment(ct_path: Path, output_dir: Path, profile: str, progress, cancelled) -> list[dict]:
    if profile not in {"chest", "abdomen", "chest_abdomen"}:
        raise ValueError("Unknown segmentation profile")
    ct_path = Path(ct_path).resolve()
    ct, affine = _image(ct_path)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if cancelled():
        raise SegmentationCancelled("Segmentation cancelled")
    device = os.environ.get("SCAN3D_DEVICE", "gpu").strip()
    runtime = _device_info(device)
    effective_device = runtime["selected_device"]
    tasks = ["total"] + (["lung_vessels"] if profile != "abdomen" else [])
    run_dir = output_dir / ("run-" + uuid.uuid4().hex)
    run_dir.mkdir()
    manifest = {"schema_version": 1, "engine": "TotalSegmentator",
                "engine_version": metadata.version("TotalSegmentator"), "resolution": "standard_full",
                "profile": profile, "output_selection": "all_nonempty_task_outputs",
                "input_sha256": _digest(ct_path), "runtime": runtime,
                "started_at": datetime.now(timezone.utc).isoformat(), "status": "running",
                "run_id": run_dir.name, "tasks": [], "empty_labels": [], "qc": []}
    manifest_path = output_dir / "inference.json"

    def save_manifest():
        content = json.dumps(manifest, indent=2) + "\n"
        for destination in (run_dir / "inference.json", manifest_path):
            pending = destination.with_suffix(".json.writing")
            pending.write_text(content)
            pending.replace(destination)

    save_manifest()
    structures = []
    try:
        for task in tasks:
            if cancelled():
                raise SegmentationCancelled("Segmentation cancelled")
            progress(f"Running full-resolution TotalSegmentator: {task} ({device})")
            task_dir = run_dir / task
            task_dir.mkdir(exist_ok=True)
            if list(task_dir.glob("*.nii*")):
                raise ValueError("Inference output already exists; use a new run directory")
            command = [sys.executable, "-m", "totalsegmentator.bin.TotalSegmentator",
                       "-i", str(ct_path), "-o", str(task_dir), "--task", task, "--device", effective_device,
                       "--report", str(run_dir / f"{task}-report.json")]
            record = {"task": task, "device": effective_device, "fast": False, "status": "running"}
            manifest["tasks"].append(record)
            save_manifest()
            started = time.monotonic()
            resources = _Resources(track_r9700="R9700" in runtime.get("gpu_name", "").upper())
            with (run_dir / f"{task}.log").open("wb") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    while process.poll() is None:
                        resources.sample(getattr(process, "pid", None))
                        if cancelled():
                            _stop(process)
                            raise SegmentationCancelled("Segmentation cancelled")
                        time.sleep(0.2)
                except BaseException:
                    _stop(process)
                    raise
                finally:
                    resources.sample(getattr(process, "pid", None))
                    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    record["resources"] = resources.result()
            record["returncode"] = process.returncode
            if process.returncode:
                record["status"] = "failed"
                raise RuntimeError(f"TotalSegmentator {task} failed (exit {process.returncode}); inspect the local worker log")
            report_path = run_dir / f"{task}-report.json"
            if not report_path.is_file():
                raise RuntimeError("TotalSegmentator did not write its required reproducibility report; verify the pinned worker version")
            native = json.loads(report_path.read_text())
            if effective_device.startswith("gpu") and native.get("device") != "gpu":
                raise RuntimeError("TotalSegmentator did not complete on the requested GPU; CPU fallback results were rejected")
            if native.get("fast") or native.get("fastest") or native.get("save_lowres"):
                raise RuntimeError("TotalSegmentator returned an unexpected reduced-resolution result")
            record["native_report"] = report_path.name
            record["runtime_seconds"] = native.get("runtime_seconds")
            record["nnunetv2_version"] = native.get("nnunetv2_version")
            masks = {path.name[:-7]: path for path in sorted(task_dir.glob("*.nii.gz"))}
            if not masks:
                raise RuntimeError(f"TotalSegmentator {task} produced no masks")
            if set(native.get("classes", {}).values()) - set(masks):
                raise RuntimeError(f"TotalSegmentator {task} did not save every declared class mask")
            if task == "lung_vessels" and not _LUNG.issubset(masks):
                raise RuntimeError("This lung_vessels model does not provide the required split artery/vein/airway outputs; verify the pinned TotalSegmentator version")
            record["labels"] = sorted(masks)
            record["status"] = "completed"
            progress(f"Checking {task} mask geometry and labels")
            for name, path in masks.items():
                if cancelled():
                    raise SegmentationCancelled("Segmentation cancelled")
                # Profiles select tasks, never an anatomical subset of their outputs.
                mask, _, _ = _binary(path, ct, affine)
                if not np.any(mask):
                    stable_id = f"{task}:{name}"
                    manifest["empty_labels"].append(stable_id)
                    manifest["qc"].append(f"Empty label omitted: {stable_id}; inspect scan coverage")
                    continue
                group, color = _appearance(name)
                structures.append({"id": f"{task}:{name}", "name": name.replace("_", " ").capitalize(),
                                   "task": task, "group": group, "color": color, "mask_file": str(path)})
            save_manifest()
        if not structures:
            raise RuntimeError("No nonempty structures were found in the requested task outputs")
        progress("Recording model checkpoint checksums")
        manifest["model_fingerprint"] = model_fingerprint(profile)
        if not manifest["model_fingerprint"]["weights_complete"]:
            raise RuntimeError("Inference completed but its exact model checkpoints could not be identified; results were not published")
        manifest["status"] = "completed"
        manifest["structure_count"] = len(structures)
        return structures
    except BaseException as exc:
        manifest["status"] = "cancelled" if isinstance(exc, SegmentationCancelled) else "failed"
        manifest["error"] = str(exc)
        raise
    finally:
        manifest["finished_at"] = datetime.now(timezone.utc).isoformat()
        save_manifest()
