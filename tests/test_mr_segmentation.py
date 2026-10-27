"""MR task metadata and native worker contracts; no real inference or GPU use."""

import hashlib
import json
from pathlib import Path
import subprocess
import sys

import nibabel as nib
import numpy as np
import pytest

from scan3d import segmentation
from scan3d.segmentation_options import normalize_options, segmentation_catalog


def test_mr_catalog_and_defaults_are_modality_specific_without_torch_import():
    script = (
        "import sys; from scan3d.segmentation_options import segmentation_catalog; "
        "c=segmentation_catalog('MR'); assert 'torch' not in sys.modules; "
        "assert c['defaults']['task']=='total_mr'; print(len(c['tasks']))"
    )
    result = subprocess.run([sys.executable, "-c", script], check=True, capture_output=True, text=True)
    assert int(result.stdout) >= 3
    catalog = segmentation_catalog("MR")
    tasks = {item["id"]: item for item in catalog["tasks"]}
    assert {"total_mr", "body_mr", "vertebrae_mr"} <= tasks.keys()
    assert not {"total", "total_v3", "lung_vessels"} & tasks.keys()
    assert tasks["total_mr"]["class_count"] == 50
    assert tasks["body_mr"]["model_class_count"] == 2 and tasks["vertebrae_mr"]["class_count"] == 25
    assert set(tasks["body_mr"]["classes"]) == {"body_trunc", "body_extremities"}
    assert all(
        item["modality"] == "MR" and not item["body_seg_supported"] and not item["robust_crop_supported"] and not item["robust_crop_default"]
        for item in tasks.values()
    )
    for profile in ("chest", "chest_abdomen", "head_neck", "other"):
        assert normalize_options(profile, modality="MR") == {"tasks": ["total_mr"], "body_seg": False, "robust_crop": False, "remove_small_blobs": False}


@pytest.mark.parametrize(
    "modality,options",
    [
        ("MR", {"tasks": ["total"]}),
        ("MR", {"tasks": ["lung_vessels"]}),
        ("CT", {"tasks": ["total_mr"]}),
        ("CT", {"tasks": ["body_mr"]}),
        ("MR", {"tasks": ["total_mr"], "body_seg": True}),
        ("MR", {"tasks": ["total_mr"], "robust_crop": True}),
    ],
)
def test_wrong_modality_and_ignored_mr_crop_flags_fail_before_runtime(tmp_path, monkeypatch, modality, options):
    monkeypatch.setattr(segmentation, "_device_info", lambda *args: pytest.fail("Invalid options touched GPU"))
    with pytest.raises(ValueError):
        segmentation.segment(tmp_path / "unused.nii.gz", tmp_path / "output", "other", lambda _: None, lambda: False, options, modality=modality)
    assert not (tmp_path / "output").exists()


def test_mr_fingerprints_include_exact_primary_and_native_mr_crop_models(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(tmp_path / "weights"))

    def model_ids(task):
        result = segmentation.model_fingerprint("other", {"tasks": [task]}, modality="MR")
        assert not result["weights_complete"] and result["modality"] == "MR"
        assert str(tmp_path) not in json.dumps(result)
        return {item["dataset_id"] for item in result["models"]}

    assert model_ids("total_mr") == {850, 851}
    assert model_ids("body_mr") == {597}
    assert model_ids("vertebrae_mr") == {756}
    assert model_ids("liver_segments_mr") == {852, 576}
    assert model_ids("brain_aneurysm") == {615}


def install_fixture_weights(weights, descriptor, *, folds=None):
    folder = weights / f"Dataset{descriptor['dataset_id']:03d}_synthetic" / f"{descriptor['trainer']}__{descriptor['plans']}__{descriptor['configuration']}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "plans.json").write_text("{}")
    (folder / "dataset.json").write_text("{}")
    for fold in descriptor["folds"] if folds is None else folds:
        (folder / f"fold_{fold}").mkdir(exist_ok=True)
        (folder / f"fold_{fold}" / "checkpoint_final.pth").write_bytes(b"Synthetic checkpoint identity fixture; not a model")
    return folder


def test_mr_crop_checkpoint_change_invalidates_fingerprint(tmp_path, monkeypatch):
    weights = tmp_path / "weights"
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(weights))
    options = {"tasks": ["liver_segments_mr"]}
    absent = segmentation.model_fingerprint("other", options, modality="MR")
    for model in absent["models"]:
        install_fixture_weights(weights, model)
    first = segmentation.model_fingerprint("other", options, modality="MR")
    assert first["weights_complete"]
    crop = next(item for item in first["models"] if item["dataset_id"] == 852)
    assert crop["resample_mm"] == 3
    checkpoint = next((weights / "Dataset852_synthetic").rglob("checkpoint_final.pth"))
    checkpoint.write_bytes(b"Changed synthetic MR crop checkpoint")
    changed = segmentation.model_fingerprint("other", options, modality="MR")
    assert changed["digest"] != first["digest"]
    assert next(item for item in first["models"] if item["dataset_id"] != 852) in changed["models"]


def test_mr_auto_folds_and_each_checkpoint_are_part_of_fingerprint(tmp_path, monkeypatch):
    weights = tmp_path / "weights"
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(weights))
    options = {"tasks": ["brain_aneurysm"]}
    absent = segmentation.model_fingerprint("head_neck", options, modality="MR")
    assert absent["models"][0]["folds"] is None and not absent["weights_complete"]
    folder = install_fixture_weights(weights, absent["models"][0], folds=[2, 0])
    (folder / "fold_all").mkdir()
    first = segmentation.model_fingerprint("head_neck", options, modality="MR")
    assert first["models"][0]["folds"] == [0, 2] and first["weights_complete"]
    checkpoint = folder / "fold_2" / "checkpoint_final.pth"
    expected_digest = hashlib.sha256(checkpoint.read_bytes()).hexdigest()
    assert any(item["sha256"] == expected_digest for item in first["models"][0]["files"])
    checkpoint.write_bytes(b"Different synthetic fold two checkpoint")
    assert segmentation.model_fingerprint("head_neck", options, modality="MR")["digest"] != first["digest"]


@pytest.mark.parametrize("remove_small_blobs", [False, True])
def test_mr_native_worker_uses_selected_task_and_preserves_reference_geometry(tmp_path, monkeypatch, remove_small_blobs):
    shape = (8, 9, 10)
    affine = np.array([[-0.7, -0.15, 0, 34], [0.1, 0.9, 0.2, -71], [0, 0.1, 1.8, 14], [0, 0, 0, 1.0]])
    path = tmp_path / "signal.nii.gz"
    values = np.linspace(0.001, 0.05, np.prod(shape), dtype=np.float32).reshape(shape)
    nib.save(nib.Nifti1Image(values, affine), path)
    original = path.read_bytes()
    commands, fingerprints = [], []
    monkeypatch.setattr(segmentation, "_device_info", lambda _: {"selected_device": "gpu:1"})
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu:1")

    def fingerprint(profile, options, *, modality):
        fingerprints.append((profile, options, modality))
        return {"digest": "synthetic", "weights_complete": True}

    monkeypatch.setattr(segmentation, "model_fingerprint", fingerprint)

    class Worker:
        pid = None
        returncode = 0

        def __init__(self, command, **_):
            commands.append(command)
            output = Path(command[command.index("-o") + 1])
            mask = np.zeros(shape, np.uint8)
            mask[2:5, 3:6, 4:7] = 1
            nib.save(nib.Nifti1Image(mask, affine), output / "liver.nii.gz")
            Path(command[command.index("--report") + 1]).write_text(
                json.dumps({"device": "gpu", "fast": False, "fastest": False, "save_lowres": False, "classes": {"1": "liver"}})
            )

        def poll(self):
            return 0

    monkeypatch.setattr(segmentation.subprocess, "Popen", Worker)
    options = {"tasks": ["total_mr"], "remove_small_blobs": remove_small_blobs}
    output = tmp_path / "output"
    structures = segmentation.segment(path, output, "other", lambda _: None, lambda: False, options, modality="MR")
    (command,) = commands
    assert command[command.index("--task") + 1] == "total_mr"
    assert command[command.index("--device") + 1] == "gpu:1"
    assert ("--remove_small_blobs" in command) == remove_small_blobs
    assert not {"--body_seg", "--robust_crop", "--fast", "--fastest", "--save_lowres"} & set(command)
    assert fingerprints == [("other", normalize_options("other", options, modality="MR"), "MR")]
    assert structures[0]["id"] == "total_mr:liver"
    np.testing.assert_allclose(nib.load(structures[0]["mask_file"]).affine, affine, atol=1e-6)
    assert path.read_bytes() == original
    manifest = json.loads((output / "inference.json").read_text())
    assert manifest["modality"] == "MR" and manifest["status"] == "completed"
    assert manifest["options"] == normalize_options("other", options, modality="MR")
