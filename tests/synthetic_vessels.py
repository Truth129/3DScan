"""Synthetic coronary-like vessels with known geometry, for the lumen measurement tests.

A vessel is a tube around an analytic axis whose cross-sections normal to the axis are exact
discs, so its true lumen area is known everywhere. A mask holds every voxel centre inside the
tube, on grids of CCTA voxel size that may be rotated, reflected or sheared. A phantom joins
vessels with balls into trees whose roots touch a box standing in for the aorta.

`Phantom.image` is the same phantom as a CT: contrast in the lumen at a level that can differ by
vessel and fall along it, a vessel wall, fat on one side of the vessels and muscle on the other,
and on request calcium or a bright neighbour beside one. It is blurred by the scanner's point
spread and sampled with noise on the grid. Its truth is still the analytic area, never anything
read back from the image.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.ndimage import binary_erosion, gaussian_filter, map_coordinates
from scipy.spatial import cKDTree
from scipy.special import ndtr


def _rotation(x: float, y: float, z: float) -> np.ndarray:
    """Rotation by the given degrees about x, then y, then z."""
    a, b, c = np.radians((x, y, z))
    rx = np.array([[1, 0, 0], [0, np.cos(a), -np.sin(a)], [0, np.sin(a), np.cos(a)]])
    ry = np.array([[np.cos(b), 0, np.sin(b)], [0, 1, 0], [-np.sin(b), 0, np.cos(b)]])
    rz = np.array([[np.cos(c), -np.sin(c), 0], [np.sin(c), np.cos(c), 0], [0, 0, 1]])
    return rz @ ry @ rx


_TURN = _rotation(20, 35, -15)
# The vessel's own frame in RAS: oblique to every grid, and off voxel centres, as real arteries are.
_POSE = _rotation(10, -25, 40)
_SHIFT = np.array([0.123, 0.271, 0.037])


def _grids() -> dict[str, np.ndarray]:
    grids = {}
    for label, spacing in {"0.35x0.35x0.5": (0.35, 0.35, 0.5), "0.5": (0.5, 0.5, 0.5)}.items():
        plain = np.diag(spacing)
        sheared = plain.copy()
        sheared[0, 2] = 0.3 * spacing[2]  # each slice steps sideways, as a tilted acquisition does
        grids[f"{label} plain"] = plain
        grids[f"{label} rotated"] = _TURN @ plain
        grids[f"{label} reflected"] = _TURN @ np.diag([-1.0, 1.0, 1.0]) @ plain
        grids[f"{label} sheared"] = _TURN @ sheared
    return grids


# Direction-times-spacing matrices: the upper-left 3x3 of a voxel-to-RAS affine.
GRIDS = _grids()
# 1 mm slices, kept out of GRIDS: a vessel lying in the slice plane can be traced as two branches here.
THICK = _TURN @ np.diag((0.35, 0.35, 1.0))


def _ras(local: np.ndarray) -> np.ndarray:
    return np.asarray(local, dtype=float) @ _POSE.T + _SHIFT


def _frame(points: np.ndarray) -> np.ndarray:
    return (np.asarray(points, dtype=float) - _SHIFT) @ _POSE


def _grid(points: np.ndarray, reach: float, matrix: np.ndarray) -> tuple[np.ndarray, tuple[int, ...], np.ndarray]:
    """Voxel centres of a grid with this matrix that covers the points and `reach` mm around them."""
    corners = np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)])
    low, high = points.min(0) - reach, points.max(0) + reach
    index = (low + corners * (high - low)) @ np.linalg.inv(matrix).T
    first, last = np.floor(index.min(0)).astype(int), np.ceil(index.max(0)).astype(int)
    shape = tuple(int(n) for n in last - first + 1)
    affine = np.eye(4)
    affine[:3, :3] = matrix
    affine[:3, 3] = matrix @ first
    return np.indices(shape).reshape(3, -1).T @ matrix.T + affine[:3, 3], shape, affine


@dataclass(frozen=True)
class Stenosis:
    """A cosine-shaped narrowing; with `eccentric` one wall stays straight."""

    centre: float  # mm along the axis
    length: float  # mm
    severity: float  # diameter stenosis, 0 to 1
    eccentric: bool = False


@dataclass(frozen=True)
class Vessel:
    """A straight tube, or with `bend` a circular arc of that radius, `length` mm long.

    It leaves `start` along the phantom frame's x turned by `heading` degrees about its z, and
    bends towards the turned y.
    """

    radius: float
    length: float
    bend: float | None = None
    stenoses: tuple[Stenosis, ...] = ()
    start: tuple[float, float, float] = (0.0, 0.0, 0.0)
    heading: float = 0.0

    def radius_at(self, s: np.ndarray) -> np.ndarray:
        r = np.full(np.shape(s), self.radius, dtype=float)
        for stenosis in self.stenoses:
            u = (np.asarray(s) - stenosis.centre) / stenosis.length
            inside = np.abs(u) <= 0.5
            r[inside] -= self.radius * stenosis.severity * (1 + np.cos(2 * np.pi * u[inside])) / 2
        return r

    def _shift(self, s: np.ndarray) -> np.ndarray:
        """How far the lumen centre moves off the axis, along the frame's z."""
        e = np.zeros(np.shape(s))
        for stenosis in self.stenoses:
            if stenosis.eccentric:
                u = (np.asarray(s) - stenosis.centre) / stenosis.length
                inside = np.abs(u) <= 0.5
                e[inside] += self.radius * stenosis.severity * (1 + np.cos(2 * np.pi * u[inside])) / 2
        return e

    def _foot(self, s: np.ndarray) -> np.ndarray:
        """Axis points in the vessel's frame."""
        s = np.asarray(s, dtype=float)
        if self.bend is None:
            return np.stack([s, np.zeros_like(s), np.zeros_like(s)], axis=-1)
        theta = s / self.bend
        return np.stack([self.bend * np.sin(theta), self.bend * (1 - np.cos(theta)), np.zeros_like(s)], axis=-1)

    def _project(self, local: np.ndarray) -> np.ndarray:
        """Arc length of the nearest axis point, valid within the bend radius of the axis."""
        if self.bend is None:
            return local[:, 0]
        return self.bend * np.arctan2(local[:, 0], self.bend - local[:, 1])

    def _local(self, points: np.ndarray) -> np.ndarray:
        return (_frame(points) - self.start) @ _rotation(0, 0, self.heading)

    def axis(self, s: np.ndarray) -> np.ndarray:
        """RAS points on the axis, which leaves the lumen centre only at eccentric narrowings."""
        return _ras(self._foot(s) @ _rotation(0, 0, self.heading).T + self.start)

    def locate(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Arc length of the nearest axis point, clipped to the vessel, and the distance to it."""
        local = self._local(points)
        s = np.clip(self._project(local), 0, self.length)
        return s, np.linalg.norm(local - self._foot(s), axis=1)

    def section(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Arc length of the nearest axis point, unclipped, and the offset from the lumen centre there:
        towards the bend's centre, and along the frame's z."""
        local = self._local(points)
        s = self._project(local)
        offset = local - self._foot(s)
        theta = 0.0 if self.bend is None else s / self.bend
        return s, offset[:, 1] * np.cos(theta) - offset[:, 0] * np.sin(theta), offset[:, 2] - self._shift(s)

    def area(self, s: np.ndarray) -> np.ndarray:
        """True area of the cross-section normal to the axis."""
        return np.pi * self.radius_at(s) ** 2

    def window_area(self, s: np.ndarray, window: float) -> np.ndarray:
        """True mean area over `window` mm of axis centred on each s, clipped to the vessel."""
        lo = np.clip(np.asarray(s) - window / 2, 0, self.length)
        hi = np.clip(np.asarray(s) + window / 2, 0, self.length)
        u = np.linspace(0, 1, 401)
        samples = self.area(lo[:, None] + (hi - lo)[:, None] * u)
        return np.trapezoid(samples, u, axis=1)

    def contains(self, points: np.ndarray) -> np.ndarray:
        local = self._local(points)
        s = self._project(local)
        offset = local - self._foot(s)
        offset[:, 2] -= self._shift(s)
        return (s >= 0) & (s <= self.length) & (np.linalg.norm(offset, axis=1) <= self.radius_at(s))

    def mask(self, matrix: np.ndarray, margin: float = 1.5) -> tuple[np.ndarray, np.ndarray]:
        """A mask of the voxel centres inside the vessel, on a grid of this matrix, and its affine."""
        centres, shape, affine = _grid(self.axis(np.linspace(0, self.length, 400)), self.radius + margin, matrix)
        return self.contains(centres).reshape(shape), affine


_FWHM = 2 * np.sqrt(2 * np.log(2))  # a Gaussian's full width at half maximum, in standard deviations
_PIECE = 4.0  # mm of vessel drawn at a time, so only the fine cells around it are worked out


@dataclass(frozen=True)
class Plaque:
    """Calcium on the outside of a vessel's lumen: an arc of the wall, `thickness` mm deep."""

    vessel: int
    centre: float  # mm along the axis
    length: float = 3.0  # mm
    thickness: float = 0.8  # mm
    arc: float = 120.0  # degrees of the wall covered
    angle: float = 0.0  # degrees around the axis, from the bend's side towards the frame's z
    hu: float = 1000.0


@dataclass(frozen=True)
class Neighbour:
    """Something bright along a vessel, as a vein or a chamber lies beside an artery: a tube parallel to it."""

    vessel: int
    start: float  # mm along the vessel's axis
    end: float
    gap: float = 0.7  # mm between the vessel's lumen and the tube
    radius: float = 2.0  # mm
    angle: float = 0.0  # degrees around the axis, as a plaque's
    hu: float = 250.0


@dataclass(frozen=True)
class Scan:
    """How a phantom is imaged. The point spread is a Gaussian, given as full widths at half maximum."""

    in_plane: float = 1.0  # mm; scans measure 0.85 to 1.3
    through_plane: float = 1.0  # mm, along the slice normal
    noise: float = 25.0  # HU, standard deviation, independent in each voxel
    lumen: float = 400.0  # HU at the roots, and in the aorta
    # Per vessel, its lumen HU at its start and at its end as fractions of `lumen`, falling evenly
    # between; (1, 1) where none is given. In scans it differs by branch and can halve along one.
    levels: tuple[tuple[float, float], ...] = ()
    fat: float = -90.0  # HU where the phantom frame's z is above `boundary`
    muscle: float = 100.0  # HU below it
    boundary: float = 0.0  # mm; the axes lie at z = 0, so 0 puts fat on one side of each vessel and muscle on the other
    wall: float = 0.5  # mm of vessel wall around each vessel's own radius; it fills a narrowing too, as soft plaque does
    wall_hu: float = 45.0
    plaques: tuple[Plaque, ...] = ()
    neighbours: tuple[Neighbour, ...] = ()
    step: float = 0.1  # mm; the phantom is drawn this finely before the blur
    tight: float = 0.2  # mm the thin mask's surface lies inside the half-height one
    generous: float = 0.15  # mm the fat mask's lies outside it
    # The varied mask: (true diameter, mm its surface lies inside the half-height one) at two sizes, and evenly between.
    varied: tuple[tuple[float, float], tuple[float, float]] = ((1.5, 0.0), (3.5, 0.2))
    seed: int = 0


@dataclass(frozen=True, eq=False)
class Imaged:
    """A phantom as a CT, with the masks a model might draw on it."""

    ct: np.ndarray  # HU, int16
    affine: np.ndarray
    lumen: np.ndarray  # voxels where the blurred lumen is at half height or more
    thin: np.ndarray  # that mask with its surface `Scan.tight` mm further in, as a tight model draws it
    fat: np.ndarray  # and `Scan.generous` mm further out, as a generous one does
    varied: np.ndarray  # and moved by an amount that depends on the vessel's size (`Scan.varied`)
    aorta: np.ndarray


@dataclass(frozen=True)
class Phantom:
    """Vessels and balls joining them, with their roots on an aorta box (phantom frame, mm)."""

    vessels: tuple[Vessel, ...]
    joints: tuple[tuple[tuple[float, float, float], float], ...] = ()  # (centre, radius)
    aorta: tuple[tuple[float, float, float], tuple[float, float, float]] = ((-3.0, -4.0, -4.0), (0.0, 4.0, 4.0))

    def masks(self, matrix: np.ndarray, margin: float = 1.5) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Lumen and aorta masks on one grid of this matrix, and its affine."""
        low, high = np.array(self.aorta)
        box = low + np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)]) * (high - low)
        points = [vessel.axis(np.linspace(0, vessel.length, 400)) for vessel in self.vessels] + [_ras(box)]
        reach = max(vessel.radius for vessel in self.vessels) + margin
        centres, shape, affine = _grid(np.concatenate(points), reach, matrix)
        lumen = np.zeros(len(centres), dtype=bool)
        for vessel in self.vessels:
            lumen |= vessel.contains(centres)
        for centre, radius in self.joints:
            lumen |= np.linalg.norm(centres - _ras(centre), axis=1) <= radius
        local = _frame(centres)
        aorta = np.all((local >= low) & (local <= high), axis=1)
        return lumen.reshape(shape), aorta.reshape(shape), affine

    def nearest(self, points: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Per point: the vessel whose wall is nearest, the arc length there and the distance to that axis."""
        located = [vessel.locate(points) for vessel in self.vessels]
        index = np.argmin([d - vessel.radius_at(s) for vessel, (s, d) in zip(self.vessels, located)], axis=0)
        pick = np.arange(len(index))
        return index, np.array([s for s, _ in located])[index, pick], np.array([d for _, d in located])[index, pick]

    def image(self, matrix: np.ndarray, scan: Scan, reach: float = 7.0) -> Imaged:
        """The phantom as a CT on a grid of this matrix, with `reach` mm of tissue around the vessels.

        Everything is drawn `scan.step` mm fine, the lumen, wall and aorta with an edge one step
        soft, which puts a disc's area right to π·step²/12. The fine grid's third axis is the
        slice normal, so the point spread is a separable Gaussian there. Blur is linear, so only
        what the objects add to the tissue is blurred on the fine grid, and the tissue's own
        blurred edge is an error function. Both are read at the voxel centres through the affine,
        then noise is added. Lumen comes before the aorta, then plaque, neighbours and wall.
        """
        h = scan.step
        axes = [vessel.axis(np.linspace(0, vessel.length, 400)) for vessel in self.vessels]
        low, high = np.array(self.aorta)
        box = _ras(low + np.array([[x, y, z] for x in (0, 1) for y in (0, 1) for z in (0, 1)]) * (high - low))
        widest = max([vessel.radius for vessel in self.vessels] + [radius for _, radius in self.joints])
        beside = max([plaque.thickness for plaque in scan.plaques] + [tube.gap + 2 * tube.radius for tube in scan.neighbours] + [scan.wall])
        centres, shape, affine = _grid(np.concatenate(axes + [box]), widest + reach, matrix)

        first = matrix[:, 0] / np.linalg.norm(matrix[:, 0])
        normal = np.cross(matrix[:, 0], matrix[:, 1])
        normal /= np.linalg.norm(normal)
        frame = np.column_stack([first, np.cross(normal, first), normal])  # x @ frame: in-plane, in-plane, through-plane
        sigma = np.array([scan.in_plane, scan.in_plane, scan.through_plane]) / _FWHM
        pad = 4 * sigma.max() + widest + beside + 2 * h
        drawn = np.concatenate(axes + [box]) @ frame
        origin = drawn.min(0) - pad
        size = np.ceil((drawn.max(0) + pad - origin) / h).astype(int) + 1
        cover = np.zeros(size, dtype=np.float32)  # how much of each fine cell is lumen
        value = np.zeros(size, dtype=np.float32)  # and that lumen's HU
        other = np.zeros(size, dtype=np.float32)  # how much of the rest of the cell is wall, neighbour or plaque
        extra = np.zeros(size, dtype=np.float32)  # and its HU

        def cells(points, radius):
            """The fine grid's slices within `radius` of these RAS points, and the RAS centres of those cells."""
            q = points @ frame
            a = np.maximum(np.floor((q.min(0) - radius - origin) / h).astype(int) - 1, 0)
            b = np.minimum(np.ceil((q.max(0) + radius - origin) / h).astype(int) + 2, size)
            index = np.stack(np.meshgrid(*[np.arange(x, y) for x, y in zip(a, b)], indexing="ij"), axis=-1).reshape(-1, 3)
            return tuple(slice(x, y) for x, y in zip(a, b)), (origin + h * index) @ frame.T

        def soft(outside):
            """Cell fraction inside a surface this far away, mm, positive outside it."""
            return np.clip(0.5 - outside / h, 0.0, 1.0)

        def tissue(points):
            return np.where(_frame(points)[:, 2] > scan.boundary, scan.fat, scan.muscle)

        def paint(share, hu, where, inside, values, over=False):
            """Give the cells more inside this object than any before it its HU; with `over`, every cell it touches."""
            inside = np.asarray(inside, dtype=np.float32).reshape(share[where].shape)
            more = (inside > 0) if over else (inside > share[where])
            share[where][more] = np.maximum(inside, share[where])[more]
            hu[where][more] = np.asarray(values, dtype=np.float32).reshape(inside.shape)[more]

        for k, vessel in enumerate(self.vessels):
            proximal, distal = scan.levels[k] if k < len(scan.levels) else (1.0, 1.0)
            for start in np.arange(0, vessel.length, _PIECE):
                where, points = cells(vessel.axis(np.linspace(start, min(start + _PIECE, vessel.length), 20)), vessel.radius + scan.wall + h)
                s, across, up = vessel.section(points)
                ends = soft(-s) * soft(s - vessel.length)
                contrast = scan.lumen * (proximal + (distal - proximal) * np.clip(s / vessel.length, 0, 1))
                paint(cover, value, where, soft(np.hypot(across, up) - vessel.radius_at(s)) * ends, contrast)
                if scan.wall:
                    # Around the axis, which the lumen's centre leaves at an eccentric narrowing.
                    paint(other, extra, where, soft(np.hypot(across, up + vessel._shift(s)) - vessel.radius - scan.wall) * ends, np.full(len(s), scan.wall_hu))
        for centre, radius in self.joints:
            where, points = cells(_ras(centre)[None], radius + scan.wall + h)
            index, s, _ = self.nearest(_ras(centre)[None])
            proximal, distal = scan.levels[index[0]] if index[0] < len(scan.levels) else (1.0, 1.0)
            hu = scan.lumen * (proximal + (distal - proximal) * s[0] / self.vessels[index[0]].length)
            out = np.linalg.norm(points - _ras(centre), axis=1) - radius
            paint(cover, value, where, soft(out), np.full(len(points), hu))
            if scan.wall:
                paint(other, extra, where, soft(out - scan.wall), np.full(len(points), scan.wall_hu))
        for tube in scan.neighbours:
            vessel = self.vessels[tube.vessel]
            where, points = cells(vessel.axis(np.linspace(tube.start, tube.end, 40)), vessel.radius + tube.gap + 2 * tube.radius + h)
            s, across, up = vessel.section(points)
            away = vessel.radius + tube.gap + tube.radius
            turn = np.radians(tube.angle)
            out = np.hypot(across - away * np.cos(turn), up + vessel._shift(s) - away * np.sin(turn)) - tube.radius
            paint(other, extra, where, soft(out) * soft(tube.start - s) * soft(s - tube.end), np.full(len(s), tube.hu), over=True)
        for plaque in scan.plaques:
            vessel = self.vessels[plaque.vessel]
            along = np.linspace(plaque.centre - plaque.length / 2, plaque.centre + plaque.length / 2, 20)
            where, points = cells(vessel.axis(along), vessel.radius + plaque.thickness + h)
            s, across, up = vessel.section(points)
            out = np.hypot(across, up) - vessel.radius_at(s)
            turn = (np.degrees(np.arctan2(up, across)) - plaque.angle + 180) % 360 - 180
            inside = (np.abs(s - plaque.centre) <= plaque.length / 2) & (out >= 0) & (out <= plaque.thickness) & (np.abs(turn) <= plaque.arc / 2)
            paint(other, extra, where, inside, np.full(len(s), plaque.hu), over=True)
        box_cells, points = cells(box, h)
        local = _frame(points)
        inside = soft(np.maximum(low - local, local - high).max(1)).reshape(cover[box_cells].shape)
        other[box_cells] *= 1 - inside  # the aorta comes before what lies beside the vessels

        # What the objects add to the tissue they displace, written over the lumen's HU a slab at a time to keep the working arrays small.
        added = value
        for a in range(0, size[0], 32):
            where = (slice(a, min(a + 32, size[0])), slice(None), slice(None))
            index = np.stack(np.meshgrid(np.arange(where[0].start, where[0].stop), np.arange(size[1]), np.arange(size[2]), indexing="ij"), axis=-1)
            under = tissue((origin + h * index.reshape(-1, 3)) @ frame.T).reshape(cover[where].shape)
            added[where] = cover[where] * (added[where] - under) + (1 - cover[where]) * other[where] * (extra[where] - under)
        added[box_cells] += (1 - cover[box_cells]) * inside * (scan.lumen - tissue(points)).reshape(inside.shape)
        del other, extra

        added = gaussian_filter(added, sigma / h, mode="constant")
        level = gaussian_filter(cover, sigma / h, mode="constant")  # the blurred lumen, 1 deep inside a wide vessel
        at = ((centres @ frame - origin) / h).T
        ct = map_coordinates(added, at, order=1, mode="constant", cval=0.0)
        across = np.sqrt((frame.T @ _POSE[:, 2]) ** 2 @ sigma**2)  # the point spread's standard deviation across the tissue boundary
        ct += scan.muscle + (scan.fat - scan.muscle) * ndtr((_frame(centres)[:, 2] - scan.boundary) / across)
        ct += np.random.default_rng(scan.seed).normal(0.0, scan.noise, len(ct))

        # The masks: half height, and that surface moved in and out. Only voxels near it need their distance to it.
        height = map_coordinates(level, at, order=1, mode="constant", cval=0.0)
        solid = level >= 0.5
        surface = np.argwhere(solid & ~binary_erosion(solid))
        lumen = height >= 0.5
        gap = np.full(len(height), np.inf)
        near = (height > 0.02) & (height < 0.98)
        if len(surface) and near.any():
            gap[near] = cKDTree((origin + h * surface) @ frame.T).query(centres[near])[0]
        # How far inside the half-height surface each voxel lies, negative outside it. The surface cells lie half a step inside it.
        inward = np.where(lumen, gap + h / 2, h / 2 - gap)
        varied = lumen.copy()
        if near.any():
            index, s, _ = self.nearest(centres[near])
            true = np.empty(len(index))
            for k, vessel in enumerate(self.vessels):
                true[index == k] = 2 * vessel.radius_at(s[index == k])
            (small, tight), (large, loose) = scan.varied
            varied[near] = inward[near] >= np.interp(true, [small, large], [tight, loose])
        local = _frame(centres)
        aorta = np.all((local >= low) & (local <= high), axis=1)
        volumes = [mask.reshape(shape) for mask in (lumen, inward >= scan.tight, inward >= -scan.generous, varied, aorta)]
        return Imaged(np.rint(ct).astype(np.int16).reshape(shape), affine, *volumes)
