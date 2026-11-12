"""Pinned nnU-Net models: catalog, weight checks, worker axes and export; no model runs."""

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import zipfile

import nibabel as nib
import numpy as np
import pytest
from scipy import ndimage as ndi
import SimpleITK as sitk

from inference_path_stub import add_package
import scan3d
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
    with pytest.raises(ValueError, match="no crop, small-blob or resampling"):
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
            Path(command[command.index("--progress") + 1]).write_text("fold 1 of 1")
            self.polls = 0

        def poll(self):
            # Still running at the first look, so the parent reads the step once.
            self.polls += 1
            return None if self.polls == 1 else 0

    monkeypatch.setattr(segmentation.subprocess, "Popen", Worker)
    output = tmp_path / "output"
    stages = []
    structures = segmentation.segment(image, output, "other", stages.append, lambda: False, {"tasks": [model.id]}, modality="MR")
    command, options = launches[0]
    assert any(stage.startswith(f"Running full-resolution MRSegmentator: {model.id}, fold 1 of 1 (") for stage in stages)
    assert command[:5] == [sys.executable, "-u", "-P", "-m", "scan3d.nnunet_worker"]
    assert command[command.index("--model-folder") + 1] == str(target)
    assert str(ROOT) in options["env"]["PYTHONPATH"].split(":")
    assert [(item["id"], item["name"], item["group"]) for item in structures] == [("mrsegmentator:liver", "Liver (MRSegmentator)", "MRSegmentator")]
    manifest = json.loads((output / "inference.json").read_text())
    assert (manifest["engine"], manifest["engine_version"]) == ("MRSegmentator", model.version)
    assert manifest["empty_labels"] == ["mrsegmentator:spleen"]
    assert manifest["model_fingerprint"]["weights_complete"] is True


def test_a_cas_net_run_keeps_its_probability_beside_the_masks_for_grow_branch(pinned, tmp_path, monkeypatch):
    model, archive = pinned
    model = replace(model, engine="cas_net")
    monkeypatch.setitem(nnunet_models.MODELS, model.id, model)
    install(model, archive)
    image = tmp_path / "image.nii.gz"
    nib.save(nib.Nifti1Image(np.zeros((4, 5, 6), np.int16), np.eye(4)), image)
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: {"selected_device": "cpu", "torch": "test"})
    commands = []

    class Worker:
        pid, returncode = None, 0

        def __init__(self, command, **_):
            commands.append(command)
            nib.save(nib.Nifti1Image(np.ones((4, 5, 6), np.uint8), np.eye(4)), Path(command[command.index("-o") + 1]) / "liver.nii.gz")
            Path(command[command.index("--report") + 1]).write_text(json.dumps({"device": "cpu", "classes": {"1": "liver"}}))
            if "--probability" in command:
                nib.save(nib.Nifti1Image(np.full((4, 5, 6), 200, np.uint8), np.eye(4)), command[command.index("--probability") + 1])

        def poll(self):
            return 0

    monkeypatch.setattr(segmentation.subprocess, "Popen", Worker)
    structures = segmentation.segment(image, tmp_path / "output", "other", lambda _: None, lambda: False, {"tasks": [model.id]}, modality="MR")
    stored = Path(commands[0][commands[0].index("--probability") + 1])
    # Outside the task's folder, so it is never read as a mask.
    assert stored.parent == Path(structures[0]["mask_file"]).parent.parent and structures[0]["probability_file"] == str(stored)
    assert [item["id"] for item in structures] == [f"{model.id}:liver"]


def test_changed_weights_stop_a_run_before_any_child_can_load_them(pinned, tmp_path, monkeypatch):
    model, archive = pinned
    target = install(model, archive)
    (target / "fold_0" / "checkpoint_final.pth").write_bytes(b"not the pinned weights")
    image = tmp_path / "image.nii.gz"
    nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), np.int16), np.eye(4)), image)
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: pytest.fail("A child started"))
    with pytest.raises(RuntimeError, match="pinned checksums"):
        segmentation.segment(image, tmp_path / "output", "other", lambda _: None, lambda: False, {"tasks": [model.id]}, modality="MR")


@pytest.mark.parametrize("axes", ["LPS", "LAS", "RAS"])
@pytest.mark.parametrize("codes", [("L", "P", "S"), ("R", "A", "S"), ("A", "R", "I")])
def test_model_input_matches_the_simpleitk_reader_the_model_was_trained_with(tmp_path, codes, axes):
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
    upstream = sitk.DICOMOrient(sitk.ReadImage(str(path)), axes)
    reference, native = _image(path)
    array, spacing, back = nnunet_worker.model_input(reference, native, axes)
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


def test_small_pieces_go_and_only_face_neighbours_join_a_piece():
    mask = np.zeros((20, 20, 20), bool)
    mask[0:5, 0:5, 0:3] = True  # 75 voxels
    mask[5:10, 5:10, 3:4] = True  # 25 more, touching the first only at a corner, as the benchmark counts
    mask[12:17, 12:17, 12:16] = True  # 100 voxels
    assert np.array_equal(nnunet_worker.without_small_pieces(mask, 0), mask)
    kept = nnunet_worker.without_small_pieces(mask, 100)
    assert kept.sum() == 100 and kept[12:17, 12:17, 12:16].all()
    assert nnunet_models.MODELS["imagecasx_coronary"].smallest_piece == nnunet_models.MODELS["imagecasx_cas_net"].smallest_piece == 100
    assert nnunet_models.MODELS["mrsegmentator"].smallest_piece == 0


def test_largest_piece_joins_only_face_neighbours_and_keeps_the_first_of_equal_pieces():
    # Expected masks are the Spinal Cord Toolbox's keep_largest_object on the same arrays.
    corner = np.zeros((6, 6), bool)
    corner[0:2, 0:2] = corner[2:5, 2:5] = True  # Touching only at a corner.
    assert np.array_equal(nnunet_worker.largest_piece(corner, True), np.pad(np.ones((3, 3), bool), ((2, 1), (2, 1))))
    assert nnunet_worker.largest_piece(corner, False) is corner
    edge = np.zeros((4, 6, 6), bool)
    edge[0:2, 0:2, 0:3] = edge[2:4, 2:5, 0:3] = True  # Touching only along an edge.
    kept = nnunet_worker.largest_piece(edge, True)
    assert kept.sum() == 18 and kept[2:4, 2:5, 0:3].all()
    # Of equal pieces, the one whose first voxel comes first in C order, not the one nearest the origin.
    equal = np.zeros((6, 6), bool)
    equal[3:5, 0:2] = equal[0:2, 3:5] = True
    expected = np.zeros((6, 6), bool)
    expected[0:2, 3:5] = True
    assert np.array_equal(nnunet_worker.largest_piece(equal, True), expected)
    ends = np.zeros((5, 10, 5), bool)
    ends[1:4, 0:3, 1:4] = ends[1:4, 7:10, 1:4] = True
    kept = nnunet_worker.largest_piece(ends, True)
    assert kept.sum() == 27 and kept[1:4, 0:3, 1:4].all()
    # In Fortran order, as nibabel reads a file, the numbering is still C order.
    assert np.array_equal(nnunet_worker.largest_piece(np.asfortranarray(equal), True), expected)
    empty, single = np.zeros((3, 4, 5), bool), np.zeros((3, 4, 5), bool)
    single[1, 2, 3] = True
    assert not nnunet_worker.largest_piece(empty, True).any()
    assert np.array_equal(nnunet_worker.largest_piece(single, True), single)


def test_cas_net_returns_bright_voxels_on_the_source_grid_through_its_tiles_mirrors_and_resampling(monkeypatch):
    torch = pytest.importorskip("torch")
    from scan3d import cas_net

    assert cas_net.CASNet3D().eval()(torch.zeros(1, 1, 16, 16, 16)).shape == (1, 2, 16, 16, 16)

    class Bright(torch.nn.Module):
        """Class 1 wins wherever the scaled scan is above 0.5, which is 400 HU, whatever the tile or mirror."""

        def forward(self, tile):
            return torch.cat([torch.full_like(tile, 0.5), tile], dim=1)

    # Small tiles, so each axis has several, and the last one is snapped to the edge.
    monkeypatch.setattr(cas_net, "PATCH", (8, 12, 16))
    monkeypatch.setattr(cas_net, "network", lambda *_: Bright())
    model = nnunet_models.MODELS["imagecasx_cas_net"]
    scan = np.zeros((1, 21, 30, 19), np.float32)  # z, y, x
    scan[0, 4:15, 6:9, 2:13] = 800
    steps = []
    labels, _, probability = cas_net.predict(model, "folder", scan, [0.5, 0.5, 0.5], "cpu", steps.append)
    assert labels.dtype == np.uint8 and np.array_equal(labels, scan[0] > 400)
    # p = (1 + sigmoid(lumen) - sigmoid(background)) / 2 as round(p * 255), on the same grid as the mask.
    # Away from the bright voxels the far corners of the scan stitch from tiny tile weights, so only the side of 0.5 is checked there.
    sigmoid = lambda value: 1 / (1 + np.exp(-value))  # noqa: E731
    assert probability.dtype == np.uint8 and np.array_equal(probability > 127, labels == 1)
    assert set(np.unique(probability[labels == 1])) == {round((1 + sigmoid(1000 / 1200) - sigmoid(0.5)) / 2 * 255)}
    assert steps == ["preparing the scan", *(f"pass {n} of 4" for n in range(1, 5)), "saving the masks"]
    # Coarser than 0.5 mm along z and finer along y; the voxel centres here survive both resamplings.
    labels, _, _ = cas_net.predict(model, "folder", scan, [1.0, 0.25, 0.5], "cpu")
    assert np.array_equal(labels, scan[0] > 400)


def test_download_takes_the_model_from_its_folder_in_a_larger_archive(pinned, tmp_path):
    model, _ = pinned
    archive = tmp_path / "several-models.zip"
    contents = {"dataset.json": b"{}", "plans.json": b"[]", "fold_0/checkpoint_best.pth": b"weights"}
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("other_model/weights.pt", b"not this one")
        for name, value in contents.items():
            bundle.writestr(f"pretrained/nnunet/{name}", value)
    model = replace(
        model,
        checkpoint="checkpoint_best.pth",
        archive_folder="pretrained/nnunet",
        files={name: sha256(value) for name, value in contents.items()},
        archive_sha256=sha256(archive.read_bytes()),
    )
    target = install(model, archive)
    assert sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()) == sorted(contents)


def test_every_pinned_model_lists_its_own_checkpoint_for_each_fold():
    for model in nnunet_models.MODELS.values():
        assert model.engine in ("nnunet", "cas_net", "sc_crop")
        if model.engine == "sc_crop":
            assert set(model.files) == {"det_model.onnx", "cls_model.onnx"} and not model.modalities
            continue
        assert not model.crop or nnunet_models.MODELS[model.crop].engine == "sc_crop"
        if model.engine == "cas_net":
            assert set(model.files) == {model.checkpoint} and not model.folds and not model.skull_strip
            assert "cas_net_sha256" in segmentation._nnunet_descriptor(model)
            continue
        assert {f"fold_{fold}/{model.checkpoint}" for fold in model.folds} | {"dataset.json", "plans.json"} == set(model.files)
        assert not model.dataset_labels or len(model.dataset_labels) == len(model.labels)
        # The worker applies the first model's mask to the second model's input array unchanged.
        assert not model.skull_strip or nnunet_models.MODELS[model.skull_strip].axes == model.axes


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


def stand_in(model_id, tmp_path, monkeypatch):
    """A registry entry with small stand-in weights in an archive laid out like the real one."""
    model = nnunet_models.MODELS[model_id]
    contents = {name: f"{model_id} {name}".encode() for name in model.files}
    archive = tmp_path / f"{model_id}.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, value in contents.items():
            bundle.writestr(f"{model.archive_folder}/{name}" if model.archive_folder else name, value)
    model = replace(model, files={name: sha256(value) for name, value in contents.items()}, archive_sha256=sha256(archive.read_bytes()))
    monkeypatch.setitem(nnunet_models.MODELS, model.id, model)
    return model, archive


def test_flames_needs_hd_bet_and_a_run_checks_both_before_starting(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_SEGMENTATION_MODELS", str(tmp_path / "models"))
    flames, flames_archive = stand_in("flames_ms_lesions", tmp_path, monkeypatch)
    hdbet, hdbet_archive = stand_in("hdbet_brain", tmp_path, monkeypatch)
    install(flames, flames_archive)
    assert not nnunet_models.installed(flames)
    install(hdbet, hdbet_archive)
    assert nnunet_models.installed(flames) and nnunet_models.installed(hdbet)
    descriptor = segmentation._nnunet_descriptor(flames)
    folders = {item["name"].split("/")[0] for item in descriptor["files"]}
    assert descriptor["complete"] and folders == {"flames_ms_lesions-hf-ea055ae", "hdbet_brain-zenodo-14445620"}
    image = tmp_path / "image.nii.gz"
    nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), np.int16), np.eye(4)), image)
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: {"selected_device": "cpu", "torch": "test"})
    commands = []

    class Started(Exception):
        pass

    def started(command, **_):
        commands.append(command)
        raise Started

    monkeypatch.setattr(segmentation.subprocess, "Popen", started)
    with pytest.raises(Started):
        segmentation.segment(image, tmp_path / "run", "other", lambda _: None, lambda: False, {"tasks": [flames.id]}, modality="MR")
    assert commands[0][commands[0].index("--skull-strip-folder") + 1] == str(nnunet_models.model_dir(hdbet))
    (nnunet_models.model_dir(hdbet) / "fold_all" / "checkpoint_final.pth").write_bytes(b"not the pinned weights")
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: pytest.fail("A child started"))
    with pytest.raises(RuntimeError, match="pinned checksums"):
        segmentation.segment(image, tmp_path / "again", "other", lambda _: None, lambda: False, {"tasks": [flames.id]}, modality="MR")


@pytest.fixture
def nnunet_version(monkeypatch):
    """The worker's report reads nnU-Net's installed version, and CI installs no nnU-Net."""
    monkeypatch.setattr(nnunet_worker, "metadata", SimpleNamespace(version=lambda _: "test"))


def test_worker_gives_flames_only_the_voxels_hd_bet_keeps(tmp_path, monkeypatch, nnunet_version):
    shape = (6, 7, 8)
    affine = np.array([[-0.9, 0.1, 0.0, 30.0], [0.1, 0.0, 1.0, -60.0], [0.0, 1.2, 0.1, 10.0], [0.0, 0.0, 0.0, 1.0]])
    values = np.random.default_rng(3).integers(1, 1000, shape).astype(np.int16)
    nib.save(nib.Nifti1Image(values, affine), tmp_path / "image.nii.gz")
    seen = []

    def predict(model, folder, image, spacing, device, step):
        seen.append((model.id, folder, image.copy()))
        if model.id == "hdbet_brain":
            brain = np.zeros(image.shape[1:], np.uint8)
            brain[1:4, 2:5, 3:6] = 1
            return brain, "test"
        return (image[0] > 0).astype(np.uint8), "test"

    monkeypatch.setattr(nnunet_worker, "predict", predict)
    arguments = f"-i {tmp_path}/image.nii.gz -o {tmp_path} --task flames_ms_lesions --model-folder flames --skull-strip-folder hdbet"
    monkeypatch.setattr(sys, "argv", ["worker", *arguments.split(), "--device", "cpu", "--report", str(tmp_path / "report.json")])
    nnunet_worker.main()
    assert [(model, str(folder)) for model, folder, _ in seen] == [("hdbet_brain", "hdbet"), ("flames_ms_lesions", "flames")]
    raw, stripped = seen[0][2][0], seen[1][2][0]
    brain = np.zeros(raw.shape, bool)
    brain[1:4, 2:5, 3:6] = True
    assert np.array_equal(stripped[brain], raw[brain]) and not stripped[~brain].any()
    mask = nib.load(tmp_path / "white_matter_lesions.nii.gz")
    _, _, back = nnunet_worker.model_input(nib.load(tmp_path / "image.nii.gz"), affine, "LAS")
    np.testing.assert_allclose(mask.affine, affine)
    assert np.array_equal(np.asarray(mask.dataobj) > 0, nnunet_worker.native_labels(brain, back))


def test_download_of_separate_files_publishes_only_files_that_match(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_SEGMENTATION_MODELS", str(tmp_path / "models"))
    model = nnunet_models.MODELS["breastdivider"]
    served = {f"{model.url}{name}": f"stand-in {name}".encode() for name in model.files}
    model = replace(model, files={name: sha256(served[model.url + name]) for name in model.files})

    class Response:
        def __init__(self, url, **_):
            self.content = served[url]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, _):
            yield self.content

    monkeypatch.setattr(download.requests, "get", Response)
    target = nnunet_models.model_dir(model)
    target.parent.mkdir(parents=True)
    with pytest.raises(RuntimeError, match="plans.json failed"):
        download.install_files(replace(model, files={**model.files, "plans.json": "0" * 64}), target)
    assert list(target.parent.iterdir()) == []
    download.install_files(model, target)
    assert sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()) == sorted(model.files)


def test_a_model_behind_a_sign_in_installs_only_from_a_downloaded_zip(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_SEGMENTATION_MODELS", str(tmp_path / "models"))
    model, archive = stand_in("mamamia_breast_tumour", tmp_path, monkeypatch)
    assert "--archive FILE" in nnunet_models.install_hint(model) and model.url in nnunet_models.install_hint(model)
    monkeypatch.setattr(download.requests, "get", lambda *_, **__: pytest.fail("A sign-in download was attempted"))
    monkeypatch.setattr(sys, "argv", ["download", model.id])
    with pytest.raises(SystemExit, match="--archive FILE"):
        download.main()
    monkeypatch.setattr(sys, "argv", ["download", model.id, "--archive", str(archive)])
    download.main()
    assert nnunet_models.installed(model)


def placed(model_id, monkeypatch):
    """A registry entry with small stand-in files already in its model folder."""
    model = nnunet_models.MODELS[model_id]
    contents = {name: f"{model_id} {name}".encode() for name in model.files}
    model = replace(model, files={name: sha256(value) for name, value in contents.items()})
    monkeypatch.setitem(nnunet_models.MODELS, model.id, model)
    for name, value in contents.items():
        (nnunet_models.model_dir(model) / name).parent.mkdir(parents=True, exist_ok=True)
        (nnunet_models.model_dir(model) / name).write_bytes(value)
    return model


@pytest.mark.parametrize("task", ["sct_cord_ms_lesions", "sct_spinal_cord"])
def test_cord_models_are_offered_on_mr_only_need_the_hidden_detector_and_a_run_checks_both(tmp_path, monkeypatch, task):
    monkeypatch.setenv("SCAN3D_SEGMENTATION_MODELS", str(tmp_path / "models"))
    model, detector = nnunet_models.MODELS[task], nnunet_models.MODELS["sc_crop_detector"]
    assert model.id in task_ids("MR") and model.id not in task_ids("CT")
    assert detector.id not in task_ids("MR") + task_ids("CT")
    with pytest.raises(ValueError, match="supported MR segmentation task"):
        normalize_options("other", {"tasks": [detector.id]}, modality="MR")
    model = placed(model.id, monkeypatch)
    entry = next(item for item in segmentation_catalog("MR")["tasks"] if item["id"] == model.id)
    assert entry["available"] is False and f"download_segmentation_model.py {model.id}" in entry["description"]
    detector = placed(detector.id, monkeypatch)
    assert next(item for item in segmentation_catalog("MR")["tasks"] if item["id"] == model.id)["available"] is True
    descriptor = segmentation._nnunet_descriptor(model)
    assert descriptor["complete"] and {item["name"].split("/")[0] for item in descriptor["files"]} == {
        f"{model.id}-{model.version}",
        "sc_crop_detector-v0.0.11",
    }
    assert descriptor.get("keep_largest", False) is model.keep_largest
    assert descriptor["sc_crop_sha256"] and descriptor["onnxruntime_version"] and descriptor["crop_padding"]["inferior"] == 100.0
    # An added path's worker imports its own onnxruntime, so the version comes from its site-packages.
    added = SimpleNamespace(site_packages=tmp_path / "site")
    assert segmentation._nnunet_descriptor(model, added)["onnxruntime_version"] == "unavailable"
    add_package(added.site_packages, "onnxruntime", "9.9.9")
    assert segmentation._nnunet_descriptor(model, added)["onnxruntime_version"] == "9.9.9"
    image = tmp_path / "image.nii.gz"
    nib.save(nib.Nifti1Image(np.zeros((4, 4, 4), np.int16), np.eye(4)), image)
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: {"selected_device": "cpu", "torch": "test"})
    commands = []

    class NoCord:
        pid = None  # Completed in-process fixture; no native worker to sample.
        returncode = 1
        report = {"error": "No spinal cord was found in this series."}

        def __init__(self, command, **_):
            commands.append(command)
            Path(command[command.index("--report") + 1]).write_text(json.dumps(self.report))

        def poll(self):
            return 1

    monkeypatch.setattr(segmentation.subprocess, "Popen", NoCord)
    with pytest.raises(RuntimeError, match="^No spinal cord was found in this series.$"):
        segmentation.segment(image, tmp_path / "run", "other", lambda _: None, lambda: False, {"tasks": [model.id]}, modality="MR")
    assert commands[0][commands[0].index("--crop-folder") + 1] == str(nnunet_models.model_dir(detector))
    NoCord.report = {}
    with pytest.raises(RuntimeError, match=r"failed \(exit 1\)"):
        segmentation.segment(image, tmp_path / "bare", "other", lambda _: None, lambda: False, {"tasks": [model.id]}, modality="MR")
    (nnunet_models.model_dir(detector) / "det_model.onnx").write_bytes(b"not the pinned detector")
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: pytest.fail("A child started"))
    with pytest.raises(RuntimeError, match=rf"checksums; nothing ran\. Move sc_crop_detector-v0\.0\.11 aside and run .*\.py {model.id} again\.$"):
        segmentation.segment(image, tmp_path / "again", "other", lambda _: None, lambda: False, {"tasks": [model.id]}, modality="MR")


def test_a_model_in_several_zips_installs_only_when_every_zip_and_file_matches(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_SEGMENTATION_MODELS", str(tmp_path / "models"))
    model = nnunet_models.MODELS["sct_cord_ms_lesions"]
    shared = {"dataset.json": b"{}", "plans.json": b"[]", "trainer_class.py": b"never unpacked"}
    zips = {}
    for fold in (0, 1):
        zips[fold] = tmp_path / f"model_fold{fold}.zip"
        with zipfile.ZipFile(zips[fold], "w") as bundle:
            bundle.writestr("crop_metadata.json", b"{}")
            for name, value in {**shared, f"fold_{fold}/checkpoint_best.pth": f"fold {fold}".encode()}.items():
                bundle.writestr(f"{model.archive_folder}/{name}", value)
    files = {name: sha256(shared[name]) for name in ("dataset.json", "plans.json")}
    files |= {f"fold_{fold}/checkpoint_best.pth": sha256(f"fold {fold}".encode()) for fold in (0, 1)}
    model = replace(model, folds=(0, 1), files=files, archives={path.name: sha256(path.read_bytes()) for path in zips.values()})
    monkeypatch.setitem(nnunet_models.MODELS, model.id, model)
    target = nnunet_models.model_dir(model)
    target.parent.mkdir(parents=True)
    other = tmp_path / "other.zip"
    with zipfile.ZipFile(other, "w") as bundle:
        bundle.writestr("weights.pt", b"not this model")
    with pytest.raises(RuntimeError, match="archive failed"):
        download.install(model, [zips[0], other], target)
    with pytest.raises(RuntimeError, match="needs all 2"):
        download.install(model, [zips[0]], target)
    with pytest.raises(RuntimeError, match="fold_1/checkpoint_best.pth failed"):
        download.install(replace(model, files={**files, "fold_1/checkpoint_best.pth": "0" * 64}), list(zips.values()), target)
    assert list(target.parent.iterdir()) == []
    detector = nnunet_models.MODELS[model.crop]
    served = {detector.url + name: f"stand-in {name}".encode() for name in detector.files}
    monkeypatch.setitem(nnunet_models.MODELS, detector.id, replace(detector, files={name: sha256(served[detector.url + name]) for name in detector.files}))

    class Response:
        def __init__(self, url, **_):
            self.content = served[url]

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, _):
            yield self.content

    monkeypatch.setattr(download.requests, "get", Response)
    monkeypatch.setattr(sys, "argv", ["download", model.id, "--archive", str(zips[1]), "--archive", str(zips[0])])
    download.main()
    assert nnunet_models.installed(model)
    assert sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file()) == sorted(files)
    # A detector folder moved aside comes back alone; the served files hold no zip.
    nnunet_models.model_dir(detector).rename(tmp_path / "aside")
    monkeypatch.setattr(sys, "argv", ["download", model.id])
    download.main()
    assert nnunet_models.installed(model)
    with pytest.raises(SystemExit, match="already exists"):
        download.main()
    # With its own folder moved aside, the model comes back from its zips, fetched this time.
    served |= {model.url + path.name: path.read_bytes() for path in zips.values()}
    target.rename(tmp_path / "aside-model")
    download.main()
    assert nnunet_models.installed(model)


def cord_crop(monkeypatch, crop_box):
    """A stand-in for scan3d.sc_crop, whose real crop_box needs onnxruntime and the detector files."""
    stand_in = SimpleNamespace(crop_box=crop_box, CordNotFound=type("CordNotFound", (RuntimeError,), {}))
    monkeypatch.setitem(sys.modules, "scan3d.sc_crop", stand_in)
    monkeypatch.setattr(scan3d, "sc_crop", stand_in, raising=False)
    return stand_in


def test_worker_runs_the_cord_model_on_the_box_and_puts_the_mask_back_on_a_reflected_sheared_grid(tmp_path, monkeypatch, nnunet_version):
    shape = (9, 10, 11)
    # A reflected first axis and two sheared ones, whose float64 lengths are not the rounded float32 zooms the toolbox reads.
    affine = np.array([[-0.74999999925, 0.1, 0.05, 30.0], [0.0, 0.0, 1.1, -60.0], [0.0, 1.2, 0.2, 10.0], [0.0, 0.0, 0.0, 1.0]])
    values = np.random.default_rng(4).integers(1, 1000, shape).astype(np.int16)
    nib.save(nib.Nifti1Image(values, affine), tmp_path / "image.nii.gz")
    bounds = ((2, 7), (1, 9), (3, 8))
    box = tuple(slice(*limits) for limits in bounds)
    calls = []

    def crop_box(image, folder, padding):
        calls.append((image.shape, str(folder), padding))
        return bounds

    stand_in = cord_crop(monkeypatch, crop_box)
    seen = []

    def predict(model, folder, image, spacing, device, step):
        seen.append((image.copy(), spacing))
        return (image[0] % 2 == 0).astype(np.uint8), "test"

    monkeypatch.setattr(nnunet_worker, "predict", predict)
    steps = tmp_path / "progress.txt"
    arguments = f"-i {tmp_path}/image.nii.gz -o {tmp_path} --task sct_cord_ms_lesions --model-folder lesions --crop-folder detector"
    report = tmp_path / "report.json"
    monkeypatch.setattr(sys, "argv", ["worker", *arguments.split(), "--device", "cpu", "--report", str(report), "--progress", str(steps)])
    nnunet_worker.main()
    model = nnunet_models.MODELS["sct_cord_ms_lesions"]
    assert calls == [(shape, "detector", model.crop_padding)]
    cropped_affine = affine.copy()
    cropped_affine[:3, 3] = affine[:3, :3] @ [2, 1, 3] + affine[:3, 3]
    expected, spacing, _ = nnunet_worker.model_input(nib.Nifti1Image(values[box], cropped_affine), cropped_affine, "LAS")
    assert np.array_equal(seen[0][0], expected) and seen[0][1] == [1.204159, 1.119151, 0.75] != spacing
    assert steps.read_text() == "finding the spinal cord"
    mask = nib.load(tmp_path / "spinal_cord_lesions.nii.gz")
    full = np.zeros(shape, bool)
    full[box] = values[box] % 2 == 0
    np.testing.assert_allclose(mask.affine, affine)
    assert np.array_equal(np.asarray(mask.dataobj) > 0, full)

    def no_cord(*_):
        raise stand_in.CordNotFound("No spinal cord was found in this series.")

    stand_in.crop_box = no_cord
    with pytest.raises(SystemExit):
        nnunet_worker.main()
    assert json.loads(report.read_text()) == {"error": "No spinal cord was found in this series."}


@pytest.mark.parametrize(("task", "name", "kept"), [("sct_spinal_cord", "spinal_cord", 8), ("sct_cord_ms_lesions", "spinal_cord_lesions", 16)])
def test_worker_keeps_only_the_largest_cord_piece_on_the_full_image_and_every_lesion(tmp_path, monkeypatch, nnunet_version, task, name, kept):
    nib.save(nib.Nifti1Image(np.ones((6, 7, 8), np.int16), np.diag([-0.8, 0.9, 1.1, 1.0])), tmp_path / "image.nii.gz")
    cord_crop(monkeypatch, lambda *_: ((1, 6), (0, 7), (0, 8)))

    def predict(model, folder, image, spacing, device, step):
        # Two equal pieces; in the model's z, y, x order the other one starts first.
        labels = np.zeros(image.shape[1:], np.uint8)
        labels[0:2, 0:2, 3:5] = labels[4:6, 0:2, 0:2] = 1
        return labels, "test"

    monkeypatch.setattr(nnunet_worker, "predict", predict)
    arguments = f"-i {tmp_path}/image.nii.gz -o {tmp_path} --task {task} --model-folder model --crop-folder detector"
    monkeypatch.setattr(sys, "argv", ["worker", *arguments.split(), "--device", "cpu", "--report", str(tmp_path / "report.json")])
    nnunet_worker.main()
    mask = np.asarray(nib.load(tmp_path / f"{name}.nii.gz").dataobj) > 0
    assert mask.sum() == kept and not mask[0].any() and mask[1:3, 0:2, 4:6].all()


@pytest.mark.parametrize(("left", "start", "own"), [(-0.8, 0, True), (-0.8, 1, False), (0.8, 0, False)])
def test_cord_model_spacing_is_the_files_own_zooms_only_for_an_uncut_corner_in_the_models_axes(tmp_path, monkeypatch, nnunet_version, left, start, own):
    image = nib.Nifti1Image(np.ones((6, 7, 8), np.int16), np.diag([left, 0.9, 1.1, 1.0]))
    image.header.set_zooms((0.5, 0.6, 0.7))  # Apart from the affine's, which the toolbox takes once it moves or turns the image.
    nib.save(image, tmp_path / "image.nii.gz")
    cord_crop(monkeypatch, lambda *_: ((start, 6), (0, 7), (0, 8)))
    seen = []

    def predict(model, folder, image, spacing, device, step):
        seen.append(spacing)
        return np.zeros(image.shape[1:], np.uint8), "test"

    monkeypatch.setattr(nnunet_worker, "predict", predict)
    arguments = f"-i {tmp_path}/image.nii.gz -o {tmp_path} --task sct_cord_ms_lesions --model-folder lesions --crop-folder detector"
    monkeypatch.setattr(sys, "argv", ["worker", *arguments.split(), "--device", "cpu", "--report", str(tmp_path / "report.json")])
    nnunet_worker.main()
    assert seen == [[0.7, 0.6, 0.5] if own else [1.1, 0.9, 0.8]]


def test_cord_model_folds_vote_without_mirrors_while_other_models_average_and_mirror(monkeypatch):
    torch = pytest.importorskip("torch")
    from nnunetv2.inference import data_iterators, predict_from_raw_data

    scan = np.arange(6, dtype=np.float32).reshape(1, 1, 2, 3)  # Values 0 to 5.
    made, loaded = [], []

    class Predictor:
        def __init__(self, **options):
            made.append(options)

        def initialize_from_trained_model_folder(self, folder, use_folds, checkpoint_name):
            self.manual_initialization(None, None, None, list(use_folds), None, "own", (0, 1, 2))

        def manual_initialization(self, network, plans, configuration, parameters, dataset_json, trainer_name, mirror_axes):
            self.list_of_parameters, self.label_manager = parameters, SimpleNamespace(has_regions=False)
            self.dataset_json = {"labels": {"background": 0, name: 1}}
            self.plans_manager = SimpleNamespace(transpose_forward=[0, 1, 2], transpose_backward=[0, 1, 2])
            self.configuration_manager = SimpleNamespace(spacing=[1, 1, 1], resampling_fn_probabilities=lambda logits, *_: logits)

        def predict_sliding_window_return_logits(self, data):
            # Fold k gives the lesion a strong score above k and a weak one below.
            fold = loaded[-1]
            return torch.stack([torch.zeros_like(data[0]), torch.where(data[0] > fold, 10.0, -1.0)])

        def predict_logits_from_preprocessed_data(self, data):
            assert len(self.list_of_parameters) == (1 if made[-1]["use_mirroring"] is False else 5)
            total = 0
            for fold in self.list_of_parameters:
                loaded.append(fold)
                total = total + self.predict_sliding_window_return_logits(data)
            return total / len(self.list_of_parameters)

    def adapter(images, *_):
        cropped = images[0].shape[1:]
        yield {
            "data": torch.from_numpy(images[0]),
            "data_properties": {
                "shape_after_cropping_and_before_resampling": cropped,
                "shape_before_cropping": cropped,
                "bbox_used_for_cropping": [[0, n] for n in cropped],
            },
        }

    monkeypatch.setattr(predict_from_raw_data, "nnUNetPredictor", Predictor)
    monkeypatch.setattr(data_iterators, "PreprocessAdapterFromNpy", adapter)
    monkeypatch.setattr(nnunet_worker, "fold_weights", lambda model, _: iter({"network_weights": fold} for fold in model.folds))
    monkeypatch.setattr(
        nnunet_worker,
        "initialize",
        lambda predictor, model, _, first: predictor.manual_initialization(*[None] * 3, [first["network_weights"]], None, "stock", (0, 1, 2)),
    )
    name, steps = "lesion", []
    labels, _ = nnunet_worker.predict(nnunet_models.MODELS["sct_cord_ms_lesions"], "folder", scan, [1, 1, 1], "cpu", steps.append)
    assert made[-1]["use_mirroring"] is False and loaded == [0, 1, 2, 3, 4]
    # A voxel needs three of five folds; averaging the scores keeps any voxel one fold marks strongly.
    assert np.array_equal(labels, scan[0] >= 3)
    assert steps == ["preparing the scan", *(f"fold {n} of 5" for n in range(1, 6)), "saving the masks"]
    name = "Lesion"
    labels, _ = nnunet_worker.predict(nnunet_models.MODELS["flames_ms_lesions"], "folder", scan, [1, 1, 1], "cpu")
    assert made[-1]["use_mirroring"] is True and np.array_equal(labels, scan[0] >= 1)
