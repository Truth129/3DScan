"""Lumen area and centreline trees of known vessels, on CCTA-sized grids that are rotated,
reflected or sheared."""

import numpy as np
import pytest
from scipy.spatial import cKDTree
from synthetic_vessels import GRIDS, Phantom, Stenosis, Vessel

from scan3d.vessel_geometry import _splice, centrelines, contact, lumen, measure, ostia

TUBES = {
    "straight r1.0": Vessel(1.0, 30),
    "straight r1.5": Vessel(1.5, 30),
    "straight r2.0": Vessel(2.0, 30),
    "arc r1.0": Vessel(1.0, 30, bend=20),
    "arc r1.5": Vessel(1.5, 30, bend=20),
}


def stations(vessel, matrix):
    mask, affine = vessel.mask(matrix)
    s = np.arange(0, vessel.length + 1e-9, 0.5)
    (result,), unassigned = measure(lumen(mask, affine), [vessel.axis(s)], window=1.0)
    assert unassigned == 0
    return s, result


@pytest.mark.parametrize("grid", GRIDS)
@pytest.mark.parametrize("tube", TUBES)
def test_tube_area_matches_the_true_area(tube, grid):
    """Unbiased, and within 5% per station on average, or 10% at a 1 mm radius."""
    vessel = TUBES[tube]
    s, result = stations(vessel, GRIDS[grid])
    inner = (s > 1) & (s < vessel.length - 1)
    error = result.area[inner] / vessel.window_area(s[inner], 1.0) - 1
    assert abs(error.mean()) <= 0.02
    assert np.abs(error).mean() <= (0.10 if vessel.radius < 1.5 else 0.05)


@pytest.mark.parametrize("grid", GRIDS)
def test_throat_area_of_a_70_percent_stenosis(grid):
    vessel = Vessel(1.5, 40, stenoses=(Stenosis(20, 10, 0.7),))
    s, result = stations(vessel, GRIDS[grid])
    assert result.area[np.abs(s - 20) <= 5].min() == pytest.approx(np.pi * 0.45**2, rel=0.2)


@pytest.mark.parametrize("grid", GRIDS)
def test_lumen_the_stations_do_not_follow_is_left_out(grid):
    """Stations along only the first half of a tube: the far half is not the last station's area."""
    vessel = Vessel(1.5, 30)
    mask, affine = vessel.mask(GRIDS[grid])
    found = lumen(mask, affine)
    s = np.arange(0, 15 + 1e-9, 0.5)
    (result,), unassigned = measure(found, [vessel.axis(s)])
    assert unassigned == 0 and result.spur[-1] and not result.spur[(s > 1) & (s < 14.5)].any()
    assert 16 < result.count.sum() * found.voxel_volume / (np.pi * 1.5**2) < 18


# Murray's law: 1.5³ = 2 × 1.19³. The ball fills the notch the cylinders leave at the junction.
Y = Phantom(
    (Vessel(1.5, 15), Vessel(1.2, 15, start=(15, 0, 0), heading=35), Vessel(1.2, 15, start=(15, 0, 0), heading=-35)),
    joints=(((15, 0, 0), 1.5),),
)
ARC = Phantom((Vessel(1.0, 30, bend=20),))


def grow(phantom, matrix):
    mask, aorta, affine = phantom.masks(matrix)
    found = lumen(mask, affine)
    return found, centrelines(found, ostia(found, np.argwhere(aorta) @ affine[:3, :3].T + affine[:3, 3]))


@pytest.mark.parametrize("grid", [name for name in GRIDS if "rotated" in name or "sheared" in name])
@pytest.mark.parametrize("phantom", ["Y", "arc"])
def test_centreline_tree_follows_the_true_axes(phantom, grid):
    """Gate G1: exact topology and the ostium within 1.5 mm. More than 3 mm from ends and
    junctions: the axis within a voxel, length within 2%, and area within 2 points of the area
    along the true axis and within the G0 limits + 2 points of the true area."""
    phantom, matrix = {"Y": Y, "arc": ARC}[phantom], GRIDS[grid]
    found, tree = grow(phantom, matrix)
    assert [branch.parent for branch in tree.branches] == ([-1, 0, 0] if phantom is Y else [-1])
    assert len(tree.roots) == 1 and not len(tree.pieces) and not any(branch.gap for branch in tree.branches)
    assert np.linalg.norm(tree.roots[0] - phantom.vessels[0].axis(np.zeros(1))[0]) <= 1.5
    voxel = abs(np.linalg.det(matrix)) ** (1 / 3)
    junction = phantom.vessels[0].axis(np.array([phantom.vessels[0].length]))
    axes = [np.arange(0, vessel.length + 1e-9, 0.5) for vessel in phantom.vessels]
    on_axis, _ = measure(found, [vessel.axis(s) for vessel, s in zip(phantom.vessels, axes)])
    stations, _ = measure(found, [branch.points for branch in tree.branches])
    for branch, result in zip(tree.branches, stations):
        which = min(range(len(phantom.vessels)), key=lambda i: np.median(phantom.vessels[i].locate(branch.points)[1]))
        vessel = phantom.vessels[which]
        s, distance = vessel.locate(branch.points)
        away = (result.s > 3) & (result.s < result.s[-1] - 3) & (np.linalg.norm(branch.points - junction, axis=1) > 3)
        assert distance[away].mean() <= 0.5 * voxel and distance[away].max() <= voxel
        first, last = np.flatnonzero(away)[[0, -1]]
        assert result.s[last] - result.s[first] == pytest.approx(s[last] - s[first], rel=0.02)
        assert result.area[away].mean() == pytest.approx(np.interp(s[away], axes[which], on_axis[which].area).mean(), rel=0.02)
        error = result.area[away] / vessel.area(s[away]) - 1
        assert np.abs(error).mean() <= (0.12 if vessel.radius < 1.5 else 0.07)


@pytest.mark.parametrize("grid", GRIDS)
def test_a_blunt_end_grows_one_branch_and_a_short_side_branch_stays(grid):
    """TEASAR reaches both rims of a flat end; the two paths must not become a fork."""
    tube = Vessel(1.5, 40)
    assert len(grow(Phantom((tube,)), GRIDS[grid])[1].branches) == 1
    side = Vessel(0.8, 6, start=(20, 0, 0), heading=70)
    assert len(grow(Phantom((tube, side), joints=(((20, 0, 0), 0.8),)), GRIDS[grid])[1].branches) == 3


@pytest.mark.parametrize("grid", GRIDS)
def test_a_clipped_side_stub_is_pruned_and_a_long_side_branch_stays(grid):
    """A side branch the mask clips 2.5 mm past the wall, about one width out, is a stub left out
    of the tree, recorded from its junction; one 10.5 mm out is a branch."""
    tube = Vessel(1.5, 40)
    stub = Vessel(1.3, 4, start=(20, 0, 0), heading=60)
    tree = grow(Phantom((tube, stub), joints=(((20, 0, 0), 1.3),)), GRIDS[grid])[1]
    assert len(tree.branches) == 1 and len(tree.stubs) == 1
    assert np.linalg.norm(tree.stubs[0][0] - stub.axis(np.zeros(1))[0]) < 3
    side = Vessel(1.2, 12, start=(20, 0, 0), heading=60)
    tree = grow(Phantom((tube, side), joints=(((20, 0, 0), 1.2),)), GRIDS[grid])[1]
    assert len(tree.branches) == 3 and not tree.stubs


@pytest.mark.parametrize("grid", GRIDS)
def test_a_pruned_stub_does_not_pull_the_vessel_it_leaves(grid):
    """Recentring must not hand the stub's lumen to the trunk's cells: it pulled the trunk 0.8-2.7 mm
    into the mouth and added 0.2-1.5 mm to its length, and next to a narrowing made a false throat."""
    tube = Vessel(1.5, 40)
    alone = grow(Phantom((tube,)), GRIDS[grid])[1].branches[0].points
    tree = grow(Phantom((tube, Vessel(1.3, 4, start=(20, 0, 0), heading=60)), joints=(((20, 0, 0), 1.3),)), GRIDS[grid])[1]
    assert len(tree.branches) == 1 and len(tree.stubs) == 1
    length = [np.linalg.norm(np.diff(points, axis=0), axis=1).sum() for points in (tree.branches[0].points, alone)]
    assert length[0] == pytest.approx(length[1], abs=0.2)


def test_a_splice_moves_only_the_pulled_stations():
    """Stations a stub's lumen pulled off the vessel take the straight path, as many as before;
    the rest keep their place."""
    path = np.column_stack([np.linspace(0, 10, 21), np.zeros(21), np.zeros(21)])
    pulled = path.copy()
    pulled[8:13, 1] = [0.2, 0.6, 0.8, 0.6, 0.2]
    out = _splice(pulled, path, 0.1)
    assert np.array_equal(out[:8], pulled[:8]) and np.array_equal(out[13:], pulled[13:]) and np.allclose(out[8:13], path[8:13])


@pytest.mark.parametrize("grid", GRIDS)
def test_a_stub_pointing_at_a_far_piece_is_a_gap_not_a_stub(grid):
    """A mask break 7 mm past a short side end: the vessel goes on, so the end stays and is a gap."""
    tube = Vessel(1.5, 40)
    stub = Vessel(1.3, 4, start=(20, 0, 0), heading=60)
    far = Vessel(1.3, 8, start=(20 + 11 * np.cos(np.radians(60)), 11 * np.sin(np.radians(60)), 0), heading=60)
    tree = grow(Phantom((tube, stub, far), joints=(((20, 0, 0), 1.3),)), GRIDS[grid])[1]
    assert len(tree.branches) == 3 and not tree.stubs and len(tree.pieces) == 1
    ends = [branch for branch in tree.branches if branch.gap]
    assert len(ends) == 1 and np.linalg.norm(ends[0].points[-1] - stub.axis(np.array([4.0]))[0]) < 2


@pytest.mark.parametrize("grid", GRIDS)
def test_a_long_vessel_pointing_at_a_far_piece_is_a_gap(grid):
    """An end 7 mm short of a piece straight ahead is a gap on every grid. Headed over its last
    2 mm, where the traced path turns to the end face's rim, it was one on 2 of 8."""
    tree = grow(Phantom((Vessel(1.5, 30), Vessel(1.5, 10, start=(37, 0, 0)))), GRIDS[grid])[1]
    assert [branch.gap for branch in tree.branches] == [True] and len(tree.pieces) == 1


def test_a_gap_and_touching_vessels_are_flagged():
    """The branch ending at a break is a gap and the far piece has no root; two trees whose
    vessels touch are flagged there and nowhere else."""
    crossing = Vessel(1.5, 20), Vessel(1.5, 15, start=(0, 8, 2.7), heading=-45)  # axes 2.7 mm apart at x = 8
    broken = Vessel(1.5, 12, start=(0, -11, 0)), Vessel(1.5, 8, start=(13.5, -11, 0))
    phantom = Phantom(crossing + broken, aorta=((-3, -14, -3), (0, 11, 6)))
    found, tree = grow(phantom, GRIDS["0.35x0.35x0.5 sheared"])
    assert len(tree.roots) == 3
    assert len(tree.pieces) == 1 and np.linalg.norm(tree.pieces[0] - broken[1].axis(np.array([4.0]))[0]) < 0.5
    touch = (crossing[0].axis(np.array([8.0]))[0] + crossing[1].axis(np.array([8 * 2**0.5]))[0]) / 2
    flagged = contact(found, tree)
    for branch, flags in zip(tree.branches, flagged):
        assert branch.gap == (broken[0].locate(branch.points)[1].max() < 1.5)
        assert np.all(np.linalg.norm(branch.points[flags] - touch, axis=1) < 5)
    assert len({branch.root for branch, flags in zip(tree.branches, flagged) if flags.any()}) == 2


@pytest.mark.parametrize("grid", GRIDS)
def test_an_obliquely_cut_ostium_grows_one_branch(grid):
    """The aorta cuts a tube that leaves it at 50 degrees; the rim of that flat face is not a fork."""
    tube = Vessel(2.3, 40, start=(-2.5, 0, 0), heading=50)
    mask, aorta, affine = Phantom((tube,), aorta=((-6.0, -8.0, -8.0), (0.0, 8.0, 8.0))).masks(GRIDS[grid])
    found = lumen(mask & ~aorta, affine)
    wall = np.argwhere(aorta) @ affine[:3, :3].T + affine[:3, 3]
    near = np.isfinite(cKDTree(wall).query(found.points, distance_upper_bound=1.5)[0])
    tree = centrelines(found, ostia(found, wall), covered=near)
    assert [branch.parent for branch in tree.branches] == [-1]
