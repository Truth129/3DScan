"""Coronary lumen geometry measured on the source voxel grid.

Positions are patient RAS millimetres through the full affine. Nothing is resampled, so oblique,
reflected and sheared grids are measured as they are, and the binary mask is the only input.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_array
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

# One of each opposite pair of the 26 neighbours, and the six face neighbours.
_HALF_NEIGHBOURS = np.array([offset for offset in itertools.product((-1, 0, 1), repeat=3) if offset > (0, 0, 0)])
_FACES = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]])


@dataclass(frozen=True, eq=False)
class Lumen:
    """The voxels of one binary lumen mask, in the forms later steps need."""

    points: np.ndarray  # (n, 3) RAS voxel centres, mm
    voxel_volume: float  # mm³
    graph: csr_array  # 26-neighbour edges, each weighted by its RAS length; one direction only
    wall: cKDTree  # midpoints between lumen voxels and their face neighbours outside the lumen


@dataclass(frozen=True, eq=False)
class Stations:
    """Measurements at points along one branch."""

    points: np.ndarray  # (m, 3) RAS, mm
    s: np.ndarray  # path length from the first station, mm
    count: np.ndarray  # lumen voxels assigned to each station
    area: np.ndarray  # mean lumen area over the window centred on each station, mm²
    inscribed: np.ndarray  # distance from each station to the nearest wall midpoint, mm


def lumen(mask: np.ndarray, affine: np.ndarray) -> Lumen:
    mask = np.asarray(mask, dtype=bool)
    affine = np.asarray(affine, dtype=float)
    if mask.ndim != 3 or affine.shape != (4, 4) or not np.isfinite(affine).all():
        raise ValueError("A lumen needs a 3D mask and a finite 4x4 affine")
    linear = affine[:3, :3]
    voxel_volume = abs(float(np.linalg.det(linear)))
    if voxel_volume == 0:
        raise ValueError("The lumen affine is singular")
    ijk = np.argwhere(mask)
    if not len(ijk):
        raise ValueError("The lumen mask is empty")
    # Keys in a crop padded by one voxel, so every neighbour of a lumen voxel has a key. argwhere
    # lists voxels in C order, so the keys are already sorted for searchsorted.
    low = ijk.min(0) - 1
    dims = ijk.max(0) - low + 2
    keys = np.ravel_multi_index((ijk - low).T, dims)
    stride = np.array([dims[1] * dims[2], dims[2], 1])

    def neighbour(offset: np.ndarray) -> np.ndarray:
        """Index of each voxel's neighbour at this offset, or -1 outside the lumen."""
        query = keys + offset @ stride
        found = np.minimum(np.searchsorted(keys, query), len(keys) - 1)
        return np.where(keys[found] == query, found, -1)

    points = ijk @ linear.T + affine[:3, 3]
    rows, cols, weights = [], [], []
    for offset in _HALF_NEIGHBOURS:
        other = neighbour(offset)
        hit = other >= 0
        rows.append(np.flatnonzero(hit))
        cols.append(other[hit])
        weights.append(np.full(int(hit.sum()), np.linalg.norm(linear @ offset)))
    n = len(points)
    graph = csr_array((np.concatenate(weights), (np.concatenate(rows), np.concatenate(cols))), shape=(n, n))
    faces = [points[neighbour(offset) < 0] + linear @ offset / 2 for offset in _FACES]
    return Lumen(points, voxel_volume, graph, cKDTree(np.concatenate(faces)))


def assign(lumen: Lumen, branches: list[np.ndarray], reach: int = 4) -> np.ndarray:
    """The station each lumen voxel belongs to, as an index into the concatenated branches.

    The path through the lumen finds the right part of the tree, so touching vessels and tight
    bends do not trade voxels. Among the stations within `reach` of that one on the same branch,
    the nearest in RAS then wins, which makes each cell a slab normal to a straight path rather
    than a shape of the voxel graph's metric. Voxels no station reaches get -1.
    """
    stations = np.concatenate(branches)
    sizes = np.array([len(branch) for branch in branches])
    starts = np.concatenate([[0], np.cumsum(sizes)[:-1]])
    branch_of = np.repeat(np.arange(len(branches)), sizes)
    _, seeds = cKDTree(lumen.points).query(stations)
    unique, first = np.unique(seeds, return_index=True)
    _, _, source = dijkstra(lumen.graph, directed=False, indices=unique, min_only=True, return_predecessors=True)
    station_of_seed = np.full(len(lumen.points), -1)
    station_of_seed[unique] = first
    result = np.full(len(lumen.points), -1)
    reached = np.flatnonzero(source >= 0)
    near = station_of_seed[source[reached]]
    branch = branch_of[near]
    window = np.clip(near[:, None] - starts[branch][:, None] + np.arange(-reach, reach + 1), 0, sizes[branch][:, None] - 1)
    candidates = starts[branch][:, None] + window
    distance = np.linalg.norm(lumen.points[reached][:, None, :] - stations[candidates], axis=2)
    result[reached] = candidates[np.arange(len(reached)), distance.argmin(1)]
    return result


def measure(lumen: Lumen, branches: list[np.ndarray], window: float = 1.0) -> tuple[list[Stations], int]:
    """Area and inscribed radius at each station, and the count of lumen voxels left unassigned.

    Each station's cell runs between the midpoints to its neighbours, so a cell's voxel volume
    over its length is its mean area. Spreading each cell's volume evenly along it gives the
    mean area over `window` mm centred on each station, clipped at the branch ends. Voxels beyond
    an end station's point count towards that station.
    """
    if any(len(branch) < 2 for branch in branches):
        raise ValueError("Each branch needs at least two stations")
    if window <= 0:
        raise ValueError("The area window must be positive")
    owner = assign(lumen, branches)
    counts = np.bincount(owner[owner >= 0], minlength=sum(len(branch) for branch in branches))
    inscribed, _ = lumen.wall.query(np.concatenate(branches))
    result, start = [], 0
    for branch in branches:
        stop = start + len(branch)
        s = np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(branch, axis=0), axis=1))])
        edges = np.concatenate([s[:1], (s[1:] + s[:-1]) / 2, s[-1:]])
        volume = np.concatenate([[0.0], np.cumsum(counts[start:stop] * lumen.voxel_volume)])
        low, high = np.clip(s - window / 2, s[0], s[-1]), np.clip(s + window / 2, s[0], s[-1])
        area = (np.interp(high, edges, volume) - np.interp(low, edges, volume)) / (high - low)
        result.append(Stations(branch, s, counts[start:stop], area, inscribed[start:stop]))
        start = stop
    return result, int((owner < 0).sum())
