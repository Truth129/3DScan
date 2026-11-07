"""Modality-scoped tasks and display-safe settings for the pinned local engine.

The upstream registry/configuration modules contain metadata only. Reading this
catalog must not import torch, initialize a GPU, download weights or validate a
license over the network.
"""

from __future__ import annotations

from importlib import metadata
from . import nnunet_models
from .totalsegmentator_license import license_configured


_EXCLUDED_TASKS = {"test", "total_highres_test"}
_OPTION_NAMES = {"tasks", "robust_crop", "body_seg", "remove_small_blobs", "high_order_resampling"}
_BOOLEAN_OPTIONS = ("robust_crop", "body_seg", "remove_small_blobs", "high_order_resampling")
REMOVE_SMALL_BLOBS_MM3 = 200

_LABELS = {
    "total": "Whole body — Total",
    "total_v3": "Whole body — Total v3",
    "body": "Body outline",
    "lung_vessels": "Lung vessels and airways",
    "heartchambers_highres": "Heart chambers — high resolution",
    "head_glands_cavities": "Head glands and cavities",
    "headneck_bones_vessels": "Head/neck bones and vessels",
    "headneck_muscles": "Head/neck muscles",
    "pleural_pericard_effusion": "Pleural and pericardial effusions",
    "vertebrae_pp": "Vertebrae C1–L5 — postprocessed",
    "vertebrae_pp_refined": "Vertebrae C1–L5 — body-refined",
    "vertebrae_body": "Vertebral bodies and discs",
    "ventricle_parts": "Brain ventricular regions",
    "liver_vessels": "Liver vessels and tumor",
    "tissue_4_types": "Tissue composition — four types",
    "total_mr": "Whole body — MRI",
    "body_mr": "Body outline — MRI",
    "vertebrae_mr": "Vertebrae — MRI",
    "brain_aneurysm": "Brain aneurysms — TOF MRI",
}

_DESCRIPTIONS = {
    "body": "Body outline, trunk, extremities and skin. Body and skin are derived from the predicted masks.",
    "vertebrae_pp": "Per-vertebra C1–L5 labels with postprocessing.",
    "vertebrae_pp_refined": "Per-vertebra C1–L5 labels refined with the vertebral-body mask.",
    "vertebrae_body": "Vertebral bodies without the vertebral arches, plus intervertebral discs.",
    "ventricle_parts": "Twelve regions of the brain ventricles.",
    "renal_arteries": "Renal arteries, celiac trunk and superior mesenteric artery.",
    "aorta_annulus": "Aortic annulus and sinotubular junction.",
    "pulmonary_artery_landmarks": "Pulmonary and aortic valve landmarks, pulmonary bifurcation and branch positions.",
    "total_mr": "Whole-body anatomical MRI model. Review results for the selected sequence and scan coverage.",
    "body_mr": "MRI trunk and extremity outlines. These are general body masks, not breast tissue or lesion labels.",
    "brain_aneurysm": "Requires a time-of-flight (TOF) MR angiography volume; not intended for routine T1/T2 imaging.",
    "liver_lesions_mr": "MRI liver lesion model. Performance depends on sequence and contrast timing; review all findings.",
}


def default_task(modality: str = "CT") -> str:
    from .domain import Modality
    from .modalities import get_modality_adapter

    return "total_mr" if get_modality_adapter(modality).modality == Modality.MR else "total"


def task_ids(modality: str = "CT") -> list[str]:
    from .modalities import get_modality_adapter

    adapter = get_modality_adapter(modality)
    from totalsegmentator.registry import TASKS, task_modality
    from totalsegmentator.map_tasks_config import TASK_CONFIGS

    return [
        *(
            task
            for task in TASKS
            if task_modality(task) == adapter.modality and task in TASK_CONFIGS and task not in _EXCLUDED_TASKS and not task.endswith("_LEGACY")
        ),
        *(model.id for model in nnunet_models.models_for(adapter.modality)),
    ]


def resolved_task_config(task: str, robust_crop: bool = False, mode: str = "default") -> dict:
    """Resolve the same defaults/overrides as upstream, without model imports."""
    from totalsegmentator.map_tasks_config import DEFAULT_CONFIG, TASK_CONFIGS

    raw = TASK_CONFIGS[task]
    config = dict(DEFAULT_CONFIG)
    config["robust_crop"] = robust_crop
    config.update(raw["sub_modes"][mode] if "sub_modes" in raw else raw)
    config["plans"] = config.get("plans") or "nnUNetPlans"
    return config


def task_capabilities(task: str) -> dict:
    from totalsegmentator.registry import task_modality

    if task in nnunet_models.MODELS:
        return {
            "uses_organ_crop": False,
            "robust_crop_default": False,
            "robust_crop_supported": False,
            "body_seg_supported": False,
            "remove_small_blobs_supported": False,
            "high_order_resampling_supported": False,
        }
    config = resolved_task_config(task)
    is_mr = task_modality(task) == "MR"
    cropped = bool(config.get("crop") or config.get("cascade"))
    crop_labels = config.get("crop") or []
    return {
        "uses_organ_crop": cropped,
        "robust_crop_default": bool(config.get("robust_crop")) and not is_mr,
        "robust_crop_supported": not is_mr
        and cropped
        and config.get("crop_model") is None
        and not any(label in {"body_trunc", "body_extremities"} for label in crop_labels),
        "body_seg_supported": not is_mr and not cropped,
        "remove_small_blobs_supported": True,
        # Upstream skips its own resampling and lets nnU-Net scale class scores back to the source grid.
        "high_order_resampling_supported": True,
    }


def run_source(options: dict) -> str:
    """A revision's `source`. A nnU-Net model always runs alone, so its first task decides."""
    return "nnunet_model" if options["tasks"][0] in nnunet_models.MODELS else "totalsegmentator"


def _model_entry(model: nnunet_models.NnunetModel, modality: str) -> dict:
    available = nnunet_models.installed(model)
    return {
        "id": model.id,
        "label": model.label,
        "engine": model.name,
        "engine_version": model.version,
        "comparison": True,
        "license_required": False,
        "license_available": True,
        "available": available,
        "modality": modality,
        "classes": list(model.labels),
        "class_count": len(model.labels),
        "model_class_count": len(model.labels),
        "description": model.description if available else nnunet_models.install_hint(model) + ".",
        **task_capabilities(model.id),
    }


def segmentation_catalog(modality: str = "CT") -> dict:
    from totalsegmentator.registry import get_task_classes, requires_license

    configured = license_configured()
    tasks = []
    for task in task_ids(modality):
        if model := nnunet_models.MODELS.get(task):
            tasks.append(_model_entry(model, modality))
            continue
        required = requires_license(task)
        available = not required or configured
        classes = list(get_task_classes(task).values())
        model_class_count = len(classes)
        # The normal per-structure NIfTI path also creates these two body masks.
        # They are imported by the pipeline and must be discoverable in the UI.
        if task == "body":
            classes += [name for name in ("body", "skin") if name not in classes]
        tasks.append(
            {
                "id": task,
                "label": _LABELS.get(task, task.replace("_", " ").capitalize()),
                "engine": "TotalSegmentator",
                "license_required": required,
                "license_available": available,
                "available": available,
                "modality": modality,
                "classes": classes,
                "class_count": len(classes),
                "model_class_count": model_class_count,
                "description": _DESCRIPTIONS.get(task, ""),
                **task_capabilities(task),
            }
        )
    return {
        "engine_version": metadata.version("TotalSegmentator"),
        "tasks": tasks,
        "license_configured": configured,
        "defaults": {"task": default_task(modality), "robust_crop": False, "body_seg": False, "remove_small_blobs": False, "high_order_resampling": False},
        "remove_small_blobs_mm3": REMOVE_SMALL_BLOBS_MM3,
    }


def normalize_options(profile: str, options: dict | None = None, *, modality: str = "CT") -> dict:
    """Canonical immutable-by-convention request for cache/provenance/worker use.

    ``None`` retains the historical profile task selection for old callers.
    New requests explicitly select tasks; an empty settings object means total.
    """
    from totalsegmentator.registry import requires_license
    from .acquisition import PROFILE_IDS

    if profile not in PROFILE_IDS:
        raise ValueError("Unknown segmentation profile")
    selected_default = default_task(modality)
    if options is None:
        options = {"tasks": [selected_default] + (["lung_vessels"] if selected_default == "total" and profile in {"chest", "chest_abdomen"} else [])}
    if not isinstance(options, dict) or set(options) - _OPTION_NAMES:
        raise ValueError("Unknown segmentation settings")
    tasks = options.get("tasks", [selected_default])
    if not isinstance(tasks, list) or not tasks or len(tasks) > 8:
        raise ValueError("Select between one and eight segmentation tasks")
    allowed = set(task_ids(modality))
    if any(not isinstance(task, str) or task not in allowed for task in tasks):
        raise ValueError(f"Select a supported {modality} segmentation task")
    if len(set(tasks)) != len(tasks):
        raise ValueError("Segmentation tasks must be unique")
    settings = {name: options.get(name, False) for name in _BOOLEAN_OPTIONS}
    if any(type(value) is not bool for value in settings.values()):
        raise ValueError("Segmentation options must be true or false")
    if selected_default == "total_mr" and (settings["body_seg"] or settings["robust_crop"]):
        raise ValueError("Body cropping and the robust-crop switch are CT-only options; MRI organ tasks use their native MRI crop model")
    models = [nnunet_models.MODELS[task] for task in tasks if task in nnunet_models.MODELS]
    if models:
        if len(tasks) > 1:
            raise ValueError(f"{models[0].name} runs on its own, not with other tasks")
        if any(settings.values()):
            raise ValueError(f"{models[0].name} has no crop, small-blob or resampling options")
        if not nnunet_models.installed(models[0]):
            raise ValueError(nnunet_models.install_hint(models[0]))
    if any(requires_license(task) for task in tasks if task not in nnunet_models.MODELS) and not license_configured():
        raise ValueError("This task requires a TotalSegmentator license configured on the workstation")
    capabilities = [task_capabilities(task) for task in tasks]
    if settings["body_seg"] and any(not item["body_seg_supported"] for item in capabilities):
        raise ValueError("Body cropping cannot be combined with a task that already crops to an organ")
    # Some native task configurations mandate robust cropping even when the CLI
    # switch is absent. Canonicalize it so UI/cache/provenance do not imply off.
    settings["robust_crop"] = settings["robust_crop"] or any(item["robust_crop_default"] for item in capabilities)
    return {"tasks": list(tasks), **settings}
