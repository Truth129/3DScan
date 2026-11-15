"""The lumen mask's widths, corrected from the CT values. Research use only.

Two masks of one scan follow the same vessels but draw them a few tenths of a millimetre apart
in width, by an amount that is nearly constant over a scan and differs between models. That moves
FFR more than the physics does. So the mask keeps its job, where each vessel is and how its width
changes along it, and the CT says only how far the mask's edge sits from the lumen's. At stations
wide enough for their own centre to show the lumen's HU, the lumen's edge is where the CT falls to
half height, corrected for the scan's blur. The median difference from the mask's diameter there
is the scan's offset, and every station's width moves by the offset found near it.

The half height is taken against soft tissue: a ray's background counts as no darker than water.
A vessel's wall is soft tissue and thinner than the blur, so against the fat beyond it the edge
reads wide. On real scans a vessel then measured up to 0.3 mm wider across its fat side than
against the muscle it lies on; with the floor the two agree within 0.1 mm. Where the wall is
darker than the muscle beside it the width still reads thin under every mask: on phantoms by
about 0.15 mm at a 400 HU lumen and 0.25 to 0.35 mm at 200 to 250 HU. So the offset is not only
the mask's error, and two masks that agree do not show the width is right.

An earlier design read each station's area from the CT alone. It needed the lumen's HU where the
vessel is too thin to show it, and real scans do not give one: the centre HU of a 2 mm vessel is
often half the proximal level, by branch. Its readings were also rougher along the vessel than a
mask's, which made lesions out of noise.

Positions are patient RAS millimetres, and the CT is read through the full affine, so oblique,
reflected and sheared grids are sampled as they are.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import cache
import warnings

import numpy as np
from scipy.ndimage import map_coordinates
from scipy.special import chndtr

from .vessel_geometry import Stations

REACH = 7.5  # mm around a station that may be read: a ray's length from a centre moved as far as it may
_RAYS = 32  # across each plane
_STEP = 0.15  # mm between samples along a ray
_SHIFT = 1.5  # mm; a plane's centre moves no farther than this from its station
_PASSES = 3  # times a plane's centre moves to the centre of the edge it finds
_TANGENT = 2.0  # mm either side of a station that give the vessel's direction there
_CHUNK = 512  # planes worked on at a time
# The rest were settled on real scans, most on two masks of each. They are not tuned on phantoms,
# and phantoms pass without some of them (_BRIGHT, _BLOOM, _CLIP, _ALONG): those are checked on such pairs, not by the tests.
_CORE = 0.35  # mm around a plane's centre whose mean is its centre HU
_NEAR = 0.9  # mm from the centre where a ray's background may start...
_BEYOND = 2.0  # ...and mm past the mask's radius where it ends
_RUN = 1.0  # mm of ray averaged into a background value
_DARKEST = 0.0  # HU, water; a background is taken no lower, so half height is against soft tissue, as the wall is, and not the fat or lung beyond it. Settled on real scans: a vessel's fat side and its muscle side then read the same width
_CONTRAST = 80.0  # HU a ray's background must lie below the centre HU...
_BRIGHT = 0.4  # ...and this fraction of the plane's own contrast, or a neighbour as bright as the lumen fills the ray
_CALCIUM = 150.0  # HU above the centre HU: calcium or a stent, not lumen
_BLOOM = 3  # rays either side of such a ray that its blooming spoils
_KEPT = 0.5  # fraction of a plane's rays that must be kept for a half-height radius
_WIDE = 1.5  # mm; from this half-height radius an edge shows the scan's blur
_FEW = 5  # with fewer such stations the blur is taken as...
_SIGMA = 0.45  # ...this standard deviation, mm
_BLUR = (0.3, 0.7)  # mm; a measured one is kept within these
_QUARTILES = 1.349  # width of a Gaussian edge from 25% to 75% of its height, in standard deviations
_SURE = 0.7  # a station is read when blur leaves its centre this fraction of the lumen's contrast or more: a radius of about 1.6 standard deviations
_CLEAN = 0.75  # and this fraction of its rays was kept
ENOUGH = 20  # stations read before a scan is corrected at all
_LIMIT = 1.0  # mm; a scan whose offset is larger is not corrected: the mask or the reading is wrong
_ALONG = 7.5  # mm either side along a branch whose read stations give a station's own offset...
_CLIP = 0.5  # ...each within this many mm of the scan's...
_FULL = 16  # ...and trusted wholly from this many, in proportion below
_THINNEST = 0.5  # a correction leaves at least this fraction of the mask's diameter
_FWHM = 2 * np.sqrt(2 * np.log(2))  # a Gaussian's full width at half maximum, in standard deviations


@dataclass(frozen=True, eq=False)
class Widths:
    """Lumen areas for a tree, one array per branch, and how the CT corrected them."""

    area: list[np.ndarray]  # mm² per station: the mask's, moved by the offset when `calibrated`
    ct_diameter: list[np.ndarray]  # mm where the CT's own edge was read, NaN elsewhere
    calcium: list[np.ndarray]  # values far above the centre HU lie in the station's plane, so it was not read
    calibrated: bool  # False: too few stations read or an offset past the limit, so the areas are the mask's own
    offset: float  # mm; median of CT minus mask diameter over the stations read, NaN with none
    blur: float  # mm; the edge width the correction took out, as a full width at half maximum, measured or assumed. The vessel's wall is part of that edge, so it is not exactly the scan's point spread
    read: int  # stations whose edge the CT gave
    stations: int


def sampler(values: np.ndarray, affine: np.ndarray, first: tuple[int, int, int] = (0, 0, 0)) -> Callable[[np.ndarray], np.ndarray]:
    """A function from RAS points (..., 3) to HU, trilinear in `values` and NaN outside them.

    `values` is the volume the affine describes, or the part of it that starts at voxel `first`.
    """
    affine = np.asarray(affine, dtype=float)
    if np.ndim(values) != 3 or affine.shape != (4, 4) or not np.isfinite(affine).all() or abs(np.linalg.det(affine[:3, :3])) < 1e-12:
        raise ValueError("A CT sampler needs a 3D volume and a finite, invertible 4x4 affine")
    inverse = np.linalg.inv(affine)

    def sample(points: np.ndarray) -> np.ndarray:
        points = np.asarray(points, dtype=float)
        index = points.reshape(-1, 3) @ inverse[:3, :3].T + inverse[:3, 3] - np.asarray(first)
        return map_coordinates(values, index.T, order=1, mode="constant", cval=np.nan, output=np.float64).reshape(points.shape[:-1])

    return sample


def areas(stations: list[Stations], parents: list[int], sample: Callable[[np.ndarray], np.ndarray], touching: list[np.ndarray] | None = None) -> Widths:
    """Each station's lumen area: the mask's, with its diameter moved by what the CT `sample` reads says the mask is off by.

    `stations` come from `vessel_geometry.measure` on the lumen mask. Per station the CT is read
    on rays in the plane across the vessel, and each ray's half-height radius found against the
    plane's own centre HU and the ray's own background (`_edges`), so no lumen HU is carried from
    anywhere. A station is read when blur leaves its centre HU nearly the lumen's, nearly all its
    rays were kept, none met calcium, and its mask area is its own vessel's: not `junction`,
    `spur` or `stub`, and not marked in `touching`, the stations `vessel_geometry.contact` gives
    per branch. A vessel traced as two branches is such lumen: each branch's mask holds part of
    it while the CT shows all of it, so reading them would give each the whole width. Its CT
    diameter is the half-height radius with the blur taken out (`_table`), doubled.

    The scan's offset is the median of CT minus mask diameter over the stations read. A station
    moves by the median over the read stations near it on its branch, pulled towards the scan's
    where they are few, and by the scan's alone with none near. A scan offset that would thin
    counts as none there: the wide vessels' offset is no evidence about a stretch too thin to
    read. Its own CT diameter is never its width: the mask's widths are smoother along a vessel
    than any per-station reading. `parents` holds each branch's parent, as `measure` takes them;
    an offset is found within a branch, so they are only checked.
    """
    if len(parents) != len(stations):
        raise ValueError("Each branch needs one parent")
    if touching is not None and [len(marks) for marks in touching] != [len(st.s) for st in stations]:
        raise ValueError("Each station needs one contact mark")
    starts = np.concatenate([[0], np.cumsum([len(st.s) for st in stations])]).astype(int)
    count = int(starts[-1])

    def split(values):
        return [values[a:b] for a, b in zip(starts[:-1], starts[1:])]

    if not count:
        return Widths([], [], [], False, float("nan"), _SIGMA * _FWHM, 0, 0)
    mask_area = np.concatenate([st.area for st in stations]).astype(float)
    drawn = np.isfinite(mask_area) & (mask_area > 0)
    mask = 2 * np.sqrt(np.where(drawn, mask_area, 0.0) / np.pi)
    half, kept, calcium, edge = _planes(np.concatenate([st.points for st in stations]), np.concatenate([_tangents(st) for st in stations]), mask / 2, sample)

    wide = np.nan_to_num(half) >= _WIDE
    sigma = _SIGMA
    if np.count_nonzero(wide) >= _FEW and np.isfinite(edge[wide]).any():
        sigma = float(np.clip(np.nanmedian(edge[wide]) / _QUARTILES, *_BLUR))
    radius, halves, core = _table(sigma)
    own = np.concatenate([~(st.junction | st.spur | (False if st.stub is None else st.stub)) for st in stations])
    if touching is not None:
        own &= ~np.concatenate(touching).astype(bool)
    with np.errstate(invalid="ignore"):
        lumen = np.interp(half / sigma, halves, radius, left=np.nan)  # in blurs; NaN stays NaN
        # Past the table's end the blur moves the edge by a constant.
        lumen = np.where(half / sigma > halves[-1], half / sigma + radius[-1] - halves[-1], lumen)
        sure = np.interp(lumen, radius, core)
        read = np.isfinite(lumen) & (sure >= _SURE) & (kept >= _CLEAN) & ~calcium & drawn & own
    ct = np.where(read, 2 * sigma * lumen, np.nan)
    offset = ct - mask
    scan = float(np.median(offset[read])) if read.any() else float("nan")
    calibrated = np.count_nonzero(read) >= ENOUGH and abs(scan) <= _LIMIT
    area = mask_area.copy()
    if calibrated:
        # Where few or no stations were read near, the scan's offset stands in, unless it thins: a mask fat on wide vessels alone would halve the thin ones.
        fallback = max(scan, 0.0)
        moved = np.full(count, fallback)
        for st, a, b in zip(stations, starts[:-1], starts[1:]):
            near = read[None, a:b] & (np.abs(st.s[:, None] - st.s[None, :]) <= _ALONG)
            found = near.any(1)
            if found.any():
                local = np.nanmedian(np.where(near[found], np.clip(offset[None, a:b], scan - _CLIP, scan + _CLIP), np.nan), axis=1)
                weight = np.minimum(1.0, near[found].sum(1) / _FULL)
                moved[a:b][found] = weight * local + (1 - weight) * fallback
        # A station the mask gives no area keeps none, so the solve still refuses it.
        area[drawn] = np.pi * np.maximum(mask + moved, _THINNEST * mask)[drawn] ** 2 / 4
    return Widths(split(area), split(ct), split(calcium), bool(calibrated), scan, sigma * _FWHM, int(np.count_nonzero(read)), count)


def _planes(points, tangent, radius, sample):
    """Per station: half-height radius in mm (NaN with too few rays kept), fraction of rays kept, whether any ray met calcium, and each kept ray's 25-75% edge width.

    The plane's centre moves to the centroid of the half-height edge it finds, since a centreline
    can sit off the axis and the centre HU is read there. With stations pushed 1 mm off the axis,
    73% of plain stations were read with no move, 91% after one and 94% after three, as many as
    unpushed, with the scan's offset within 0.01 mm.
    """
    theta = (np.arange(_RAYS) + 0.5) * 2 * np.pi / _RAYS
    rho = (np.arange(int(round((REACH - _SHIFT) / _STEP))) + 0.5) * _STEP
    helper = np.where(np.abs(tangent[:, :1]) < 0.9, [[1.0, 0.0, 0.0]], [[0.0, 1.0, 0.0]])
    u = np.cross(tangent, helper)
    u /= np.linalg.norm(u, axis=1, keepdims=True)
    v = np.cross(tangent, u)
    half, kept = np.empty(len(points)), np.empty(len(points))
    calcium, edge = np.zeros(len(points), dtype=bool), np.empty((len(points), _RAYS))
    # A long tree is worked on a part at a time, to keep the working arrays small.
    for start in range(0, len(points), _CHUNK):
        part = slice(start, min(start + _CHUNK, len(points)))
        ray = np.cos(theta)[None, :, None] * u[part, None, :] + np.sin(theta)[None, :, None] * v[part, None, :]  # (station, ray, 3)
        centre = points[part].copy()
        for turn in range(_PASSES + 1):
            values = sample(centre[:, None, None, :] + rho[None, None, :, None] * ray[:, :, None, :])
            good, edge_at, crossing, hot = _edges(values, rho, radius[part] + _BEYOND)
            with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
                warnings.simplefilter("ignore", RuntimeWarning)  # a plane with no ray kept has no mean
                mean = np.sqrt(np.nanmean(np.where(good, edge_at**2, np.nan), axis=1))
                if turn < _PASSES:
                    # A ray left out takes the others' radius, so that the ray opposite it does not pull the centre away.
                    r = np.where(good, edge_at, mean[:, None])
                    centroid = 2 / 3 * np.stack([(r**3 * np.cos(theta)).sum(1), (r**3 * np.sin(theta)).sum(1)], axis=1) / (r**2).sum(1)[:, None]
                    away = centre + np.nan_to_num(centroid[:, :1] * u[part] + centroid[:, 1:] * v[part]) - points[part]
                    centre = points[part] + away * np.minimum(1.0, _SHIFT / np.maximum(np.linalg.norm(away, axis=1, keepdims=True), 1e-9))
        kept[part], calcium[part] = good.mean(1), hot.any(1)
        half[part] = np.where(kept[part] >= _KEPT, mean, np.nan)
        edge[part] = np.where(good, crossing(0.25) - crossing(0.75), np.nan)
    return half, kept, calcium, edge


def _edges(values, rho, reach):
    """For planes of CT values (station, ray, sample): the rays kept, each ray's half-height radius, a function giving its radius at any fraction of its height, and the rays that met calcium.

    A plane's centre HU is the mean within the core. A ray's background is its lowest running mean
    out to `reach`, taken no lower than water. So a ray is judged against its own side's tissue
    where that is muscle or brighter, and against soft tissue where fat or lung lies beyond the
    wall: half height against those lies outside the lumen. A ray is left out when it leaves the
    volume, when its background is not well below the centre HU (a chamber, a vein or another
    branch fills it), or when it or a ray near it passes values far above the centre HU.
    """
    count = int(round(_RUN / _STEP))
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)  # a plane wholly outside the volume has no centre HU or median
        centre = np.nanmean(values[:, :, rho <= _CORE], axis=(1, 2))
        total = np.cumsum(np.nan_to_num(values), axis=2)
        run = (total[:, :, count:] - total[:, :, :-count]) / count
        middle = rho[1 : len(rho) - count + 1] + (count - 1) * _STEP / 2
        allowed = (middle[None, :] >= _NEAR) & (middle[None, :] <= reach[:, None])
        background = np.maximum(np.where(allowed[:, None, :], run, np.inf).min(2), _DARKEST)
        whole = np.isfinite(values).all(2) & np.isfinite(background)
        typical = np.nanmedian(np.where(whole, background, np.nan), axis=1)
        reached = (rho[None, :] <= reach[:, None])[:, None, :]
        hot = (np.where(reached & np.isfinite(values), values, -np.inf) > (centre + _CALCIUM)[:, None, None]).any(2)
        bloom = hot.copy()
        for turn in range(1, _BLOOM + 1):
            bloom |= np.roll(hot, turn, axis=1) | np.roll(hot, -turn, axis=1)
        valid = whole & ~bloom & (centre[:, None] - background >= np.maximum(_CONTRAST, _BRIGHT * (centre - typical))[:, None])
    row, column = np.indices(valid.shape)

    def crossing(fraction):
        """Radius where each ray first falls below this fraction of the way from its background to the centre HU, NaN if it never does."""
        with np.errstate(invalid="ignore", divide="ignore"):
            target = background + fraction * (centre[:, None] - background)
            below = values < target[:, :, None]
            at = below.argmax(2)
            before, after = values[row, column, np.maximum(at - 1, 0)], values[row, column, at]
            radius = rho[np.maximum(at - 1, 0)] + (before - target) / (before - after) * _STEP
        return np.where(below.any(2) & (at > 0), radius, np.nan)

    half = crossing(0.5)
    return valid & np.isfinite(half), half, crossing, hot


@cache
def _profiles():
    """Unit discs blurred by a unit Gaussian: their radii, distances from the centre, and each disc's value at each distance.

    The value is the chance that a unit Gaussian centred that far away falls inside the disc, a
    noncentral chi-square. It holds for any blur once lengths are in blurs, so it is worked out once.
    """
    radius = np.concatenate([np.linspace(0.05, 3, 60), np.linspace(3.1, 12, 60)])
    distance = np.linspace(0, 16, 1601)
    return radius, distance, chndtr(radius[:, None] ** 2, 2, distance[None, :] ** 2)


def _table(sigma):
    """For lumens of each radius under a blur of `sigma` mm, in blurs: the radius, the half-height radius `_edges` would read, and the centre HU as a fraction of the lumen's contrast.

    The half height is of the vessel's own core mean, not of the lumen HU, which nothing gives:
    blur lowers a thin vessel's centre, so its half-height edge lies outside the lumen's. The core
    is a fixed length, so the table differs by blur.
    """
    radius, distance, profile = _profiles()
    inside = distance <= _CORE / sigma
    core = (profile[:, inside] * distance[inside]).sum(1) / distance[inside].sum()  # weighted by area
    at = (profile < core[:, None] / 2).argmax(1)
    row = np.arange(len(radius))
    before, after = profile[row, at - 1], profile[row, at]
    return radius, distance[at - 1] + (before - core / 2) / (before - after) * (distance[1] - distance[0]), core


def _tangents(st: Stations) -> np.ndarray:
    """Unit direction of the branch at each station, over `_TANGENT` mm either side so one station's jitter does not tilt the plane."""
    count = len(st.points)
    step = max(1, int(round(_TANGENT / max(float(np.median(np.diff(st.s))), 1e-3)))) if count > 1 else 1
    tangent = st.points[np.minimum(np.arange(count) + step, count - 1)] - st.points[np.maximum(np.arange(count) - step, 0)]
    length = np.linalg.norm(tangent, axis=1, keepdims=True)
    return np.where(length > 1e-9, tangent / np.maximum(length, 1e-9), [[0.0, 0.0, 1.0]])
