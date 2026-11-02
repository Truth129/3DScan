"""Pinned models in nnU-Net's folder format, separate from TotalSegmentator's catalog.

Metadata only: reading this imports neither PyTorch nor nnU-Net and starts no GPU.
Requests never download a model; scripts/download_segmentation_model.py does. A
checkpoint is a pickle that nnU-Net loads with weights_only=False, so a run checks
every file against these hashes first.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path

from .config import ROOT


@dataclass(frozen=True, slots=True)
class NnunetModel:
    id: str
    name: str
    version: str
    label: str
    description: str
    license: str
    modalities: tuple[str, ...]
    # In nnU-Net label order, starting at label 1.
    labels: tuple[str, ...]
    folds: tuple[int, ...]
    tile_step_size: float
    url: str
    archive_sha256: str
    files: dict[str, str]


MODELS = {
    model.id: model
    for model in (
        NnunetModel(
            id="mrsegmentator",
            name="MRSegmentator",
            version="1.2.0",
            label="Whole body — MRSegmentator",
            description="Structures on MRI and CT from a separate group's model. Results stay beside other models' results, so you can compare them.",
            license="Code Apache-2.0; the repository states no separate license for the weights",
            modalities=("CT", "MR"),
            labels=(
                "spleen",
                "right_kidney",
                "left_kidney",
                "gallbladder",
                "liver",
                "stomach",
                "pancreas",
                "right_adrenal_gland",
                "left_adrenal_gland",
                "left_lung",
                "right_lung",
                "heart",
                "aorta",
                "inferior_vena_cava",
                "portal_vein_and_splenic_vein",
                "left_iliac_artery",
                "right_iliac_artery",
                "left_iliac_vena",
                "right_iliac_vena",
                "esophagus",
                "small_bowel",
                "duodenum",
                "colon",
                "urinary_bladder",
                "spine",
                "sacrum",
                "left_hip",
                "right_hip",
                "left_femur",
                "right_femur",
                "left_autochthonous_muscle",
                "right_autochthonous_muscle",
                "left_iliopsoas_muscle",
                "right_iliopsoas_muscle",
                "left_gluteus_maximus",
                "right_gluteus_maximus",
                "left_gluteus_medius",
                "right_gluteus_medius",
                "left_gluteus_minimus",
                "right_gluteus_minimus",
            ),
            folds=(0, 1, 2, 3, 4),
            tile_step_size=0.5,
            url="https://github.com/hhaentze/MRSegmentator/releases/download/v1.2.0/weights.zip",
            archive_sha256="dd26c1b511908a1f2a752b7fe8f5ca9c5603fc3e69fa52f57bf3ccdd52ceb286",
            files={
                "dataset.json": "019686402ce0335ca90acaa188b8e423585bafcb47fbea1e055635172023efee",
                "plans.json": "cf5854736d759357cb7a88c7f3428f75e60bfa88ae7af0dcb7f22dbca8106fe3",
                "fold_0/checkpoint_final.pth": "30a73ea32621bcbffddffa044769ff6d609c2817f743923ce53e90afbf780100",
                "fold_1/checkpoint_final.pth": "bb122d2d84052ac7bfba5fcdcc2cd16b82185451696379cfe2a0208f1c6dd2da",
                "fold_2/checkpoint_final.pth": "efe53ee273454864cf4d0a1d5b26b7799c9ff486de971a9cb6e1d7760f4ad169",
                "fold_3/checkpoint_final.pth": "6897c318111b23a285322597730b6c31c9975a5bcc6fd16a8577135a203bf7a5",
                "fold_4/checkpoint_final.pth": "c2255425395e1c6b117746ad8e190fc3f9d9a8d995e1bd69db69a8ab0f5e019d",
            },
        ),
    )
}


def models_for(modality: str) -> list[NnunetModel]:
    return [model for model in MODELS.values() if modality in model.modalities]


def model_dir(model: NnunetModel) -> Path:
    default = ROOT / ".runtime" / "segmentation-models"
    return Path(os.environ.get("SCAN3D_SEGMENTATION_MODELS") or default).expanduser().resolve() / f"{model.id}-{model.version}"


def installed(model: NnunetModel) -> bool:
    """File presence only, because the catalog is read often. A run checks the hashes."""
    directory = model_dir(model)
    return all((directory / name).is_file() for name in model.files)


def install_hint(model: NnunetModel) -> str:
    return f"{model.name} weights are not installed; run scripts/download_segmentation_model.py {model.id} on the workstation"
