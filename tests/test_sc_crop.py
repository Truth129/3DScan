"""The sc-crop detector port: OpenCV's resize, slices, box arithmetic and failures; ONNX sessions faked."""

import sys
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest

from scan3d import sc_crop

PAD = dict(superior=40.0, inferior=100.0, left=20.0, right=20.0, anterior=20.0, posterior=20.0)

# cv2.resize(INTER_LINEAR) 4.11.0 outputs for the image below; they differ from a float bilinear.
OPENCV = {
    (3, 4, 5, 7): [
        [[0, 37, 74], [39, 77, 113], [103, 140, 177], [166, 75, 112], [212, 11, 48], [129, 74, 111], [77, 114, 151]],
        [[75, 112, 47], [78, 115, 86], [83, 120, 150], [139, 99, 136], [184, 79, 116], [102, 84, 121], [50, 87, 124]],
        [[188, 225, 6], [136, 173, 46], [53, 90, 109], [99, 136, 173], [144, 181, 218], [61, 98, 135], [9, 46, 83]],
        [[147, 184, 119], [150, 132, 103], [155, 49, 79], [134, 95, 131], [114, 151, 177], [119, 156, 94], [122, 159, 42]],
        [[120, 157, 194], [159, 105, 142], [223, 22, 59], [158, 67, 104], [94, 131, 149], [157, 194, 67], [197, 234, 15]],
    ],
    (5, 7, 3, 4): [
        [[44, 81, 119], [207, 20, 57], [177, 214, 27], [115, 152, 189]],
        [[60, 97, 134], [222, 35, 72], [192, 229, 42], [130, 167, 204]],
        [[74, 111, 149], [87, 50, 87], [207, 95, 57], [145, 182, 113]],
    ],
    (3, 6, 7, 2): [
        [[111, 148, 185], [188, 225, 6]],
        [[96, 133, 170], [173, 210, 28]],
        [[53, 90, 127], [130, 167, 94]],
        [[9, 46, 83], [86, 123, 160]],
        [[75, 112, 149], [152, 79, 116]],
        [[141, 178, 215], [218, 35, 72]],
        [[163, 200, 237], [240, 21, 58]],
    ],
    (4, 6, 2, 3): [[[69, 106, 143], [163, 72, 109], [129, 166, 75]], [[121, 158, 131], [87, 124, 161], [181, 90, 127]]],
}


@pytest.mark.parametrize("sizes", OPENCV)
def test_resize_matches_opencv_to_the_bit(sizes):
    rows, columns, height, width = sizes
    image = (np.arange(rows * columns * 3) * 37 % 256).astype(np.uint8).reshape(rows, columns, 3)
    assert sc_crop.resize(image, width, height).tolist() == OPENCV[sizes]


def test_slices_stack_the_neighbours_and_the_detector_sees_them_reversed():
    data = np.zeros((4, 5, 3), np.float32)
    data[1, 2, 1] = 7  # x 1, y 2, slice 1: row 5 - 1 - 2 and column 4 - 1 - 1 of its plane
    slices = sc_crop.axial_slices(data)
    lit = [[tuple(np.argwhere(s[:, :, channel] == 255).ravel()) for channel in range(3)] for s in slices]
    assert lit == [[(2, 2), (), ()], [(), (2, 2), ()], [(), (), (2, 2)]]
    size, margins = sc_crop.letterbox(slices[0].shape[:2])
    assert (size, margins) == ((256, 320), (0, 0, 0, 0))
    channels = np.full((5, 4, 3), (10, 20, 30), np.uint8)
    tensor = sc_crop.detector_input(channels, size, (2, 3, 4, 5))
    assert tensor.shape == (1, 3, 325, 265) and tensor.dtype == np.float32
    assert [tensor[0, c, 100, 100] for c in range(3)] == [np.float32(30) / 255, np.float32(20) / 255, np.float32(10) / 255]
    assert tensor[0, 0, 0, 0] == np.float32(114) / 255
    assert sc_crop.classifier_input(channels)[0, :, 160, 160].tolist() == [np.float32(10) / 255, np.float32(20) / 255, np.float32(30) / 255]


def test_detected_box_undoes_the_letterbox_and_ignores_weak_rows():
    rows = np.zeros((300, 6), np.float32)
    rows[0] = (11, 0, 84, 320, 0.5, 0)  # exactly the resized 73 x 320 slice inside 11 px of padding
    rows[1] = (0, 0, 96, 320, 0.4, 0)
    assert sc_crop.detected_box(rows, (257, 59), (320, 96)) == (0.5, 0.5, 1.0, 1.0)
    rows[:, 4] = (0.4, 0.1) + (0,) * 298
    rows[0] = (0, 160, 48, 320, 0.4, 0)  # clipped at the left edge
    cx, cy, w, h = sc_crop.detected_box(rows, (257, 59), (320, 96))
    assert w == pytest.approx(37 * 59 / 73 / 59, abs=1e-6) and cx == pytest.approx(w / 2) and (cy, h) == (0.75, 0.5)
    rows[0, 4] = sc_crop.CONFIDENCE
    assert sc_crop.detected_box(rows, (257, 59), (320, 96)) is None


class Classifier:
    def __init__(self, cord=()):
        self.cord, self.seen = cord, []

    def run(self, _, feed):
        value = float(feed["images"][0, 1, 160, 160])
        self.seen.append(value)
        return [np.array([[0.0, 1.0 if value in self.cord else 0.0]], np.float32)]


def test_classifier_walks_runs_upward_and_keeps_the_validated_run_and_all_above():
    slices = [np.full((4, 4, 3), z * 10, np.uint8) for z in range(10)]
    boxes = {z: (0.5, 0.5, 0.1, 0.1) for z in (1, 2, 5, 6, 7, 9)}
    six = np.float32(60) / 255
    classifier = Classifier(cord=(six,))
    assert sorted(sc_crop.validated(boxes, slices, classifier)) == [5, 6, 7, 9]
    assert classifier.seen == [np.float32(z * 10) / 255 for z in (1, 2, 5, 6)]
    assert sc_crop.validated(boxes, slices, Classifier()) == boxes


def test_box_arithmetic_rounds_slices_to_even_and_pads_whole_voxels():
    boxes = {1: (0.5, 0.75, 0.1, 0.2), 5: (0.45, 0.7, 0.1, 0.2)}
    # Slices 1 and 5 at 10 mm land on native 2.5 and 12.5, which round to 2 and 12; 12 is exclusive.
    assert sc_crop.las_box(boxes, (200, 100, 30), (0.5, 0.5, 4.0), dict.fromkeys(PAD, 0.0)) == ((80, 110), (15, 40), (2, 12))
    # 4.2 mm is 8.4 and 1.05 voxels, padded up to 9 and 2.
    assert sc_crop.las_box(boxes, (200, 100, 30), (0.5, 0.5, 4.0), dict.fromkeys(PAD, 4.2)) == ((71, 119), (6, 49), (0, 14))
    assert sc_crop.las_box(boxes, (200, 100, 30), (0.5, 0.5, 4.0), PAD) == ((40, 150), (0, 80), (0, 22))


class Detector:
    """Reports the bright pixels of the current slice as one box, as the real detector finds a cord."""

    def run(self, _, feed):
        rows = np.zeros((1, 300, 6), np.float32)
        y, x = np.nonzero(feed["images"][0, 1] > 0.6)
        if len(x):
            rows[0, 0] = (x.min(), y.min(), x.max() + 1, y.max() + 1, 0.9, 0)
        return [rows]


@pytest.fixture
def sessions(monkeypatch):
    opened = []

    def session(path, providers):
        opened.append((path.rsplit("/", 1)[-1], providers))
        return Detector() if path.endswith("det_model.onnx") else Classifier(cord=())

    monkeypatch.setitem(sys.modules, "onnxruntime", SimpleNamespace(InferenceSession=session))
    return opened


def scan(shape, affine, cord):
    data = np.zeros(shape, np.int16)
    data[cord] = 1000
    return nib.Nifti1Image(data, affine)


def test_crop_box_pads_by_the_header_zooms(sessions, tmp_path):
    # Column norms just under 0.5 mm would give 41 voxels of padding; the float32 header says 0.5 and 40.
    affine = np.diag([0.49999999383, 0.49999999759, 3.00000038, 1.0])
    image = scan((200, 200, 10), affine, np.s_[120:140, 60:80, :])
    unpadded = sc_crop.crop_box(image, tmp_path, dict.fromkeys(PAD, 0.0))
    padded = sc_crop.crop_box(image, tmp_path, PAD)
    assert np.abs(np.subtract(unpadded[:2], [(120, 140), (60, 80)])).max() <= 2
    assert [(a - c, d - b) for (a, b), (c, d) in zip(unpadded[:2], padded[:2])] == [(40, 40), (40, 40)]
    assert padded[2] == (0, 10)
    assert sessions[:2] == [("det_model.onnx", ["CPUExecutionProvider"]), ("cls_model.onnx", ["CPUExecutionProvider"])]


def test_crop_box_without_resampling_does_not_mirror_left_and_right(sessions, tmp_path):
    affine = np.diag([-1.0, 1.0, 10.0, 1.0])  # LAS at 1 x 1 x 10 mm: the detector's own grid
    image = scan((60, 80, 8), affine, np.s_[40:48, 30:40, :])
    # Upstream returns about (12, 20) on x here, the cord's mirror image.
    assert sc_crop.crop_box(image, tmp_path, dict.fromkeys(PAD, 0.0))[:2] == ((40, 48), (30, 40))


def test_crop_box_is_on_the_images_own_axes(sessions, tmp_path):
    # Stored posterior, inferior, right: a sagittal series, whose first two axes run against LAS.
    affine = np.array([[0, 0, 1.0, 0], [-1.0, 0, 0, 0], [0, -3.0, 0, 0], [0, 0, 0, 1.0]])
    box = sc_crop.crop_box(scan((80, 8, 60), affine, np.s_[30:40, :, 40:48]), tmp_path, dict.fromkeys(PAD, 0.0))
    # The highest slice found is exclusive, and here that is index 0.
    assert np.abs(np.subtract([box[0], box[2]], [(30, 40), (40, 48)])).max() <= 2 and box[1] == (1, 8)


def test_failures_say_what_to_do(sessions, tmp_path, monkeypatch):
    with pytest.raises(sc_crop.CordNotFound, match="No spinal cord was found"):
        sc_crop.crop_box(scan((60, 80, 8), np.diag([1.0, 1.0, 3.0, 1.0]), np.s_[0:0]), tmp_path, PAD)
    with pytest.raises(sc_crop.CordNotFound, match="too narrow"):
        sc_crop.crop_box(scan((20, 257, 8), np.diag([1.0, 1.0, 3.0, 1.0]), np.s_[8:12, 100:110, :]), tmp_path, PAD)
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    with pytest.raises(RuntimeError, match="needs onnxruntime"):
        sc_crop.crop_box(scan((60, 80, 8), np.diag([1.0, 1.0, 3.0, 1.0]), np.s_[0:0]), tmp_path, PAD)
