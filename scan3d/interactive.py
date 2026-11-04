"""Stateful, workstation-only nnInteractive correction of one binary mask.

Arrays retain nibabel's native i,j,k order. nnInteractive accepts voxel arrays
and does not apply image spacing or patient transforms; physical RAS prompts
are therefore transformed through the complete reference CT affine here.
Nothing in this module downloads weights, activates revisions, or imports a
GPU runtime merely to answer a readiness request.
"""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from importlib import metadata
from pathlib import Path
import gc
import os
import sys
import threading
import time

import numpy as np

from .config import device_request as configured_device
from .fileio import hash_file
from .imaging import _binary, _image, _save_mask, _voxel
from .segmentation import SegmentationCancelled, _device_info


ENGINE_VERSION = "2.6.0"
MODEL_ID = "nnInteractive_v1.0"
MODEL_REPOSITORY = "MIC-DKFZ/nnInteractive"
MODEL_REVISION = "3f308d751c00644e4fde6f09c600264b393b21b5"
CHECKPOINT_SHA256 = "b3ac4421f85457bbd1aa0d87f5e67bcb7bc8e2ce6b824b6ac45077cc5d630ea9"
MODEL_LICENSE = "CC BY-NC-SA 4.0"
MIOPEN_FIND_MODE = "FAST"


@contextmanager
def _kernel_settings():
    """Avoid exhaustive cold MIOpen search during this interactive GPU stage.

    nnInteractive enables convolution benchmarking in its constructor. On a
    cold ROCm cache that can spend minutes searching before the first result.
    FAST uses MIOpen's immediate/find database path; the reference grid, model,
    device, and numerical precision remain unchanged. Set this before torch's
    first MIOpen call and restore the process settings before another GPU job.
    """
    previous_mode = os.environ.get("MIOPEN_FIND_MODE")
    os.environ["MIOPEN_FIND_MODE"] = MIOPEN_FIND_MODE
    try:
        import torch

        previous_benchmark = torch.backends.cudnn.benchmark
        previous_threads = torch.get_num_threads()
        try:
            torch.backends.cudnn.benchmark = False
            torch.set_num_threads(min(8, os.cpu_count() or 1))
            yield
        finally:
            torch.backends.cudnn.benchmark = previous_benchmark
            torch.set_num_threads(previous_threads)
    finally:
        if previous_mode is None:
            os.environ.pop("MIOPEN_FIND_MODE", None)
        else:
            os.environ["MIOPEN_FIND_MODE"] = previous_mode


def _model_path() -> Path:
    default = Path(__file__).resolve().parents[1] / ".runtime" / "nninteractive" / MODEL_ID
    return Path(os.environ.get("SCAN3D_NNINTERACTIVE_MODEL", str(default))).expanduser().resolve()


def _identity(path: Path) -> tuple:
    path = Path(path).resolve()
    stat = path.stat()
    return str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


@lru_cache(maxsize=32)
def _sha256(identity: tuple) -> str:
    return hash_file(identity[0])


def _model_files(directory: Path) -> list[Path]:
    # Deliberately load the pinned fold instead of guessing among checkpoints.
    files = [directory / "fold_0" / "checkpoint_final.pth", directory / "plans.json", directory / "dataset.json"]
    modern = directory / "inference_info.json"
    files.append(modern if modern.is_file() else directory / "inference_session_class.json")
    if any(not path.is_file() or path.stat().st_size == 0 for path in files):
        raise RuntimeError("The pinned nnInteractive model has not been installed completely")
    return files


def status() -> dict:
    """Lightweight availability, with no torch import, GPU probe, or local paths.

    File presence is checked here; checkpoint integrity and the selected device
    are verified by the worker process before any prediction.
    """
    versions = {}
    for name in ("nnInteractive", "nninteractive-client"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    installed = all(versions.values())
    try:
        _model_files(_model_path())
        model_available = True
    except (OSError, RuntimeError):
        model_available = False
    reason = None
    if not installed:
        reason = "nnInteractive and its matching client package are not installed"
    elif any(version != ENGINE_VERSION for version in versions.values()):
        reason = f"This correction adapter requires nnInteractive {ENGINE_VERSION} and its matching client"
    elif not model_available:
        reason = "The pinned nnInteractive model has not been installed completely"
    return {
        "installed": installed,
        "version": versions["nnInteractive"],
        "model_available": model_available,
        "available": reason is None,
        "reason": reason,
        "model_id": MODEL_ID,
        "model_revision": MODEL_REVISION,
        "license": MODEL_LICENSE,
    }


def _new_session(device_info: dict):
    import torch
    from nnInteractive.inference.inference_session import nnInteractiveInferenceSession

    session = nnInteractiveInferenceSession(
        device=torch.device(f"cuda:{device_info['gpu_index']}" if "gpu_index" in device_info else "cpu"),
        use_torch_compile=False,
        verbose=False,
        torch_n_threads=min(8, os.cpu_count() or 1),
        do_autozoom=True,
        interactions_storage="blosc2",
        enable_undo=True,
    )
    # The upstream constructor overrides this flag; change it before its model
    # initialization performs the first 192-cubed warmup convolution.
    torch.backends.cudnn.benchmark = False
    return session


def _prompts(prompts: list[dict], affine: np.ndarray, shape: tuple) -> list[dict]:
    from .interactive_prompts import normalize_prompts

    normalized = normalize_prompts(prompts, affine, shape)
    return [{**item, "voxel": _voxel(affine, shape, item["ras"]).tolist()} if item["kind"] == "point" else item for item in normalized]


def _batches(records, count):
    if not isinstance(records, list) or len(records) > 256:
        raise ValueError("Correction apply history must contain at most 256 batches")
    result, previous = [], 0
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("Every correction apply boundary must be a record")
        end = record.get("end")
        full = record.get("full_refine", False)
        autozoom = record.get("autozoom", True)
        legacy = record.get("legacy", False)
        if (
            isinstance(end, bool)
            or not isinstance(end, int)
            or not previous <= end <= count
            or not all(isinstance(value, bool) for value in (full, autozoom, legacy))
            or (end == previous and not full)
        ):
            raise ValueError("Invalid correction apply boundary")
        result.append({"end": end, "autozoom": autozoom, "full_refine": full, "legacy": legacy})
        previous = end
    if previous != count:
        raise ValueError("Apply history must cover every correction prompt")
    return result


def _marks(prompts, affine, shape, start=0):
    """The engine's form of these normalized marks, numbered by their place in the draft."""
    from .interactive_prompts import mark_name, materialize_prompt

    # A draft saved before such marks were refused can hold one; name it.
    return [materialize_prompt(prompt, affine, shape, mark_name(prompt, start + number)) for number, prompt in enumerate(prompts, 1)]


def _summary(before: np.ndarray, after: np.ndarray, affine: np.ndarray) -> dict:
    # Slab reductions avoid allocating a full-volume difference mask per click.
    changed = before_count = after_count = 0
    for offset in range(0, before.shape[0], 16):
        initial = before[offset : offset + 16]
        current = after[offset : offset + 16]
        changed += int(np.count_nonzero(initial != current))
        before_count += int(np.count_nonzero(initial))
        after_count += int(np.count_nonzero(current))
    voxel_ml = abs(float(np.linalg.det(affine[:3, :3]))) / 1000
    return {
        "changed_voxels": changed,
        "before_voxel_count": before_count,
        "after_voxel_count": after_count,
        "before_volume_ml": before_count * voxel_ml,
        "after_volume_ml": after_count * voxel_ml,
    }


class InteractiveRuntime:
    """Keep one loaded model and CT between prompts, on one thread.

    The service runs this engine inside the supervised worker process from
    `interactive_worker`, never in the API process. Validation scripts may
    still run it directly in their own process.
    """

    def __init__(self):
        self._session = None
        self._model_identity = None
        self._model_provenance = None
        self._device = None
        self._ct_identity = None
        self._image = self._affine = self._baseline = self._target = None
        self._state_key = None
        self._applied = []
        self._applied_batches = []
        self._owner_thread = None

    @staticmethod
    def _cancel(cancelled):
        if cancelled():
            raise SegmentationCancelled("nnInteractive correction was cancelled")

    def _load_model(self, progress):
        readiness = status()
        if not readiness["available"]:
            raise RuntimeError(readiness["reason"])
        directory = _model_path()
        files = _model_files(directory)
        device_request = configured_device()
        identity = (tuple(_identity(path) for path in files), device_request)
        if self._session is not None and identity == self._model_identity:
            return
        self.release()
        progress("Loading the nnInteractive correction model")
        hashes = {path.relative_to(directory).as_posix(): _sha256(_identity(path)) for path in files}
        checkpoint_hash = hashes["fold_0/checkpoint_final.pth"]
        if checkpoint_hash != CHECKPOINT_SHA256:
            raise RuntimeError("nnInteractive checkpoint integrity does not match the pinned official model")
        device = _device_info(device_request)
        self._device = device
        self._session = _new_session(device)
        # Loading also warms the network. This runs on the same thread as prediction.
        self._session.initialize_from_trained_model_folder(str(directory), use_fold=0)
        if not self._session.supports_initial_label or not self._session.supported_interactions.get("points"):
            raise RuntimeError("This nnInteractive checkpoint cannot refine an existing mask with points")
        self._model_identity = identity
        self._model_provenance = {
            "engine": "nnInteractive",
            "engine_version": ENGINE_VERSION,
            "model_id": MODEL_ID,
            "model_repository": MODEL_REPOSITORY,
            "model_revision": MODEL_REVISION,
            "checkpoint_sha256": checkpoint_hash,
            "model_files_sha256": hashes,
            "license": MODEL_LICENSE,
            "device": device["selected_device"],
            "gpu_name": device.get("gpu_name"),
            "torch_version": device["torch"],
            "hip_version": device.get("hip"),
            "cuda_version": device.get("cuda"),
            "compile": False,
            "autozoom": True,
            "interaction_storage": "blosc2",
            # MIOpen is ROCm's. Other builds never read the setting, so it did not shape their result.
            "miopen_find_mode": MIOPEN_FIND_MODE if device.get("hip") else None,
            "convolution_benchmark": False,
            "torch_threads": min(8, os.cpu_count() or 1),
            "axis_order": "native_i_j_k",
            "coordinate_system": "RAS_mm",
            "wrapper_sha256": _sha256(_identity(Path(__file__))),
        }

    def predict(
        self, ct_path: Path, mask_path: Path, prompts: list[dict], output_path: Path, progress, cancelled, session_key: str, *, batches=None, options=None
    ) -> dict:
        thread = threading.get_ident()
        if self._owner_thread is not None and self._owner_thread != thread:
            raise RuntimeError("nnInteractive runtime must remain on its owning GPU worker thread")
        with _kernel_settings():
            return self._predict(ct_path, mask_path, prompts, output_path, progress, cancelled, session_key, batches=batches, options=options)

    def _predict(
        self, ct_path: Path, mask_path: Path, prompts: list[dict], output_path: Path, progress, cancelled, session_key: str, *, batches=None, options=None
    ) -> dict:
        """Write an uncommitted candidate, reusing an exact warm prompt prefix.

        Undo, edited prompts, a changed baseline, or a different session reloads
        the original mask and replays the requested prompt sequence. A cancelled
        or failed prediction invalidates all warm state before another request.
        """
        thread = threading.get_ident()
        if self._owner_thread is not None and self._owner_thread != thread:
            raise RuntimeError("nnInteractive runtime must remain on its owning GPU worker thread")
        self._owner_thread = thread
        try:
            started = time.monotonic()
            self._cancel(cancelled)
            ct_path, mask_path, output_path = (Path(path).resolve() for path in (ct_path, mask_path, output_path))
            if output_path in {ct_path, mask_path}:
                raise ValueError("A correction must write a new candidate mask")
            if not isinstance(session_key, str) or not session_key:
                raise ValueError("A correction session key is required")
            ct_identity, mask_identity = _identity(ct_path), _identity(mask_path)
            image, affine = _image(ct_path)
            normalized = _prompts(prompts, affine, image.shape)
            settings = {"autozoom": True, "start_empty": False, **(options or {})}
            if set(settings) != {"autozoom", "start_empty"} or not all(isinstance(v, bool) for v in settings.values()):
                raise ValueError("Invalid correction options")
            # Standalone callers retain the original point-wise API. Application
            # jobs always supply their persisted Apply boundaries explicitly.
            history = _batches(batches, len(normalized)) if batches is not None else None
            model_started = time.monotonic()
            self._load_model(progress)
            model_seconds = time.monotonic() - model_started
            self._owner_thread = thread
            self._cancel(cancelled)
            prepare_started = time.monotonic()
            if self._ct_identity != ct_identity:
                progress("Preparing the source CT for interactive correction")
                values = np.asanyarray(image.dataobj)
                if values.dtype.kind not in "buif" or not np.isfinite(values).all():
                    raise ValueError("Reference CT must contain finite scalar intensities")
                if float(values.min()) == float(values.max()):
                    raise ValueError("Reference CT has no intensity variation for interactive correction")
                # nnInteractive normalizes its own float32 copy. Do not window,
                # resample, reorient, or transpose the clinical reference here.
                self._session.set_image(values[None])
                self._ct_identity = ct_identity
                self._image, self._affine = image, affine
                self._state_key = None
            state_key = (session_key, ct_identity, mask_identity, settings["start_empty"], history is not None)
            prefix = (
                self._state_key == state_key
                and len(normalized) >= len(self._applied)
                and normalized[: len(self._applied)] == self._applied
                and (history is None or (len(history) >= len(self._applied_batches) and history[: len(self._applied_batches)] == self._applied_batches))
            )
            if not prefix:
                progress("Initializing correction from the saved segmentation")
                baseline, _, _ = _binary(mask_path, image, affine)
                self._baseline = baseline
                # Reuse the target for the same image, but never alias baseline.
                if self._target is None or self._target.shape != image.shape:
                    self._target = np.zeros(image.shape, dtype=np.uint8)
                self._session.set_target_buffer(self._target)
                initial = np.zeros_like(baseline) if settings["start_empty"] else baseline
                self._session.add_initial_seg_interaction(initial, run_prediction=False)
                # Fail before any prompt if an upstream version changes initial
                # mask semantics. Slab comparisons bound temporary memory even
                # for a whole-body CT rather than allocating a full-volume diff.
                if self._target.shape != initial.shape or any(
                    not np.array_equal(self._target[offset : offset + 16], initial[offset : offset + 16]) for offset in range(0, baseline.shape[0], 16)
                ):
                    raise RuntimeError("nnInteractive changed the saved mask during initialization")
                self._state_key, self._applied, self._applied_batches = state_key, [], []
            prepare_seconds = time.monotonic() - prepare_started
            applied_now = 0
            changed_bbox = None
            prediction_started = time.monotonic()
            batch_statistics = []
            if history is None:
                for prompt in normalized[len(self._applied) :]:
                    self._cancel(cancelled)
                    if prompt["kind"] != "point":
                        raise ValueError("Drawing tools require persisted Apply batch boundaries")
                    progress(f"Refining segmentation with point {len(self._applied) + 1} of {len(normalized)}")
                    changed_bbox = self._session.add_point_interaction(prompt["voxel"], include_interaction=prompt["include"], run_prediction=True)
                    self._cancel(cancelled)
                    self._applied.append(prompt)
                    applied_now += 1
            else:
                from .interactive_batch import register_and_predict, refine_whole_mask

                for batch in history[len(self._applied_batches) :]:
                    self._cancel(cancelled)
                    start, end = len(self._applied), batch["end"]
                    pending = normalized[start:end]
                    if batch["legacy"]:
                        for prompt in pending:
                            if prompt["kind"] != "point":
                                raise ValueError("Legacy prediction history supports points only")
                            changed_bbox = self._session.add_point_interaction(prompt["voxel"], include_interaction=prompt["include"], run_prediction=True)
                            self._cancel(cancelled)
                            applied_now += 1
                    elif pending:
                        marks = _marks(pending, affine, image.shape, start)
                        stats = register_and_predict(self._session, marks, progress, cancelled, autozoom=batch["autozoom"])
                        batch_statistics.append(stats)
                        changed_bbox = stats.get("changed_bbox_ijk", changed_bbox)
                        applied_now += stats["prediction_regions"]
                    if batch["full_refine"]:
                        progress("Refining the whole current mask")
                        marks = _marks(normalized[:end], affine, image.shape)
                        full_stats = refine_whole_mask(self._session, progress, cancelled, autozoom=batch["autozoom"], marks=marks)
                        batch_statistics.append(full_stats)
                        changed_bbox = full_stats.get("changed_bbox_ijk", changed_bbox)
                        applied_now += full_stats["prediction_regions"]
                    self._cancel(cancelled)
                    self._applied = normalized[:end]
                    self._applied_batches.append(batch)
            prediction_seconds = time.monotonic() - prediction_started
            self._cancel(cancelled)
            if self._target.dtype != np.uint8 or self._target.min() < 0 or self._target.max() > 1:
                raise RuntimeError("nnInteractive returned a nonbinary candidate mask")
            summary = _summary(self._baseline, self._target, affine)
            provenance = {
                **self._model_provenance,
                **summary,
                "prompts": normalized,
                "prompt_count": len(normalized),
                "predictions_this_request": applied_now,
                "apply_batches": history,
                "options": settings,
                "batch_statistics": batch_statistics,
                "autozoom": settings["autozoom"],
                "batch_adapter_sha256": _sha256(_identity(Path(__file__).with_name("interactive_batch.py"))),
                "prompt_rasterizer_sha256": _sha256(_identity(Path(__file__).with_name("interactive_prompts.py"))),
                "reused_prompt_prefix": prefix,
                "last_changed_bbox_ijk": changed_bbox,
                "source_ct_sha256": _sha256(ct_identity),
                "input_mask_sha256": _sha256(mask_identity),
            }
            self._cancel(cancelled)
            artifact_started = time.monotonic()
            _save_mask(self._target, image, output_path)
            provenance["output_mask_sha256"] = _sha256(_identity(output_path))
            provenance["elapsed_seconds"] = time.monotonic() - started
            provenance["timings"] = {
                "model_seconds": model_seconds,
                "prepare_seconds": prepare_seconds,
                "prediction_seconds": prediction_seconds,
                "write_seconds": time.monotonic() - artifact_started,
            }
            return provenance
        except BaseException:
            self.release()
            raise

    def release(self):
        """Free warm CT/model state before TotalSegmentator needs the GPU."""
        session, device = self._session, self._device
        self._session = None
        self._model_identity = self._model_provenance = self._device = None
        self._ct_identity = self._state_key = None
        self._image = self._affine = self._baseline = self._target = None
        self._applied = []
        self._applied_batches = []
        self._owner_thread = None
        if session is not None:
            # A vendor session may fail before creating its optional executor.
            # Cleanup must also work for that partially initialized state.
            executor = getattr(session, "executor", None)
            if executor is not None:
                executor.shutdown(wait=True, cancel_futures=True)
            del session
            gc.collect()
        torch = sys.modules.get("torch")
        if device is not None and "gpu_index" in device and torch is not None:
            try:
                with torch.cuda.device(device["gpu_index"]):
                    torch.cuda.empty_cache()
            except RuntimeError:
                # Cleanup cannot hide the original GPU/cancellation failure.
                pass
