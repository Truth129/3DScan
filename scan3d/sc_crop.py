"""The spinal cord's bounding box, found as the Spinal Cord Toolbox finds it before its lesion model.

A port of sc-crop's detect() (version 0.12.1, as the toolbox vendors it) with its configuration and
the detector and classifier of model release v0.0.11. Upstream runs the detector through Ultralytics,
which resizes with OpenCV. Neither is a dependency here: the letterbox, the box arithmetic (as
Ultralytics 8.4.209 scales boxes) and OpenCV's fixed-point bilinear resize are written out so the box is
the same, and the two ONNX files run on onnxruntime's CPU provider. onnxruntime is imported only inside
crop_box, so the API process never loads it.

Adapted from sc-crop (github.com/ivadomed/sc-crop):

    MIT License

    Copyright (c) 2026 NeuroPoly Lab, Polytechnique Montréal

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.
"""

from __future__ import annotations

import math

import numpy as np
from nibabel import orientations
from nibabel.processing import resample_to_output

LAS = orientations.axcodes2ornt(("L", "A", "S"))
SPACING = (1.0, 1.0, 10.0)  # mm; the detector reads 1 mm axial slices 10 mm apart
SIZE = 320
STRIDE = 32
GREY = 114  # letterbox padding
CONFIDENCE = np.float32(0.1)
CORD = 0.5  # the classifier's "spinal cord" probability that validates a run of slices
NOT_FOUND = "No spinal cord was found in this scan. Check that the series shows the spinal cord, or choose another series."
TOO_NARROW = (
    "This scan is too narrow for the spinal cord detector: its axial slices are about ten times longer than they are wide. "
    "Choose a series that covers more of the spine from side to side and front to back."
)


class CordNotFound(RuntimeError):
    pass


def resize(image, width, height):
    """OpenCV's INTER_LINEAR resize of an 8-bit H x W x C image, to the bit.

    OpenCV rounds the weights to 1/2048, mixes columns in integers, then mixes rows with its SIMD
    rule, which drops low bits before multiplying. Past the edge a column takes the edge pixel with a
    weight of one; a row takes the edge row but keeps its computed weights. An exact halving comes
    out as OpenCV's area average, which it uses there.
    """
    rows, columns = image.shape[:2]
    x, fx = _taps(columns, width)
    fx[(x < 0) | (x >= columns - 1)] = 0
    x = np.clip(x, 0, columns - 1)
    y, fy = _taps(rows, height)
    a0, a1, b0, b1 = (weight[:, None] for weight in _weights(fx) + _weights(fy))
    source = image.astype(np.int64)
    mixed = source[:, x] * a0 + source[:, np.minimum(x + 1, columns - 1)] * a1
    top, bottom = mixed[np.clip(y, 0, rows - 1)] >> 4, mixed[np.clip(y + 1, 0, rows - 1)] >> 4
    out = ((top * b0[:, None] >> 16) + (bottom * b1[:, None] >> 16) + 2) >> 2
    return np.clip(out, 0, 255).astype(np.uint8)


def _taps(size, new_size):
    """The first source index and the float32 fraction toward the next, for each new index."""
    scale = 1.0 / (new_size / size)
    position = ((np.arange(new_size) + 0.5) * scale - 0.5).astype(np.float32)
    first = np.floor(position)
    return first.astype(np.int64), position - first


def _weights(fraction):
    return [np.rint(weight * np.float32(2048)).astype(np.int64) for weight in (np.float32(1) - fraction, fraction)]


def pad(image, top, bottom, left, right):
    return np.pad(image, ((top, bottom), (left, right), (0, 0)), constant_values=GREY)


def to_uint8(plane):
    """sc-crop's per-slice scaling from the 0.5th to 99.5th percentile, background included.

    In float32 with the limits rounded to float32, as NumPy 1 computes upstream's expression.
    """
    lo, hi = np.percentile(plane, [0.5, 99.5])
    if hi <= lo:
        return np.zeros(plane.shape, np.uint8)
    scaled = (np.clip(plane, np.float32(lo), np.float32(hi)) - np.float32(lo)) / np.float32(hi - lo) * np.float32(255)
    return scaled.astype(np.uint8)


def axial_slices(data):
    """Each axial slice, index 0 most inferior, as rows anterior to posterior and the slices above,
    at and below it as three uint8 channels; outside the volume a neighbour is black."""
    rl, ap, count = data.shape
    planes = [np.zeros((ap, rl), np.uint8)] + [to_uint8(data[:, :, z].T[::-1, ::-1]) for z in range(count)] + [np.zeros((ap, rl), np.uint8)]
    return [np.stack([planes[z + 2], planes[z + 1], planes[z]], axis=2) for z in range(count)]


def letterbox(shape):
    """Ultralytics' minimum-rectangle letterbox for a dynamic ONNX detector: the resized size and the
    top, bottom, left and right padding."""
    rows, columns = shape
    ratio = min(SIZE / rows, SIZE / columns)
    width, height = max(round(columns * ratio), 1), max(round(rows * ratio), 1)
    dw, dh = (SIZE - width) % STRIDE / 2, (SIZE - height) % STRIDE / 2
    return (width, height), (round(dh - 0.1), round(dh + 0.1), round(dw - 0.1), round(dw + 0.1))


def detector_input(slice_, size, padding):
    image = pad(resize(slice_, *size) if size != slice_.shape[1::-1] else slice_, *padding)
    # Ultralytics takes arrays as BGR and reverses the channels: the detector sees inferior first.
    return np.ascontiguousarray(image[None, :, :, ::-1].transpose(0, 3, 1, 2)).astype(np.float32) / np.float32(255)


def detected_box(rows, shape, input_shape):
    """The best box of one slice as centre x, centre y, width and height, each a fraction of the slice;
    None without one. Scaled back by Ultralytics 8.4.209's rule, in float32 as it does."""
    rows = rows[rows[:, 4] > CONFIDENCE]
    if not len(rows):
        return None
    box = rows[int(rows[:, 4].argmax()), :4].copy()
    (h0, w0), (h1, w1) = shape, input_shape
    gain = min(h1 / h0, w1 / w0)
    new_h, new_w = max(round(h0 * gain), 1), max(round(w0 * gain), 1)
    box[[0, 2]] = np.clip((box[[0, 2]] - np.float32(round((w1 - new_w) / 2 - 0.1))) / np.float32(new_w / w0), 0, w0)
    box[[1, 3]] = np.clip((box[[1, 3]] - np.float32(round((h1 - new_h) / 2 - 0.1))) / np.float32(new_h / h0), 0, h0)
    x1, y1, x2, y2 = box
    width, height = np.float32(w0), np.float32(h0)
    return float((x1 + x2) / 2 / width), float((y1 + y2) / 2 / height), float((x2 - x1) / width), float((y2 - y1) / height)


def classifier_input(slice_):
    rows, columns = slice_.shape[:2]
    ratio = SIZE / max(rows, columns)
    height, width = round(rows * ratio), round(columns * ratio)
    image = resize(slice_, width, height) if (height, width) != (rows, columns) else slice_
    dh, dw = SIZE - height, SIZE - width
    image = pad(image, dh // 2, dh - dh // 2, dw // 2, dw - dw // 2)
    return image.astype(np.float32).transpose(2, 0, 1)[None] / np.float32(255)


def validated(boxes, slices, classifier):
    """sc-crop's classifier filter: walking runs of consecutive slices from the most inferior, the first
    run with a slice the classifier calls cord keeps itself and every box above it; else all are kept."""
    runs = []
    for z in sorted(boxes):
        if runs and z == runs[-1][-1] + 1:
            runs[-1].append(z)
        else:
            runs.append([z])
    for run in runs:
        for z in run:
            if float(classifier.run(None, {"images": classifier_input(slices[z])})[0][0][1]) >= CORD:
                return {k: box for k, box in boxes.items() if k >= run[0]}
    return boxes


def las_box(boxes, shape, zooms, padding):
    """Half-open bounds on the native LAS axes, padded in mm, as sc-crop aggregates and pads them.

    The highest slice found is an exclusive bound, as upstream.
    """
    rl, ap, count = shape
    si_zoom = zooms[2] / SPACING[2]
    lows, highs = [], []
    for z, (cx, cy, w, h) in boxes.items():
        rl_c, ap_c, rl_half, ap_half = cx * rl, (1.0 - cy) * ap, w / 2 * rl, h / 2 * ap
        z = min(count - 1, round(z / si_zoom))
        lows.append((max(0, int(rl_c - rl_half)), max(0, int(ap_c - ap_half)), z))
        highs.append((min(rl, int(rl_c + rl_half)), min(ap, int(ap_c + ap_half)), z))
    low, high = [min(values) for values in zip(*lows)], [max(values) for values in zip(*highs)]
    before, after = (padding["right"], padding["posterior"], padding["inferior"]), (padding["left"], padding["anterior"], padding["superior"])
    return tuple(
        (max(0, low[axis] - math.ceil(before[axis] / zooms[axis])), min(shape[axis], high[axis] + math.ceil(after[axis] / zooms[axis]))) for axis in range(3)
    )


def crop_box(image, folder, padding):
    """The padded spinal cord box of a 3D nibabel image, from det_model.onnx and cls_model.onnx in folder.

    padding holds the margin in mm beyond each face: superior, inferior, left, right, anterior and
    posterior. Returns ((x0, x1), (y0, y1), (z0, z1)), half-open voxel bounds on the image's own axes.
    """
    try:
        import onnxruntime
    except ImportError as error:
        raise RuntimeError(
            "The spinal cord detector needs onnxruntime, which this job's Python environment lacks. "
            "Run scripts/bootstrap.sh again, or scripts/add_inference_path.sh for an added GPU."
        ) from error

    native = orientations.io_orientation(image.affine)
    las = image.as_reoriented(orientations.ornt_transform(native, LAS))
    # The header's float32 zooms, not the affine's column norms: they decide whole voxels of padding.
    zooms = tuple(float(zoom) for zoom in las.header.get_zooms()[:3])
    if all(abs(target - zoom) < 0.01 for target, zoom in zip(SPACING, zooms)):
        # Upstream keeps LAS order here, which mirrors its box left to right.
        data = las.get_fdata(dtype=np.float32)[::-1]
    else:
        data = resample_to_output(las, voxel_sizes=SPACING, order=1).get_fdata(dtype=np.float32)
    slices = axial_slices(data)
    size, margins = letterbox(slices[0].shape[:2])
    input_shape = (size[1] + margins[0] + margins[1], size[0] + margins[2] + margins[3])
    # The detector ranks its 300 best of h*w*21/1024 candidate boxes, at strides 8, 16 and 32.
    if input_shape[0] * input_shape[1] * 21 // 1024 < 300:
        raise CordNotFound(TOO_NARROW)
    providers = ["CPUExecutionProvider"]
    detector = onnxruntime.InferenceSession(str(folder / "det_model.onnx"), providers=providers)
    classifier = onnxruntime.InferenceSession(str(folder / "cls_model.onnx"), providers=providers)
    boxes = {}
    for z, slice_ in enumerate(slices):
        box = detected_box(detector.run(None, {"images": detector_input(slice_, size, margins)})[0][0], slice_.shape[:2], input_shape)
        if box is not None:
            boxes[z] = box
    boxes = validated(boxes, slices, classifier)
    if not boxes:
        raise CordNotFound(NOT_FOUND)
    bounds = las_box(boxes, las.shape, zooms, padding)
    result = [None] * 3
    for axis, (target, flip) in enumerate(orientations.ornt_transform(LAS, native)):
        lo, hi = bounds[axis]
        n = las.shape[axis]
        result[int(target)] = (lo, hi) if flip == 1 else (n - hi, n - lo)
    return tuple(result)
