"""The body stats report: what TotalSegmentator predicts of the patient and the scan from the image alone.

Research use only, not for diagnostic use. `report_worker.body_stats` runs upstream's own body stats
command (`totalseg_get_body_stats`, its default CNN backend) on the CPU; this module says where its
licensed models live and turns upstream's result into the report's. It reads no mask, so the report
has no needs and no recipe, and runs on a CT or an MR.
"""

from __future__ import annotations

from importlib import metadata
import os
from pathlib import Path

from . import segmentation_options
from .reports import Definition

# Upstream's CNN model folders under its weights folder, five folds each, which its own command downloads with the license.
FOLDERS = {"CT": "ct_all_ext_splitOrig_noise_can1_huber", "MR": "mr_all_ext_splitOrig_noise_can1_huber"}
FOLDS = 5
# Upstream's keys, the report's ids and names, in the order the report shows them.
STATS = (
    ("weight", "weight", "Weight"),
    ("size", "height", "Height"),
    ("bmi", "bmi", "Body mass index"),
    ("bsa", "bsa", "Body surface area"),
    ("age", "age", "Age"),
    ("sex", "sex", "Sex"),
)
SCAN = (
    ("contrast", "contrast", "Contrast"),
    ("pi_time", "injection_time", "Time since injection"),
    ("verte_upper", "top_vertebra", "Highest vertebra in view"),
    ("verte_lower", "bottom_vertebra", "Lowest vertebra in view"),
    ("mr_sequence", "sequence", "Sequence"),
)
# Upstream writes kg/m^2, m^2 and seconds.
UNITS = {"weight": "kg", "size": "cm", "age": "years", "bmi": "kg/m²", "bsa": "m²", "pi_time": "s"}
NO_LICENSE = "Body stats needs a TotalSegmentator license configured on this workstation"


def missing_model(modality):
    return f"The body stats model for {modality} is not installed on this server"


def folder(modality):
    """The model folder as upstream's config.get_weights_dir finds it, which the worker's call resolves it
    with, read here without importing upstream."""
    if "TOTALSEG_WEIGHTS_PATH" in os.environ:
        weights = Path(os.environ["TOTALSEG_WEIGHTS_PATH"])
    else:
        home = Path("/tmp") if str(Path.home()) == "/" else Path.home()
        weights = Path(os.environ.get("TOTALSEG_HOME_DIR", str(home / ".totalsegmentator"))) / "nnunet" / "results"
    return weights / "lightning_models" / FOLDERS[modality]


def installed(modality):
    """Every fold has a checkpoint. Without the folder upstream would download it, which a job must never do."""
    return all(any((folder(modality) / f"version_{fold}" / "checkpoints").glob("*.ckpt")) for fold in range(FOLDS))


def unavailable(modality):
    if not segmentation_options.license_configured():
        return NO_LICENSE
    return None if installed(modality) else missing_model(modality)


def identity(modality):
    """The model's files and sizes, and the libraries that load and run it, so a changed one is predicted again."""
    root = folder(modality)
    files = sorted([path.relative_to(root).as_posix(), path.stat().st_size] for path in root.rglob("*", recurse_symlinks=True) if path.is_file())
    libraries = {}
    for name in ("monai", "torch"):
        try:
            libraries[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            libraries[name] = None
    return {"files": files, "libraries": libraries}


def _entries(upstream, rows, spread):
    """An entry for each row upstream gave a value for, with its unit and, of `spread`, what upstream gave."""
    entries = []
    for key, identifier, name in rows:
        given = upstream.get(key) or {}
        if given.get("value") is None:
            continue
        entry = {"id": identifier, "name": name, "value": given["value"]}
        if key in UNITS:
            entry["unit"] = UNITS[key]
        entry.update((field, given[field]) for field in spread if field in given)
        entries.append(entry)
    return entries


def shape(raw):
    """The result's report fields from the worker's output: upstream's own values, not rounded again."""
    upstream = raw["upstream"]
    return {
        "modality": raw["modality"],
        "stats": _entries(upstream, STATS, ("min", "max", "stddev", "probability")),
        # Spread across five models of a class index or a scan setting reads as nothing, so only the range and probability.
        "scan": _entries(upstream, SCAN, ("min", "max", "probability")),
        "upstream": upstream,
        "warnings": list(raw["warnings"]),
    }


def summary(result):
    values = {entry["id"]: entry["value"] for entry in result["stats"]}
    return {
        "weight_kg": values.get("weight"),
        "height_cm": values.get("height"),
        "bmi": values.get("bmi"),
        "age_years": values.get("age"),
        "sex": values.get("sex"),
    }


DEFINITION = Definition(
    id="body_stats",
    title="Body stats",
    engine="TotalSegmentator body stats",
    module="monai",
    # Bump when the worker or this module changes a value, so saved results are not reused.
    version=1,
    needs=(),
    recipe=(),
    shape=shape,
    summary=summary,
    modalities=("CT", "MR"),
    unavailable=unavailable,
    identity=identity,
)
