"""The lumen mask's widths corrected from the CT values, on synthetic vessels of known width imaged
with blur, a wall and noise on CCTA-sized grids, under the masks a tight and a generous model draw."""

from dataclasses import replace

import numpy as np
import pytest
from synthetic_vessels import GRIDS, THICK, Neighbour, Phantom, Plaque, Scan, Vessel

from scan3d.ffr_physics import Physiology, analyse
from scan3d.lumen_ct import areas, sampler
from scan3d.vessel_geometry import centrelines, contact, lumen, measure, ostia

SHEARED = GRIDS["0.35x0.35x0.5 sheared"]


def trace(image, mask):
    """The app's stations on this mask, those the physics holds, and those whose lumen touches lumen far along the tree."""
    found = lumen(mask, image.affine)
    tree = centrelines(found, ostia(found, np.argwhere(image.aorta) @ image.affine[:3, :3].T + image.affine[:3, 3]))
    parents = [branch.parent for branch in tree.branches]
    stations, _ = measure(found, [branch.points for branch in tree.branches], parents=parents, stubs=tree.stubs)
    touching = contact(found, tree)
    return stations, parents, analyse(tree, stations, Physiology(150, share=1 / 3), touching).held, touching


def diameter(area):
    return 2 * np.sqrt(area / np.pi)


@pytest.mark.parametrize("grid", ["0.35x0.35x0.5 sheared", "0.5 reflected"])
def test_a_thin_and_a_fat_mask_of_one_vessel_come_to_the_same_true_width(grid):
    """The masks of one 3.4 mm vessel differ by 0.7 mm. The CT moves each by its own offset onto the vessel's half-height edge."""
    image = Phantom((Vessel(1.7, 25),)).image(GRIDS[grid], Scan(in_plane=1.1, through_plane=0.9, seed=1))
    drawn, used = [], []
    for mask in (image.thin, image.fat):
        (st,), parents, (held,), touching = trace(image, mask)
        widths = areas([st], parents, sampler(image.ct, image.affine), touching)
        assert widths.calibrated and widths.read >= 40 and widths.stations == len(st.s) and not widths.calcium[0].any()
        assert 0.9 < widths.blur < 1.5
        read = np.isfinite(widths.ct_diameter[0])
        assert np.count_nonzero(read) == widths.read
        assert np.median(widths.ct_diameter[0][read] - diameter(st.area[read])) == pytest.approx(widths.offset)
        # A station's own reading is never its width: the widths keep the mask's shape, which is smoother than the readings.
        assert np.ptp(diameter(widths.area[0]) - diameter(st.area)) < 0.1 < np.ptp(widths.ct_diameter[0][read] - diameter(st.area[read]))
        drawn.append(np.median(diameter(st.area[~held])))
        used.append(diameter(widths.area[0][~held]))
    assert drawn[1] - drawn[0] > 0.5
    # With fat on one side and muscle on the other, as here, the widths come out a little under 0.1 mm thin: the wall is darker than the muscle.
    for values in used:
        assert np.median(values) == pytest.approx(3.4, abs=0.15) and np.abs(values - 3.4).max() < 0.3
    assert np.median(used[0]) == pytest.approx(np.median(used[1]), abs=0.05)


@pytest.mark.parametrize(
    "scan, low, high",
    [
        (Scan(fat=-90, muscle=-90, levels=((1.0, 0.6),), seed=7), -0.08, 0.08),
        (Scan(fat=-800, muscle=-90, boundary=1.7 + 1.5, levels=((1.0, 0.6),), seed=7), -0.08, 0.08),
        (Scan(fat=-90, muscle=-90, wall=0.0, levels=((1.0, 0.6),), seed=7), -0.4, -0.2),
        (Scan(fat=100, muscle=100, levels=((1.0, 0.6),), seed=7), -0.3, -0.05),
    ],
    ids=["in fat", "in fat, lung 1.5 mm away", "in fat, no wall", "in muscle"],
)
def test_fat_or_lung_beyond_the_wall_does_not_widen_a_vessel(scan, low, high):
    """The half height is taken against soft tissue, so a 3.4 mm vessel with a wall reads its width in fat, and lung
    beside it adds nothing. The limits, pinned so that a change to the edge rule shows: with no wall it reads thin in
    fat, and thin in muscle where the wall is darker than the muscle."""
    image = Phantom((Vessel(1.7, 25),)).image(SHEARED, scan)
    used = []
    for mask in (image.thin, image.fat):
        (st,), parents, (held,), touching = trace(image, mask)
        widths = areas([st], parents, sampler(image.ct, image.affine), touching)
        assert widths.calibrated
        used.append(np.median(diameter(widths.area[0][~held])))
    assert low < used[0] - 3.4 < high and used[0] == pytest.approx(used[1], abs=0.05)


def test_without_a_wall_the_blur_is_measured_and_taken_out():
    """A wall hides a missing blur correction, which reads this vessel 0.17 mm thin at a point spread of 1.3 mm.
    In soft tissue, so that the edge rule's floor plays no part."""
    image = Phantom((Vessel(1.7, 25),)).image(SHEARED, Scan(in_plane=1.3, through_plane=1.3, wall=0.0, fat=40.0, muscle=40.0, seed=9))
    (st,), parents, (held,), touching = trace(image, image.thin)
    widths = areas([st], parents, sampler(image.ct, image.affine), touching)
    assert widths.calibrated and 1.2 < widths.blur < 1.5
    assert np.median(diameter(widths.area[0][~held])) == pytest.approx(3.4, abs=0.1)


def test_a_falling_lumen_hu_and_a_bright_neighbour_leave_every_width_sound():
    """A daughter's lumen HU falls to 55% of the trunk's while a 300 HU vein runs 0.6 mm beside it: no lumen HU is carried there to mislead."""
    vessels = (Vessel(1.9, 14), Vessel(1.2, 18, start=(14, 0, 0), heading=35), Vessel(1.0, 14, start=(14, 0, 0), heading=-35))
    phantom = Phantom(vessels, joints=(((14, 0, 0), 1.9),))
    scan = Scan(levels=((1.0, 0.9), (0.85, 0.5), (0.8, 0.7)), neighbours=(Neighbour(1, 3, 15, gap=0.6, radius=1.6, angle=70, hu=300),), seed=2)
    image = phantom.image(SHEARED, scan)
    stations, parents, held, touching = trace(image, image.thin)
    widths = areas(stations, parents, sampler(image.ct, image.affine), touching)
    assert widths.calibrated and 0.3 < widths.offset < 0.9
    for st, area, hold in zip(stations, widths.area, held):
        vessel, s, _ = phantom.nearest(st.points[~hold])
        true = 2 * np.array([vessels[k].radius_at(np.array([x]))[0] for k, x in zip(vessel, s)])
        ratio = diameter(area[~hold]) / true
        assert ratio.min() > 0.75 and ratio.max() < 1.25
        # Uncorrected, the tight mask draws these vessels a fifth thin.
        assert np.abs(np.median(ratio) - 1) < np.abs(np.median(diameter(st.area[~hold]) / true) - 1)


def test_branches_the_mask_draws_differently_each_move_by_their_own_offset():
    """A mask that draws the 3.6 mm trunk 0.4 mm thinner than it draws the 2.2 mm daughters: one offset for the scan would leave both wrong."""
    vessels = (Vessel(1.8, 16), Vessel(1.1, 22, start=(16, 0, 0), heading=35), Vessel(1.1, 22, start=(16, 0, 0), heading=-35))
    phantom = Phantom(vessels, joints=(((16, 0, 0), 1.8),))
    image = phantom.image(SHEARED, Scan(seed=13))
    stations, parents, held, touching = trace(image, image.varied)
    widths = areas(stations, parents, sampler(image.ct, image.affine), touching)
    moved = [np.median(diameter(area[~hold]) - diameter(st.area[~hold])) for st, area, hold in zip(stations, widths.area, held)]
    assert widths.calibrated and moved[0] > max(moved[1:]) + 0.2
    for st, area, hold, vessel in zip(stations, widths.area, held, vessels):
        assert np.median(diameter(area[~hold])) == pytest.approx(2 * vessel.radius, abs=0.15)


def test_vessels_too_thin_to_read_are_not_thinned_by_the_wide_ones_offset():
    """A mask 0.6 mm fat on the trunk and right on 1.5 mm daughters the CT cannot read: the trunk's offset would halve them."""
    vessels = (Vessel(1.9, 16), Vessel(0.75, 20, start=(16, 0, 0), heading=35), Vessel(0.8, 20, start=(16, 0, 0), heading=-35))
    image = Phantom(vessels, joints=(((16, 0, 0), 1.9),)).image(SHEARED, Scan(in_plane=1.3, through_plane=1.3, varied=((2.2, 0.0), (3.2, -0.3)), seed=15))
    stations, parents, _, touching = trace(image, image.varied)
    widths = areas(stations, parents, sampler(image.ct, image.affine), touching)
    assert widths.calibrated and widths.offset < -0.2
    assert np.isfinite(widths.ct_diameter[0]).any() and (widths.area[0] <= stations[0].area).all()
    for st, area, read in zip(stations[1:], widths.area[1:], widths.ct_diameter[1:]):
        assert np.isnan(read).all() and area == pytest.approx(st.area)


def test_a_vessel_traced_as_two_branches_is_not_given_its_width_twice():
    """On 1 mm slices the tracer follows the 2.6 mm daughter, which lies in the slice plane, as two branches that each
    hold part of its lumen. The CT shows the whole vessel at both, so read, each would take its full width."""
    vessels = (Vessel(1.8, 16), Vessel(1.3, 24, start=(16, 0, 0), heading=35), Vessel(1.05, 24, start=(16, 0, 0), heading=-35))
    image = Phantom(vessels, joints=(((16, 0, 0), 1.8),)).image(THICK, Scan(seed=21))
    stations, parents, _, touching = trace(image, image.lumen)
    widths = areas(stations, parents, sampler(image.ct, image.affine), touching)
    assert widths.calibrated and widths.offset < 0.4 and not np.isfinite(np.concatenate(widths.ct_diameter)[np.concatenate(touching)]).any()
    # Scored on the lumen volume the solve is given, which counts a vessel once however it was traced.
    volume = sum(np.trapezoid(area, st.s) for st, area in zip(stations, widths.area))
    assert volume == pytest.approx(sum(np.pi * vessel.radius**2 * vessel.length for vessel in vessels), rel=0.15)


def test_stations_a_millimetre_off_the_axis_give_the_same_offset():
    image = Phantom((Vessel(1.7, 25),)).image(SHEARED, Scan(seed=3))
    (st,), parents, _, _ = trace(image, image.thin)
    aside = np.cross(st.points[-1] - st.points[0], [0.3, -0.5, 0.8])
    moved = replace(st, points=st.points + aside / np.linalg.norm(aside))
    on, off = (areas([each], parents, sampler(image.ct, image.affine)) for each in (st, moved))
    assert off.calibrated and off.read >= 0.9 * on.read and off.offset == pytest.approx(on.offset, abs=0.05)


def test_calcium_beside_the_lumen_is_marked_and_not_read():
    vessel = Vessel(1.7, 25)
    image = Phantom((vessel,)).image(SHEARED, Scan(plaques=(Plaque(0, centre=12, length=3, thickness=0.8, arc=120, angle=40),), seed=4))
    (st,), parents, _, _ = trace(image, image.lumen)
    widths = areas([st], parents, sampler(image.ct, image.affine))
    s = vessel.locate(st.points)[0]
    beside, clear = np.abs(s - 12) <= 1.0, np.abs(s - 12) >= 4.0
    assert widths.calcium[0][beside].all() and not widths.calcium[0][clear].any() and np.isnan(widths.ct_diameter[0][beside]).all()
    # Those stations still move by the offset read either side of them.
    assert widths.calibrated and np.abs(diameter(widths.area[0][beside]) - diameter(st.area[beside]) - widths.offset).max() < 0.1


def test_a_scan_the_ct_cannot_correct_keeps_the_masks_widths():
    vessel = Vessel(1.7, 25)
    image = Phantom((vessel,)).image(SHEARED, Scan(seed=5))
    (st,), parents, _, _ = trace(image, image.thin)
    sample = sampler(image.ct, image.affine)
    assert areas([st], parents, sample).calibrated
    # Too few stations read: a short piece of vessel, and a CT with no contrast in it.
    few = replace(st, **{name: getattr(st, name)[:15] for name in ("points", "s", "count", "area", "inscribed", "spur", "junction", "stub")})
    for stations, read in ((few, sample), (st, sampler(np.full(image.ct.shape, 40.0), image.affine))):
        widths = areas([stations], parents, read)
        assert not widths.calibrated and widths.read < 20 and (widths.area[0] == stations.area).all()
    # An offset past 1 mm: the mask is not this vessel's, or the reading is wrong.
    swollen = replace(st, area=st.area * 2.5)
    widths = areas([swollen], parents, sample)
    assert not widths.calibrated and widths.read >= 20 and widths.offset < -1 and (widths.area[0] == swollen.area).all()
    # A smaller offset is applied, but leaves every station at least half the mask's width.
    few = np.arange(len(st.s)) // 4 == 5
    odd = replace(st, area=np.where(few, np.pi * 0.5**2, st.area * 2.2))
    widths = areas([odd], parents, sample)
    assert widths.calibrated and widths.offset < -0.5 and diameter(widths.area[0][few]) == pytest.approx(0.5)
    with pytest.raises(ValueError, match="one parent"):
        areas([st], [], sample)
    with pytest.raises(ValueError, match="one contact mark"):
        areas([st], parents, sample, [])
