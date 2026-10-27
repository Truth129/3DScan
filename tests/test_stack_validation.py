"""Evidence checks for the runtime benchmark, using oblique synthetic masks."""

import argparse
import importlib.util
import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest


spec = importlib.util.spec_from_file_location("stack_validation", Path(__file__).resolve().parents[1] / "scripts/stack_validation.py")
validation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validation)


@pytest.fixture
def masks(tmp_path):
    theta = np.deg2rad(23)
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(theta), -np.sin(theta), 0],
                               [np.sin(theta), np.cos(theta), 0], [0, 0, 1]]) @ np.diag([-2., 3., 4.])
    affine[:3, 3] = [12, -24, 36]
    values = np.zeros((7, 8, 9), np.uint8)
    values[2:5, 2:6, 3:7] = 1

    def save(name, array=values, matrix=affine):
        image = nib.Nifti1Image(array, matrix)
        image.header.set_xyzt_units("mm")
        path = tmp_path / (name + ".nii.gz")
        nib.save(image, path)
        return path
    return values, affine, save


def test_mask_comparison_uses_physical_volume_and_exact_voxels(masks):
    values, _, save = masks
    changed = values.copy()
    changed[0, 0, 0] = 1
    result = validation.compare_masks(save("before"), save("after", changed))
    assert result["changed_voxels"] == 1
    assert result["left_voxels"] == 48
    assert result["right_voxels"] == 49
    assert result["dice"] == pytest.approx(96 / 97)
    assert result["volume_difference_ml"] == pytest.approx(.024)


def test_reflected_or_translated_grid_cannot_pass_comparison(masks):
    values, affine, save = masks
    shifted = affine.copy()
    shifted[0, 3] += 1
    with pytest.raises(ValueError, match="grids differ"):
        validation.compare_masks(save("left"), save("shifted", values, shifted))


def test_comparison_rejects_nonbinary_values(masks):
    values, _, save = masks
    invalid = values.copy()
    invalid[0, 0, 0] = 2
    with pytest.raises(ValueError, match="binary"):
        validation.compare_masks(save("valid"), save("bad", invalid))


def test_empty_masks_agree_exactly(masks):
    values, _, save = masks
    empty = save("empty", np.zeros_like(values))
    result = validation.compare_masks(empty, empty)
    assert result["dice"] == 1
    assert result["changed_voxels"] == 0
    assert result["relative_volume_difference"] is None


def test_seed_selection_is_reproducible_foreground(masks):
    values, affine, _ = masks
    seeds = validation.seed_points(values, affine)
    assert seeds == validation.seed_points(values, affine)
    assert seeds[0] != seeds[1]
    assert all(values[tuple(seed)] == 1 for seed in seeds)
    with pytest.raises(ValueError, match="nonempty"):
        validation.seed_points(np.zeros_like(values), affine)


def test_output_directory_cannot_reuse_existing_artifacts(tmp_path):
    output = validation.new_output(tmp_path / "proof")
    (output / "preserved").write_text("original")
    with pytest.raises(FileExistsError):
        validation.new_output(output)
    assert (output / "preserved").read_text() == "original"


def test_compare_checks_same_ct_options_and_weights(tmp_path, masks):
    _, _, save = masks
    mask = save("mask")
    common = {"status": "passed", "source_ct_sha256": "same-input", "options": {"tasks": ["total"]},
              "profile": "chest_abdomen", "model_fingerprint": {"models": [{"sha256": "same-checkpoint"}]},
              "provenance": {"python": "different-is-allowed"}, "end_to_end_seconds": 4,
              "masks": {"total:liver": str(mask)}}
    left, right = tmp_path / "left", tmp_path / "right"
    left.mkdir()
    right.mkdir()
    validation.write_json(left / "report.json", common)
    validation.write_json(right / "report.json", common)
    args = argparse.Namespace(left=left, right=right, output=tmp_path / "comparison.json")
    validation.compare(args)
    result = json.loads(args.output.read_text())
    assert result["all_voxels_identical"] is True
    assert result["status"] == "completed"
    for key, replacement in (("source_ct_sha256", "different-input"),
                             ("modality", "MR"),
                             ("options", {"tasks": ["total"], "fast": True}),
                             ("model_fingerprint", {"models": [{"sha256": "different-weights"}]})):
        validation.write_json(right / "report.json", {**common, key: replacement})
        with pytest.raises(ValueError):
            validation.compare(args)
