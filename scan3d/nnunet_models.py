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
    # nnU-Net's fold "all" is one model trained on every case.
    folds: tuple[int | str, ...]
    tile_step_size: float
    # A zip archive, or a folder URL ending in "/" when the model is published as separate files.
    url: str
    # Empty when the files are separate; each file is still pinned below.
    archive_sha256: str
    files: dict[str, str]
    checkpoint: str = "checkpoint_final.pth"
    # The voxel axes of the training images, in nibabel's codes. MRSegmentator reoriented to LPS.
    axes: str = "LPS"
    # The model's folder inside the archive, when it is not the archive's root.
    archive_folder: str = ""
    # The names in the model's dataset.json, when 3DScan shows other names.
    dataset_labels: tuple[str, ...] = ()
    # Pieces of a mask with fewer voxels than this are dropped; only voxels sharing a face join a piece.
    smallest_piece: int = 0
    # Another pinned model whose first label keeps the voxels this model sees, for a model
    # trained on skull-stripped images. Everything outside it is set to zero first.
    skull_strip: str = ""
    # The archive needs a sign-in to download, so it is installed from a file someone fetched.
    sign_in: bool = False
    # "nnunet" runs nnU-Net's predictor; "cas_net" runs ImageCAS-X's CAS-Net (scan3d/cas_net.py),
    # one network whose checkpoint is the only file and whose tile_step_size is its tile overlap.
    engine: str = "nnunet"


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
            # The ImageCAS-X paper scores every method after this cleanup (appendix E.4).
            smallest_piece=100,
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
        NnunetModel(
            id="imagecasx_cas_net",
            name="ImageCAS-X CAS-Net",
            version="zenodo-23894668",
            label="Coronary arteries — ImageCAS-X CAS-Net",
            description="Coronary artery lumen on gated coronary CT angiography, from the ImageCAS-X benchmark's CAS-Net, which scored above its nnU-Net with fewer stray pieces. It was trained at one centre on scans without artefacts. Results stay beside other models' results, so you can compare them.",
            license="Weights CC BY 4.0 (Zenodo record 23894668); network code MIT (ImageCAS-X's port; the original CAS-Net repository states no license)",
            modalities=("CT",),
            labels=("coronary_arteries",),
            folds=(),
            tile_step_size=0.5,
            url="https://zenodo.org/records/23894668/files/pretrained_weights.zip",
            archive_sha256="4e5ec4510847ab5af561f906e21faed22aec574caa4a21517f2ebbf21dcdd334",
            archive_folder="pretrained_weights",
            checkpoint="cas_net.pt",
            # It read the ImageCAS files with SimpleITK, unchanged, as LAS x, y, z arrays.
            axes="LAS",
            smallest_piece=100,
            engine="cas_net",
            files={"cas_net.pt": "f3e280b74e2f3c32ac122996100694fb7d1357f3aa2e582e5e6bb7461f90d6c1"},
        ),
        NnunetModel(
            id="hdbet_brain",
            name="HD-BET",
            version="zenodo-14445620",
            label="Brain — HD-BET",
            description="The brain without skull, scalp or orbits on MRI of any contrast, from HD-BET 2.0. FLAMeS uses it to remove the skull first.",
            license="Weights CC BY-NC 4.0 (Zenodo record 14445620); code Apache-2.0",
            modalities=("MR",),
            labels=("brain",),
            folds=("all",),
            tile_step_size=0.5,
            url="https://zenodo.org/records/14445620/files/release_v1.5.0.zip",
            archive_sha256="125c40e2a19acd8827a42a0e922c6140ed5518e20ff7ccfadc0ebdf32e17dfc8",
            # FLAMeS's voxel order, so this model's mask lines up with FLAMeS's input.
            axes="LAS",
            files={
                "dataset.json": "aad907d0ec0abbd49da7eabacf640a768efd83e8e96e8b60322a2739b7de766a",
                "plans.json": "c4994d570255573277d57dfd2789a4c5c25dfe6c4c050625f1d186adbbcf8811",
                "fold_all/checkpoint_final.pth": "d31dc59b4c5fe0028070901870c44c0b526f48c507ce50804941344356df7b52",
            },
        ),
        NnunetModel(
            id="flames_ms_lesions",
            name="FLAMeS",
            version="hf-ea055ae",
            label="MS lesions — FLAMeS",
            description="Multiple sclerosis white-matter lesions on a FLAIR series, from FLAMeS, an nnU-Net trained on 668 scans from three vendors at 1.5 and 3 T. HD-BET removes the skull first. Lesions under about 10 mm³ are often missed.",
            license="Weights CC BY-NC 4.0 (Hugging Face FrancescoLR/FLAMeS-model); HD-BET weights CC BY-NC 4.0",
            modalities=("MR",),
            labels=("white_matter_lesions",),
            dataset_labels=("Lesion",),
            folds=(0, 1, 2, 3, 4),
            tile_step_size=0.5,
            url="https://huggingface.co/FrancescoLR/FLAMeS-model/resolve/ea055aec0f55baa15d984c4fe92035aaa21c7b5c/Dataset004_WML.zip",
            archive_sha256="943f49d4997fc752322f0892b4aa6dcfce64f99a8a8c445d38785f54af09c277",
            archive_folder="Dataset004_WML/nnUNetTrainer_8000epochs__nnUNetPlans__3d_fullres",
            # Its training images are stored left to right, front to back, then foot to head, as
            # after registration to MNI152; which way each axis runs is unpublished. The model
            # mirrors on all three axes, so only the axis order matters.
            axes="LAS",
            skull_strip="hdbet_brain",
            files={
                "dataset.json": "833d31b097702f812940f9a8fd51eece30e75f3ba472ece975cbb657d671f06a",
                "plans.json": "387d1440eb8ef62a930623d4a0bf9ee4e4886e12a922977c2b60d7b90b791d33",
                "fold_0/checkpoint_final.pth": "4fc7db24e7c1541b59023df2a2f498a964eb629fe38f4eaec763297dd3fdf861",
                "fold_1/checkpoint_final.pth": "3966dd256074a0ea3c7d16c3a8e7412a6cb5071220df71b54745aeb50c33c33c",
                "fold_2/checkpoint_final.pth": "734be69b968cc6578bcf079bb79db166cd8f5edba0f0dc19a62bbe68021364d8",
                "fold_3/checkpoint_final.pth": "8e989caccdcf45368cc8f1ef8a00598f9f072c1fbe23e57458fc572fea434c3f",
                "fold_4/checkpoint_final.pth": "ff723a2eda28a7f78f75d96a993d029ea8f65aa3657f5aea7caab9b217792df2",
            },
        ),
        NnunetModel(
            id="breastdivider",
            name="BreastDivider",
            version="hf-e02a0ad",
            label="Breasts — BreastDivider",
            description="The left and right breast on MRI of the breasts in any common sequence, from BreastDivider, an nnU-Net trained on 13,752 public scans. Its accuracy has been measured only against labels it helped make.",
            license="Weights CC BY-NC-SA 4.0 (Hugging Face ykirchhoff/BreastDividerModel); code Apache-2.0",
            modalities=("MR",),
            labels=("left_breast", "right_breast"),
            dataset_labels=("left", "right"),
            folds=("all",),
            tile_step_size=0.5,
            url="https://huggingface.co/ykirchhoff/BreastDividerModel/resolve/e02a0ad9c2200a20df4bbb490b8fd8d647337fb3/",
            archive_sha256="",
            # nnU-Net's reader turned every image to RAS, and the model never mirrors left to
            # right, so this order decides which breast is which.
            axes="RAS",
            files={
                "dataset.json": "0570e0acea9ee107c986732ed0656cb658e9e448ba9f3faa0391c136b9291e6a",
                "plans.json": "5618d66983c643e8558f4c67fac7101d7fa6cc52465fabf8ce2c576f9d7dab0b",
                "fold_all/checkpoint_final.pth": "18441fe5536ce9209d216f8a9ba6e6331c69f54a0acb3fa2fca6428214b92d18",
            },
        ),
        NnunetModel(
            id="mamamia_breast_tumour",
            name="MAMA-MIA nnU-Net",
            version="synapse-syn61906708.1",
            label="Breast tumour — MAMA-MIA",
            description="The primary breast tumour on one post-contrast DCE phase, from the MAMA-MIA challenge baseline, an nnU-Net trained on 1,506 cases. It scored Dice 0.69 on 516 exams from three unseen centres. It can also mark enhancing tissue away from the tumour.",
            license="Weights CC BY-NC 4.0 (Synapse syn61906708)",
            modalities=("MR",),
            labels=("breast_tumour",),
            dataset_labels=("tumor",),
            folds=(0, 1, 2, 3, 4),
            tile_step_size=0.5,
            url="https://www.synapse.org/Synapse:syn61906708",
            archive_sha256="cac74adc7920d1f29b2302d4e91c5fcab3f76e895a0a9a4980c6068222fb486b",
            archive_folder="full_image_dce_mri_tumor_segmentation",
            sign_in=True,
            # Its training images kept their acquisition order: axial Duke scans and sagittal
            # I-SPY scans. Axial order suits axial DCE, and the model mirrors on all three axes.
            axes="LPS",
            files={
                "dataset.json": "cedd8b4e1e709784de09c41348b3a774541b38e5113ceb5a6fdc92301d5a6061",
                "plans.json": "15013ddf2434e407253d1a051871545a8d2ee055e5a7aa25c92c2a64137799a0",
                "fold_0/checkpoint_final.pth": "d9a4738be5b148a4fe367d50a73ad473b3395ce432a21695562c84d699dd73ee",
                "fold_1/checkpoint_final.pth": "99385966691e55081cf70400bb4d56f4c20165087d5af83c3dc335fb6d851174",
                "fold_2/checkpoint_final.pth": "197473a0ffaa43069a389305edef787adbde20939d48bfd4d5eb2f72560b538e",
                "fold_3/checkpoint_final.pth": "1e8eae1ad1ceaeb9cb374f3e2214473696feb84f1ffa0e7cb3454f5b26515b2a",
                "fold_4/checkpoint_final.pth": "24d8308c1e44d849b39983f4e059221abf8ffbecc85c76fcef91b11ceb8ac445",
            },
        ),
    )
}


def models_for(modality: str) -> list[NnunetModel]:
    return [model for model in MODELS.values() if modality in model.modalities]


def required(model: NnunetModel) -> tuple[NnunetModel, ...]:
    """The model and any model it runs first."""
    return (model, MODELS[model.skull_strip]) if model.skull_strip else (model,)


def model_dir(model: NnunetModel) -> Path:
    default = ROOT / ".runtime" / "segmentation-models"
    return Path(os.environ.get("SCAN3D_SEGMENTATION_MODELS") or default).expanduser().resolve() / f"{model.id}-{model.version}"


def installed(model: NnunetModel) -> bool:
    """File presence only, because the catalog is read often. A run checks the hashes."""
    return all((model_dir(part) / name).is_file() for part in required(model) for name in part.files)


def install_hint(model: NnunetModel) -> str:
    if model.sign_in:
        return (
            f"{model.name} weights are not installed; download them from {model.url} (free sign-in), then run "
            f"scripts/download_segmentation_model.py {model.id} --archive FILE on the workstation"
        )
    return f"{model.name} weights are not installed; run scripts/download_segmentation_model.py {model.id} on the workstation"
