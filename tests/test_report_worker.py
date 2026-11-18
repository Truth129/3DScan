"""TotalSegmentator's aorta and pulmonary artery reports measuring synthetic vessels of known width, on a
straight and an oblique grid."""

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

from scan3d import pulmonary_report, report_worker  # noqa: E402

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
