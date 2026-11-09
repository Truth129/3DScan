"""Synthetic coronary-like vessels with known geometry, for the lumen measurement tests.

A vessel is a tube around an analytic axis whose cross-sections normal to the axis are exact
discs, so its true lumen area is known everywhere. A mask holds every voxel centre inside the
tube, on grids of CCTA voxel size that may be rotated, reflected or sheared. A phantom joins
vessels with balls into trees whose roots touch a box standing in for the aorta.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


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
