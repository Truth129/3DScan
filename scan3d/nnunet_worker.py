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


def without_small_pieces(mask, smallest):
    if not smallest:
        return mask
    pieces, _ = ndi.label(mask, structure=np.ones((3, 3, 3)))
    keep = np.bincount(pieces.ravel()) >= smallest
    keep[0] = False
    return keep[pieces]


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


def predict(model, model_folder, image, spacing, device, step=lambda _: None):
    import torch
    from nnunetv2.inference.data_iterators import PreprocessAdapterFromNpy
    from nnunetv2.inference.predict_from_raw_data import nnUNetPredictor

    selected = torch.device("cpu") if device == "cpu" else torch.device("cuda", int(device.partition(":")[2] or 0))
    predictor = nnUNetPredictor(tile_step_size=model.tile_step_size, use_gaussian=True, use_mirroring=True, device=selected, allow_tqdm=False)
    predictor.initialize_from_trained_model_folder(str(model_folder), use_folds=model.folds, checkpoint_name=model.checkpoint)
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
    logits = predictor.predict_logits_from_preprocessed_data(prepared["data"]).cpu()
    del prepared
    step("saving the masks")
    shape = properties["shape_after_cropping_and_before_resampling"]
    target = [spacing[axis] for axis in plans.transpose_forward]
    return full_size_labels(
        logits,
        lambda channel: configuration.resampling_fn_probabilities(channel, shape, configuration.spacing, target),
        properties,
        plans.transpose_backward,
    ), torch.__version__


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("-i", dest="image", type=Path, required=True)
    parser.add_argument("-o", dest="output", type=Path, required=True)
    parser.add_argument("--task", required=True, choices=sorted(MODELS))
    parser.add_argument("--model-folder", type=Path, required=True)
    parser.add_argument("--device", required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--progress", type=Path, help="A file that always holds the current step, for the parent to show")
    args = parser.parse_args()
    model = MODELS[args.task]
    started = time.monotonic()

    def step(text):
        if args.progress:
            pending = args.progress.with_name(args.progress.name + ".tmp")
            pending.write_text(text)
            os.replace(pending, args.progress)

    reference, affine = _image(args.image)
    image, spacing, back = model_input(reference, affine, model.axes)
    labels, torch_version = predict(model, args.model_folder, image, spacing, args.device, step)
    labels = native_labels(labels, back)
    for value, name in enumerate(model.labels, 1):
        _save_mask(without_small_pieces(labels == value, model.smallest_piece), reference, args.output / f"{name}.nii.gz")
    report = {
        "device": "cpu" if args.device == "cpu" else "gpu",
        "fast": False,
        "fastest": False,
        "save_lowres": False,
        "torch_version": torch_version,
        "nnunetv2_version": metadata.version("nnunetv2"),
        "runtime_seconds": round(time.monotonic() - started, 3),
        "classes": {str(value): name for value, name in enumerate(model.labels, 1)},
    }
    args.report.write_text(json.dumps(report))


if __name__ == "__main__":
    main()
