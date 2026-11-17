"""TotalSegmentator's aorta report measuring a synthetic aorta of known width, on a straight and an oblique grid."""

import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

pytest.importorskip("totalsegmentator.aorta_report.pipeline")

from scan3d import report_worker  # noqa: E402

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


def run(folder, turn, needs=NEEDS):
    """The worker on the phantom under `turn`, with the phantom's frame placed by it in RAS."""
    affine = np.eye(4)
    affine[:3, :3] = turn
    affine[:3, 3] = turn @ LOW + (12.0, -30.0, 40.0)
    # A true lumen 20 mm across, wider than the aorta mask, as one model's edge can lie outside another's.
    masks = {**phantom(), "aorta_true_lumen": phantom(RADIUS + 2)["aorta"]}

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
