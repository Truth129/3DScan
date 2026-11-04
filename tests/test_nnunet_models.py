"""Pinned nnU-Net models: catalog, weight checks, worker axes and export; no model runs."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import zipfile

import nibabel as nib
import numpy as np
import pytest
from scipy import ndimage as ndi
import SimpleITK as sitk

from scan3d import nnunet_models, nnunet_worker, segmentation
from scan3d.config import ROOT
from scan3d.imaging import _image
from scan3d.segmentation_options import normalize_options, run_source, segmentation_catalog, task_ids
from scripts import download_segmentation_model as download


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


@pytest.fixture
def pinned(tmp_path, monkeypatch):
    """The real registry entry with small stand-in weight files, installed in a scratch folder."""
    monkeypatch.setenv("SCAN3D_SEGMENTATION_MODELS", str(tmp_path / "models"))
    contents = {"dataset.json": b"{}", "plans.json": b"[]", "fold_0/checkpoint_final.pth": b"weights"}
    archive = tmp_path / "weights.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, value in contents.items():
            bundle.writestr(name, value)
    model = replace(
        nnunet_models.MODELS["mrsegmentator"],
        labels=("liver", "spleen"),
        folds=(0,),
        files={name: sha256(value) for name, value in contents.items()},
        archive_sha256=sha256(archive.read_bytes()),
    )
    monkeypatch.setitem(nnunet_models.MODELS, model.id, model)
    return model, archive


def install(model, archive):
    target = nnunet_models.model_dir(model)
    target.parent.mkdir(parents=True, exist_ok=True)
    download.install(model, archive, target)
    return target


def test_catalog_offers_the_model_on_both_modalities_and_says_how_to_install_it(pinned):
    model, archive = pinned
    for modality in ("CT", "MR"):
        assert model.id in task_ids(modality)
        entry = next(item for item in segmentation_catalog(modality)["tasks"] if item["id"] == model.id)
        assert entry["available"] is False and entry["comparison"] is True and entry["modality"] == modality
        assert f"download_segmentation_model.py {model.id}" in entry["description"]
        assert not entry["remove_small_blobs_supported"] and not entry["body_seg_supported"]
    install(model, archive)
    assert next(item for item in segmentation_catalog("MR")["tasks"] if item["id"] == model.id)["available"] is True


def test_options_refuse_missing_weights_other_tasks_and_totalsegmentator_switches(pinned):
    model, archive = pinned
    with pytest.raises(ValueError, match="not installed"):
        normalize_options("other", {"tasks": [model.id]})
    install(model, archive)
    options = normalize_options("other", {"tasks": [model.id]}, modality="MR")
    assert options["tasks"] == [model.id] and run_source(options) == "nnunet_model"
    assert run_source({"tasks": ["total"]}) == "totalsegmentator"
    with pytest.raises(ValueError, match="on its own"):
        normalize_options("other", {"tasks": [model.id, "total"]})
    with pytest.raises(ValueError, match="no crop or small-blob"):
        normalize_options("other", {"tasks": [model.id], "remove_small_blobs": True})


def test_run_starts_the_worker_and_records_the_model_as_its_engine(pinned, tmp_path, monkeypatch):
    model, archive = pinned
    target = install(model, archive)
    shape = (8, 9, 10)
    affine = np.array([[-0.7, -0.15, 0.0, 34.0], [0.1, 0.9, 0.2, -71.0], [0.0, 0.1, 1.8, 14.0], [0.0, 0.0, 0.0, 1.0]])
    image = tmp_path / "image.nii.gz"
    nib.save(nib.Nifti1Image(np.full(shape, 123, np.int16), affine), image)
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: {"selected_device": "gpu:0", "torch": "test"})
    launches = []

    class Worker:
        pid = None  # Completed in-process fixture; no native worker to sample.
        returncode = 0

        def __init__(self, command, **options):
            launches.append((command, options))
            directory = Path(command[command.index("-o") + 1])
            for name, filled in (("liver", True), ("spleen", False)):
                nib.save(nib.Nifti1Image(np.full(shape, filled, np.uint8), affine), directory / f"{name}.nii.gz")
            Path(command[command.index("--report") + 1]).write_text(json.dumps({"device": "gpu", "classes": {"1": "liver", "2": "spleen"}}))

        def poll(self):
            return 0

    monkeypatch.setattr(segmentation.subprocess, "Popen", Worker)
    output = tmp_path / "output"
    structures = segmentation.segment(image, output, "other", lambda _: None, lambda: False, {"tasks": [model.id]}, modality="MR")
    command, options = launches[0]
    assert command[:5] == [sys.executable, "-u", "-P", "-m", "scan3d.nnunet_worker"]
    assert command[command.index("--model-folder") + 1] == str(target)
    assert str(ROOT) in options["env"]["PYTHONPATH"].split(":")
    assert [(item["id"], item["name"], item["group"]) for item in structures] == [("mrsegmentator:liver", "Liver (MRSegmentator)", "MRSegmentator")]
    manifest = json.loads((output / "inference.json").read_text())
    assert (manifest["engine"], manifest["engine_version"]) == ("MRSegmentator", model.version)
    assert manifest["empty_labels"] == ["mrsegmentator:spleen"]
    assert manifest["model_fingerprint"]["weights_complete"] is True


def test_changed_weights_stop_a_run_before_any_child_can_load_them(pinned, tmp_path, monkeypatch):
    model, archive = pinned
    target = install(model, archive)
    (target / "fold_0" / "checkpoint_final.pth").write_bytes(b"not the pinned weights")
    image = tmp_path / "image.nii.gz"
    nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), np.int16), np.eye(4)), image)
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: pytest.fail("A child started"))
    with pytest.raises(RuntimeError, match="pinned checksums"):
        segmentation.segment(image, tmp_path / "output", "other", lambda _: None, lambda: False, {"tasks": [model.id]}, modality="MR")


@pytest.mark.parametrize("codes", [("L", "P", "S"), ("R", "A", "S"), ("A", "R", "I")])
def test_model_input_matches_the_simpleitk_reader_the_model_was_trained_with(tmp_path, codes):
    values = np.random.default_rng(1).integers(-1000, 1000, (7, 9, 11)).astype(np.int16)
    affine = np.eye(4)
    affine[:3, :3] = 0
    affine[:3, 3] = 5, -7, 11
    for column, (code, size) in enumerate(zip(codes, (0.8, 1.1, 2.5))):
        axis, sign = {"R": (0, 1), "L": (0, -1), "A": (1, 1), "P": (1, -1), "S": (2, 1), "I": (2, -1)}[code]
        affine[axis, column] = sign * size
    path = tmp_path / "image.nii.gz"
    nib.save(nib.Nifti1Image(values, affine), path)
    assert nib.aff2axcodes(affine) == codes
    upstream = sitk.DICOMOrient(sitk.ReadImage(str(path)), "LPS")
    reference, native = _image(path)
    array, spacing, back = nnunet_worker.model_input(reference, native)
    assert np.array_equal(array[0], sitk.GetArrayFromImage(upstream))
    np.testing.assert_allclose(spacing, list(upstream.GetSpacing())[::-1])
    assert np.array_equal(nnunet_worker.native_labels(array[0], back), values)


def test_model_axes_on_a_sheared_grid_face_the_patient_and_keep_every_voxel():
    shape = (6, 7, 8)
    affine = np.array([[-0.7, -0.15, 0.0, 34.0], [0.1, 0.0, 1.2, -71.0], [0.05, 1.1, 0.2, 14.0], [0.0, 0.0, 0.0, 1.0]])
    world = affine[:3, :3] @ np.indices(shape).reshape(3, -1) + affine[:3, 3:]
    right, anterior, superior = (nib.Nifti1Image(axis.reshape(shape).astype(np.float32), affine) for axis in world)
    array = {name: nnunet_worker.model_input(image, affine)[0][0] for name, image in (("R", right), ("A", anterior), ("S", superior))}
    assert np.diff(array["S"], axis=0).mean() > 0 and np.diff(array["A"], axis=1).mean() < 0 and np.diff(array["R"], axis=2).mean() < 0
    _, _, back = nnunet_worker.model_input(right, affine)
    np.testing.assert_array_equal(nnunet_worker.native_labels(array["R"], back), np.asarray(right.dataobj))


def test_one_channel_export_gives_nnunets_full_export():
    logits = np.random.default_rng(2).normal(size=(5, 8, 9, 10)).astype(np.float32)
    cropped = (10, 12, 14)
    zoom = [new / old for new, old in zip(cropped, logits.shape[1:])]
    properties = {
        "shape_after_cropping_and_before_resampling": cropped,
        "shape_before_cropping": (13, 15, 16),
        "bbox_used_for_cropping": [[1, 11], [2, 14], [0, 14]],
    }

    def resample(data):
        return np.stack([ndi.zoom(channel, zoom, order=1) for channel in data])

    labels = nnunet_worker.full_size_labels(logits, resample, properties, [2, 0, 1])
    expected = np.zeros(properties["shape_before_cropping"], np.uint8)
    expected[1:11, 2:14, 0:14] = resample(logits).argmax(0)
    assert labels.dtype == np.uint8 and np.array_equal(labels, expected.transpose(2, 0, 1))


def test_download_publishes_only_pinned_files_that_match(pinned):
    model, archive = pinned
    target = nnunet_models.model_dir(model)
    target.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="archive failed"):
        download.install(replace(model, archive_sha256="0" * 64), archive, target)
    with pytest.raises(RuntimeError, match="plans.json failed"):
        download.install(replace(model, files={**model.files, "plans.json": "0" * 64}), archive, target)
    assert list(target.parent.iterdir()) == []
    download.install(model, archive, target)
    assert sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()) == sorted(model.files)
