"""The steady hyperaemic pressure solve against closed forms, and analyse on hand-built and phantom trees."""

from dataclasses import replace

import numpy as np
import pytest
from synthetic_vessels import GRIDS, Phantom, Stenosis, Vessel

from scan3d.coronary import indeterminate
from scan3d.ffr_physics import MMHG, Network, Physiology, analyse, band, expansion, poiseuille, solve, stenosis
from scan3d.vessel_geometry import Branch, Stations, Tree, centrelines, lumen, measure, ostia

MU, RHO, KT = 0.0035, 1060.0, 1.52
INLET, VENOUS = 93.0 * MMHG, 5.0 * MMHG


def tube(length, radius, spacing=0.5):
    s = np.arange(0, length + 1e-9, spacing)
    return s, np.full(len(s), np.pi * radius**2)


def straight(*resistances):
    return tuple(np.zeros_like(r) for r in resistances)


def test_poiseuille_matches_a_uniform_tube():
    s, area = tube(20.0, 1.5)
    resistance = poiseuille(s, area, MU)
    exact = 8 * MU * 20e-3 / (np.pi * 1.5e-3**4)
    assert resistance.sum() == pytest.approx(exact, rel=1e-9)
    flow, pressure = solve(Network((-1,), (resistance,), straight(resistance), {0: 3e9}), INLET, VENOUS)
    assert flow[0] == pytest.approx((INLET - VENOUS) / (exact + 3e9), rel=1e-9)
    assert pressure[0][0] - pressure[0][-1] == pytest.approx(exact * flow[0], rel=1e-9)


def test_a_lesion_matches_the_closed_form():
    """A 3 mm tube narrowed to 1.2 mm over stations 1-3 (2 mm): Young–Tsai viscous on the stretch, Poiseuille either side."""
    healthy, throat = np.pi * 1.5**2, np.pi * 0.6**2
    s, area = np.arange(5.0), np.array([healthy, throat, throat, throat, healthy])
    # Kv = 32 (0.83 Ls + 1.64 Ds)/D0 (A0/As)² with Ls 2, Ds 1.2, D0 3 mm and A0/As = 2.5², over A0 D0.
    viscous = 32 * (0.83 * 2 + 1.64 * 1.2) / 3 * 2.5**4 * MU / (np.pi * 1.5e-3**2 * 3e-3)
    assert stenosis(healthy, throat, 2.0, MU) == pytest.approx(viscous, rel=1e-12)
    resistance = poiseuille(s, area, MU)
    resistance[1:3] = 0
    resistance[1] = viscous
    loss = np.zeros(4)
    loss[1] = expansion(healthy, throat, RHO, KT)
    flow, pressure = solve(Network((-1,), (resistance,), (loss,), {0: 3e9}), INLET, VENOUS)
    linear, drop = resistance.sum() + 3e9, INLET - VENOUS
    exact = 2 * drop / (linear + np.sqrt(linear**2 + 4 * loss[1] * drop))
    assert flow[0] == pytest.approx(exact, rel=1e-9)
    assert pressure[0][-1] == pytest.approx(VENOUS + 3e9 * exact, rel=1e-9)
    assert expansion(healthy, 1.1 * healthy, RHO, KT) == 0 and stenosis(healthy, 1.1 * healthy, 2.0, MU) == 0


def test_a_y_matches_the_hand_solution():
    """Unequal daughters and outlets: flow conserved, pressure continuous and never rising."""
    parts = [poiseuille(*tube(15.0, 1.5), MU), poiseuille(*tube(20.0, 1.2), MU), poiseuille(*tube(12.0, 1.0), MU)]
    outlets = {1: 4e9, 2: 7e9}
    flow, pressure = solve(Network((-1, 0, 0), tuple(parts), straight(*parts), outlets), INLET, VENOUS)
    r0, r1, r2 = (part.sum() for part in parts)
    one, two = r1 + outlets[1], r2 + outlets[2]
    det = (r0 + one) * (r0 + two) - r0**2
    drop = INLET - VENOUS
    assert flow[1:] == pytest.approx([drop * two / det, drop * one / det], rel=1e-9)
    assert flow[0] == pytest.approx(flow[1] + flow[2], rel=1e-12)
    for child in (1, 2):
        assert pressure[child][0] == pressure[0][-1]
        assert np.all(np.diff(np.concatenate([pressure[0], pressure[child]])) <= 0)


def hand_tree(side=6.0, onward=25.0, onward_radius=1.4, gap=False, small=False):
    """A tapering root narrowed 60% over 8 mm, an r 0.8 side branch and a continuation tapering
    from `onward_radius`; with `gap`, a lumen gap at the continuation's end; with `small`, a second
    5 mm tree."""
    s0 = np.arange(0, 20 + 1e-9, 0.5)
    u = (s0 - 10) / 8
    r0 = 1.6 - 0.15 * s0 / 20 - np.where(np.abs(u) <= 0.5, 1.55 * 0.6 * (1 + np.cos(2 * np.pi * u)) / 2, 0)
    s1, s2 = np.arange(0, side + 1e-9, 0.5), np.arange(0, onward + 1e-9, 0.5)
    shapes = [(s0, r0, -1, 0), (s1, np.full(len(s1), 0.8), 0, 0), (s2, onward_radius - 0.2 * s2 / 25, 0, 0)]
    if small:
        s3 = np.arange(0, 5 + 1e-9, 0.5)
        shapes.append((s3, np.full(len(s3), 1.0), -1, 1))
    branches, stations = [], []
    for k, (s, r, parent, root) in enumerate(shapes):
        points = np.column_stack([s, np.full(len(s), 10.0 * k), np.zeros(len(s))])
        junction = (s <= 2.3) if parent >= 0 else (s >= s[-1] - 2.3) & (k == 0)
        none = np.zeros(len(s), dtype=bool)
        branches.append(Branch(points, parent, root, gap and k == 2))
        stations.append(Stations(points, s, np.ones(len(s), dtype=int), np.pi * r**2, r - 0.2, none, junction))
    return Tree(np.zeros((2 if small else 1, 3)), branches, np.empty((0, 3)), np.empty(0)), stations


@pytest.mark.parametrize(("side", "onward", "onward_radius"), [(6, 25, 1.4), (3, 25, 1.4), (6, 5, 1.2)])
def test_one_loss_and_one_lesion_at_a_narrowing_and_short_branches_keep_their_own_size(side, onward, onward_radius):
    """Includes a side branch too short for any trusted station, and a short daughter smaller
    than its parent, whose outlets must not take the trunk's radius."""
    tree, stations = hand_tree(side, onward, onward_radius)
    result = analyse(tree, stations, Physiology(150, 120, 80, 60, share=1 / 3))
    assert [np.count_nonzero(loss) for loss in result.network.loss] == [1, 0, 0]
    (lesion,) = result.lesions
    assert lesion.branch == 0 and lesion.stenosis == pytest.approx(0.6, abs=0.05)
    assert lesion.mld == pytest.approx(2 * 0.595, rel=0.05) and lesion.length == pytest.approx(4.04, abs=0.2)
    assert result.outlet_radius[1] == pytest.approx(0.8, rel=0.1)
    assert result.outlet_radius[2] == pytest.approx(onward_radius - 0.2 * onward / 25, rel=0.1)
    assert np.all(np.isfinite(result.ffr[0])) and 0.5 < result.ffr[2][-1] < 1


def test_ffr_is_withheld_with_a_reason():
    tree, stations = hand_tree(gap=True, small=True)
    result = analyse(tree, stations, Physiology(150, share=1 / 3))
    assert result.reason == [None, None, "FFR not reported: lumen gap", "tree left out of the flow split"]
    assert np.isnan(result.ffr[2]).all() and np.isnan(result.ffr[3]).all() and np.isfinite(result.ffr[0]).all()
    assert any("lumen gap on branch 2" in flag for flag in result.flags)
    # The model does not use the heart rate, so leaving it out assumes nothing.
    assert [flag for flag in result.flags if "assumed" in flag] == ["blood pressure assumed"]
    lone = analyse(*hand_tree(), Physiology(150))
    assert lone.reason == ["one coronary tree only"] * 3 and np.isnan(lone.flow).all()


def test_a_plausible_tree_raises_no_plausibility_flag():
    result = analyse(*hand_tree(), Physiology(150, 120, 80, 60, share=1 / 3))
    assert not [flag for flag in result.flags if "for the vessel's size" in flag or "daughters imply" in flag]


def test_a_wide_branch_given_too_little_flow_reads_slow():
    """A 3.2 mm trunk and its continuation at 3% of the heart's flow run at 0.04-0.05 m/s, below a resting heart's; one line names both."""
    result = analyse(*hand_tree(), Physiology(150, 120, 80, 60, share=0.03))
    assert "flow is slow for the vessel's size on branches 0, 2 (0.04 to 0.05 m/s): the flow split or the reference diameter may be off" in result.flags
    assert len([flag for flag in result.flags if "slow" in flag]) == 1


def test_a_wide_branch_given_too_much_flow_reads_fast():
    """A lone 3 mm tube taking all of the heart's flow runs at 1.62 m/s."""
    s = np.arange(0, 30 + 1e-9, 0.5)
    points = np.column_stack([s, np.zeros(len(s)), np.zeros(len(s))])
    radius, none = np.full(len(s), 1.5), np.zeros(len(s), dtype=bool)
    tree = Tree(np.zeros((1, 3)), [Branch(points, -1, 0, False)], np.empty((0, 3)), np.empty(0))
    stations = [Stations(points, s, np.ones(len(s), dtype=int), np.pi * radius**2, radius - 0.2, none, s >= s[-1] - 2.3)]
    result = analyse(tree, stations, Physiology(150, 120, 80, 60, share=1.0))
    assert "flow is fast for the vessel's size on branch 0 (1.62 m/s): the flow split or the reference diameter may be off" in result.flags


def test_a_parent_thinner_than_its_daughters_is_flagged():
    """A 2.9 mm trunk feeding a 5.0 mm continuation and a 1.6 mm side branch, whose Murray size is 5.1 mm."""
    result = analyse(*hand_tree(onward_radius=2.5), Physiology(150, 120, 80, 60, share=1 / 3))
    assert "parent is thinner than its daughters imply on branch 0 (57% of the expected diameter): diameters there may read small" in result.flags


def test_an_isolated_spur_run_at_a_throat_is_not_held():
    """A throat's inscribed radius collapses, so its stations read spur; holding them erased a real lesion.
    A spur run touching the ostium zone is still held."""
    tree, stations = hand_tree()
    s = stations[0].s
    stations[0] = replace(stations[0], spur=(np.abs(s - 10) <= 0.5) | (s == 1.0) | (s == 1.5))
    result = analyse(tree, stations, Physiology(150, share=1 / 3))
    assert np.array_equal(result.held[0], (s < 2) | stations[0].junction)
    (lesion,) = result.lesions
    assert lesion.stenosis == pytest.approx(0.6, abs=0.05) and not lesion.held


def test_a_branch_with_no_trusted_station_takes_its_reference_area():
    """A 3 mm continuation is all junction and tip zone, whose mixed cells read narrow: no lesion there."""
    tree, stations = hand_tree(onward=3)
    stations[2] = replace(stations[2], area=np.full(len(stations[2].s), np.pi * 0.9**2))
    result = analyse(tree, stations, Physiology(150, share=1 / 3))
    assert result.held[2].all() and [lesion.branch for lesion in result.lesions] == [0]


def test_runs_under_a_lesion_length_apart_are_one_lesion():
    """The throat station reads 0.75 of the reference, splitting the narrowing's run below 0.7 in two."""
    tree, stations = hand_tree()
    area = stations[0].area.copy()
    area[stations[0].s == 10] = np.pi * 1.15**2
    stations[0] = replace(stations[0], area=area)
    (lesion,) = analyse(tree, stations, Physiology(150, share=1 / 3)).lesions
    assert lesion.length == pytest.approx(4.04, abs=0.2) and lesion.stenosis == pytest.approx(0.6, abs=0.05)


def test_the_band_holds_the_nominal_and_widens_at_a_lesion():
    """The 60% narrowing's band crosses 0.80; the same tree without it stays above, and narrower."""
    tree, stations = hand_tree(gap=True)
    healthy = [replace(stations[0], area=np.pi * (1.6 - 0.15 * stations[0].s / 20) ** 2), *stations[1:]]
    widths = []
    for sample in (stations, healthy):
        result = analyse(tree, sample, Physiology(150, 120, 80, 60, share=1 / 3))
        low, high = band(result, tree, sample, 0.4)
        for nominal, below, above in zip(result.ffr, low, high):
            assert np.array_equal(np.isnan(nominal), np.isnan(below)) and np.array_equal(np.isnan(nominal), np.isnan(above))
            shown = ~np.isnan(nominal)
            assert np.all(below[shown] <= nominal[shown]) and np.all(nominal[shown] <= above[shown])
        widths.append(high[1][-1] - low[1][-1])
        crosses = indeterminate(low[1][-1], high[1][-1])
        assert crosses if sample is stations else not crosses
    assert widths[0] > 2 * widths[1]
    assert indeterminate(0.80, 0.81) and not indeterminate(0.81, 0.9) and not indeterminate(0.7, 0.80)


def test_a_narrowed_daughter_reads_lower_ffr_end_to_end():
    narrowed = Vessel(1.2, 20, start=(15, 0, 0), heading=35, stenoses=(Stenosis(8, 6, 0.6),))
    phantom = Phantom((Vessel(1.5, 15), narrowed, Vessel(1.2, 20, start=(15, 0, 0), heading=-35)), joints=(((15, 0, 0), 1.5),))
    mask, aorta, affine = phantom.masks(GRIDS["0.35x0.35x0.5 sheared"])
    found = lumen(mask, affine)
    tree = centrelines(found, ostia(found, np.argwhere(aorta) @ affine[:3, :3].T + affine[:3, 3]))
    stations, _ = measure(found, [branch.points for branch in tree.branches], parents=[branch.parent for branch in tree.branches])
    result = analyse(tree, stations, Physiology(150, 120, 80, 60, share=1 / 3))
    a, b = sorted((1, 2), key=lambda k: narrowed.locate(tree.branches[k].points)[1].mean())
    assert [np.count_nonzero(loss) for loss in result.network.loss] == [int(k == a) for k in range(3)]
    (lesion,) = result.lesions
    assert lesion.branch == a and lesion.stenosis == pytest.approx(0.6, abs=0.1)
    assert result.ffr[a][-1] < result.ffr[b][-1] and result.ffr[b][-1] > 0.9


def test_a_healthy_wide_vessel_has_no_lesion():
    """The spur rule drops real end-cap lumen at a blunt tip, so those stations are held."""
    mask, aorta, affine = Phantom((Vessel(2.5, 30),)).masks(GRIDS["0.35x0.35x0.5 rotated"])
    found = lumen(mask, affine)
    tree = centrelines(found, ostia(found, np.argwhere(aorta) @ affine[:3, :3].T + affine[:3, 3]))
    stations, _ = measure(found, [branch.points for branch in tree.branches], parents=[branch.parent for branch in tree.branches])
    assert analyse(tree, stations, Physiology(150, share=1 / 3)).lesions == []
