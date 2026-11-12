"""Anatomical groups for every model's outputs and tasks, in one table.

Metadata only. Groups never decide a colour (scan3d/segmentation.py keeps colours as they were).
"""

from __future__ import annotations

from fnmatch import fnmatchcase

# Every model's outputs share these anatomical groups. Findings sit with their organ.
GROUPS = (
    "Spine",
    "Bones",
    "Arteries",
    "Veins",
    "Airways",
    "Lung lobes",
    "Heart",
    "Chest organs",
    "Abdominal organs",
    "Brain",
    "Head organs",
    "Head and neck",
    "Teeth",
    "Breast",
    "Muscles",
    "Tissues",
    "Body",
    "Implants",
)
# Read top to bottom: the first row with a pattern matching the output's name decides, whatever the task.
_NAME_GROUPS = (
    ("Veins", ("*vein*", "*vena*")),
    ("Arteries", ("*arter*", "*aorta*", "brachiocephalic_trunk")),
    ("Airways", ("*airway*", "trachea")),
    ("Spine", ("vertebrae*", "disc_*", "intervertebral_discs", "sacrum", "spine", "spinal_*")),
    (
        "Bones",
        ("rib_*", "clavicula_*", "scapula_*", "humerus_*", "femur_*", "*_femur", "hip_left", "hip_right", "*_hip", "skull", "sternum", "costal_cartilages"),
    ),
    ("Lung lobes", ("lung_*_lobe_*", "lung_left", "lung_right", "*_lung", "lung", "lung_pleural")),
    ("Heart", ("heart", "heart_*", "atrial_appendage_left")),
    # The same jaw, sinus and teeth go under one header whichever model made them.
    ("Head and neck", ("thyroid_gland", "*jawbone", "*maxillary_sinus")),
    ("Teeth", ("teeth_lower", "teeth_upper")),
    ("Chest organs", ("esophagus",)),
    ("Brain", ("brain",)),
    ("Breast", ("*breast*",)),
    ("Muscles", ("*gluteus*", "*autochthon*", "*iliopsoas*")),
)
# Otherwise the task decides. The Task list groups tasks by this too; a whole-body task's other outputs
# are abdominal organs.
TASK_GROUPS = {
    **dict.fromkeys(("total", "total_v3", "total_mr", "mrsegmentator"), "Whole body"),
    **dict.fromkeys(("body", "body_mr", "trunk_cavities"), "Body"),
    **dict.fromkeys(
        (
            "vertebrae_body",
            "vertebrae_pp",
            "vertebrae_pp_refined",
            "vertebrae_mr",
            "totalspineseg",
            "totalspineseg_step1",
            "sct_spinal_cord",
            "sct_cord_ms_lesions",
            "sc_crop_detector",
        ),
        "Spine",
    ),
    **dict.fromkeys(("appendicular_bones", "appendicular_bones_mr"), "Bones"),
    **dict.fromkeys(("coronary_arteries", "renal_arteries", "aortic_dissection", "imagecasx_coronary", "imagecasx_cas_net"), "Arteries"),
    **dict.fromkeys(("lung_vessels", "lung_nodules"), "Lung lobes"),
    **dict.fromkeys(("heartchambers_highres", "aortic_sinuses", "aorta_annulus", "pulmonary_artery_landmarks"), "Heart"),
    "pleural_pericard_effusion": "Chest organs",
    **dict.fromkeys(
        ("liver_vessels", "liver_lesions", "liver_lesions_mr", "liver_segments", "liver_segments_mr", "kidney_cysts"),
        "Abdominal organs",
    ),
    **dict.fromkeys(
        ("brain_structures", "ventricle_parts", "cerebral_bleed", "brain_aneurysm", "hdbet_brain", "flames_ms_lesions"),
        "Brain",
    ),
    **dict.fromkeys(("face", "face_mr", "head_glands_cavities"), "Head organs"),
    **dict.fromkeys(("headneck_bones_vessels", "craniofacial_structures"), "Head and neck"),
    "teeth": "Teeth",
    **dict.fromkeys(("breasts", "breastdivider", "mamamia_breast_tumour"), "Breast"),
    **dict.fromkeys(
        ("head_muscles", "headneck_muscles", "oculomotor_muscles", "abdominal_muscles", "thigh_shoulder_muscles", "thigh_shoulder_muscles_mr"),
        "Muscles",
    ),
    **dict.fromkeys(("tissue_types", "tissue_types_mr", "tissue_4_types"), "Tissues"),
    "hip_implant": "Implants",
}
# The Task list shows its groups in this order.
TASK_GROUP_ORDER = ("Whole body", "Body", *(value for value in GROUPS if value != "Body"))


def group(name: str, task: str | None) -> str:
    """The group of a model output; "Other" only for a task this table has not met."""
    for value, patterns in _NAME_GROUPS:
        if any(fnmatchcase(name, pattern) for pattern in patterns):
            return value
    value = TASK_GROUPS.get(task, "Whole body" if task is None else "Other")
    return "Abdominal organs" if value == "Whole body" else value
