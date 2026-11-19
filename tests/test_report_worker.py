"""TotalSegmentator's aorta and pulmonary artery reports measuring synthetic vessels of known width, on a
straight and an oblique grid, its spine report measuring synthetic vertebral bodies of known height, and
its body stats command called as upstream's own generator, here a stand-in."""

import contextlib
import json
import re
from pathlib import Path
import sys
import types

import nibabel as nib
import numpy as np
import pytest

pytest.importorskip("totalsegmentator.aorta_report.pipeline")
pytest.importorskip("totalsegmentator.pulmonary_report.pipeline")

from scan3d import body_stats_report, pulmonary_report, report_worker, spine_report  # noqa: E402

RADIUS = 8.0  # mm; the tube is 16 mm across everywhere
# Upstream dilates each cut plane by one 0.8 mm voxel before measuring and rounds to 1 mm, so its
# diameters read up to about 2 mm wide and its ends sit up to about 1.5 mm outside the wall.
WALL = (RADIUS - 1.5, RADIUS + 2.0)
ARCH = 20.0  # mm from the arch's centre to its axis
# A 1 mm grid around the phantom, in its own frame: x left-right, y front-back, z up.
LOW, SHAPE = np.array([-25.0, -48.0, -130.0]), (51, 81, 211)


def axis(samples=2000):
    """Points along the tube's axis from the annulus on: up the ascending part, over the arch, down."""
    up = [(0, ARCH, z) for z in np.linspace(0, 40, samples // 4)]
    turn = [(0, ARCH * np.cos(t), 40 + ARCH * np.sin(t)) for t in np.linspace(0, np.pi, samples // 4)]
    down = [(0, -ARCH, z) for z in np.linspace(40, -100, samples // 2)]
    return np.array(up + turn + down, dtype=float)


def phantom(radius=RADIUS):
    """The masks by upstream name, in the phantom's frame, with the aorta `radius` across its axis."""
    x, y, z = np.meshgrid(*(LOW[i] + np.arange(SHAPE[i]) for i in range(3)), indexing="ij")
    ascending = (x**2 + (y - ARCH) ** 2 <= radius**2) & (z >= 0) & (z <= 40)
    arch = (np.hypot(x, np.hypot(y, z - 40) - ARCH) <= radius) & (z >= 40)
    descending = (x**2 + (y + ARCH) ** 2 <= radius**2) & (z >= -100) & (z <= 40)
    return {
        "aorta": ascending | arch | descending,
        "annulus_proper": (x**2 + (y - ARCH) ** 2 <= RADIUS**2) & (z >= -1) & (z <= 2),
        "sinotubular_junction": (x**2 + (y - ARCH) ** 2 <= (RADIUS + 1) ** 2) & (z >= 24) & (z <= 27),
        "brachiocephalic_trunk": (x**2 + (y - 10) ** 2 <= 16) & (z >= 50) & (z <= 75),
        "subclavian_artery_left": (x**2 + (y + 10) ** 2 <= 16) & (z >= 50) & (z <= 75),
        "vertebrae_T12": (np.abs(x) <= 8) & (y >= -44) & (y <= -32) & (z >= -55) & (z <= -35),
        "celiac_trunk": (x**2 + (z + 65) ** 2 <= 9) & (y >= -20) & (y <= 0),
        "iliac_artery_right": ((x - 6) ** 2 + (y + ARCH) ** 2 <= 25) & (z >= -125) & (z <= -101),
    }


NEEDS = {
    "aorta": "aorta",
    "annulus": "annulus_proper",
    "sinotubular_junction": "sinotubular_junction",
    "brachiocephalic_trunk": "brachiocephalic_trunk",
    "subclavian_artery_left": "subclavian_artery_left",
    "vertebrae_T12": "vertebrae_T12",
    "iliac_artery_right": "iliac_artery_right",
    "celiac_trunk": "celiac_trunk",
}


def rotation(x, z):
    turn = np.array([[np.cos(z), -np.sin(z), 0], [np.sin(z), np.cos(z), 0], [0, 0, 1]])
    tilt = np.array([[1, 0, 0], [0, np.cos(x), -np.sin(x)], [0, np.sin(x), np.cos(x)]])
    return turn @ tilt


def run(folder, turn, needs=NEEDS, extra=None):
    """The worker on the phantom under `turn`, with the phantom's frame placed by it in RAS, and `extra` masks by name."""
    affine = np.eye(4)
    affine[:3, :3] = turn
    affine[:3, 3] = turn @ LOW + (12.0, -30.0, 40.0)
    # A true lumen 20 mm across, wider than the aorta mask, as one model's edge can lie outside another's.
    masks = {**phantom(), "aorta_true_lumen": phantom(RADIUS + 2)["aorta"], **(extra or {})}

    def save(name, values):
        image = nib.Nifti1Image(values, affine)
        image.header.set_xyzt_units("mm")
        nib.save(image, folder / f"{name}.nii.gz")
        return str(folder / f"{name}.nii.gz")

    ct = save("ct", np.where(masks["aorta"], 300, 0).astype(np.int16))
    request = {
        "report": "aorta",
        "ct": ct,
        "masks": {need: {"path": save(label, masks[label].astype(np.uint8)), "label": label} for need, label in needs.items()},
        "work": str(folder / "work"),
        "output": str(folder / "out"),
    }
    raw = report_worker.run(request)
    return raw, request, lambda point: turn.T @ (np.asarray(point) - affine[:3, 3]) + LOW


@pytest.fixture(scope="module", params=[np.eye(3), rotation(np.radians(25), np.radians(20))], ids=["straight", "oblique"])
def measured(request, tmp_path_factory):
    return run(tmp_path_factory.mktemp("aorta"), request.param)


def test_every_landmark_measures_the_tube_on_its_wall(measured):
    raw, request, local = measured
    curve = axis()

    def off_axis(point):
        return np.min(np.linalg.norm(curve - local(point), axis=1))

    landmarks = raw["upstream"]["landmarks"]
    assert sorted(raw["geometry"], key=int) == [str(n) for n in range(1, 12)], raw["missing"]
    for number, shape in raw["geometry"].items():
        landmark = landmarks[number]
        diameter = landmark["diameter"] * 10
        assert 2 * RADIUS - 1 <= diameter <= 2 * RADIUS + 2.5, number
        start, end = np.array(landmark["start"]), np.array(landmark["end"])
        assert np.linalg.norm(end - start) == pytest.approx(diameter, abs=1), number
        assert all(WALL[0] <= off_axis(point) <= WALL[1] for point in (start, end, landmark["start_pd"], landmark["end_pd"])), number
        assert off_axis(shape["centre"]) < 1.5, number
        assert np.linalg.norm(shape["normal"]) == pytest.approx(1)
        assert len(shape["outline"]) == report_worker.RING
        assert all(WALL[0] <= off_axis(point) <= WALL[1] for point in shape["outline"]), number
        assert (Path(request["output"]) / shape["picture"]).is_file()
    assert (Path(request["output"]) / "cpr.png").is_file() and raw["files"][0] == "cpr.png"
    assert raw["contrast"] == {"aorta_median_hu": 300.0, "native": False} and raw["dissection"] is False
    steps = np.linalg.norm(np.diff(raw["centreline"], axis=0), axis=1)
    assert np.median(steps) == pytest.approx(1, abs=0.1)
    assert off_axis(raw["centreline"][0]) < 2 and local(raw["centreline"][0])[2] < 3, "the centreline starts at the annulus"
    stats = raw["upstream"]["section_stats"]
    assert all(stats[name]["length"] is not None for name in stats)


def upstream(request, tmp_path, monkeypatch):
    """Upstream's own generator on the folders the worker wrote."""
    from totalsegmentator.aorta_report import pipeline

    for name in ("render_previews", "cleanup_workdir"):
        monkeypatch.setattr(pipeline, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "get_contrast_phase", lambda *args: "contrast")
    monkeypatch.setattr(pipeline, "render_report_image", lambda *args: None)
    monkeypatch.setattr(pipeline, "serialize_outputs", lambda *args: (None, None))
    work = Path(request["work"])
    *_, done = pipeline.create_aorta_report(
        work / "ct.nii.gz", work / "totalseg", work / "details", {}, tmp_path, None, delete_tmp=False, annulus_fn="annulus_proper.nii.gz"
    )
    return json.loads(json.dumps(report_worker._plain({key: done["report_json"][key] for key in ("landmarks", "section_stats")})))


def test_upstreams_own_generator_gives_the_same_numbers(measured, tmp_path, monkeypatch):
    raw, request, _ = measured
    assert upstream(request, tmp_path, monkeypatch) == raw["upstream"]


def test_a_true_lumen_without_a_false_lumen_is_measured_as_upstream_does(tmp_path, monkeypatch):
    # A revision holds no empty mask, while upstream's own run always has both lumen files.
    raw, request, _ = run(tmp_path, np.eye(3), {**NEEDS, "aorta_true_lumen": "aorta_true_lumen"})
    details = Path(request["work"]) / "details"
    assert (details / "aorta_false_lumen.nii.gz").is_file() and not nib.load(details / "aorta_false_lumen.nii.gz").get_fdata().any()
    (tmp_path / "upstream").mkdir()
    assert upstream(request, tmp_path / "upstream", monkeypatch) == raw["upstream"]
    # The true lumen joins the measured aorta, and no false lumen means no dissection.
    assert raw["upstream"]["landmarks"]["6"]["diameter"] * 10 > 2 * RADIUS + 2.5 and raw["dissection"] is False


@pytest.mark.parametrize(("bottom", "dissection"), [(20, False), (-100, True)], ids=["small", "large"])
def test_a_false_lumen_counts_as_a_dissection_only_from_5_ml(tmp_path, bottom, dissection):
    # Half of the descending aorta from 30 mm down: 11 mm of it holds about 1 ml, 131 mm about 12 ml.
    x, y, z = np.meshgrid(*(LOW[i] + np.arange(SHAPE[i]) for i in range(3)), indexing="ij")
    false_lumen = (x > 0) & (x**2 + (y + ARCH) ** 2 <= RADIUS**2) & (z >= bottom) & (z <= 30)
    lumens = {"aorta_true_lumen": "aorta_true_lumen", "aorta_false_lumen": "aorta_false_lumen"}
    raw, _, _ = run(tmp_path, np.eye(3), {**NEEDS, **lumens}, {"aorta_false_lumen": false_lumen})
    sentence = r"A false lumen of [\d.]+ ml was found, too small to count as a dissection \(under 5 ml\)\."
    small = [line for line in raw["warnings"] if re.fullmatch(sentence, line)]
    assert raw["dissection"] is dissection and len(small) == (0 if dissection else 1), raw["warnings"]


# The pulmonary phantom, in its own 1 mm frame: x left-right, y front-back, z up. A trunk rises from
# the valve at z = 0 into a crossbar at z = 50 whose two halves are the left (+x) and right branches.
TRUNK, BRANCH, CROSSBAR = 12.0, 8.0, 50.0  # mm: the trunk is 24 mm across, each branch 16 mm
PA_LOW, PA_SHAPE = np.array([-70.0, -25.0, -10.0]), (141, 51, 81)
# Each landmark mask is a disc 3 mm thick across the vessel: its centre, its axis and the vessel's
# radius there. The annulus is the vessel's own width, as upstream measures that landmark on the
# mask itself; the others are 1 mm wider than the vessel, which upstream cuts.
DISCS = {
    "pul_annulus": ((0, 0, 1), 2, TRUNK),
    "pul_sinotubular_junction": ((0, 0, 17), 2, TRUNK),
    "pul_bifurcation": ((0, 0, 35), 2, TRUNK),
    "pul_left_start": ((27, 0, CROSSBAR), 0, BRANCH),
    "pul_right_start": ((-27, 0, CROSSBAR), 0, BRANCH),
}
PA_NEEDS = {"pulmonary_artery": "pulmonary_artery", **{name: name for name in DISCS}}


def pulmonary_phantom():
    x, y, z = np.meshgrid(*(PA_LOW[i] + np.arange(PA_SHAPE[i]) for i in range(3)), indexing="ij")
    local = np.stack((x, y, z), axis=-1)
    masks = {"pulmonary_artery": ((x**2 + y**2 <= TRUNK**2) & (z >= 0) & (z <= CROSSBAR)) | ((y**2 + (z - CROSSBAR) ** 2 <= BRANCH**2) & (np.abs(x) <= 60))}
    for name, (centre, along, radius) in DISCS.items():
        offset = local - centre
        across = np.delete(offset, along, axis=-1)
        masks[name] = (np.abs(offset[..., along]) <= 1) & (np.linalg.norm(across, axis=-1) <= radius + (name != "pul_annulus"))
    return masks


def run_pulmonary(folder, turn, needs=PA_NEEDS):
    """The pulmonary worker on the phantom under `turn`, and the map from RAS back to the phantom's frame."""
    affine = np.eye(4)
    affine[:3, :3] = turn
    affine[:3, 3] = turn @ PA_LOW + (5.0, 10.0, 60.0)
    masks = pulmonary_phantom()

    def save(name, values):
        image = nib.Nifti1Image(values, affine)
        image.header.set_xyzt_units("mm")
        nib.save(image, folder / f"{name}.nii.gz")
        return str(folder / f"{name}.nii.gz")

    request = {
        "report": "pulmonary",
        "ct": save("ct", np.where(masks["pulmonary_artery"], 300, 0).astype(np.int16)),
        "masks": {need: {"path": save(label, masks[label].astype(np.uint8)), "label": label} for need, label in needs.items()},
        "work": str(folder / "work"),
        "output": str(folder / "out"),
    }
    return report_worker.run(request), request, lambda point: turn.T @ (np.asarray(point) - affine[:3, 3]) + PA_LOW


@pytest.fixture(scope="module", params=[np.eye(3), rotation(np.radians(25), np.radians(20))], ids=["straight", "oblique"])
def pulmonary(request, tmp_path_factory):
    return run_pulmonary(tmp_path_factory.mktemp("pulmonary"), request.param)


def test_every_pulmonary_landmark_measures_the_vessel(pulmonary):
    raw, request, local = pulmonary
    landmarks = raw["upstream"]["landmarks"]
    assert sorted(raw["geometry"], key=int) == ["1", "2", "3", "4", "5"] and raw["missing"] == {}, raw["missing"]
    # Read alike on the straight and the oblique grid: at the annulus, measured on its own mask, the
    # 24 mm trunk reads 25 mm (24 perpendicular, 4.4 cm2 against 4.5). Where upstream cuts the artery,
    # widening the cut by a voxel, the trunk reads 26 mm (26, 5.0 cm2) and the 16 mm branches 18 mm
    # (17, 2.4 cm2 against 2.0), as the aorta's tubes do.
    for (centre, along, radius), number in zip(DISCS.values(), sorted(raw["geometry"], key=int)):
        landmark, shape = landmarks[number], raw["geometry"][number]
        diameter = landmark["diameter"] * 10
        assert 2 * radius - 1 <= diameter <= 2 * radius + 2.5, number
        assert 2 * radius - 2 <= landmark["diameter_pd"] * 10 <= diameter, number
        assert 0.95 * np.pi * radius**2 / 100 <= landmark["area"] <= np.pi * (radius + 1.5) ** 2 / 100, number
        start, end = np.array(landmark["start"]), np.array(landmark["end"])
        assert np.linalg.norm(end - start) == pytest.approx(diameter, abs=1), number
        start_pd, end_pd = np.array(landmark["start_pd"]), np.array(landmark["end_pd"])
        assert np.linalg.norm(end_pd - start_pd) == pytest.approx(landmark["diameter_pd"] * 10, abs=1), number
        # Every end lies on the vessel's wall in the landmark's plane.
        for point in (start, end, start_pd, end_pd):
            offset = local(point) - centre
            assert abs(offset[along]) <= 1.5 and radius - 1.5 <= np.linalg.norm(np.delete(offset, along)) <= radius + 2, number
        assert np.linalg.norm(local(shape["centre"]) - centre) < 1.5, number
        # The normal lies along the vessel, pointing away from the valve.
        axis = np.eye(3)[along] * (np.sign(centre[0]) if along == 0 else 1)
        assert local(shape["normal"]) - local(np.zeros(3)) == pytest.approx(axis, abs=0.05), number
        assert len(shape["outline"]) == report_worker.RING
        assert all(radius - 2 <= np.linalg.norm(np.delete(local(point) - centre, along)) <= radius + 2 for point in shape["outline"]), number
        assert (Path(request["output"]) / shape["picture"]).is_file()
    assert raw["files"] == [f"plane-{number}.png" for number in range(1, 6)]
    assert [landmarks[str(number)]["diameter"] for number in range(1, 6)] == [2.5, 2.6, 2.6, 1.8, 1.8]


def upstream_pulmonary(request, tmp_path, monkeypatch):
    """Upstream's own generator on the folders the worker wrote, without its display server and pictures."""
    from totalsegmentator.pulmonary_report import pipeline

    monkeypatch.setitem(sys.modules, "xvfbwrapper", types.SimpleNamespace(Xvfb=contextlib.nullcontext))
    for name in ("plot_pulmonary_seg", "render_report_image", "cleanup_workdir"):
        monkeypatch.setattr(pipeline, name, lambda *args, **kwargs: None)
    monkeypatch.setattr(pipeline, "serialize_outputs", lambda *args: (None, None))
    work = Path(request["work"])
    *_, done = pipeline.create_pulmonary_report(work / "ct.nii.gz", work / "totalseg", work / "details", {}, tmp_path, None, delete_tmp=False)
    landmarks = {number: report_worker._numbers(landmark) for number, landmark in done["report_json"]["landmarks"].items()}
    return json.loads(json.dumps(report_worker._plain({"landmarks": landmarks})))


def test_upstreams_own_pulmonary_generator_gives_the_same_numbers(pulmonary, tmp_path, monkeypatch):
    raw, request, _ = pulmonary
    assert upstream_pulmonary(request, tmp_path, monkeypatch) == raw["upstream"]


def test_a_pulmonary_landmark_the_revision_lacks_is_not_measured(tmp_path):
    needs = {need: label for need, label in PA_NEEDS.items() if label != "pul_bifurcation"}
    raw, request, _ = run_pulmonary(tmp_path, np.eye(3), needs)
    stand_in = Path(request["work"]) / "details" / "pul_bifurcation.nii.gz"
    assert stand_in.is_file() and not nib.load(stand_in).get_fdata().any()
    assert sorted(raw["geometry"]) == ["1", "2", "4", "5"] and raw["missing"] == {"3": ["the pulmonary bifurcation was not found"]}
    landmarks = pulmonary_report.shape(raw)["landmarks"]
    assert [entry["available"] for entry in landmarks] == [True, True, False, True, True]
    assert landmarks[2]["unavailable_reason"] == "Not measured: the pulmonary bifurcation was not found" and landmarks[2]["diameter_mm"] is None


# The spine phantom, in RAS mm: block-shaped vertebral bodies 41 mm wide (x), 31 mm deep (y, front
# +) and 12 voxels or 24 mm high (z) on a 1 x 1 x 2 mm grid, 8 mm apart. L1 is a wedge whose front is
# 60% of its back, and T11 runs off the top of the scan. The scan is larger than the worker's crop.
BODY_HEIGHT, BODY_TOPS = 24.0, {"T11": 41.0, "T12": 7.0, "L1": -25.0, "L2": -57.0, "L3": -89.0}
SPINE_X, SPINE_Y, SPINE_Z = np.arange(-60.0, 61.0), np.arange(-70.0, 71.0), np.arange(-200.0, 40.0, 2.0)


def spine_phantom(coordinates, back=None):
    """Each body's mask at the voxels' RAS `coordinates`, a body named in `back` moved that far back."""
    x, y, z = coordinates
    masks = {}
    for name, top in BODY_TOPS.items():
        # The wedge's top falls from the back to 60% of the height at the front.
        height = BODY_HEIGHT * (1 - 0.4 * np.clip((y + 15) / 30, 0, 1)) if name == "L1" else BODY_HEIGHT
        bottom = top - BODY_HEIGHT
        masks[f"vertebrae_{name}"] = (np.abs(x) <= 20) & (np.abs(y + (back or {}).get(name, 0)) <= 15) & (z > bottom) & (z < bottom + height)
    return masks


def spine_grid(flipped):
    """The phantom's grid, straight or with its first two axes reversed, as an LPS-ordered scan's."""
    affine = np.diag([1.0, 1.0, 2.0, 1.0])
    affine[:3, 3] = (SPINE_X[0], SPINE_Y[0], SPINE_Z[0])
    if flipped:
        affine[:2, :2] = -np.eye(2)
        affine[:2, 3] = (SPINE_X[-1], SPINE_Y[-1])
    return affine


@contextlib.contextmanager
def picture_texts():
    """The texts drawn on matplotlib axes while open, which include upstream's slice numbers, and the shape
    of each image drawn, which shows the margins it had around the bodies."""
    import matplotlib.axes

    texts, text, imshow = [], matplotlib.axes.Axes.text, matplotlib.axes.Axes.imshow

    def record(self, x, y, s, *args, **kwargs):
        texts.append(str(s))
        return text(self, x, y, s, *args, **kwargs)

    def image(self, values, *args, **kwargs):
        texts.append(np.shape(values))
        return imshow(self, values, *args, **kwargs)

    matplotlib.axes.Axes.text, matplotlib.axes.Axes.imshow = record, image
    try:
        yield texts
    finally:
        matplotlib.axes.Axes.text, matplotlib.axes.Axes.imshow = text, imshow


def run_spine(folder, flipped, names=None, back=None, edit=None, value=200):
    """The worker on the phantom's bodies in `names` (all by default), with the texts on its picture. `edit`
    changes the masks by name first; `value` is the CT inside the bodies, 0 outside."""
    affine = spine_grid(flipped)
    shape = (len(SPINE_X), len(SPINE_Y), len(SPINE_Z))
    voxels = np.stack(np.meshgrid(*(np.arange(n) for n in shape), indexing="ij"), axis=-1).reshape(-1, 3)
    masks = spine_phantom((voxels @ affine[:3, :3].T + affine[:3, 3]).T, back)
    masks = {name: values.reshape(shape) for name, values in masks.items() if names is None or name.removeprefix("vertebrae_") in names}
    if edit:
        edit(masks)

    def save(name, values):
        image = nib.Nifti1Image(values, affine)
        image.header.set_xyzt_units("mm")
        nib.save(image, folder / f"{name}.nii.gz")
        return str(folder / f"{name}.nii.gz")

    request = {
        "report": "spine",
        "ct": save("ct", np.where(np.any(list(masks.values()), axis=0), value, 0).astype(np.int16 if value == int(value) else np.float64)),
        "masks": {name: {"path": save(name, values.astype(np.uint8)), "label": name} for name, values in masks.items()},
        "work": str(folder / "work"),
        "output": str(folder / "out"),
    }
    with picture_texts() as texts:
        raw = report_worker.run(request)
    return raw, request, masks, affine, texts


def upstream_spine(request, masks, affine, folder, monkeypatch):
    """Upstream's own measurement and picture texts on the whole scan, with all 24 labels as its command reads
    them. The picture is drawn but not saved, which changes no text."""
    from totalsegmentator.map_to_binary import class_map
    from totalsegmentator.spine_report import measure_verte_height

    labels = np.zeros(next(iter(masks.values())).shape, dtype=np.uint8)
    for number, name in spine_report.LEVELS:
        if f"vertebrae_{name}" in masks:
            labels[masks[f"vertebrae_{name}"]] = number
    ct = nib.as_closest_canonical(nib.load(request["ct"]))
    verte = nib.as_closest_canonical(nib.Nifti1Image(labels, affine))
    label_map = class_map["vertebrae_pp"]
    monkeypatch.setattr(measure_verte_height.plt, "savefig", lambda *args, **kwargs: None)
    folder.mkdir(exist_ok=True)
    with picture_texts() as texts:
        measure_verte_height.get_verte_height(
            ct, verte, label_map, verte, {}, None, folder / "spine.png", folder / "heights.json", False, horizontal_mask_type="raw", spine_range="all"
        )
    return json.loads((folder / "heights.json").read_text()), texts


@pytest.fixture(scope="module", params=[False, True], ids=["straight", "flipped"])
def spine(request, tmp_path_factory):
    pytest.importorskip("totalsegmentator.spine_report.measure_verte_height")
    return run_spine(tmp_path_factory.mktemp("spine"), request.param)


def test_every_spine_body_is_measured_at_its_known_height(spine):
    from totalsegmentator.spine_report import measure_verte_height

    raw, request, masks, affine, _ = spine
    result = spine_report.shape(raw)
    assert raw["unavailable"] == {"vertebrae_T11": "Its body touches the edge of the scan"}
    assert [entry["name"] for entry in result["vertebrae"]] == ["T11", "T12", "L1", "L2", "L3"]
    t11, *bodies = result["vertebrae"]
    assert not t11["available"] and t11["heights"][0] == {"id": "posterior", "mm": None, "ends": None} and t11["centre"] is None
    for entry in bodies:
        heights = {height["id"]: height for height in entry["heights"]}
        # The three lines run up the body's back, middle and front, on its measured slice.
        assert [heights[key]["ends"][0][1] for key in ("posterior", "middle", "anterior")] == sorted(heights[key]["ends"][0][1] for key in heights)
        assert all(abs(height["ends"][0][0] - entry["centre"][0]) < 1e-6 for height in entry["heights"])
        assert entry["normal"] == [1.0, 0.0, 0.0] and len(entry["outline"]) == report_worker.RING
        assert all(abs(x - entry["centre"][0]) < 1e-6 and abs(y) <= 16 for x, y, _ in entry["outline"])
        # The outline spans the body on its slice, front to back and head to foot, on a grid whose slice
        # spacing is twice its pixel spacing, and no point of it stays at the centre.
        index = int(round((np.linalg.inv(affine) @ [*entry["centre"], 1.0])[0]))
        section = np.argwhere(masks[entry["id"]][index])
        section = np.column_stack((np.full(len(section), index), section)) @ affine[:3, :3].T + affine[:3, 3]
        outline = np.array(entry["outline"])
        for axis, step in ((1, 1.0), (2, 2.0)):
            assert abs(outline[:, axis].min() - section[:, axis].min()) <= 1.5 * step, (entry["name"], axis)
            assert abs(outline[:, axis].max() - section[:, axis].max()) <= 1.5 * step, (entry["name"], axis)
        assert np.linalg.norm(outline - entry["centre"], axis=1).min() > 5
        assert entry["volume_ml"] == pytest.approx(41 * 31 * BODY_HEIGHT / 1000 * (0.8 if entry["name"] == "L1" else 1), rel=0.06)
        assert (entry["mean_hu"], entry["median_hu"], entry["small_body"]) == (200.0, 200.0, False)
        # Upstream's own rule on the unrounded heights: the smallest under 80% of the largest.
        heights_mm = {key: {"height_mm": value} for key, value in raw["geometry"][entry["id"]]["heights"].items()}
        assert entry["height_flag"] == measure_verte_height.detect_potential_fracture(heights_mm)["is_fracture"]
        if entry["name"] == "L1":
            # 15% and 85% of the way across a body whose front is 60% of its back.
            assert heights["posterior"]["mm"] == pytest.approx(BODY_HEIGHT * 0.94, abs=2.5)
            assert heights["anterior"]["mm"] == pytest.approx(BODY_HEIGHT * 0.66, abs=2.5)
            assert entry["height_flag"] and 20 < entry["height_difference_percent"] < 40
        else:
            assert all(height["mm"] == pytest.approx(BODY_HEIGHT, abs=2.5) for height in entry["heights"]), entry["name"]
            assert not entry["height_flag"] and entry["height_difference_percent"] < 10
    assert result["lumbar_max_difference_percent"] == bodies[1]["height_difference_percent"]
    assert result["density_thresholds_hu"] == {"native": 90.25, "arterial": 110.25, "portal_venous": 105.0} and result["warnings"] == []
    assert raw["files"] == ["spine.png"]
    from PIL import Image

    with Image.open(Path(request["output"]) / "spine.png") as picture:
        assert max(picture.size) < 3000


def test_upstreams_own_spine_measurement_gives_the_same_numbers(spine, tmp_path, monkeypatch):
    raw, request, masks, affine, texts = spine
    heights, upstream_texts = upstream_spine(request, masks, affine, tmp_path, monkeypatch)
    assert heights == raw["upstream"]
    # The picture's texts include each body's slice number, counted on the whole scan, not the crop.
    assert texts == upstream_texts and texts


def test_a_body_beside_a_gap_is_measured_as_upstreams_command_does(tmp_path, monkeypatch):
    # T12's height axis runs from T11's centre, moved back, not on to L2's; L2 has no body on either side.
    # T11 reaches down into T12, which keeps the voxels they share, and the CT is not whole numbers.
    pytest.importorskip("totalsegmentator.spine_report.measure_verte_height")

    def reach(masks):
        masks["vertebrae_T11"][..., 102:109] = masks["vertebrae_T11"][..., [109]]
        assert (masks["vertebrae_T11"] & masks["vertebrae_T12"]).any()

    raw, request, masks, affine, texts = run_spine(tmp_path, False, ("T11", "T12", "L2"), {"T11": 3.0}, reach, 200.5)
    assert list(raw["geometry"]) == ["vertebrae_T12", "vertebrae_L2"] and raw["unavailable"] == {"vertebrae_T11": report_worker.SPINE_EDGE}
    assert (raw["upstream"], texts) == upstream_spine(request, masks, affine, tmp_path / "upstream", monkeypatch)
    assert raw["warnings"] == ["L2 has no neighbouring body, so its height runs along the scan's own axis and its front and back may be swapped."]
    # T12's heights sit just under a tie (23.6513): upstream's two-decimal JSON rounds them up again, and
    # the result reads as the picture, which rounds the height once.
    t12 = spine_report.shape(raw)["vertebrae"][1]
    (text,) = (text for text in texts if isinstance(text, str) and text.startswith("T12:"))
    assert [f"{height['mm']:.1f}" for height in reversed(t12["heights"])] == text.split(": ")[1].split(" (")[0].split(" - ")
    assert round(raw["upstream"]["vertebrae_T12_height_middle_mm"], 1) != t12["heights"][1]["mm"]


def test_a_body_with_nothing_on_its_slice_draws_no_picture(tmp_path, monkeypatch):
    # L2 in two thin pieces left and right of the midline: upstream measures it on the empty slice between
    # them and draws nothing, it holds under 2 ml, and nothing is left 4 mm inside it.
    pytest.importorskip("totalsegmentator.spine_report.measure_verte_height")

    def split(masks):
        masks["vertebrae_L2"][41:80] = False

    raw, request, masks, affine, texts = run_spine(tmp_path, False, ("L2",), edit=split)
    assert (raw["upstream"], texts) == upstream_spine(request, masks, affine, tmp_path / "upstream", monkeypatch)
    assert raw["files"] == [] and not (Path(request["output"]) / "spine.png").exists()
    (l2,) = spine_report.shape(raw)["vertebrae"]
    assert l2["available"] and [height["mm"] for height in l2["heights"]] == [None] * 3 and l2["height_difference_percent"] is None
    assert l2["small_body"] and l2["volume_ml"] < 2 and l2["median_hu"] is None
    assert "nothing was left inside L2, so no density is given there." in raw["warnings"][-1]
    assert "No height line was found on L2: the mask may be in pieces or cover more than one body." in raw["warnings"]


@pytest.fixture
def body_stats(tmp_path, monkeypatch):
    """A body stats request on a stand-in for upstream's command, which records how it was called; the
    model folder has its five folds for CT and none for MR."""
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(tmp_path / "weights"))
    for fold in range(5):
        checkpoints = body_stats_report.folder("CT") / f"version_{fold}" / "checkpoints"
        checkpoints.mkdir(parents=True)
        (checkpoints / "last.ckpt").write_bytes(b"synthetic")
    calls, behaviour = [], {}

    def get_body_stats(img, modality, **options):
        calls.append((img, modality, options))
        if "raise" in behaviour:
            raise behaviour["raise"]
        yield {"id": 1, "progress": 2, "status": "Loading data"}
        yield {"id": 2, "progress": 75, "status": "Predicting body stats with CNN"}
        result = {"weight": {"value": 96.57, "unit": "kg", "min": np.float32(90.45), "max": 104.5, "stddev": 4.7341}, "sex": {"value": "M", "unit": None}}
        yield {"id": 3, "progress": 100, "status": "Done", "result": result}

    monkeypatch.setitem(sys.modules, "totalsegmentator.bin.totalseg_get_body_stats", types.SimpleNamespace(get_body_stats=get_body_stats))
    request = {"report": "body_stats", "ct": str(tmp_path / "image.nii.gz"), "modality": "CT", "masks": {}}
    request.update(work=str(tmp_path / "work"), output=str(tmp_path / "out"), progress=str(tmp_path / "progress.txt"))
    return request, calls, behaviour


def test_body_stats_runs_upstreams_generator_on_the_cpu(body_stats):
    request, calls, _ = body_stats
    steps = []
    raw = report_worker.body_stats(request, steps.append)
    assert steps == ["Loading data", "Predicting body stats with CNN", "Done"]
    ((img, modality, options),) = calls
    assert (img, modality) == (Path(request["ct"]), "ct")
    assert options == {"f_type": "niigz", "device": "cpu", "model_type": "cnn", "quiet": True}
    assert raw["upstream"]["weight"]["min"] == pytest.approx(90.45) and type(raw["upstream"]["weight"]["min"]) is float
    assert raw["upstream"]["sex"] == {"value": "M", "unit": None}
    assert (raw["modality"], raw["files"], raw["warnings"], raw["report_version"]) == ("CT", [], [], raw["engine_version"])


@pytest.mark.parametrize(
    ("modality", "error", "sentence"),
    [
        ("MR", None, "The body stats model for MR is not installed on this server"),
        ("CT", SystemExit(1), "Body stats needs a TotalSegmentator license configured on this workstation"),
        (
            "CT",
            ImportError("CNN body-stats inference requires monai to load the pretrained checkpoints."),
            "This server has no monai, which TotalSegmentator body stats needs",
        ),
    ],
    ids=["no-model", "no-license", "no-monai"],
)
def test_body_stats_fails_with_a_sentence(body_stats, modality, error, sentence):
    request, calls, behaviour = body_stats
    request["modality"] = modality
    if error is not None:
        behaviour["raise"] = error
    with pytest.raises(SystemExit):
        report_worker.run(request)
    assert json.loads((Path(request["output"]) / "error.json").read_text()) == {"error": sentence}
    # Without its model upstream is never called, so it cannot download one.
    assert len(calls) == (0 if error is None else 1)
