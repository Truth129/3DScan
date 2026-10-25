"""Validated CT tasks and display-safe settings for the pinned local engine.

The upstream registry/configuration modules contain metadata only. Reading this
catalog must not import torch, initialize a GPU, download weights or validate a
license over the network.
"""

from __future__ import annotations

from importlib import metadata
from .totalsegmentator_license import license_configured


_EXCLUDED_TASKS = {"test", "total_highres_test"}
_OPTION_NAMES = {"tasks", "robust_crop", "body_seg", "remove_small_blobs"}
_BOOLEAN_OPTIONS = ("robust_crop", "body_seg", "remove_small_blobs")
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
}


def task_ids() -> list[str]:
    from totalsegmentator.registry import TASKS, task_modality
    from totalsegmentator.map_tasks_config import TASK_CONFIGS

    return [task for task in TASKS if task_modality(task) == "CT"
            and task in TASK_CONFIGS and task not in _EXCLUDED_TASKS
            and not task.endswith("_LEGACY")]


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
    config = resolved_task_config(task)
    cropped = bool(config.get("crop") or config.get("cascade"))
    crop_labels = config.get("crop") or []
    return {
        "uses_organ_crop": cropped,
        "robust_crop_default": bool(config.get("robust_crop")),
        "robust_crop_supported": cropped and config.get("crop_model") is None
        and not any(label in {"body_trunc", "body_extremities"} for label in crop_labels),
        "body_seg_supported": not cropped,
    }


def segmentation_catalog() -> dict:
    from totalsegmentator.registry import get_task_classes, requires_license

    configured = license_configured()
    tasks = []
    for task in task_ids():
        required = requires_license(task)
        available = not required or configured
        classes = list(get_task_classes(task).values())
        model_class_count = len(classes)
        # The normal per-structure NIfTI path also creates these two body masks.
        # They are imported by the pipeline and must be discoverable in the UI.
        if task == "body":
            classes += [name for name in ("body", "skin") if name not in classes]
        tasks.append({"id": task, "label": _LABELS.get(task, task.replace("_", " ").capitalize()),
                      "license_required": required, "license_available": available,
                      "available": available, "modality": "CT", "classes": classes,
                      "class_count": len(classes), "model_class_count": model_class_count,
                      "description": _DESCRIPTIONS.get(task, ""),
                      **task_capabilities(task)})
    return {"engine_version": metadata.version("TotalSegmentator"), "tasks": tasks,
            "license_configured": configured,
            "defaults": {"task": "total", "robust_crop": False, "body_seg": False,
                         "remove_small_blobs": False},
            "remove_small_blobs_mm3": REMOVE_SMALL_BLOBS_MM3}


def normalize_options(profile: str, options: dict | None = None) -> dict:
    """Canonical immutable-by-convention request for cache/provenance/worker use.

    ``None`` retains the historical profile task selection for old callers.
    New requests explicitly select tasks; an empty settings object means total.
    """
    from totalsegmentator.registry import requires_license
    from .acquisition import PROFILE_IDS

    if profile not in PROFILE_IDS:
        raise ValueError("Unknown segmentation profile")
    if options is None:
        options = {"tasks": ["total"] + (["lung_vessels"] if profile in {"chest", "chest_abdomen"} else [])}
    if not isinstance(options, dict) or set(options) - _OPTION_NAMES:
        raise ValueError("Unknown segmentation settings")
    tasks = options.get("tasks", ["total"])
    if not isinstance(tasks, list) or not tasks or len(tasks) > 8:
        raise ValueError("Select between one and eight segmentation tasks")
    allowed = set(task_ids())
    if any(not isinstance(task, str) or task not in allowed for task in tasks):
        raise ValueError("Select a supported CT segmentation task")
    if len(set(tasks)) != len(tasks):
        raise ValueError("Segmentation tasks must be unique")
    settings = {name: options.get(name, False) for name in _BOOLEAN_OPTIONS}
    if any(type(value) is not bool for value in settings.values()):
        raise ValueError("Segmentation options must be true or false")
    if any(requires_license(task) for task in tasks) and not license_configured():
        raise ValueError("This task requires a TotalSegmentator license configured on the workstation")
    capabilities = [task_capabilities(task) for task in tasks]
    if settings["body_seg"] and any(not item["body_seg_supported"] for item in capabilities):
        raise ValueError("Body cropping cannot be combined with a task that already crops to an organ")
    # Some native task configurations mandate robust cropping even when the CLI
    # switch is absent. Canonicalize it so UI/cache/provenance do not imply off.
    settings["robust_crop"] = settings["robust_crop"] or any(
        item["robust_crop_default"] for item in capabilities)
    return {"tasks": list(tasks), **settings}
