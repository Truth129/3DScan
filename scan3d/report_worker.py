"""One measurement report in a child process, measured by TotalSegmentator's own report code.

Research use only, not for diagnostic use. `python -P -m scan3d.report_worker REQUEST` reads a JSON
request naming the report, the CT, the revision's masks by need, a scratch folder and an output
folder. It writes `worker.json` and the pictures there, or `error.json` with one plain sentence. The
API process never imports this module: upstream's report code pulls in PyTorch.
"""

from __future__ import annotations

from contextlib import redirect_stdout
from importlib import metadata
import json
import logging
import os
from pathlib import Path
import re
import shutil
import sys

import nibabel as nib
import numpy as np
from scipy import ndimage

from .fileio import atomic_json
from .imaging import _binary, _image

NATIVE_HU = 100  # median CT value in the aorta below which the scan counts as native, as no contrast reached it
MARGIN_MM = 30.0  # kept around the aorta; upstream crops its own grid to the aorta plus 20 mm
RING = 72  # angle bins of a cross-section's outline
# Upstream's file names, and the folder each is read from.
_TOTALSEG = ("aorta", "brachiocephalic_trunk", "subclavian_artery_left", "celiac_trunk", "vertebrae_T12", "iliac_artery_right")
_DETAILS = ("annulus_proper", "sinotubular_junction")
_LUMENS = ("aorta_true_lumen", "aorta_false_lumen")
# Upstream's structure keys, as a reason names them.
_STRUCTURES = {
    "annulus": "aortic annulus",
    "sinotub_junc": "sinotubular junction",
    "brachio": "brachiocephalic trunk",
    "subclavian": "left subclavian artery",
    "T12": "T12 vertebra",
    "celiac": "celiac trunk",
    "iliac": "right iliac artery",
}
# Upstream's errors, said as what the reader can act on.
_FAILURES = {
    "annulus mask is empty": "The aortic annulus mask is empty near the aorta",
    "centerline from the aorta mask": "The aorta mask is too small or broken to trace a centreline",
    "Not enough vertices": "The aorta mask is too small or broken to trace a centreline",
    "Centerline is empty": "The aorta mask is too small or broken to trace a centreline",
}
# Upstream's warning about its own grid, which says nothing about the scan.
_NOISE = ("Affine contains negative values",)


class Failure(Exception):
    """A sentence for the job's error."""


class _Warnings(logging.Handler):
    """Upstream's WARNING lines, from its logger and its prints, without file paths."""

    def __init__(self):
        super().__init__()
        self.lines = []

    def emit(self, record):
        self.add(record.getMessage())

    def add(self, text):
        for line in str(text).splitlines():
            line = line.strip()
            if not line.startswith("WARNING"):
                continue
            line = re.sub(r"(?<![\w/])/[^\s'\"()]+", lambda match: Path(match.group()).name, line.removeprefix("WARNING").lstrip(": "))
            if line and not any(noise in line for noise in _NOISE) and line not in self.lines:
                self.lines.append(line)


class _Tee:
    """Stdout that still reaches the log, with upstream's printed warnings collected."""

    def __init__(self, stream, warnings):
        self.stream, self.warnings = stream, warnings

    def write(self, text):
        self.warnings.add(text)
        return self.stream.write(text)

    def flush(self):
        self.stream.flush()


def _plain(value):
    """JSON-ready copy of upstream's numbers: numpy scalars and arrays become floats and lists."""
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, np.ndarray)):
        return [_plain(item) for item in value]
    if isinstance(value, (np.bool_, bool)):
        return bool(value)
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return float(value) if np.isfinite(value) else None
    return value


def _points(affine, voxels):
    return np.asarray(voxels, dtype=float) @ affine[:3, :3].T + affine[:3, 3]


def _ring(points, centre, normal, voxel):
    """A closed outline of a cross-section: its voxels projected into the plane, filled, and cut by
    RING rays from the centre. Rays, not angle bins, because a thin bin at the rim of an oblique
    plane can hold only voxels from inside it."""
    helper = np.eye(3)[int(np.argmin(np.abs(normal)))]
    u = np.cross(normal, helper)
    u /= np.linalg.norm(u)
    v = np.cross(normal, u)
    flat = np.stack(((points - centre) @ u, (points - centre) @ v), axis=1)
    pixel = voxel / 2
    origin = flat.min(0) - 6 * pixel
    pixels = np.floor((flat - origin) / pixel).astype(int)
    filled = np.zeros(pixels.max(0) + 7, dtype=bool)
    filled[tuple(pixels.T)] = True
    filled = ndimage.binary_fill_holes(ndimage.binary_closing(filled, iterations=2))
    steps = np.arange(0, np.linalg.norm(flat, axis=1).max() + voxel, pixel / 2)
    angles = np.linspace(0, 2 * np.pi, RING, endpoint=False)
    ring = []
    for angle in angles:
        direction = np.array([np.cos(angle), np.sin(angle)])
        index = np.floor((steps[:, None] * direction - origin) / pixel).astype(int)
        index = np.clip(index, 0, np.array(filled.shape) - 1)
        inside = filled[tuple(index.T)]
        # The first run from the centre; a centre just off the section takes the run it reaches.
        start = int(np.argmax(inside)) if inside.any() else 0
        end = start + int(np.argmin(inside[start:])) - 1 if not inside[start:].all() else len(steps) - 1
        ring.append(centre + steps[max(end, 0)] * (direction[0] * u + direction[1] * v))
    return np.array(ring)


def _inputs(request, work):
    """Upstream's two mask folders and the CT, cropped from the CT's own grid around the aorta.

    The crop keeps the CT's voxel lattice, so upstream resamples onto the same points it would from
    the whole scan; it only saves reading and filtering the rest of the body.
    """
    image, affine = _image(Path(request["ct"]))
    masks = request["masks"]
    aorta, _, _ = _binary(Path(masks["aorta"]["path"]), image, affine)
    if not aorta.any():
        raise Failure("The aorta mask is empty")
    spacing = np.linalg.norm(affine[:3, :3], axis=0)
    margin = np.ceil(MARGIN_MM / spacing).astype(int) + 1
    found = np.argwhere(aorta)
    low = np.maximum(found.min(0) - margin, 0)
    high = np.minimum(found.max(0) + margin + 1, aorta.shape)
    window = tuple(slice(int(a), int(b)) for a, b in zip(low, high))
    cropped = affine.copy()
    cropped[:3, 3] = _points(affine, [low])[0]

    def save(values, path):
        output = nib.Nifti1Image(values, cropped)
        output.header.set_xyzt_units("mm")
        nib.save(output, path)
        return path

    totalseg, details, scratch = work / "totalseg", work / "details", work / "scratch"
    for folder in (totalseg, details, scratch):
        folder.mkdir(parents=True, exist_ok=True)
    ct = np.asarray(image.dataobj[window], dtype=np.float64)
    whole = np.array_equal(ct, np.round(ct)) and ct.min() >= -32768 and ct.max() <= 32767
    ct_path = save(ct.astype(np.int16 if whole else np.float32), work / "ct.nii.gz")
    median = float(np.median(ct[aorta[window]]))
    del ct

    labels = {need["label"]: Path(need["path"]) for need in masks.values()}
    # A revision holds no empty mask, so one lumen can come without the other. Upstream's own run
    # always has both files, so the absent one is written empty below.
    names = [*_TOTALSEG, *_DETAILS, *(_LUMENS if any(name in labels for name in _LUMENS) else ())]
    from totalsegmentator.aorta_report.constants import VESSELS_TO_PLOT

    # Upstream reads the vessels it draws in previews we never make; empty masks stand in for them.
    names += [name for name in VESSELS_TO_PLOT if name not in names]
    empty = None
    for name in names:
        target = (details if name in _DETAILS or name in _LUMENS else totalseg) / f"{name}.nii.gz"
        if name in labels:
            values = aorta if name == "aorta" else _binary(labels[name], image, affine)[0]
            save(np.asarray(values[window], dtype=np.uint8), target)
        elif empty is None:
            empty = save(np.zeros(tuple(high - low), dtype=np.uint8), target)
        else:
            try:
                os.link(empty, target)
            except OSError:
                shutil.copyfile(empty, target)
    return ct_path, totalseg, details, scratch, median


def aorta(request, progress):
    """TotalSegmentator's aorta report from "Loading data" through the curved view, in upstream's order."""
    work, output = Path(request["work"]), Path(request["output"])
    output.mkdir(parents=True, exist_ok=True)
    progress("Cropping the scan")
    ct_path, totalseg, details, scratch, median = _inputs(request, work)
    native = median < NATIVE_HU

    warnings = _Warnings()
    logger = logging.getLogger("scan3d.report_worker.aorta")
    logger.handlers[:] = [warnings, logging.StreamHandler(sys.stderr)]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        from totalsegmentator.aorta_report import VERSION, plotting
        from totalsegmentator.aorta_report.constants import LANDMARK_DEPENDENCIES, STRUCTURE_DEFINITIONS
        from totalsegmentator.aorta_report.centerline import get_normal_for_cl_point
        from totalsegmentator.aorta_report.cpr import cpr
        from totalsegmentator.aorta_report.geometry import find_mask_normal
        from totalsegmentator.aorta_report.landmarks import attach_structure_anchors, build_centerline, create_landmarks
        from totalsegmentator.aorta_report.measurements import create_landmark_planes, diameter_profiles, measure_landmarks, measure_sections
        from totalsegmentator.aorta_report.pipeline import _load_report_data
        from totalsegmentator.aorta_report.rendering import prepare_results
    except ImportError as error:
        raise Failure(f"TotalSegmentator's aorta report could not load: {error}") from error
    # Upstream frames the curved view in an HTML page with its makers' logo and the study's details,
    # rendered by wkhtmltoimage. Only the figure is kept.
    plotting.compose_report_image = lambda content, **_: content

    with redirect_stdout(_Tee(sys.stdout, warnings)):
        progress("Loading masks")
        data = _load_report_data(
            ct_path, totalseg, details, "aorta.nii.gz", "annulus_proper.nii.gz", scratch, "native" if native else "contrast", False, logger, False
        )
        progress("Creating the centreline")
        _, centerline, resampled = build_centerline(data["aorta"], data["annulus"], data["affine"], data["spacing"], logger, False)
        structures = attach_structure_anchors(data["structures"], data["aorta"], centerline, data["spacing"], logger)
        landmarks = create_landmarks(structures, data["annulus_volume"], centerline, data["aorta_img"], data["spacing"], logger)
        progress("Measuring landmarks")
        max_diameter = create_landmark_planes(landmarks, centerline, data["aorta"], data["annulus"], data["spacing"], logger, False)
        measure_landmarks(landmarks, data["ct_img"], data["true_lumen"], data["false_lumen"], data["affine"], max_diameter, scratch, logger)
        progress("Measuring sections")
        section_stats, _ = measure_sections(
            landmarks, data["aorta_img"], data["aorta_totalseg"], data["true_lumen"], data["false_lumen"], centerline, data["spacing"], max_diameter, logger
        )
        progress("Drawing the curved view")
        res_ct, res_seg, (true_cpr, false_cpr), cpr_info = cpr(
            data["ct_img"],
            data["aorta_img"],
            centerline,
            max_diameter,
            fast=False,
            debug=False,
            extra_mask_imgs=[
                nib.Nifti1Image(data["true_lumen"].astype(np.uint8), data["affine"]),
                nib.Nifti1Image(data["false_lumen"].astype(np.uint8), data["affine"]),
            ],
            return_info=True,
        )
        curve_positions, (aorta_curve, true_curve, false_curve) = diameter_profiles(
            [data["aorta_totalseg"], data["true_lumen"], data["false_lumen"]], resampled, data["spacing"], max(max_diameter * 1.6, 60)
        )
        # Upstream anchors the profile's legend below its axes, past the default bottom margin of 0.11.
        plotting.plt.rcParams["figure.subplot.bottom"] = 0.15
        plotting.plot_cpr_overview(
            res_ct,
            res_seg,
            output / "cpr.png",
            -700,
            1000,
            true_cpr,
            false_cpr,
            landmarks,
            np.asarray([vertex.point for vertex in centerline]),
            data["affine"],
            cpr_info["sample_positions_mm"],
            curve_positions,
            aorta_curve,
            true_curve,
            false_curve,
            {},
            scratch,
        )

    progress("Saving")
    affine = np.asarray(data["affine"], dtype=float)
    points = _points(affine, [vertex.point for vertex in centerline])
    files, geometry, missing = ["cpr.png"], {}, {}
    given = {need["label"] for need in request["masks"].values()}

    def set_aside(name):
        """Why upstream left a structure out, from the request and what attach_structure_anchors kept on it."""
        title = _STRUCTURES[name]
        if name == "annulus":
            return f"the {title} mask is too small"
        if STRUCTURE_DEFINITIONS[name][0] not in given:
            return f"the {title} was not found"
        if not structures[name]["intersect_aorta"].any():
            # Upstream asks 200 mm3 of these, and a touch on the aorta of the rest.
            return f"the {title} mask is too small" if name in ("T12", "sinotub_junc", "iliac") else f"the {title} mask does not touch the aorta"
        # Upstream places the two arch branches only together.
        other = {"brachio": "subclavian", "subclavian": "brachio"}.get(name)
        if other and not structures[other]["intersect_aorta"].any():
            return set_aside(other)
        return f"the {title} could not be placed on the centreline"

    for number, landmark in sorted(landmarks.items()):
        if landmark["empty"]:
            missing[number] = list(dict.fromkeys(set_aside(name) for name in LANDMARK_DEPENDENCIES[number] if structures[name]["empty"]))
            continue
        index = landmark["cl_idx"]
        # The plane's normal as create_landmark_planes took it, turned to point away from the heart.
        normal = find_mask_normal(data["annulus"].astype(np.uint8)) if landmark["name"] == "annulus" else get_normal_for_cl_point(centerline, index)
        normal = affine[:3, :3] @ np.asarray(normal, dtype=float)
        normal /= np.linalg.norm(normal)
        ahead = points[max(index - 5, 0)] - points[index] if index else points[index] - points[min(index + 5, len(points) - 1)]
        if normal @ ahead < 0:
            normal = -normal
        centre = _points(affine, [landmark["cl_point"]])[0]
        voxels = np.argwhere(landmark["roi"])
        if not len(voxels):
            # Upstream found no cross-section here and reports a zero diameter; the result says so.
            geometry[number] = {"centre": centre.tolist(), "normal": normal.tolist(), "outline": []}
            continue
        outline = _ring(_points(affine, voxels), centre, normal, float(min(data["spacing"])))
        geometry[number] = {"centre": centre.tolist(), "normal": normal.tolist(), "outline": outline.tolist()}
        picture = Path(landmark.get("plane_img") or "")
        if picture.is_file():
            shutil.copyfile(picture, output / f"plane-{number}.png")
            geometry[number]["picture"] = f"plane-{number}.png"
            files.append(f"plane-{number}.png")
    results = prepare_results(dict(sorted(landmarks.items())), section_stats, {})
    return {
        "engine_version": metadata.version("TotalSegmentator"),
        "report_version": VERSION,
        "contrast": {"aorta_median_hu": round(median, 1), "native": native},
        # The false lumen upstream keeps, which is empty on a native scan or without lumen masks.
        "dissection": bool(np.any(data["false_lumen"])),
        "upstream": _plain({"landmarks": results["landmarks"], "section_stats": results["section_stats"]}),
        "geometry": {str(number): value for number, value in geometry.items()},
        "missing": {str(number): value for number, value in missing.items()},
        "centreline": _points(affine, [vertex.point for vertex in resampled[::-1]]).tolist(),
        "warnings": warnings.lines,
        "files": files,
    }


WORKERS = {"aorta": aorta}


def run(request):
    """Measure, writing worker.json, or error.json and SystemExit(1) on a failure with a sentence."""
    output = Path(request["output"])
    output.mkdir(parents=True, exist_ok=True)
    progress_path = Path(request["progress"]) if request.get("progress") else None

    def progress(text):
        if progress_path:
            progress_path.write_text(text)

    try:
        raw = WORKERS[request["report"]](request, progress)
    except (Failure, ValueError) as error:
        text = str(error)
        sentence = next((plain for marker, plain in _FAILURES.items() if marker in text), text) if not isinstance(error, Failure) else text
        atomic_json(output / "error.json", {"error": sentence})
        raise SystemExit(1) from error
    atomic_json(output / "worker.json", raw)
    return raw


if __name__ == "__main__":
    run(json.loads(Path(sys.argv[1]).read_text()))
