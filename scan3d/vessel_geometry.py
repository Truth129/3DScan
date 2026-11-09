"""Coronary lumen geometry measured on the source voxel grid.

Positions are patient RAS millimetres through the full affine. Nothing is resampled, so oblique,
reflected and sheared grids are measured as they are, and the binary mask is the only input.
"""

from __future__ import annotations

import itertools
from collections import deque
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_array
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

# One of each opposite pair of the 26 neighbours, and the six face neighbours.
_HALF_NEIGHBOURS = np.array([offset for offset in itertools.product((-1, 0, 1), repeat=3) if offset > (0, 0, 0)])
_FACES = np.array([[1, 0, 0], [-1, 0, 0], [0, 1, 0], [0, -1, 0], [0, 0, 1], [0, 0, -1]])

# Centreline settings, checked by gate G1 on the synthetic phantoms.
_WALL_POWER = 2  # a traced path costs 1/(wall distance)^power per mm, so it keeps to the centre
_COVER = 1.5  # a traced path covers the lumen within this many vessel radii of it, plus a voxel
_SPUR = 3.0  # mm; a shorter end branch is a spur, as is one shorter than twice the radius it leaves
_SMOOTH = 2  # stations on each side of a moving average, applied twice when recentring
_PASSES = 2  # recentring passes
_GAP_REACH = 5.0  # mm from a branch end to a rootless piece that makes the end a gap
_GAP_VOLUME = 5.0  # mm³; smaller rootless pieces are noise, not the far side of a gap


@dataclass(frozen=True, eq=False)
class Lumen:
    """The voxels of one binary lumen mask, in the forms later steps need."""

    points: np.ndarray  # (n, 3) RAS voxel centres, mm
    voxel_volume: float  # mm³
    edge: float  # longest voxel edge, mm
    graph: csr_array  # 26-neighbour edges, each weighted by its RAS length; one direction only
    wall: cKDTree  # midpoints between lumen voxels and their face neighbours outside the lumen


@dataclass(frozen=True, eq=False)
class Branch:
    """One vessel segment between a root or junction and a junction or end."""

    points: np.ndarray  # (m, 3) RAS, about the station spacing apart, proximal first
    parent: int  # the branch whose last point this one starts from, or -1 at a root
    root: int  # index into Tree.roots
    gap: bool  # ends near a rootless piece of lumen, so the flow through it is unknown


@dataclass(frozen=True, eq=False)
class Tree:
    """Centreline trees grown from the ostia, and the lumen they could not reach."""

    roots: np.ndarray  # (k, 3) RAS ostium points
    branches: list[Branch]  # parents before children
    pieces: np.ndarray  # (p, 3) RAS centroids of lumen components with no root
    piece_volume: np.ndarray  # (p,) mm³


@dataclass(frozen=True, eq=False)
class Stations:
    """Measurements at points along one branch."""

    points: np.ndarray  # (m, 3) RAS, mm
    s: np.ndarray  # path length from the first station, mm
    count: np.ndarray  # lumen voxels assigned to each station
    area: np.ndarray  # mean lumen area over the window centred on each station, mm²
    inscribed: np.ndarray  # distance from each station to the nearest wall midpoint, mm
    spur: np.ndarray  # some of the station's voxels were too far from it to count, such as a pruned stub
    junction: np.ndarray  # within reach of a junction, where the branches' lumen mixes


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
    edge = float(np.linalg.norm(linear, axis=0).max())
    return Lumen(points, voxel_volume, edge, graph, cKDTree(np.concatenate(faces)))


def ostia(lumen: Lumen, aorta: np.ndarray, contact: float = 1.5) -> np.ndarray:
    """One root per patch where the lumen meets the aorta, as indices into the lumen points.

    `aorta` holds the RAS centres of the aorta mask's voxels. Lumen voxels within `contact` mm of
    it form patches, joined through the lumen. Each root is the voxel nearest the middle of its
    patch's layer closest to the aorta, so the tree starts at the ostium, not inside the patch.
    """
    distance, _ = cKDTree(np.asarray(aorta, dtype=float).reshape(-1, 3)).query(lumen.points, distance_upper_bound=contact)
    near = np.flatnonzero(np.isfinite(distance))
    if not len(near):
        return near
    count, patch = connected_components(lumen.graph[near][:, near], directed=False)
    roots = []
    for label in range(count):
        members = near[patch == label]
        layer = members[distance[members] <= distance[members].min() + lumen.edge]
        middle = lumen.points[layer].mean(0)
        roots.append(layer[np.linalg.norm(lumen.points[layer] - middle, axis=1).argmin()])
    return np.array(roots)


def centrelines(lumen: Lumen, roots: np.ndarray, spacing: float = 0.5) -> Tree:
    """Centreline trees grown from the roots by TEASAR, split at junctions and recentred.

    The farthest uncovered voxel is traced back to the tree on a metric that makes the wall
    expensive, the lumen around the new path is covered, and so on until nothing is left. End
    branches shorter than a spur are pruned. Each branch is then resampled every `spacing` mm and
    moved to the centroids of its stations' cells, so it follows the lumen centre between voxel
    centres. Lumen with no root is listed as pieces; a branch that ends near a sizeable piece is
    marked `gap`, since a break in the mask hides the flow and the narrowing that caused it.
    """
    roots = np.asarray(roots, dtype=int)
    if not len(roots):
        raise ValueError("A centreline needs at least one root")
    if spacing <= 0:
        raise ValueError("The station spacing must be positive")
    n = len(lumen.points)
    radius, _ = lumen.wall.query(lumen.points)
    edges = lumen.graph.tocoo()
    toll = edges.data * (radius[edges.row] ** -_WALL_POWER + radius[edges.col] ** -_WALL_POWER) / 2
    reach = dijkstra(lumen.graph, directed=False, indices=roots, min_only=True)
    _, previous, _ = dijkstra(csr_array((toll, (edges.row, edges.col)), shape=(n, n)), directed=False, indices=roots, min_only=True, return_predecessors=True)
    chains = _prune(lumen, roots, radius, _skeleton(lumen, roots, radius, reach, previous))

    stations = [_resample(lumen.points[chain], spacing) for chain, _, _ in chains]
    # A root whose whole component lies within its first cover grows no branch.
    for _ in range(_PASSES if chains else 0):
        owner = assign(lumen, stations)
        inside = owner >= 0
        total = sum(len(branch) for branch in stations)
        counts = np.bincount(owner[inside], minlength=total)
        centroid = np.concatenate(stations)
        filled = counts > 0
        for axis in range(3):
            sums = np.bincount(owner[inside], weights=lumen.points[inside, axis], minlength=total)
            centroid[filled, axis] = sums[filled] / counts[filled]
        moved, start = [], 0
        for chain, branch in zip(chains, stations):
            points = centroid[start : start + len(branch)]
            start += len(branch)
            # The end cell of a polyline is a half-space, so its centroid drifts sideways pass by
            # pass. Roots, junctions and ends stay on their TEASAR voxels instead.
            points[[0, -1]] = lumen.points[[chain[0][0], chain[0][-1]]]
            moved.append(_resample(_smooth(_smooth(points)), spacing))
        stations = moved

    _, component = connected_components(lumen.graph, directed=False)
    rootless = ~np.isin(component, component[roots])
    _, inverse, sizes = np.unique(component[rootless], return_inverse=True, return_counts=True)
    pieces = np.stack([np.bincount(inverse, weights=lumen.points[rootless, axis]) for axis in range(3)], 1) / sizes[:, None]
    volume = sizes * lumen.voxel_volume
    gap = np.zeros(len(chains), dtype=bool)
    tips = [i for i in range(len(chains)) if all(up != i for _, up, _ in chains)]
    sizeable = volume[inverse] >= _GAP_VOLUME
    if sizeable.any() and tips:
        distance, _ = cKDTree(lumen.points[rootless][sizeable]).query([stations[i][-1] for i in tips], distance_upper_bound=_GAP_REACH)
        gap[tips] = np.isfinite(distance)
    branches = [Branch(points, up, k, bool(gap[i])) for i, (points, (_, up, k)) in enumerate(zip(stations, chains))]
    return Tree(lumen.points[roots], branches, pieces.reshape(-1, 3), volume)


def contact(lumen: Lumen, tree: Tree, apart: float = 10.0) -> list[np.ndarray]:
    """Per branch, the stations whose lumen touches lumen more than `apart` mm away along the tree.

    Touching vessels share voxel faces, so their areas mix and a path can jump between them.
    Another tree counts as infinitely far away.
    """
    stations = [branch.points for branch in tree.branches]
    if not stations:
        return []
    sizes = np.array([len(points) for points in stations])
    owner = assign(lumen, stations)
    edges = lumen.graph.tocoo()
    a, b = owner[edges.row], owner[edges.col]
    keep = (a >= 0) & (b >= 0) & (a != b)
    a, b = a[keep], b[keep]

    lengths = np.array([_arc(points)[-1] for points in stations])
    start = np.zeros(len(stations))
    for i, branch in enumerate(tree.branches):
        if branch.parent >= 0:
            start[i] = start[branch.parent] + lengths[branch.parent]
    depth = np.repeat(start, sizes) + np.concatenate([_arc(points) for points in stations])
    branch_of = np.repeat(np.arange(len(stations)), sizes)
    first, second = branch_of[a], branch_of[b]

    def lineage(i: int) -> list[int]:
        out = []
        while i >= 0:
            out.append(i)
            i = tree.branches[i].parent
        return out

    pairs, inverse = np.unique(first * len(stations) + second, return_inverse=True)
    common = np.empty(len(pairs), dtype=int)
    for j, pair in enumerate(pairs):
        ancestors = set(lineage(int(pair) // len(stations)))
        common[j] = next((i for i in lineage(int(pair) % len(stations)) if i in ancestors), -1)
    meet = common[inverse]
    # Paths from the root part where the lineages part: at a station when one branch is an
    # ancestor of the other, otherwise at the end of the last shared branch.
    fork = np.where(meet == first, depth[a], np.where(meet == second, depth[b], (start + lengths)[meet]))
    far = (meet < 0) | (np.abs(depth[a] + depth[b] - 2 * fork) > apart)
    flags = np.zeros(sizes.sum(), dtype=bool)
    flags[a[far]] = True
    flags[b[far]] = True
    return np.split(flags, np.cumsum(sizes)[:-1])


def _skeleton(lumen: Lumen, roots: np.ndarray, radius: np.ndarray, reach: np.ndarray, previous: np.ndarray) -> np.ndarray:
    """TEASAR's tree as the parent of each lumen voxel: -1 at a root, -2 off the tree.

    A new path covers the lumen within _COVER vessel radii of it, plus a voxel, measured through
    the lumen so that the cover cannot jump to a nearby vessel. A path voxel's vessel radius is the
    largest wall distance among the path voxels whose own wall distance reaches it along the path.
    Towards the far end an end face is the nearest wall, so the path voxels within two radii of the
    tip cover a vessel diameter, plus a voxel, taking the radius as the largest wall distance among
    the path voxels within two of their own wall distances of the tip, plus half a voxel. Otherwise
    the rim across a blunt end, or across where a loop is cut, is traced as a false fork.
    """
    n = len(lumen.points)
    parent = np.full(n, -2)
    parent[roots] = -1
    pending = np.isfinite(reach)
    # The lumen graph both ways, plus a node n with an edge to every voxel. Only the new path's
    # edges are finite, each the most cover less the voxel's own, so one search within the most
    # cover finds the voxels within some path voxel's cover.
    both = (lumen.graph + lumen.graph.T).tocsr()
    indptr = np.append(both.indptr, both.indptr[-1] + n).astype(np.int32)
    indices = np.append(both.indices, np.arange(n)).astype(np.int32)
    graph = csr_array((np.append(both.data, np.full(n, np.inf)), indices, indptr), shape=(n + 1, n + 1))
    new, cover = roots, _COVER * radius[roots] + lumen.edge
    while True:
        graph.data[both.nnz + new] = cover.max() - cover
        pending[dijkstra(graph, indices=n, limit=cover.max())[:n] <= cover.max()] = False
        graph.data[both.nnz + new] = np.inf
        far = int(np.argmax(np.where(pending, reach, -1.0)))
        if not pending[far]:
            return parent
        path, voxel = [], far
        while parent[voxel] == -2:
            path.append(voxel)
            voxel = int(previous[voxel])
        new = np.array(path)
        parent[new] = np.append(new[1:], voxel)
        s, wall = _arc(lumen.points[new]), radius[new]
        cover = _COVER * np.where(np.abs(s[:, None] - s) <= wall, wall, 0.0).max(1) + lumen.edge
        end = wall[s <= 2 * wall].max()
        tip = s <= 2 * end
        cover[tip] = np.maximum(cover[tip], 2 * (end + lumen.edge))


def _chains(parent: np.ndarray, roots: np.ndarray) -> list[tuple[list[int], int, int]]:
    """(voxels, parent chain, root) from each root or junction to the next junction or end."""
    children: dict[int, list[int]] = {}
    for voxel in np.flatnonzero(parent >= 0):
        children.setdefault(int(parent[voxel]), []).append(int(voxel))
    chains = []
    queue = deque((int(root), -1, k) for k, root in enumerate(roots))
    while queue:
        start, up, k = queue.popleft()
        for child in children.get(start, []):
            chain = [start, child]
            while len(children.get(chain[-1], ())) == 1:
                chain.append(children[chain[-1]][0])
            chains.append((chain, up, k))
            if chain[-1] in children:
                queue.append((chain[-1], len(chains) - 1, k))
    return chains


def _prune(lumen: Lumen, roots: np.ndarray, radius: np.ndarray, parent: np.ndarray) -> list[tuple[list[int], int, int]]:
    """Chains once every spur is gone. Where all of a junction's branches are spurs, the longest stays."""
    while True:
        chains = _chains(parent, roots)
        junctions = {chain[0] for chain, _, _ in chains}
        starts: dict[int, list[tuple[float, list[int], bool]]] = {}
        for chain, _, _ in chains:
            length = _arc(lumen.points[chain])[-1]
            spur = chain[-1] not in junctions and length < max(_SPUR, 2 * radius[chain[0]])
            starts.setdefault(chain[0], []).append((length, chain, spur))
        pruned = False
        for leaving in starts.values():
            if len(leaving) < 2:
                continue
            spurs = sorted((item for item in leaving if item[2]), key=lambda item: item[0])
            if len(spurs) == len(leaving):
                spurs.pop()
            for _, chain, _ in spurs:
                parent[chain[1:]] = -2
                pruned = True
        if not pruned:
            return chains


def _arc(points: np.ndarray) -> np.ndarray:
    return np.concatenate([[0.0], np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))])


def _resample(points: np.ndarray, spacing: float) -> np.ndarray:
    """Points evenly spaced along the polyline, about `spacing` apart, keeping both ends."""
    s = _arc(points)
    t = np.linspace(0, s[-1], max(round(s[-1] / spacing), 1) + 1)
    return np.stack([np.interp(t, s, points[:, axis]) for axis in range(3)], 1)


def _smooth(points: np.ndarray) -> np.ndarray:
    """Moving average over up to _SMOOTH stations each side, narrowing so the ends stay put."""
    i = np.arange(len(points))
    k = np.minimum(np.minimum(i, len(points) - 1 - i), _SMOOTH)
    total = np.concatenate([np.zeros((1, 3)), np.cumsum(points, axis=0)])
    return (total[i + k + 1] - total[i - k]) / (2 * k + 1)[:, None]


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


def measure(lumen: Lumen, branches: list[np.ndarray], window: float = 1.0, parents: list[int] | None = None) -> tuple[list[Stations], int]:
    """Area and inscribed radius at each station, and the count of lumen voxels left unassigned.

    Each station's cell runs between the midpoints to its neighbours, so a cell's voxel volume
    over its length is its mean area. Spreading each cell's volume evenly along it gives the
    mean area over `window` mm centred on each station, clipped at the branch ends. Voxels beyond
    an end station's point count towards that station. A voxel farther from its station than
    the cover distance (as `centrelines` uses) is lumen the tree does not follow, such as a
    pruned stub, so it is left out and the station marked `spur`. With `parents` (each branch's
    parent, -1 at a root), stations within that distance of a junction are marked `junction`.
    At blunt ends the rule also drops real end-cap lumen, so end stations read low; the physics
    holds spur stations. Feed it `centrelines` output: at an eccentric narrowing off the true
    axis, the spur rule would remove real lumen.
    """
    if any(len(branch) < 2 for branch in branches):
        raise ValueError("Each branch needs at least two stations")
    if window <= 0:
        raise ValueError("The area window must be positive")
    if parents is not None and len(parents) != len(branches):
        raise ValueError("Each branch needs one parent")
    owner = assign(lumen, branches)
    points = np.concatenate(branches)
    inscribed, _ = lumen.wall.query(points)
    inside = np.flatnonzero(owner >= 0)
    far = np.linalg.norm(lumen.points[inside] - points[owner[inside]], axis=1) > _COVER * inscribed[owner[inside]] + lumen.edge
    counts = np.bincount(owner[inside[~far]], minlength=len(points))
    spur = np.bincount(owner[inside[far]], minlength=len(points)) > 0
    result, start = [], 0
    for branch in branches:
        stop = start + len(branch)
        s = _arc(branch)
        edges = np.concatenate([s[:1], (s[1:] + s[:-1]) / 2, s[-1:]])
        volume = np.concatenate([[0.0], np.cumsum(counts[start:stop] * lumen.voxel_volume)])
        low, high = np.clip(s - window / 2, s[0], s[-1]), np.clip(s + window / 2, s[0], s[-1])
        area = (np.interp(high, edges, volume) - np.interp(low, edges, volume)) / (high - low)
        junction = np.zeros(len(branch), dtype=bool)
        result.append(Stations(branch, s, counts[start:stop], area, inscribed[start:stop], spur[start:stop], junction))
        start = stop
    for child, parent in enumerate(parents or ()):
        if parent >= 0:
            reach = _COVER * result[parent].inscribed[-1] + lumen.edge
            result[parent].junction[result[parent].s >= result[parent].s[-1] - reach] = True
            result[child].junction[result[child].s <= reach] = True
    return result, int((owner < 0).sum())
