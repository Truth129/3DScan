"""Full-resolution, modality-scoped, explicitly selected-device worker adapter."""

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

from .config import ROOT, device_request
from .fileio import atomic_text, hash_file as _digest
from .imaging import _binary_nonempty, _image
from . import nnunet_models, segmentation_options
from .segmentation_options import normalize_options, resolved_task_config, task_capabilities


class SegmentationCancelled(InterruptedError):
    pass


_LOBES = ("lung_lower_lobe_left", "lung_lower_lobe_right", "lung_middle_lobe_right", "lung_upper_lobe_left", "lung_upper_lobe_right")
_LUNG = {"lung_arteries", "lung_veins", "lung_airways", "lung_airways_wall"}
# Far longer than a cold PyTorch import should need. A hung GPU driver must not hold the serialized lane.
DEVICE_CHECK_SECONDS = 300.0
# nnU-Net's only trace of re-running a part with results in system memory: its report omits it.
# Upstream catches any RuntimeError there, so the QC line says 'usually'.
_RESULTS_MOVED = b"Prediction on device was unsuccessful"
_FAILURE_LOG_BYTES = 64 * 1024
_NAME_COLORS = ["#d88a72", "#ad91d6", "#a6c682", "#e3bb78", "#7ebbb7", "#c092b8"]
# Saturated yellow: ivory vanishes on bright CT bone in the slices, and a fill still tints white.
BONE_COLOR = "#f5cc00"
# A pinned model keeps its own group, so its bones are found by name.
_MODEL_BONES = {"mrsegmentator": {"spine", "sacrum", "left_hip", "right_hip", "left_femur", "right_femur"}}


def _name_color(name):
    return _NAME_COLORS[int.from_bytes(hashlib.sha256(name.encode()).digest()[:2], "little") % len(_NAME_COLORS)]


def _structure_name(name, task):
    label = name.replace("_", " ").capitalize()
    # Two models can name an organ alike, and an exported SEG or ROI shows only this name.
    return f"{label} ({nnunet_models.MODELS[task].name})" if task in nnunet_models.MODELS else label


def _appearance(name, task=None):
    if task in nnunet_models.MODELS:
        return nnunet_models.MODELS[task].name, BONE_COLOR if name in _MODEL_BONES.get(task, ()) else _name_color(name)
    if "vein" in name or "vena" in name:
        return "Veins", "#4d82e8"
    if "arter" in name or "aorta" in name or name == "brachiocephalic_trunk":
        return "Arteries", "#eb655d"
    if "airway" in name or name == "trachea":
        return "Airways", "#f1dfb1"
    if name.startswith("lung_") and (task in (None, "total", "total_v3", "total_mr", "lung_vessels") or name in _LOBES):
        colors = ["#dca6d0", "#ad9ed8", "#e7ad8f", "#b5d59a", "#8fc3d7"]
        return "Lung lobes", colors[_LOBES.index(name)] if name in _LOBES else "#91cbd9"
    if name.startswith(("rib_", "vertebrae_", "clavicula", "scapula", "hip_", "humerus_", "femur_")) or name in {
        "skull",
        "sacrum",
        "sternum",
        "costal_cartilages",
        "vertebrae",
        "intervertebral_discs",
    }:
        return "Bones", BONE_COLOR
    if name in {"heart", "atrial_appendage_left", "spinal_cord", "esophagus", "thyroid_gland"}:
        return "Chest organs", "#ce94a7"
    if name == "brain":
        return "Head organs", _name_color(name)
    if name.startswith(("gluteus_", "autochthon_", "iliopsoas_")):
        return "Muscles", _name_color(name)
    if task not in (None, "total", "total_v3", "total_mr"):
        groups = {
            "body": "Body",
            "body_mr": "Body",
            "tissue_types_mr": "Tissues",
            "appendicular_bones_mr": "Bones",
            "vertebrae_mr": "Bones",
            "face_mr": "Head organs",
            "thigh_shoulder_muscles_mr": "Muscles",
            "brain_aneurysm": "Brain findings",
            "heartchambers_highres": "Heart",
            "brain_structures": "Brain structures",
            "cerebral_bleed": "Brain findings",
            "ventricle_parts": "Brain structures",
            "head_glands_cavities": "Head organs",
            "head_muscles": "Muscles",
            "headneck_muscles": "Muscles",
            "oculomotor_muscles": "Muscles",
            "abdominal_muscles": "Muscles",
            "thigh_shoulder_muscles": "Muscles",
            "headneck_bones_vessels": "Head and neck",
            "craniofacial_structures": "Head and neck",
            "teeth": "Teeth",
            "tissue_types": "Tissues",
            "tissue_4_types": "Tissues",
            "lung_nodules": "Lung findings",
            "pleural_pericard_effusion": "Chest findings",
            "appendicular_bones": "Bones",
            "vertebrae_body": "Bones",
            "vertebrae_pp": "Bones",
            "vertebrae_pp_refined": "Bones",
            "hip_implant": "Implants",
            "face": "Head organs",
        }
        group = groups.get(task, task.replace("_", " ").capitalize())
        return group, BONE_COLOR if group == "Bones" else _name_color(name)
    return "Abdominal organs", _name_color(name)


def _device_info(device):
    """Resolve a SCAN3D_DEVICE request. Only auto may choose the CPU: a GPU request fails instead.

    This imports PyTorch, so the API process goes through _resolve_device.
    """
    import torch

    if device == "auto":
        device = "gpu" if torch.cuda.is_available() else "cpu"
    if device == "cpu":
        return {"device": "cpu", "selected_device": "cpu", "torch": torch.__version__}
    if device != "gpu" and not (device.startswith("gpu:") and device[4:].isdigit()):
        raise ValueError("SCAN3D_DEVICE must be auto, gpu, gpu:N or cpu")
    if not torch.cuda.is_available():
        # An added path sets its own device, so SCAN3D_DEVICE=cpu could only move the default path.
        if os.environ.get("SCAN3D_INFERENCE_PATH"):
            raise RuntimeError(
                "GPU inference was requested but PyTorch cannot access a GPU; check the driver, then repeat this GPU's check "
                "(see Add a second GPU in docs/INSTALL.md). No other GPU was tried."
            )
        raise RuntimeError("GPU inference was requested but PyTorch cannot access a GPU; check the CUDA or ROCm setup, or set SCAN3D_DEVICE=cpu")
    if ":" in device:
        index = int(device[4:])
    else:
        # PyTorch also enumerates integrated GPUs and reports their share of system RAM as
        # total_memory, which can exceed a discrete card's real VRAM.
        def preference(i):
            properties = torch.cuda.get_device_properties(i)
            return not getattr(properties, "is_integrated", False), properties.total_memory

        index = max(range(torch.cuda.device_count()), key=preference)
    if index >= torch.cuda.device_count():
        raise ValueError("Requested GPU index is unavailable")
    return {
        "device": device,
        "selected_device": f"gpu:{index}",
        "torch": torch.__version__,
        "gpu_index": index,
        "gpu_name": torch.cuda.get_device_name(index),
        "hip": torch.version.hip,
        "cuda": torch.version.cuda,
    }


@lru_cache(maxsize=256)
def _checkpoint_digest(path, size, modified_ns, changed_ns):
    # The stat tuple invalidates this small in-process cache when a checkpoint
    # is downloaded/replaced. Checkpoint bytes are hashed once per file version.
    return _digest(Path(path))


def _model_configurations(options: dict) -> list[tuple[str, dict]]:
    """Exact primary and auxiliary model configurations used by the native API."""
    configurations = []

    def collect(task, robust_crop=False, body_seg=False, label=None, ancestors=()):
        if model := nnunet_models.MODELS.get(task):
            configurations.append((task, {"nnunet_model": model.id, "version": model.version, "folds": model.folds, "files": model.files}))
            return
        if task in ancestors:
            raise ValueError("Cyclic TotalSegmentator crop-model configuration")
        label = label or task
        config = resolved_task_config(task, robust_crop)
        configurations.append((label, config))
        if config.get("crop") or config.get("cascade"):
            if config.get("crop_model") is not None:
                # Upstream recursively invokes that task with its own defaults.
                collect(config["crop_model"], label=label + ":crop_model", ancestors=(*ancestors, task))
            elif any(name in {"body_trunc", "body_extremities"} for name in (config.get("crop") or [])):
                configurations.append((label + ":crop", resolved_task_config("body", mode="fast")))
            elif task.endswith("_mr"):
                configurations.append((label + ":crop", resolved_task_config("total_mr", mode="fast")))
            else:
                mode = "fast" if config.get("robust_crop") else "fastest"
                configurations.append((label + ":crop", resolved_task_config("total", mode=mode)))
        elif body_seg:
            configurations.append((label + ":body_crop", resolved_task_config("body", mode="fast")))
        if task == "vertebrae_pp_refined":
            configurations.append((label + ":refinement", resolved_task_config("vertebrae_body")))

    for task in options["tasks"]:
        collect(task, options["robust_crop"], options["body_seg"])
    return configurations


def _pinned_files(part):
    """One model folder's files, and whether every pinned file is there and matches."""
    directory = nnunet_models.model_dir(part)
    files = []
    matches = True
    for name, expected in part.files.items():
        path = directory / name
        if not path.is_file():
            matches = False
            continue
        stat = path.stat()
        digest = _checkpoint_digest(str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
        matches = matches and digest == expected
        files.append({"name": f"{directory.name}/{name}", "bytes": stat.st_size, "sha256": digest})
    return files, matches


def _nnunet_descriptor(model, inference_path=None):
    """Identity of one pinned model and any model it runs first. Complete only when every file matches its pin."""
    files = []
    matches = True
    for part in nnunet_models.required(model):
        part_files, part_matches = _pinned_files(part)
        files += part_files
        matches = matches and part_matches
    descriptor = {
        "task": model.id,
        "engine": model.name,
        "version": model.version,
        "folds": list(model.folds),
        "smallest_piece": model.smallest_piece,
        "worker_sha256": _digest(Path(__file__).with_name("nnunet_worker.py")),
        "files": files,
        "complete": matches,
    }
    if model.keep_largest:
        descriptor["keep_largest"] = True
    if model.engine == "cas_net":
        # Its network and inference are this repository's code, not a package's.
        descriptor["cas_net_sha256"] = _digest(Path(__file__).with_name("cas_net.py"))
    if model.crop:
        # The crop box comes from this repository's port of sc-crop, run by onnxruntime.
        descriptor["crop_padding"] = model.crop_padding
        descriptor["sc_crop_sha256"] = _digest(Path(__file__).with_name("sc_crop.py"))
        try:
            # The version the worker's interpreter imports, as inference_path.versions() reads.
            if inference_path:
                from .inference_paths import installed

                descriptor["onnxruntime_version"] = installed(inference_path.site_packages, "onnxruntime")
            else:
                descriptor["onnxruntime_version"] = metadata.version("onnxruntime")
        except metadata.PackageNotFoundError:
            descriptor["onnxruntime_version"] = "unavailable"
    return descriptor


def model_fingerprint(profile: str, options: dict | None = None, *, modality: str = "CT", inference_path=None) -> dict:
    """Resolved model/checkpoint identity for provenance and safe cache reuse.

    This does not download or import a model or initialize a GPU. An incomplete
    fingerprint cannot authorize a cache hit. Recompute after the first run,
    once TotalSegmentator has downloaded its model files.

    An inference path changes `versions` and `device_request` and adds no key, so each
    card keeps its own results, the default path's identity never moves, and both the
    cache key and the digest change together.
    """
    options = normalize_options(profile, options, modality=modality)

    if inference_path:
        versions = inference_path.versions()
    else:
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
    configurations = _model_configurations(options)
    models = []
    for task, config in configurations:
        if "nnunet_model" in config:
            models.append(_nnunet_descriptor(nnunet_models.MODELS[task], inference_path))
            continue
        ids = config["task_id"] if isinstance(config["task_id"], list) else [config["task_id"]]
        for dataset_id in ids:
            descriptor = {
                "task": task,
                "dataset_id": dataset_id,
                "trainer": config["trainer"],
                "configuration": config["model"],
                "plans": config["plans"],
                "folds": config["folds"],
                "resample_mm": config.get("resample"),
                "files": [],
            }
            datasets = [p for p in base.glob(f"Dataset{dataset_id:03d}*") if p.is_dir() and p.name.split("_", 1)[0] == f"Dataset{dataset_id:03d}"]
            if len(datasets) == 1:
                folder = datasets[0] / f"{config['trainer']}__{config['plans']}__{config['model']}"
                folds = config["folds"]
                if folds is None:
                    # nnU-Net auto-detects numeric fold directories when unset.
                    folds = sorted(int(p.name[5:]) for p in folder.glob("fold_*") if p.is_dir() and p.name[5:].isdigit())
                    descriptor["folds"] = folds
                required = [folder / "plans.json", folder / "dataset.json"]
                required += [folder / f"fold_{fold}" / "checkpoint_final.pth" for fold in folds]
                for path in required:
                    if not path.is_file():
                        continue
                    stat = path.stat()
                    descriptor["files"].append(
                        {
                            "name": path.relative_to(base).as_posix(),
                            "bytes": stat.st_size,
                            "sha256": _checkpoint_digest(str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns),
                        }
                    )
                descriptor["complete"] = bool(folds) and len(descriptor["files"]) == len(required)
            else:
                descriptor["complete"] = False
            models.append(descriptor)
    config_digest = hashlib.sha256(json.dumps(configurations, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {
        "schema_version": 2,
        "versions": versions,
        "profile": profile,
        "modality": modality,
        "options": options,
        "task_configuration_sha256": config_digest,
        "device_request": inference_path.identity if inference_path else device_request(),
        "fast": False,
        "fastest": False,
        "wrapper_sha256": _digest(Path(__file__)),
        "options_adapter_sha256": _digest(Path(segmentation_options.__file__)),
        "models": models,
        "weights_complete": bool(models) and all(model["complete"] for model in models),
    }
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


def _resolve_device(device, cancelled, inference_path=None):
    """Return _device_info(device) from a short-lived child process.

    The API process runs no inference, and a GPU runtime started here would stay
    resident in it. Exit returns the runtime, at the cost of one import per job.
    An inference path answers from its own interpreter, the only one that can see its card.
    """
    inherited = inference_path.environment() if inference_path else os.environ
    # Import exactly this release of scan3d, as the nnInteractive worker does.
    env = {**inherited, "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), inherited.get("PYTHONPATH"))))}
    command = [inference_path.python if inference_path else sys.executable, "-P", "-m", "scan3d.segmentation", device]
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, env=env, start_new_session=True)
    deadline = time.monotonic() + DEVICE_CHECK_SECONDS
    try:
        while True:
            try:
                answer = process.communicate(timeout=0.2)[0]
                break
            except subprocess.TimeoutExpired:
                pass
            if cancelled():
                raise SegmentationCancelled("Segmentation cancelled")
            if time.monotonic() > deadline:
                raise RuntimeError(f"PyTorch did not report a device within {DEVICE_CHECK_SECONDS:g} s; check the GPU driver")
    except BaseException:
        _stop(process)
        raise
    finally:
        process.stdout.close()
    try:
        reply = json.loads(answer)
    except ValueError:
        raise RuntimeError(f"The device check failed (exit {process.returncode}); inspect the service log") from None
    if "runtime" in reply:
        return reply["runtime"]
    raise (ValueError if reply["invalid"] else RuntimeError)(reply["error"])


def _report_device(device):
    """Child entry point: python -P -m scan3d.segmentation REQUEST writes one JSON answer."""
    # GPU runtimes can log to stdout natively, so the answer keeps its own copy of
    # the pipe and everything else joins stderr in the service log.
    answer = os.fdopen(os.dup(1), "w")
    os.dup2(2, 1)
    try:
        reply = {"runtime": _device_info(device)}
    except Exception as exc:
        reply = {"error": str(exc) or type(exc).__name__, "invalid": isinstance(exc, ValueError)}
    with answer:
        json.dump(reply, answer)


class _Resources:
    """Optional Linux process-tree RSS and whole-device VRAM samples.

    VRAM comes from the amdgpu driver's sysfs counters, so other vendors report none.
    """

    def __init__(self, track_amd_vram=False):
        self.rss_peak = None
        self.vram_path = None
        self.vram_baseline = None
        self.vram_peak = None
        self.pci_device = None
        if track_amd_vram:
            largest = 0
            for device in sorted(Path("/sys/class/drm").glob("card[0-9]*/device")):
                try:
                    total = int((device / "mem_info_vram_total").read_text().strip())
                    candidate = device / "mem_info_vram_used"
                    value = int(candidate.read_text().strip())
                except (OSError, ValueError):
                    continue
                if total > largest:
                    largest = total
                    self.vram_path = candidate
                    self.pci_device = device.resolve().name
                    self.vram_baseline = self.vram_peak = value

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
        return {
            "sample_interval_seconds": 0.2,
            "worker_tree_rss_peak_bytes": self.rss_peak,
            "rss_scope": "Sampled sum of child process-tree RSS; shared pages may be counted more than once; excludes API parent",
            "gpu_pci_device": self.pci_device,
            "whole_device_vram_baseline_bytes": self.vram_baseline,
            "whole_device_vram_peak_bytes": self.vram_peak,
            "whole_device_vram_peak_above_baseline_bytes": (self.vram_peak - self.vram_baseline if self.vram_peak is not None else None),
            "vram_scope": "Sampled whole GPU use via amdgpu sysfs; includes graphics and other processes; not per-process allocation"
            if self.vram_path
            else None,
        }


def _confirm_card(inference_path, runtime, tasks):
    """Stop before a run exists unless all of it would happen on this path's proven card."""
    inference_path.confirm(runtime["selected_device"], runtime.get("gpu_name"), runtime.get("cuda"))
    for task in tasks:
        # Upstream starts a crop model with only "gpu" from gpu:N, which is the first card.
        if task not in nnunet_models.MODELS and runtime.get("gpu_index") and resolved_task_config(task).get("crop_model"):
            raise ValueError(
                f"TotalSegmentator would run part of {task} on the first GPU, so it cannot run on {inference_path.label}. Nothing ran and no other GPU was tried."
            )


def _failure_cause(returncode, log_path):
    """Why a task on a path stopped. On a smaller card, out of memory is the expected failure."""
    if returncode == -signal.SIGKILL:
        return "the process was killed, usually because system memory ran out"
    with log_path.open("rb") as log:
        log.seek(max(0, log_path.stat().st_size - _FAILURE_LOG_BYTES))
        if b"out of memory" in log.read().lower():
            return "it ran out of GPU memory"
    return f"exit {returncode}"


def segment(
    ct_path: Path, output_dir: Path, profile: str, progress, cancelled, options: dict | None = None, *, modality: str = "CT", inference_path=None
) -> list[dict]:
    legacy_options = options is None
    options = normalize_options(profile, options, modality=modality)
    ct_path = Path(ct_path).resolve()
    ct, affine = _image(ct_path)
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if cancelled():
        raise SegmentationCancelled("Segmentation cancelled")
    tasks = options["tasks"]
    model = nnunet_models.MODELS.get(tasks[0])
    engine, engine_version = (model.name, model.version) if model else ("TotalSegmentator", metadata.version("TotalSegmentator"))
    # Before any child starts, because loading a checkpoint runs its pickle.
    if model and not _nnunet_descriptor(model)["complete"]:
        # A model run first has its own folder, and the script fetches only the folders that are gone.
        folders = " and ".join(nnunet_models.model_dir(part).name for part in nnunet_models.required(model) if not _pinned_files(part)[1])
        raise RuntimeError(
            f"{model.name} weights do not match their pinned checksums; nothing ran. "
            f"Move {folders} aside and run scripts/download_segmentation_model.py {model.id} again."
        )
    device = inference_path.device if inference_path else device_request()
    runtime = _resolve_device(device, cancelled, inference_path)
    effective_device = runtime["selected_device"]
    if inference_path:
        _confirm_card(inference_path, runtime, tasks)
    run_dir = output_dir / ("run-" + uuid.uuid4().hex)
    run_dir.mkdir()
    manifest = {
        "schema_version": 1,
        "engine": engine,
        "engine_version": engine_version,
        "resolution": "standard_full",
        "profile": profile,
        "modality": modality,
        "options": options,
        "output_selection": "all_nonempty_task_outputs",
        "input_sha256": _digest(ct_path),
        "runtime": runtime,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "status": "running",
        "run_id": run_dir.name,
        "tasks": [],
        "empty_labels": [],
        "qc": [],
    }
    if inference_path:
        manifest["inference_path"] = inference_path.id
    manifest_path = output_dir / "inference.json"

    def save_manifest():
        content = json.dumps(manifest, indent=2) + "\n"
        for destination in (run_dir / "inference.json", manifest_path):
            atomic_text(destination, content)

    save_manifest()
    structures = []
    # The default path's child keeps inheriting this process's environment, exactly as before.
    spawn_options = {"env": inference_path.environment()} if inference_path else {}
    if model:
        inherited = spawn_options.get("env", os.environ)
        spawn_options = {"env": {**inherited, "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), inherited.get("PYTHONPATH"))))}}
    try:
        for task in tasks:
            if cancelled():
                raise SegmentationCancelled("Segmentation cancelled")
            progress(f"Running full-resolution {engine}: {task} ({device})")
            task_dir = run_dir / task
            task_dir.mkdir(exist_ok=True)
            if list(task_dir.glob("*.nii*")):
                raise ValueError("Inference output already exists; use a new run directory")
            # nnU-Net starts manager processes while temporarily hiding stdout.
            # Buffered output can be inherited and replayed when those children
            # restore the original stream. Unbuffered output also makes tail -F
            # report each stage promptly instead of delaying it until exit.
            command = [
                inference_path.python if inference_path else sys.executable,
                "-u",
                *(("-P", "-m", "scan3d.nnunet_worker") if model else ("-m", "totalsegmentator.bin.TotalSegmentator")),
                "-i",
                str(ct_path),
                "-o",
                str(task_dir),
                "--task",
                task,
                "--device",
                effective_device,
                "--report",
                str(run_dir / f"{task}-report.json"),
            ]
            progress_path = run_dir / f"{task}-progress.txt"
            if model:
                command += ["--model-folder", str(nnunet_models.model_dir(model)), "--progress", str(progress_path)]
                if model.skull_strip:
                    command += ["--skull-strip-folder", str(nnunet_models.model_dir(nnunet_models.MODELS[model.skull_strip]))]
                if model.crop:
                    command += ["--crop-folder", str(nnunet_models.model_dir(nnunet_models.MODELS[model.crop]))]
            # Beside the task's folder, whose every NIfTI is read as a mask.
            probability_path = run_dir / f"{task}-probability.nii.gz" if model and model.engine == "cas_net" else None
            if probability_path:
                command += ["--probability", str(probability_path)]
            for option in ("robust_crop", "body_seg", "remove_small_blobs", "high_order_resampling"):
                if options[option]:
                    command.append("--" + option)
            if inference_path and not model:
                command.append("--verbose")  # Only prints more, and upstream hides nnU-Net's output without it.
            capabilities = task_capabilities(task)
            effective = {
                "robust_crop": options["robust_crop"] and capabilities["robust_crop_supported"],
                "body_seg": options["body_seg"],
                "remove_small_blobs": options["remove_small_blobs"],
                "remove_small_blobs_mm3": (segmentation_options.REMOVE_SMALL_BLOBS_MM3 if options["remove_small_blobs"] else None),
                "high_order_resampling": options["high_order_resampling"],
            }
            record = {
                "task": task,
                "device": effective_device,
                "fast": False,
                "status": "running",
                "options": {key: value for key, value in options.items() if key != "tasks"},
                "effective_options": effective,
            }
            if options["robust_crop"] and not capabilities["robust_crop_supported"]:
                record["option_notes"] = ["Robust crop is only used by tasks with a standard organ crop model."]
            if modality == "MR" and capabilities["uses_organ_crop"]:
                record.setdefault("option_notes", []).append("MRI organ crops use the native 3 mm MRI model; CT crop switches do not apply.")
            manifest["tasks"].append(record)
            save_manifest()
            started = time.monotonic()
            resources = _Resources(track_amd_vram=bool(runtime.get("hip")))
            log_path = run_dir / f"{task}.log"
            with log_path.open("wb") as log:
                process = subprocess.Popen(command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True, **spawn_options)
                shown = None
                try:
                    while process.poll() is None:
                        resources.sample(process.pid)
                        if cancelled():
                            _stop(process)
                            raise SegmentationCancelled("Segmentation cancelled")
                        # A pinned model's run can take minutes with nothing else to show.
                        if model and progress_path.is_file() and (current := progress_path.read_text()) != shown:
                            shown = current
                            progress(f"Running full-resolution {engine}: {task}, {current} ({device})")
                        time.sleep(0.2)
                except BaseException:
                    _stop(process)
                    raise
                finally:
                    resources.sample(process.pid)
                    record["elapsed_seconds"] = round(time.monotonic() - started, 3)
                    record["resources"] = resources.result()
            record["returncode"] = process.returncode
            if process.returncode:
                record["status"] = "failed"
                # A pinned model's worker leaves a sentence in its report for a failure the reader can act on.
                if model and (failure := run_dir / f"{task}-report.json").is_file() and (sentence := json.loads(failure.read_text()).get("error")):
                    raise RuntimeError(sentence)
                if inference_path:
                    cause = _failure_cause(process.returncode, log_path)
                    raise RuntimeError(f"{engine} {task} failed on {inference_path.label}: {cause}. No other GPU was tried.")
                raise RuntimeError(f"{engine} {task} failed (exit {process.returncode}); inspect the local worker log")
            report_path = run_dir / f"{task}-report.json"
            if not report_path.is_file():
                raise RuntimeError(f"{engine} did not write its required reproducibility report; verify the pinned worker version")
            native = json.loads(report_path.read_text())
            if effective_device.startswith("gpu") and native.get("device") != "gpu":
                raise RuntimeError(f"{engine} did not complete on the requested GPU; CPU fallback results were rejected")
            if inference_path:
                # The child's own measurement, so this shows the spawn used the chosen interpreter.
                if native.get("torch_version") != runtime["torch"]:
                    raise RuntimeError(
                        f"{engine} {task} ran on PyTorch {native.get('torch_version')}, not the {runtime['torch']} that {inference_path.label} was checked on. "
                        "No result was kept and no other GPU was tried."
                    )
                if _RESULTS_MOVED in log_path.read_bytes():
                    record["results_moved_to_cpu"] = True
                    manifest["qc"].append(
                        f"Part of {task} re-ran with results in system memory on {inference_path.label}, usually because GPU memory ran out. The run took longer."
                    )
            if native.get("fast") or native.get("fastest") or native.get("save_lowres"):
                raise RuntimeError(f"{engine} returned an unexpected reduced-resolution result")
            record["native_report"] = report_path.name
            record["runtime_seconds"] = native.get("runtime_seconds")
            record["nnunetv2_version"] = native.get("nnunetv2_version")
            masks = {path.name[:-7]: path for path in sorted(task_dir.glob("*.nii.gz"))}
            if not masks:
                raise RuntimeError(f"{engine} {task} produced no masks")
            if set(native.get("classes", {}).values()) - set(masks):
                raise RuntimeError(f"{engine} {task} did not save every declared class mask")
            if task == "lung_vessels" and not _LUNG.issubset(masks):
                raise RuntimeError(
                    "This lung_vessels model does not provide the required split artery/vein/airway outputs; verify the pinned TotalSegmentator version"
                )
            if probability_path and not probability_path.is_file():
                raise RuntimeError(f"{engine} did not save its probability for Grow branch; verify the worker version")
            record["labels"] = sorted(masks)
            record["status"] = "completed"
            progress(f"Checking {task} mask geometry and labels")
            validation_started = time.monotonic()
            for name, path in masks.items():
                if cancelled():
                    raise SegmentationCancelled("Segmentation cancelled")
                # Tasks select models, never an anatomical subset of their outputs.
                if not _binary_nonempty(path, ct, affine):
                    stable_id = f"{task}:{name}"
                    manifest["empty_labels"].append(stable_id)
                    manifest["qc"].append(f"Empty label omitted: {stable_id}; inspect scan coverage")
                    continue
                group, color = _appearance(name, task)
                structures.append(
                    {"id": f"{task}:{name}", "name": _structure_name(name, task), "task": task, "group": group, "color": color, "mask_file": str(path)}
                )
                if probability_path:
                    # Later edits of this segment keep it, so Grow branch reads the run's own probability.
                    structures[-1]["probability_file"] = str(probability_path)
            record["mask_validation_seconds"] = round(time.monotonic() - validation_started, 3)
            save_manifest()
        if not structures:
            # For a lesion model this is the answer on a scan without lesions, not a fault.
            raise RuntimeError(f"{engine} marked nothing in this scan." if model else "No nonempty structures were found in the requested task outputs")
        progress("Recording model checkpoint checksums")
        source_options = {"modality": modality} if modality != "CT" else {}
        if inference_path:
            source_options["inference_path"] = inference_path
        manifest["model_fingerprint"] = (
            model_fingerprint(profile, **source_options) if legacy_options else model_fingerprint(profile, options, **source_options)
        )
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


if __name__ == "__main__":
    _report_device(sys.argv[1])
