"""Lumen area along a known axis, on CCTA-sized grids that are rotated, reflected or sheared."""

import numpy as np
import pytest
from synthetic_vessels import GRIDS, Stenosis, Vessel

from scan3d.vessel_geometry import lumen, measure

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
