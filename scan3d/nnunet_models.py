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
    checkpoint: str = "checkpoint_final.pth"
    # The voxel axes of the training images, in nibabel's codes. MRSegmentator reoriented to LPS.
    axes: str = "LPS"
    # The model's folder inside the archive, when it is not the archive's root.
    archive_folder: str = ""
    # The names in the model's dataset.json, when 3DScan shows other names.
    dataset_labels: tuple[str, ...] = ()


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
        NnunetModel(
            id="imagecasx_coronary",
            name="ImageCAS-X nnU-Net",
            version="zenodo-23894668",
            label="Coronary arteries — ImageCAS-X nnU-Net",
            description="Coronary artery lumen on gated coronary CT angiography, from the ImageCAS-X benchmark's nnU-Net. It was trained at one centre on scans without artefacts. Results stay beside other models' results, so you can compare them.",
            license="Weights CC BY 4.0 (Zenodo record 23894668); code MIT",
            modalities=("CT",),
            labels=("coronary_arteries",),
            dataset_labels=("lumen",),
            folds=(0, 1, 2, 3, 4),
            tile_step_size=0.5,
            url="https://zenodo.org/records/23894668/files/pretrained_weights.zip",
            archive_sha256="4e5ec4510847ab5af561f906e21faed22aec574caa4a21517f2ebbf21dcdd334",
            archive_folder="pretrained_weights/nnunet/nnUNetTrainer__nnUNetPlans__3d_fullres",
            # The authors published only the best checkpoint of each fold.
            checkpoint="checkpoint_best.pth",
            # nnU-Net read the ImageCAS files unchanged, and all 800 ImageCAS-X label headers are LAS.
            axes="LAS",
            files={
                "dataset.json": "43d1e6d7b11c3af807484ce1bb4c660f1ae83ca1934eebf7390e675ab24403da",
                "plans.json": "1d1d83ab3d0af4542f23ed047ea9769249e07058d85348ee483e95e757245e14",
                "fold_0/checkpoint_best.pth": "8150d2031b0f45431e2499daa8b172b19873f1fac9ce5e94b38f3f34c99933d5",
                "fold_1/checkpoint_best.pth": "409ca2a1a464b2dbd633575c8a91fab630a5427f6d59545e5e9cc2da5ad5fdd5",
                "fold_2/checkpoint_best.pth": "f79a1b4b3f6f3028434939774a137e52be7a4409edd1ab0e9a2fdb30afe0a691",
                "fold_3/checkpoint_best.pth": "18cf22acd8332cac77eb640216e358ea43c0ab6aba91e76e2e0056e83b87f86a",
                "fold_4/checkpoint_best.pth": "c4449840327b7d2275346531c98fa2fcc3f620df088d4a960918ed57070f29a2",
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
