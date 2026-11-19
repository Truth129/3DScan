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
import types

import nibabel as nib
import numpy as np
from scipy import ndimage

from .fileio import atomic_json
from .imaging import _binary, _foreground_bounds, _image

NATIVE_HU = 100  # median CT value in the aorta below which the scan counts as native, as no contrast reached it
FALSE_LUMEN_ML = 5.0  # the dissection model leaves stray false lumen under 1 ml on normal aortas; a real one is far larger
MARGIN_MM = 30.0  # kept around the measured vessel; upstream crops its own grid to it plus 20 mm
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
# The pulmonary report's landmark masks by upstream's file name, in its landmark order, as a reason names them.
_PULMONARY = {
    "pul_annulus": "pulmonary valve annulus",
    "pul_sinotubular_junction": "pulmonary sinotubular junction",
    "pul_bifurcation": "pulmonary bifurcation",
    "pul_left_start": "start of the left pulmonary artery",
    "pul_right_start": "start of the right pulmonary artery",
}
NO_LANDMARKS = "None of the five pulmonary landmarks was found"
NO_BODY = "No vertebral body was found"
SPINE_EDGE = "Its body touches the edge of the scan"
# Kept around the vertebral bodies: along the axes nearest front-back and head-foot, past the 40 mm
# upstream pads its picture by, and along the axis nearest left-right, past its 3 and 4 mm erosions.
SPINE_MARGIN_MM = (45.0, 10.0)
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


def _crop(request, work, anchor, empty):
    """The CT cropped from its own grid to the `anchor` need's mask plus MARGIN_MM, saved as
    `work/ct.nii.gz`, with the cropped CT values, the anchor's mask on the crop and a writer that
    saves a mask by upstream's name on the same crop.

    The crop keeps the CT's voxel lattice, so upstream resamples onto the same points it would from
    the whole scan; it only saves reading and filtering the rest of the body. A name the request
    lacks is written as an empty mask, since upstream reads a file for every name.
    """
    image, affine = _image(Path(request["ct"]))
    masks = request["masks"]
    vessel, _, _ = _binary(Path(masks[anchor]["path"]), image, affine)
    if not vessel.any():
        raise Failure(empty)
    spacing = np.linalg.norm(affine[:3, :3], axis=0)
    margin = np.ceil(MARGIN_MM / spacing).astype(int) + 1
    found = np.argwhere(vessel)
    low = np.maximum(found.min(0) - margin, 0)
    high = np.minimum(found.max(0) + margin + 1, vessel.shape)
    window = tuple(slice(int(a), int(b)) for a, b in zip(low, high))
    cropped = affine.copy()
    cropped[:3, 3] = _points(affine, [low])[0]

    def save(values, path):
        output = nib.Nifti1Image(values, cropped)
        output.header.set_xyzt_units("mm")
        nib.save(output, path)
        return path

    work.mkdir(parents=True, exist_ok=True)
    ct = np.asarray(image.dataobj[window], dtype=np.float64)
    whole = np.array_equal(ct, np.round(ct)) and ct.min() >= -32768 and ct.max() <= 32767
    ct_path = save(ct.astype(np.int16 if whole else np.float32), work / "ct.nii.gz")

    labels = {need["label"]: Path(need["path"]) for need in masks.values()}
    stand_in = None

    def write(name, target):
        nonlocal stand_in
        if name in labels:
            values = vessel if name == masks[anchor]["label"] else _binary(labels[name], image, affine)[0]
            save(np.asarray(values[window], dtype=np.uint8), target)
        elif stand_in is None:
            stand_in = save(np.zeros(tuple(high - low), dtype=np.uint8), target)
        else:
            try:
                os.link(stand_in, target)
            except OSError:
                shutil.copyfile(stand_in, target)

    return ct_path, ct, vessel[window], write


def _inputs(request, work):
    """Upstream's two mask folders and the CT, cropped from the CT's own grid around the aorta."""
    ct_path, ct, aorta, write = _crop(request, work, "aorta", "The aorta mask is empty")
    median = float(np.median(ct[aorta]))
    del ct
    totalseg, details, scratch = work / "totalseg", work / "details", work / "scratch"
    for folder in (totalseg, details, scratch):
        folder.mkdir(parents=True, exist_ok=True)

    labels = {need["label"] for need in request["masks"].values()}
    # A revision holds no empty mask, so one lumen can come without the other. Upstream's own run
    # always has both files, so the absent one is written empty.
    names = [*_TOTALSEG, *_DETAILS, *(_LUMENS if any(name in labels for name in _LUMENS) else ())]
    from totalsegmentator.aorta_report.constants import VESSELS_TO_PLOT

    # Upstream reads the vessels it draws in previews we never make; empty masks stand in for them.
    names += [name for name in VESSELS_TO_PLOT if name not in names]
    for name in names:
        write(name, (details if name in _DETAILS or name in _LUMENS else totalseg) / f"{name}.nii.gz")
    return ct_path, totalseg, details, scratch, median


def _logger(name):
    """Upstream's logger for one run, its WARNING lines collected and everything else in the log."""
    warnings = _Warnings()
    logger = logging.getLogger(f"scan3d.report_worker.{name}")
    logger.handlers[:] = [warnings, logging.StreamHandler(sys.stderr)]
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger, warnings


def aorta(request, progress):
    """TotalSegmentator's aorta report from "Loading data" through the curved view, in upstream's order."""
    work, output = Path(request["work"]), Path(request["output"])
    output.mkdir(parents=True, exist_ok=True)
    progress("Cropping the scan")
    ct_path, totalseg, details, scratch, median = _inputs(request, work)
    native = median < NATIVE_HU

    logger, warnings = _logger("aorta")
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
    # The false lumen upstream keeps, which is empty on a native scan or without lumen masks.
    false_lumen = float(np.count_nonzero(data["false_lumen"]) * np.prod(data["spacing"]) / 1000)
    if 0 < false_lumen < FALSE_LUMEN_ML:
        line = f"A false lumen of {false_lumen:.2g} ml was found, too small to count as a dissection (under {FALSE_LUMEN_ML:g} ml)."
        if line not in warnings.lines:
            warnings.lines.append(line)
    return {
        "engine_version": metadata.version("TotalSegmentator"),
        "report_version": VERSION,
        "contrast": {"aorta_median_hu": round(median, 1), "native": native},
        "dissection": false_lumen >= FALSE_LUMEN_ML,
        "upstream": _plain({"landmarks": results["landmarks"], "section_stats": results["section_stats"]}),
        "geometry": {str(number): value for number, value in geometry.items()},
        "missing": {str(number): value for number, value in missing.items()},
        "centreline": _points(affine, [vertex.point for vertex in resampled[::-1]]).tolist(),
        "warnings": warnings.lines,
        "files": files,
    }


def pulmonary(request, progress):
    """TotalSegmentator's pulmonary artery report from "Loading data" through its results, in upstream's
    order, without the 3D previews and HTML page, which change no number."""
    work, output = Path(request["work"]), Path(request["output"])
    output.mkdir(parents=True, exist_ok=True)
    given = {need["label"] for need in request["masks"].values()}
    if not given & set(_PULMONARY):
        raise Failure(NO_LANDMARKS)
    progress("Cropping the scan")
    ct_path, ct, _, write = _crop(request, work, "pulmonary_artery", "The pulmonary artery mask is empty")
    del ct
    totalseg, details, scratch = work / "totalseg", work / "details", work / "scratch"
    for folder in (totalseg, details, scratch):
        folder.mkdir(parents=True, exist_ok=True)
    write("pulmonary_artery", totalseg / "pulmonary_artery.nii.gz")
    # Upstream wants all five landmark files; one the revision lacks is written empty, which it reads
    # as an empty landmark.
    for name in _PULMONARY:
        write(name, details / f"{name}.nii.gz")

    logger, warnings = _logger("pulmonary")
    try:
        from totalsegmentator.aorta_report.geometry import find_center, find_mask_normal
        from totalsegmentator.pulmonary_report import VERSION
        from totalsegmentator.pulmonary_report.measurements import create_landmark_planes, measure_landmarks
        from totalsegmentator.pulmonary_report.pipeline import _load_report_data
        from totalsegmentator.pulmonary_report.rendering import prepare_results
    except ImportError as error:
        raise Failure(f"TotalSegmentator's pulmonary artery report could not load: {error}") from error

    with redirect_stdout(_Tee(sys.stdout, warnings)):
        progress("Loading masks")
        data = _load_report_data(ct_path, totalseg, details, "pulmonary_artery.nii.gz", scratch, logger, False)
        landmarks = data["landmarks"]
        # Read before the planes, which also mark empty a landmark whose plane misses the artery.
        unread = {number for number, landmark in landmarks.items() if landmark["empty"]}
        progress("Measuring landmarks")
        max_diameter = create_landmark_planes(landmarks, data["pulmonary_artery"], data["spacing"], logger)
        measure_landmarks(landmarks, data["ct_img"], data["affine"], max_diameter, scratch, logger, False)

    progress("Saving")
    affine = np.asarray(data["affine"], dtype=float)
    # Normals point away from the pulmonary valve, as the aorta's point away from the heart: from the
    # annulus landmark's centre when it has a mask, else from the artery mask's centre, which leaves
    # the trunk's normals a fixed but arbitrary sign. The annulus's own normal points into the artery.
    artery = _points(affine, [find_center(data["pulmonary_artery"])])[0]
    valve = artery if 1 in unread else _points(affine, [find_center(landmarks[1]["data"])])[0]
    files, geometry, missing = [], {}, {}
    for number, landmark in sorted(landmarks.items()):
        name = landmark["name"]
        if landmark["empty"]:
            if name not in given:
                missing[number] = [f"the {_PULMONARY[name]} was not found"]
            elif number in unread:
                missing[number] = [f"the {_PULMONARY[name]} mask is empty near the pulmonary artery"]
            else:
                missing[number] = ["no cross-section of the pulmonary artery was found there"]
            continue
        centre = _points(affine, [landmark["cl_point"]])[0]
        # The normal upstream cut the artery with; landmark 1's plane is its own mask, whose normal is the same.
        normal = affine[:3, :3] @ np.asarray(find_mask_normal(landmark["data"]), dtype=float)
        normal /= np.linalg.norm(normal)
        if normal @ (artery - centre if number == 1 else centre - valve) < 0:
            normal = -normal
        voxels = np.argwhere(landmark["roi"])
        outline = _ring(_points(affine, voxels), centre, normal, float(min(data["spacing"])))
        geometry[number] = {"centre": centre.tolist(), "normal": normal.tolist(), "outline": outline.tolist()}
        picture = Path(landmark.get("plane_img") or "")
        if picture.is_file():
            shutil.copyfile(picture, output / f"plane-{number}.png")
            geometry[number]["picture"] = f"plane-{number}.png"
            files.append(f"plane-{number}.png")
    results = prepare_results(landmarks, {})
    return {
        "engine_version": metadata.version("TotalSegmentator"),
        "report_version": VERSION,
        # A landmark whose plane missed the artery keeps its empty plane array; only the numbers are wanted.
        "upstream": _plain({"landmarks": {number: _numbers(landmark) for number, landmark in results["landmarks"].items()}}),
        "geometry": {str(number): value for number, value in geometry.items()},
        "missing": {str(number): value for number, value in missing.items()},
        "warnings": warnings.lines,
        "files": files,
    }


def _numbers(landmark):
    return {key: value for key, value in landmark.items() if key != "roi"}


def spine(request, progress):
    """TotalSegmentator's spine report from its vertebral heights through its combined picture, in upstream's
    order, without its contrast phase classifier and front page, which change no measured number."""
    work, output = Path(request["work"]), Path(request["output"])
    output.mkdir(parents=True, exist_ok=True)
    try:
        from totalsegmentator.bin.totalseg_spine_report import ART_HU_ADDON, HU_THRESHOLD, PV_HU_ADDON, VERSION
        from totalsegmentator.map_to_binary import class_map
        from totalsegmentator.spine_report import measure_verte_height as upstream
    except ImportError as error:
        raise Failure(f"TotalSegmentator's spine report could not load: {error}") from error

    progress("Cropping the scan")
    image, affine = _image(Path(request["ct"]))
    numbers = {name: number for number, name in class_map["vertebrae_pp"].items()}
    needs = {mask["label"]: need for need, mask in request["masks"].items()}
    # Each body's own box, so the scan-sized masks are read once and never held together.
    boxes = {}
    for label in sorted(needs, key=numbers.__getitem__):
        values = _binary(Path(request["masks"][needs[label]]["path"]), image, affine)[0]
        if (found := _foreground_bounds(values)) is not None:
            low, high = found[0], found[1] + 1
            # A copy, as a view would keep the whole mask.
            boxes[label] = (low, values[tuple(slice(int(a), int(b)) for a, b in zip(low, high))].copy())
        del values
    if not boxes:
        raise Failure(NO_BODY)
    # The crop keeps the CT's own voxel lattice, so upstream measures the same voxels as on the whole scan.
    # Clamped to the scan, a body touching its edge still touches the crop's, which upstream refuses to
    # measure, and any other body stays SPINE_MARGIN_MM inside.
    spacing = np.linalg.norm(affine[:3, :3], axis=0)
    across = np.argmax(np.abs(affine[:3, :3] / spacing), axis=0) == 0  # the voxel axis nearest left-right
    margin = np.ceil(np.where(across, SPINE_MARGIN_MM[1], SPINE_MARGIN_MM[0]) / spacing).astype(int)
    low = np.maximum(np.min([box[0] for box in boxes.values()], axis=0) - margin, 0)
    high = np.minimum(np.max([box[0] + box[1].shape for box in boxes.values()], axis=0) + margin, image.shape)
    # Even offsets from both ends: upstream's fallback slice rounds a centroid's half to the even slice.
    low, high = low - low % 2, high + (np.array(image.shape) - high) % 2
    window = tuple(slice(int(a), int(b)) for a, b in zip(low, high))
    cropped = affine.copy()
    cropped[:3, 3] = _points(affine, [low])[0]
    labels = np.zeros(tuple(high - low), dtype=np.uint8)
    # C1 to L5, so the lower body keeps a voxel two corrected masks share.
    for label, (start, values) in boxes.items():
        labels[tuple(slice(int(a), int(a) + n) for a, n in zip(start - low, values.shape))][values] = numbers[label]
    ct = np.asarray(image.dataobj[window], dtype=np.float64)
    if np.array_equal(ct, np.round(ct)) and ct.min() >= -32768 and ct.max() <= 32767:
        ct = ct.astype(np.int16)

    def canonical(values):
        crop = nib.Nifti1Image(values, cropped)
        crop.header.set_xyzt_units("mm")
        return nib.as_closest_canonical(crop)

    ct_img, verte_img = canonical(ct), canonical(labels)
    del ct
    found = set(np.unique(labels).tolist())

    # Upstream's only warnings on this path are prints starting "Warning:", which go to the log: an empty
    # mask, printed for each absent body as in its own run, and "No vertebrae body mask found, using full
    # vertebrae masks", which is how its own command runs.
    progress("Measuring vertebrae")
    # All 24 labels, as upstream's command reads them from its output's header, present or not: an absent
    # body keeps an empty centroid, so a body beside a gap takes its height axis as upstream's command does.
    processor = upstream.VertebraeProcessor(ct_img, verte_img, class_map["vertebrae_pp"], verte_img, {}, horizontal_mask_type="raw", spine_range="all")
    info = processor.process_vertebrae()
    work.mkdir(parents=True, exist_ok=True)
    upstream.save_results(info, work / "heights.json")
    results = json.loads((work / "heights.json").read_text())

    files = []
    if info:
        progress("Drawing the picture")
        # Upstream prints each body's slice number counted on the whole scan's canonical first axis, from
        # ct_data.shape[0], the only thing it reads of ct_data there; the crop's would count from the crop.
        axis = int(np.flatnonzero(nib.io_orientation(affine)[:, 0] == 0)[0])
        flipped = nib.io_orientation(affine)[axis, 1] < 0
        length = int(high[axis]) if flipped else image.shape[axis] - int(low[axis])
        savefig, annotate = upstream.plt.savefig, upstream._add_combined_annotations
        # Upstream saves at 600 dpi, about 4800 x 7200 pixels, far beyond a DICOM Secondary Capture's use.
        upstream.plt.savefig = lambda *args, **kwargs: savefig(*args, **{**kwargs, "dpi": 200})
        upstream._add_combined_annotations = lambda ax, ct_data, *args, **kwargs: annotate(ax, types.SimpleNamespace(shape=(length,)), *args, **kwargs)
        try:
            upstream.create_combined_spine_visualization(ct_img, processor.ct_data, info, list(info), output / "spine.png", False, "all")
        finally:
            upstream.plt.savefig, upstream._add_combined_annotations = savefig, annotate
        # Upstream draws nothing when no measured body has a voxel on its slice.
        if (output / "spine.png").is_file():
            files.append("spine.png")

    progress("Saving")
    canon = np.asarray(ct_img.affine, dtype=float)
    normal = canon[:3, 0] / np.linalg.norm(canon[:3, 0])  # the canonical first axis, toward the patient's right
    # The coarser of the slice's two spacings: _ring closes gaps of a few of its pixels, so on the finer one
    # rows a slice spacing apart stay apart and the outline stops at the first gap.
    voxel = float(max(ct_img.header.get_zooms()[1:3]))
    geometry, unavailable = {}, {}
    for label, entry in info.items():
        x = entry["x_slice"]
        centre = _points(canon, [(x, entry["centroid"][1], entry["centroid"][2])])[0]
        ends, heights = {}, {}
        for key, measurement in entry["height_measurements"].items():
            if measurement["height_start"] and measurement["height_end"]:
                ends[upstream.POSITION_MAPPING[key]] = _points(canon, [(x, *measurement["height_start"]), (x, *measurement["height_end"])]).tolist()
                # Unrounded, as the picture prints it: its two-decimal JSON rounded again can differ by 0.1 mm.
                heights[upstream.POSITION_MAPPING[key]] = float(measurement["height_mm"])
        section = np.argwhere(entry["body_mask"][x])
        outline = _ring(_points(canon, np.column_stack((np.full(len(section), x), section))), centre, normal, voxel) if len(section) else np.empty((0, 3))
        geometry[needs[label]] = {
            "centre": centre.tolist(),
            "normal": normal.tolist(),
            "ends": ends,
            "heights": heights,
            "outline": outline.tolist(),
            "height_flag": bool(upstream.detect_potential_fracture(entry["height_measurements"])["is_fracture"]),
            "small_body": entry["body_volume_mm3"] / 1000 < upstream.BODY_VOLUME_YELLOW_THRESHOLD_ML,
        }
    for label, need in needs.items():
        if label not in info:
            body = labels == numbers[label]
            edge = body.any() and any(body.take(index, axis=axis).any() for axis in range(3) for index in (0, -1))
            unavailable[need] = SPINE_EDGE if edge else "Could not be measured"
    warnings = []
    # Upstream takes a body's height axis from its neighbours' centres, and the scan's own axis without either.
    alone = [label.removeprefix("vertebrae_") for label in info if not {numbers[label] - 1, numbers[label] + 1} & (found - {0})]
    if alone:
        one = len(alone) == 1
        warnings.append(
            f"{' and '.join(alone)} {'has' if one else 'have'} no neighbouring body, so {'its height runs' if one else 'their heights run'} along the scan's own "
            f"axis and {'its front and back' if one else 'their fronts and backs'} may be swapped."
        )
    # A label in pieces, or left over two bodies by the model, has its centre outside the mask, and upstream draws no line.
    lineless = [
        label.removeprefix("vertebrae_")
        for label, entry in info.items()
        if not any(measurement["height_start"] and measurement["height_end"] for measurement in entry["height_measurements"].values())
    ]
    if lineless:
        warnings.append(f"No height line was found on {' and '.join(lineless)}: the mask may be in pieces or cover more than one body.")
    hollow = [label.removeprefix("vertebrae_") for label, entry in info.items() if entry["mean_intensity"] == 0.0 and entry["median_intensity"] == 0.0]
    if hollow:
        warnings.append(
            f"TotalSegmentator reads CT values 4 mm inside each body's edge, and nothing was left inside {' and '.join(hollow)}, so no density is given there."
        )
    return {
        "engine_version": metadata.version("TotalSegmentator"),
        "report_version": VERSION,
        "upstream": _plain(results),
        "geometry": geometry,
        "unavailable": unavailable,
        # Upstream's own command never reaches its arterial threshold, because its phase names are arterial_early and arterial_late.
        "thresholds": {"native": HU_THRESHOLD, "arterial": HU_THRESHOLD + ART_HU_ADDON, "portal_venous": HU_THRESHOLD + PV_HU_ADDON},
        "warnings": warnings,
        "files": files,
    }


WORKERS = {"aorta": aorta, "pulmonary": pulmonary, "spine": spine}


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
