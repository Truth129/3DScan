"""Task selection, native flags and checkpoint provenance without real inference."""

from pathlib import Path
import json
import subprocess
import sys

import nibabel as nib
import numpy as np
import pytest

from scan3d import segmentation
from scan3d.segmentation_options import normalize_options, segmentation_catalog


@pytest.mark.parametrize("name,task,group", [
    ("eye_left", "head_glands_cavities", "Head organs"),
    ("heart_myocardium", "heartchambers_highres", "Heart"),
    ("body_trunc", "body", "Body"),
    ("torso_fat", "tissue_types", "Tissues"),
    ("lung_nodules", "lung_nodules", "Lung findings"),
    ("lung_pleural", "pleural_pericard_effusion", "Chest findings"),
    ("aorta", "heartchambers_highres", "Arteries"),
    ("lung_veins", "lung_vessels", "Veins"),
    ("skull", "craniofacial_structures", "Bones"),
    ("liver", "total_v3", "Abdominal organs"),
])
def test_task_appearance_has_meaningful_fallback_and_keeps_anatomical_priorities(name, task, group):
    assert segmentation._appearance(name, task)[0] == group


def test_catalog_is_ct_only_stable_and_does_not_initialize_torch():
    result = subprocess.run([sys.executable, "-c", "import sys; "
                             "from scan3d.segmentation_options import segmentation_catalog; "
                             "c=segmentation_catalog(); "
                             "assert 'torch' not in sys.modules; "
                             "print(len(c['tasks']))"], check=True, capture_output=True, text=True)
    assert int(result.stdout) >= 30
    catalog = segmentation_catalog()
    tasks = {item["id"]: item for item in catalog["tasks"]}
    assert {"total", "body", "lung_vessels", "liver_vessels", "heartchambers_highres"} <= tasks.keys()
    assert {"total_v3", "renal_arteries", "aorta_annulus", "aortic_dissection", "pulmonary_artery_landmarks"} <= tasks.keys()
    assert not {"total_mr", "body_mr", "brain_aneurysm", "test", "total_highres_test", "lung_vessels_LEGACY"} & tasks.keys()
    assert tasks["total"]["class_count"] == 117
    assert tasks["lung_vessels"]["robust_crop_default"] is True
    assert tasks["lung_vessels"]["body_seg_supported"] is False
    assert tasks["total"]["body_seg_supported"] is True
    assert tasks["total"]["robust_crop_supported"] is False
    assert catalog["remove_small_blobs_mm3"] == 200


def test_options_defaults_are_explicit_and_historical_profiles_remain_compatible():
    assert normalize_options("chest", {}) == {"tasks": ["total"], "robust_crop": False,
                                                "body_seg": False, "remove_small_blobs": False}
    assert normalize_options("chest")["tasks"] == ["total", "lung_vessels"]
    assert normalize_options("abdomen")["tasks"] == ["total"]
    assert normalize_options("abdomen", {"tasks": ["lung_vessels"], "robust_crop": False})["robust_crop"] is True


def test_catalog_exposes_output_names_and_correct_vertebral_and_brain_labels():
    from totalsegmentator.registry import get_task_classes

    catalog = segmentation_catalog()
    tasks = {item["id"]: item for item in catalog["tasks"]}
    for item in tasks.values():
        assert item["modality"] == "CT"
        assert item["class_count"] == len(item["classes"])
        assert len(item["classes"]) == len(set(item["classes"]))
        assert set(get_task_classes(item["id"]).values()) <= set(item["classes"])
    assert "skull" in tasks["total"]["classes"]
    assert "skull" in tasks["craniofacial_structures"]["classes"]
    assert tasks["body"]["model_class_count"] == 2
    assert set(tasks["body"]["classes"]) == {"body", "body_trunc", "body_extremities", "skin"}
    assert tasks["vertebrae_pp"]["label"] == "Vertebrae C1–L5 — postprocessed"
    assert tasks["vertebrae_pp_refined"]["label"] == "Vertebrae C1–L5 — body-refined"
    assert "posterior" not in tasks["vertebrae_pp"]["label"].lower()
    assert "Brain" in tasks["ventricle_parts"]["label"]
    assert {"renal_arteries", "celiac_trunk", "superior_mesenteric_artery"} <= set(tasks["renal_arteries"]["classes"])


@pytest.mark.parametrize("options", [
    {"tasks": []}, {"tasks": "total"}, {"tasks": ["total", "total"]},
    {"tasks": ["total_mr"]}, {"tasks": ["--device=cpu"]}, {"tasks": [123]},
    {"tasks": ["test"]}, {"tasks": ["lung_vessels_LEGACY"]},
    {"body_seg": "false"}, {"robust_crop": 1}, {"remove_small_blobs": 200},
    {"fast": True}, {"device": "cpu"},
])
def test_rejects_unknown_tasks_and_untyped_or_unapproved_options(options):
    with pytest.raises(ValueError):
        normalize_options("chest", options)


def test_rejects_body_crop_where_native_task_would_ignore_it():
    with pytest.raises(ValueError, match="already crops"):
        normalize_options("chest", {"tasks": ["lung_vessels"], "body_seg": True})
    assert normalize_options("chest", {"tasks": ["total"], "body_seg": True})["body_seg"] is True


def test_license_check_is_local_and_never_discloses_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALSEG_HOME_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="license configured"):
        normalize_options("chest", {"tasks": ["heartchambers_highres"]})
    secret = "private-key-123456"  # same 18-character offline shape checked upstream
    assert len(secret) == 18
    (tmp_path / "config.json").write_text(json.dumps({"license_number": secret}))
    catalog = segmentation_catalog()
    assert secret not in json.dumps(catalog)
    licensed = next(item for item in catalog["tasks"] if item["id"] == "heartchambers_highres")
    assert licensed["license_available"] is True
    assert normalize_options("chest", {"tasks": ["heartchambers_highres"]})["robust_crop"] is True
    (tmp_path / "config.json").write_text(json.dumps({"license_number": "short-secret"}))
    with pytest.raises(ValueError) as error:
        normalize_options("chest", {"tasks": ["heartchambers_highres"]})
    assert "short-secret" not in str(error.value)


def test_fingerprint_records_exact_task_options_and_auxiliary_crop_models(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(tmp_path / "weights"))

    def fingerprint(task, **flags):
        return segmentation.model_fingerprint("chest", {"tasks": [task], **flags})

    total = fingerprint("total")
    assert {model["dataset_id"] for model in total["models"]} == {291, 292, 293, 294, 295}
    assert total["weights_complete"] is False
    assert fingerprint("total", body_seg=True)["digest"] != total["digest"]
    assert fingerprint("total", remove_small_blobs=True)["digest"] != total["digest"]
    assert fingerprint("total", robust_crop=True)["digest"] != total["digest"]
    assert {model["dataset_id"] for model in fingerprint("total", body_seg=True)["models"]} == {291, 292, 293, 294, 295, 300}
    assert {model["dataset_id"] for model in fingerprint("liver_vessels")["models"]} == {8, 298}
    assert {model["dataset_id"] for model in fingerprint("liver_vessels", robust_crop=True)["models"]} == {8, 297}
    assert {model["dataset_id"] for model in fingerprint("lung_vessels")["models"]} == {117, 297}
    assert fingerprint("lung_vessels", robust_crop=False)["digest"] == fingerprint("lung_vessels", robust_crop=True)["digest"]
    assert {model["dataset_id"] for model in fingerprint("abdominal_muscles")["models"]} == {952, 300}
    assert {model["dataset_id"] for model in fingerprint("teeth")["models"]} == {113, 115, 298}
    assert {model["dataset_id"] for model in fingerprint("vertebrae_pp_refined")["models"]} == {803, 305}
    assert str(tmp_path) not in json.dumps(total)


@pytest.mark.parametrize("task,flags,auxiliary_id", [
    ("total", {"body_seg": True}, 300),
    ("liver_vessels", {"robust_crop": True}, 297),
    ("teeth", {}, 298),
])
def test_auxiliary_checkpoint_changes_invalidate_cache_identity(tmp_path, monkeypatch, task, flags, auxiliary_id):
    weights = tmp_path / "weights"
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(weights))
    options = {"tasks": [task], **flags}
    missing = segmentation.model_fingerprint("chest", options)
    for model in missing["models"]:
        directory = (weights / f"Dataset{model['dataset_id']:03d}_fixture"
                     / f"{model['trainer']}__{model['plans']}__{model['configuration']}")
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plans.json").write_text("{}")
        (directory / "dataset.json").write_text("{}")
        for fold in model["folds"]:
            (directory / f"fold_{fold}").mkdir(exist_ok=True)
            (directory / f"fold_{fold}" / "checkpoint_final.pth").write_bytes(b"fixture weights")
    first = segmentation.model_fingerprint("chest", options)
    assert first["weights_complete"] is True
    auxiliary = next((weights / f"Dataset{auxiliary_id:03d}_fixture").rglob("checkpoint_final.pth"))
    auxiliary.write_bytes(b"different auxiliary model")
    changed = segmentation.model_fingerprint("chest", options)
    assert changed["digest"] != first["digest"]
    unchanged = next(model for model in first["models"] if model["dataset_id"] != auxiliary_id)
    assert unchanged in changed["models"]


@pytest.mark.parametrize("task,flags,expected_flags,names", [
    ("total", {"body_seg": True, "remove_small_blobs": True}, {"--body_seg", "--remove_small_blobs"}, ["liver", "skull"]),
    ("liver_vessels", {"robust_crop": True, "remove_small_blobs": True}, {"--robust_crop", "--remove_small_blobs"}, ["liver_vessels", "liver_tumor"]),
    ("lung_vessels", {"robust_crop": False}, {"--robust_crop"}, sorted(segmentation._LUNG)),
])
def test_selected_task_flags_and_masks_keep_native_patient_geometry(tmp_path, monkeypatch, task, flags, expected_flags, names):
    shape = (8, 9, 10)
    affine = np.array([[-.7, -.15, 0., 34.], [.1, .9, .2, -71.], [0., .1, 1.8, 14.], [0., 0., 0., 1.]])
    ct_path = tmp_path / "ct.nii.gz"
    nib.save(nib.Nifti1Image(np.full(shape, 123, np.int16), affine), ct_path)
    commands = []
    fingerprints = []
    monkeypatch.setattr(segmentation, "_device_info", lambda _: {"selected_device": "gpu:1"})
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu")

    def fingerprint(profile, options):
        fingerprints.append((profile, options))
        return {"digest": "fixture", "weights_complete": True}

    monkeypatch.setattr(segmentation, "model_fingerprint", fingerprint)

    class Worker:
        pid = None  # Completed in-process fixture; no native worker to sample.
        returncode = 0

        def __init__(self, command, **_):
            commands.append(command)
            directory = Path(command[command.index("-o") + 1])
            for name in names:
                mask = np.zeros(shape, np.uint8)
                mask[2:5, 3:6, 4:7] = 1
                nib.save(nib.Nifti1Image(mask, affine), directory / f"{name}.nii.gz")
            Path(command[command.index("--report") + 1]).write_text(json.dumps({
                "device": "gpu", "fast": False, "fastest": False, "save_lowres": False,
                "classes": {str(i + 1): name for i, name in enumerate(names)}}))

        def poll(self):
            return 0

    monkeypatch.setattr(segmentation.subprocess, "Popen", Worker)
    options = {"tasks": [task], **flags}
    output = tmp_path / "output"
    structures = segmentation.segment(ct_path, output, "chest", lambda _: None, lambda: False, options)
    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == [sys.executable, "-u", "-m", "totalsegmentator.bin.TotalSegmentator"]
    assert command[command.index("--task") + 1] == task
    assert command[command.index("--device") + 1] == "gpu:1"
    actual_flags = {flag for flag in command if flag in {"--body_seg", "--robust_crop", "--remove_small_blobs"}}
    assert actual_flags == expected_flags
    assert not {"--fast", "--fastest", "--save_lowres"} & set(command)
    assert {entry["id"] for entry in structures} == {f"{task}:{name}" for name in names}
    for structure in structures:
        image = nib.load(structure["mask_file"])
        assert image.shape == shape
        np.testing.assert_allclose(image.affine, affine, rtol=1e-6, atol=1e-6)
    manifest = json.loads((output / "inference.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["options"] == normalize_options("chest", options)
    assert manifest["tasks"][0]["effective_options"]["remove_small_blobs_mm3"] == (200 if flags.get("remove_small_blobs") else None)
    assert fingerprints == [("chest", normalize_options("chest", options))]
