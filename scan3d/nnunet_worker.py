"""One run of one pinned nnU-Net model, in a child process that may import PyTorch.

Writes one mask per label on the reference grid, and the same report fields as
TotalSegmentator's --report, so segmentation.segment checks both engines alike.
"""

from __future__ import annotations

import argparse
from importlib import metadata
import json
import os
from pathlib import Path
import time

from nibabel import orientations
import numpy as np
from scipy import ndimage as ndi

from .imaging import _image, _save_mask
from .nnunet_models import MODELS


def model_input(image, affine, axes="LPS"):
    """The voxels as nnU-Net was trained on them, with the transform that undoes this.

    The models read images with SimpleITK, which gives a z, y, x array of the training
    files' voxel axes. Axes are only reordered and flipped, so an oblique grid keeps every
    voxel value.
    """
    model_axes = orientations.axcodes2ornt(tuple(axes))
    native = orientations.io_orientation(affine)
    forward = orientations.ornt_transform(native, model_axes)
    values = orientations.apply_orientation(np.asarray(image.dataobj, dtype=np.float32), forward)
    spacing = np.empty(3)
    spacing[forward[:, 0].astype(int)] = np.linalg.norm(affine[:3, :3], axis=0)
    return values.transpose(2, 1, 0)[np.newaxis], spacing[::-1].tolist(), orientations.ornt_transform(model_axes, native)


def native_labels(labels, back):
    return orientations.apply_orientation(labels.transpose(2, 1, 0), back)


def toolbox_spacing(zooms):
    """The Spinal Cord Toolbox's spacing: each float32 header zoom rounded to that float's own precision.

    Upstream gives nnU-Net these, not the affine's float64 column norms, and nnU-Net's resampled
    shape rounds at exact halves, so the two can differ by a voxel.
    """
    return [round(float(zoom), -int(np.floor(np.log10(np.spacing(zoom)))) - 1) for zoom in np.asarray(zooms, dtype=np.float32)]


def skull_stripped(image, labels):
    """The model input with every voxel outside label 1 at zero, as in skull-stripped training images."""
    return np.where(labels == 1, image, np.float32(0))


def without_small_pieces(mask, smallest):
    # As ImageCAS-X's cleanup, which labels with scipy's default: only voxels sharing a face join a piece.
    if not smallest:
        return mask
    pieces, _ = ndi.label(mask)
    keep = np.bincount(pieces.ravel()) >= smallest
    keep[0] = False
    return keep[pieces]


def largest_piece(mask, largest):
    # As the Spinal Cord Toolbox's keep_largest_object, on the mask in the file's voxel order: scipy's default
    # labelling joins only face neighbours, and numbers pieces by their first voxel in C order, so argmax's
    # first maximum keeps, of equal pieces, the one that starts first. Its zeroing under 0.001 does nothing to a mask.
    if not largest:
        return mask
    pieces, count = ndi.label(mask)
    if count < 2:
        return mask
    return pieces == np.bincount(pieces.ravel())[1:].argmax() + 1


def full_size_labels(logits, resample, properties, transpose_backward):
    """nnU-Net's export, one channel at a time.

    Upstream resamples all channels together, which takes tens of GB for 41 channels on a
    large CT. The result is the same, because the argmax needs only a running best.
    """
    shape = properties["shape_after_cropping_and_before_resampling"]
    best = np.full(shape, -np.inf, dtype=np.float32)
    labels = np.zeros(shape, dtype=np.uint8)
    for channel in range(len(logits)):
        values = np.asarray(resample(logits[channel : channel + 1]), dtype=np.float32)[0]
        winners = values > best
        labels[winners] = channel
        best[winners] = values[winners]
    restored = np.zeros(properties["shape_before_cropping"], dtype=np.uint8)
    restored[tuple(slice(*limits) for limits in properties["bbox_used_for_cropping"])] = labels
    return restored.transpose(transpose_backward)


def fold_weights(model, model_folder):
    """Each fold's checkpoint in turn, loaded only when asked for, so folds are never all in memory at once."""
    import torch

    for fold in model.folds:
        # A pickle, checked against its pin before this child started.
        checkpoint = torch.load(Path(model_folder) / f"fold_{fold}" / model.checkpoint, map_location="cpu", weights_only=False)
        kept = {key: checkpoint.get(key) for key in ("network_weights", "trainer_name", "init_args", "inference_allowed_mirroring_axes")}
        del checkpoint
        yield kept


def initialize(predictor, model, model_folder, first):
    """As initialize_from_trained_model_folder, with the registry's stock trainer building the network.

    Upstream looks the checkpoint's trainer up by name, and a trainer nnU-Net lacks would fail.
    """
    from nnunetv2.utilities.find_objects import recursive_find_trainer_class_by_name
    from nnunetv2.utilities.label_handling.label_handling import determine_num_input_channels
    from nnunetv2.utilities.plans_handling.plans_handler import PlansManager

    dataset_json = json.loads((Path(model_folder) / "dataset.json").read_text())
    plans = PlansManager(json.loads((Path(model_folder) / "plans.json").read_text()))
    configuration = plans.get_configuration(first["init_args"]["configuration"])
    network = recursive_find_trainer_class_by_name(model.trainer).build_network_architecture(
        plans,
        configuration,
        determine_num_input_channels(plans, configuration, dataset_json),
        plans.get_label_manager(dataset_json).num_segmentation_heads,
        enable_deep_supervision=False,
    )
    network.load_state_dict(first["network_weights"])
    predictor.manual_initialization(
        network, plans, configuration, [first["network_weights"]], dataset_json, first["trainer_name"], first["inference_allowed_mirroring_axes"]
    )


def predict(model, model_folder, image, spacing, device, step=lambda _: None):
    import torch
    from nnunetv2.inference.data_iterators import PreprocessAdapterFromNpy
    from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor

    selected = torch.device("cpu") if device == "cpu" else torch.device("cuda", int(device.partition(":")[2] or 0))
    predictor = nnUNetPredictor(tile_step_size=model.tile_step_size, use_gaussian=True, use_mirroring=model.mirror, device=selected, allow_tqdm=False)
    if model.trainer:
        weights = fold_weights(model, model_folder)
        initialize(predictor, model, model_folder, next(weights))
        if not model.vote:
            predictor.list_of_parameters += [fold["network_weights"] for fold in weights]
    else:
        predictor.initialize_from_trained_model_folder(str(model_folder), use_folds=model.folds, checkpoint_name=model.checkpoint)
        weights = iter(())
    # A wrong order here would silently mislabel anatomy.
    named = sorted((value, name) for name, value in predictor.dataset_json["labels"].items() if value)
    expected = model.dataset_labels or model.labels
    if tuple(name for _, name in named) != expected or [value for value, _ in named] != list(range(1, len(model.labels) + 1)):
        raise RuntimeError(f"{model.name} labels do not match the pinned list")
    if predictor.label_manager.has_regions:
        raise RuntimeError(f"{model.name} is region-based, which this worker does not export")
    plans, configuration = predictor.plans_manager, predictor.configuration_manager
    step("preparing the scan")
    adapter = PreprocessAdapterFromNpy([image], None, [{"spacing": spacing}], None, plans, predictor.dataset_json, configuration)
    prepared = next(adapter)
    properties = prepared["data_properties"]
    # Upstream runs each fold through this once, so counting calls shows a long run is moving.
    sliding_window, folds_started = predictor.predict_sliding_window_return_logits, []

    def counted(data):
        folds_started.append(None)
        step(f"fold {len(folds_started)} of {len(model.folds)}")
        return sliding_window(data)

    predictor.predict_sliding_window_return_logits = counted
    shape = properties["shape_after_cropping_and_before_resampling"]
    target = [spacing[axis] for axis in plans.transpose_forward]

    def export(logits):
        return full_size_labels(
            logits,
            lambda channel: configuration.resampling_fn_probabilities(channel, shape, configuration.spacing, target),
            properties,
            plans.transpose_backward,
        )

    if not model.vote:
        logits = predictor.predict_logits_from_preprocessed_data(prepared["data"]).cpu()
        del prepared
        step("saving the masks")
        return export(logits), torch.__version__
    # As the Spinal Cord Toolbox: each fold's own mask, then the voxels more than half the folds mark.
    votes = None
    for fold in range(len(model.folds)):
        if fold:
            predictor.list_of_parameters = [next(weights)["network_weights"]]
        labels = export(predictor.predict_logits_from_preprocessed_data(prepared["data"]).cpu())
        predictor.list_of_parameters = []
        if votes is None:
            votes = np.zeros((len(model.labels), *labels.shape), dtype=np.uint8)
        for value, count in enumerate(votes, 1):
            count += labels == value
    del prepared
    step("saving the masks")
    labels = np.zeros(votes.shape[1:], dtype=np.uint8)
    for value, count in enumerate(votes, 1):
        labels[count > len(model.folds) / 2] = value
    return labels, torch.__version__


def run(model, model_folder, image, spacing, device, step=lambda _: None):
    """Labels, the PyTorch version, and CAS-Net's uint8 probability (None for other engines)."""
    if model.engine == "cas_net":
        from . import cas_net  # Imports PyTorch, which only this child may do.

        return cas_net.predict(model, model_folder, image, spacing, device, step)
    return *predict(model, model_folder, image, spacing, device, step), None


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", dest="image", type=Path, required=True)
    parser.add_argument("-o", dest="output", type=Path, required=True)
    parser.add_argument("--task", required=True, choices=sorted(MODELS))
    parser.add_argument("--model-folder", type=Path, required=True)
    parser.add_argument("--skull-strip-folder", type=Path, help="The folder of the model named by the model's skull_strip")
    parser.add_argument("--crop-folder", type=Path, help="The folder of the helper named by the model's crop")
    parser.add_argument("--device", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--progress", type=Path, help="A file that always holds the current step, for the parent to show")
    parser.add_argument("--probability", type=Path, help="Where CAS-Net saves its uint8 lumen probability on the reference grid")
    args = parser.parse_args()
    model = MODELS[args.task]
    started = time.monotonic()

    def step(text):
        if args.progress:
            pending = args.progress.with_name(args.progress.name + ".tmp")
            pending.write_text(text)
            os.replace(pending, args.progress)

    reference, affine = _image(args.image)
    source, box = reference, ()
    if model.crop:
        if not args.crop_folder:
            raise RuntimeError(f"{model.name} needs --crop-folder")
        from . import sc_crop  # Imports onnxruntime, which only this child may do.

        step("finding the spinal cord")
        try:
            box = tuple(slice(*bounds) for bounds in sc_crop.crop_box(reference, args.crop_folder, model.crop_padding))
        except RuntimeError as error:
            # No cord found, or a scan or install the search cannot use: a sentence the reader can act on.
            args.report.write_text(json.dumps({"error": str(error)}))
            raise SystemExit(1) from error
        # As upstream, the model sees only the box. Upstream first saves the crop in the file's integer type,
        # which rounds a scaled image and, when it has negative values, leaves nnU-Net's nonzero crop no exact
        # zeros; these are the file's own values, so zero margins are still cropped.
        source = reference.slicer[box]
    image, spacing, back = model_input(source, affine, model.axes)
    if model.crop:
        # The toolbox writes the crop with a new affine, which resets the zooms to its column norms; the
        # file's own zooms survive only a crop at the origin of an image already in the model's axes.
        kept = not any(part.start for part in box) and np.array_equal(orientations.io_orientation(affine), orientations.axcodes2ornt(tuple(model.axes)))
        spacing = toolbox_spacing(reference.header.get_zooms()[2::-1] if kept else spacing)
    if model.skull_strip:
        if not args.skull_strip_folder:
            raise RuntimeError(f"{model.name} needs --skull-strip-folder")
        # The registry gives both models the same voxel order, so the mask fits this array as it is.
        brain, _, _ = run(MODELS[model.skull_strip], args.skull_strip_folder, image, spacing, args.device, lambda text: step(f"removing the skull, {text}"))
        image = skull_stripped(image, brain)
    labels, torch_version, probability = run(model, args.model_folder, image, spacing, args.device, step)
    labels = native_labels(labels, back)
    if box:
        full = np.zeros(reference.shape, dtype=np.uint8)
        full[box] = labels
        labels = full
    if args.probability and probability is not None:
        # Saved like a mask, on the reference grid; Grow branch reads it, and nothing else does.
        full = np.zeros(reference.shape, dtype=np.uint8)
        full[box] = native_labels(probability, back)
        _save_mask(full, reference, args.probability)
        del full, probability
    for value, name in enumerate(model.labels, 1):
        mask = largest_piece(labels == value, model.keep_largest)
        _save_mask(without_small_pieces(mask, model.smallest_piece), reference, args.output / f"{name}.nii.gz")
    report = {
        "device": "cpu" if args.device == "cpu" else "gpu",
        "fast": False,
        "fastest": False,
        "save_lowres": False,
        "torch_version": torch_version,
        "nnunetv2_version": metadata.version("nnunetv2") if model.engine == "nnunet" else None,
        "runtime_seconds": round(time.monotonic() - started, 3),
        "classes": {str(value): name for value, name in enumerate(model.labels, 1)},
    }
    args.report.write_text(json.dumps(report))


if __name__ == "__main__":
    main()
