"""Steady hyperaemic pressure and FFR along coronary centreline trees. Research use only.

A reduced-order (0D) model. Each station interval has a Poiseuille resistance, and each narrowed
stretch has one Young–Tsai expansion loss. Outlet resistances come from LV mass, blood pressure and
a Murray flow split (Sharma 2012), with the hyperaemic resistance ratio taken from Danad 2014's
rest and hyperaemic perfusion. Geometry arrives in mm and mm² from vessel_geometry; the solver
works in SI and reports mmHg.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from scipy.optimize import least_squares, root

from .vessel_geometry import Stations, Tree

MMHG = 133.322387415  # Pa

_OSTIUM = 1.0  # mm of a root branch whose area is held: the first cells read 0.36-1.68 of the lumen
_TIP = 1.5  # mm of an end whose area is held: blunt ends read 0.2-0.64 of the lumen
_LESION = 0.7  # a reported lesion has r_eq below this fraction of the reference...
_LESION_LENGTH = 1.0  # ...for at least this many mm
_START = 0.87  # an expansion-loss region reaches r_eq/r_ref at or below this (Fossan 2018)...
_EXTEND = 0.883  # ...and runs while r_eq/r_ref stays below this
_FIT_LOW, _FIT_HIGH = 0.9, 1.25  # the reference fit keeps stations within these fractions of the last fit
_FIT_PASSES = 5
_STEP = 8  # a largest child with fewer candidates continues its parent's reference intercept
_FEW = 3  # a reference group with fewer fitted stations takes its median radius
_FLOOR = 0.05  # mm; a reference fit never goes thinner than this
_SMALL_TREE = 10.0  # mm; shorter trees are left out of the flow split


@dataclass(frozen=True)
class Physiology:
    """Patient inputs and model constants, in clinical units."""

    lv_mass: float  # g, from the myocardium mask or typed
    sbp: float | None = None  # mmHg; both None means 120/80, assumed
    dbp: float | None = None
    heart_rate: float | None = None  # bpm; recorded, unused by this model
    share: float | None = None  # fraction of whole-heart flow into the trees solved; needed for one tree
    perfusion: float = 1.0  # resting, mL/min/g (Danad 2014)
    heart_factor: float = 1.5  # whole heart over LV mass (Sharma 2012)
    tcri: float = 0.31  # hyperaemic over resting resistance: 1.00/3.26 mL/min/g (Danad 2014)
    exponent: float = 3.0  # outlet flow in proportion to radius to this power (Murray)
    venous: float = 5.0  # mmHg
    viscosity: float = 0.0035  # Pa s
    density: float = 1060.0  # kg/m³
    kt: float = 1.52  # expansion-loss coefficient (Seeley and Young 1976, as cited by Mirramezani 2020)

    def __post_init__(self):
        given = (self.sbp, self.dbp, self.heart_rate, self.share)
        positive = (self.lv_mass, self.perfusion, self.heart_factor, self.tcri, self.exponent, self.viscosity, self.density)
        values = positive + (self.kt, self.venous) + tuple(value for value in given if value is not None)
        if not all(isinstance(value, (int, float)) and np.isfinite(value) for value in values):
            raise ValueError("Physiology inputs must be finite numbers")
        if not (all(value > 0 for value in positive) and self.kt >= 0 and self.venous >= 0):
            raise ValueError("Physiology inputs must be positive")
        if self.tcri > 1 or (self.heart_rate is not None and self.heart_rate <= 0):
            raise ValueError("TCRI must be at most 1 and the heart rate positive")
        if self.share is not None and not 0 < self.share <= 1:
            raise ValueError("The flow share must be more than 0 and at most 1")

    def mean_pressure(self) -> float:
        """Mean aortic pressure in mmHg, from the cuff pressures or 120/80."""
        if (self.sbp is None) != (self.dbp is None):
            raise ValueError("Enter both systolic and diastolic pressure, or neither")
        sbp, dbp = (120.0, 80.0) if self.sbp is None else (self.sbp, self.dbp)
        if not sbp > dbp > self.venous:
            raise ValueError("Blood pressure needs systolic above diastolic above venous pressure")
        return dbp + (sbp - dbp) / 3

    def assumed(self) -> tuple[str, ...]:
        # Only inputs the model uses; the heart rate is recorded, never assumed.
        return ("blood pressure",) if self.sbp is None else ()


@dataclass(frozen=True, eq=False)
class Network:
    """What the solver sees: per branch, a drop of resistance·Q + loss·Q² on each station interval."""

    parents: tuple[int, ...]  # -1 at a root; parents before children
    resistance: tuple[np.ndarray, ...]  # (m-1,) Pa s/m³
    loss: tuple[np.ndarray, ...]  # (m-1,) Pa s²/m⁶
    outlets: dict[int, float]  # end branch -> resistance to venous pressure, Pa s/m³; trees without outlets are not solved


@dataclass(frozen=True, eq=False)
class Lesion:
    """A reported narrowing: r_eq under 0.7 of the reference for at least 1 mm, with runs less
    than 1 mm apart taken as one."""

    branch: int
    throat: int  # station with the least r_eq/r_ref
    start: float  # mm from the ostium where r_eq crosses 0.7 r_ref, or the branch end
    end: float
    length: float  # mm
    mld: float  # minimum lumen diameter, 2 r_eq at the throat, mm
    stenosis: float  # diameter stenosis, 1 - r_eq/r_ref at the throat
    held: bool  # touches a held station, so the narrowing may be under-read


@dataclass(frozen=True, eq=False)
class Result:
    inputs: Physiology
    aortic: float  # mean aortic pressure used, mmHg
    network: Network
    reference: list[np.ndarray]  # healthy radius r_ref per station, mm
    lesions: list[Lesion]
    outlet_radius: dict[int, float]  # end branch -> radius used for the flow split, mm
    flow: np.ndarray  # hyperaemic flow per branch, mL/min; NaN where not solved
    pressure: list[np.ndarray]  # per station, mmHg; NaN where not solved
    ffr: list[np.ndarray]  # P/Pa per station; NaN where not reported
    reason: list[str | None]  # per branch, why FFR is not reported
    held: list[np.ndarray]  # stations whose area was replaced by the nearest trusted one
    flags: tuple[str, ...]


def poiseuille(s: np.ndarray, area: np.ndarray, viscosity: float) -> np.ndarray:
    """Resistance of each interval between stations, Pa s/m³, from s in mm and area in mm².

    The trapezoid rule on 8πμ/A², so a uniform tube gives exactly 8μL/(πr⁴).
    """
    s, area = np.asarray(s, dtype=float) * 1e-3, np.asarray(area, dtype=float) * 1e-6
    return 4 * np.pi * viscosity * np.diff(s) * (area[:-1] ** -2 + area[1:] ** -2)


def expansion(reference: float, throat: float, density: float, kt: float) -> float:
    """Young–Tsai expansion loss, Pa s²/m⁶, for areas in mm²: the drop is this times Q|Q|."""
    if throat >= reference:
        return 0.0
    return kt * density / 2 * (1 / (throat * 1e-6) - 1 / (reference * 1e-6)) ** 2


def solve(network: Network, inlet: float, venous: float) -> tuple[np.ndarray, list[np.ndarray]]:
    """Flow in each branch (m³/s) and pressure at each station (Pa), with every root at `inlet`.

    The unknowns are the outlet flows, so flow is conserved at every junction by construction.
    Each outlet's residual is the pressure left after its path's drops and its outlet resistance.
    The residuals are the gradient of a strictly convex function, so the root is unique. A tree
    with no outlets is not solved, and its branches get NaN.
    """
    parents = network.parents
    count = len(parents)
    if len(network.resistance) != count or len(network.loss) != count:
        raise ValueError("The network needs resistance and loss for every branch")
    if any(not -1 <= parent < branch for branch, parent in enumerate(parents)):
        raise ValueError("Each branch's parent must come before it")
    if not inlet > venous:
        raise ValueError("Inlet pressure must exceed venous pressure")
    children = [[] for _ in parents]
    tree_of = list(range(count))
    for branch, parent in enumerate(parents):
        if parent >= 0:
            children[parent].append(branch)
            tree_of[branch] = tree_of[parent]
    for branch, (resistance, loss) in enumerate(zip(network.resistance, network.loss)):
        if len(resistance) < 1 or len(loss) != len(resistance) or not (np.isfinite(resistance).all() and np.isfinite(loss).all()):
            raise ValueError(f"Branch {branch} needs finite resistance and loss on each interval")
        if (resistance < 0).any() or (loss < 0).any():
            raise ValueError(f"Branch {branch} has a negative resistance or loss")
    for branch, value in network.outlets.items():
        if not 0 <= branch < count or children[branch] or not (np.isfinite(value) and value > 0):
            raise ValueError(f"Outlet {branch} must be an end branch with a positive resistance")
    leaves = sorted(network.outlets)
    for branch in range(count):
        if not children[branch] and branch not in network.outlets and any(tree_of[leaf] == tree_of[branch] for leaf in leaves):
            raise ValueError(f"End branch {branch} has no outlet resistance")

    path = np.zeros((count, len(leaves)))
    for column, leaf in enumerate(leaves):
        branch = leaf
        while branch >= 0:
            path[branch, column] = 1
            branch = parents[branch]
    solved = path.any(1)
    flow = np.full(count, np.nan)
    if leaves:
        viscous = np.array([resistance.sum() for resistance in network.resistance])
        loss = np.array([loss.sum() for loss in network.loss])
        outlet = np.array([network.outlets[leaf] for leaf in leaves])
        drop = inlet - venous
        scale = drop / outlet  # each outlet's flow with nothing upstream of it

        def residual(x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
            q = path @ (x * scale)
            f = (drop - path.T @ (viscous * q + loss * q * np.abs(q)) - outlet * x * scale) / drop
            jacobian = -(path.T @ ((viscous + 2 * loss * np.abs(q))[:, None] * path) + np.diag(outlet)) * scale / drop
            return f, jacobian

        start = np.linalg.solve(path.T @ (viscous[:, None] * path) + np.diag(outlet), np.full(len(leaves), drop))
        found = root(residual, start / scale, jac=True, method="hybr", options={"xtol": 1e-12})
        # hybr can report no progress with the residual already at 1e-16, so the residual decides.
        if not np.abs(residual(found.x)[0]).max() <= 1e-10:
            raise ValueError(f"The pressure solve did not converge: {found.message}")
        flow[solved] = (path @ (found.x * scale))[solved]
        if (flow[solved] <= 0).any():
            raise ValueError(f"The pressure solve gave no forward flow in branch {int(np.flatnonzero(solved & ~(flow > 0))[0])}")
    pressure = []
    for branch in range(count):
        if not solved[branch]:
            pressure.append(np.full(len(network.resistance[branch]) + 1, np.nan))
            continue
        drops = network.resistance[branch] * flow[branch] + network.loss[branch] * flow[branch] ** 2
        start = inlet if parents[branch] < 0 else pressure[parents[branch]][-1]
        pressure.append(start - np.concatenate([[0.0], np.cumsum(drops)]))
    return flow, pressure


def analyse(tree: Tree, stations: list[Stations], physiology: Physiology, contact: list[np.ndarray] | None = None) -> Result:
    """Reference radius and lesions, boundary conditions, the steady hyperaemic solve and gaps.

    `stations` come from `measure(..., parents=...)` on the tree's branches; `contact` from
    `vessel_geometry.contact`, whose stations are left out of the reference fit.
    """
    parents = [branch.parent for branch in tree.branches]
    if len(stations) != len(parents) or (contact is not None and len(contact) != len(parents)):
        raise ValueError("Stations and contact flags need one entry per branch")
    children = [[] for _ in parents]
    depth = np.zeros(len(parents))  # mm from the ostium to each branch's start
    for branch, parent in enumerate(parents):
        if parent >= 0:
            children[parent].append(branch)
            depth[branch] = depth[parent] + stations[parent].s[-1]
    area, held = _held(stations, parents, children)
    for branch, (values, st) in enumerate(zip(area, stations)):
        empty = np.flatnonzero(~(np.isfinite(values) & (values > 0)))
        if len(empty):
            raise ValueError(f"Branch {branch} station {empty[0]} has no lumen area")
        back = np.flatnonzero(np.diff(st.s) < 0)
        if len(back):
            raise ValueError(f"Branch {branch} station {back[0] + 1} runs backwards along the centreline")
    radius = [np.sqrt(values / np.pi) for values in area]
    leaves = [branch for branch in range(len(parents)) if not children[branch]]
    paths = _paths(tree, stations, parents, depth, leaves)
    reference = _reference(stations, parents, children, held, depth, paths, contact)
    for branch, untrusted in enumerate(held):
        if untrusted.all():
            # Nothing here can be measured: a short branch between junctions is all mixed cells,
            # which read narrow and would put an expansion loss on a stretch too short to report.
            area[branch] = np.pi * reference[branch] ** 2
            radius[branch] = reference[branch].copy()

    lesions, loss = [], []
    for branch, st in enumerate(stations):
        ratio = radius[branch] / reference[branch]
        spans = []
        for first, last in _runs(ratio < _LESION):
            start = _crossing(st.s, ratio, first - 1, first)
            end = _crossing(st.s, ratio, last + 1, last)
            # Runs closer than a lesion length are one narrowing that a station or two reads
            # just above the threshold, so they are one lesion over the whole span.
            if spans and start - spans[-1][3] < _LESION_LENGTH:
                spans[-1] = (spans[-1][0], last, spans[-1][2], end)
            else:
                spans.append((first, last, start, end))
        for first, last, start, end in spans:
            if end - start >= _LESION_LENGTH:
                throat = first + int(np.argmin(ratio[first : last + 1]))
                lesions.append(
                    Lesion(
                        branch,
                        throat,
                        depth[branch] + start,
                        depth[branch] + end,
                        end - start,
                        2 * radius[branch][throat],
                        1 - ratio[throat],
                        bool(held[branch][first : last + 1].any()),
                    )
                )
        coefficient = np.zeros(len(st.s) - 1)
        for first, last in _runs(ratio < _EXTEND):
            if ratio[first : last + 1].min() > _START or (last == len(ratio) - 1 and not children[branch]):
                continue
            throat = first + int(np.argmin(ratio[first : last + 1]))
            healthy = np.pi * reference[branch][min(last + 1, len(ratio) - 1)] ** 2
            coefficient[min(throat, len(coefficient) - 1)] += expansion(healthy, area[branch][throat], physiology.density, physiology.kt)
        loss.append(coefficient)

    tree_of = [branch.root for branch in tree.branches]
    longest = {}
    for leaf in leaves:
        longest[tree_of[leaf]] = max(longest.get(tree_of[leaf], 0.0), depth[leaf] + stations[leaf].s[-1])
    split = sorted(root for root, length in longest.items() if length >= _SMALL_TREE)
    reason: list[str | None] = [None if tree_of[branch] in split else "tree left out of the flow split" for branch in range(len(parents))]
    aortic = physiology.mean_pressure()
    outlets, outlet_radius = {}, {}
    if len(split) == 1 and physiology.share is None:
        reason = [text or "one coronary tree only" for text in reason]
    else:
        ends = [leaf for leaf in leaves if tree_of[leaf] in split]
        for leaf in ends:
            trusted = np.flatnonzero(~held[leaf])
            outlet_radius[leaf] = float(reference[leaf][trusted[-1] if len(trusted) else -1])
        weight = np.array([outlet_radius[leaf] ** physiology.exponent for leaf in ends])
        total = physiology.perfusion * physiology.heart_factor * physiology.lv_mass / 60 * 1e-6  # m³/s at rest
        rest = (physiology.share or 1.0) * total * weight / weight.sum()
        outlets = {leaf: physiology.tcri * (aortic - physiology.venous) * MMHG / flow for leaf, flow in zip(ends, rest)}
    flags = [f"{name} assumed" for name in physiology.assumed()]
    if physiology.share is not None and len(split) > 1:
        # A share given for one tree and left in place scales every tree's flow, so it shows.
        flags.append(f"flow share {physiology.share:g} applied to {len(split)} trees")
    for branch, item in enumerate(tree.branches):
        if item.gap and reason[branch] is None:
            reason[branch] = "FFR not reported: lumen gap"
            flags.append(f"lumen gap on branch {branch}: FFR upstream assumes normal flow into it and may read low")

    network = Network(tuple(parents), tuple(poiseuille(st.s, a, physiology.viscosity) for st, a in zip(stations, area)), tuple(loss), outlets)
    flow, pressure = solve(network, aortic * MMHG, physiology.venous * MMHG)
    pressure = [values / MMHG for values in pressure]
    ffr = [values / aortic if text is None else np.full(len(values), np.nan) for values, text in zip(pressure, reason)]
    return Result(physiology, aortic, network, reference, lesions, outlet_radius, flow * 6e7, pressure, ffr, reason, held, tuple(flags))


def _held(stations: list[Stations], parents: list[int], children: list[list[int]]) -> tuple[list[np.ndarray], list[np.ndarray]]:
    """Areas with untrusted stations replaced by the nearest trusted one in the same branch.

    Untrusted: the first mm of a root branch, the last 1.5 mm of an end, junction zones, whose
    mixed cells read false narrowings worth 0.5-7 mmHg on a healthy Y, and spur runs touching
    those zones, which at blunt ends lose real end-cap lumen (a 2.5 mm tube's last trusted station
    read 0.40-0.48 of its area). A branch with no trusted station takes its own median area here,
    and `analyse` gives it its reference area once that is fitted.
    """
    area, held = [], []
    for branch, st in enumerate(stations):
        zone = st.junction.copy()
        if parents[branch] < 0:
            zone |= st.s < _OSTIUM
        if not children[branch]:
            zone |= st.s > st.s[-1] - _TIP
        untrusted = zone.copy()
        # A spur run touching an ostium, tip or junction zone is lost end-cap or junction lumen and
        # extends the zone. An isolated one is a throat: its inscribed radius, and so the spur rule's
        # cover, collapses there, and holding it would erase a real narrowing.
        for first, last in _runs(st.spur):
            if zone[max(first - 1, 0) : last + 2].any():
                untrusted[first : last + 1] = True
        trusted = np.flatnonzero(~untrusted)
        if len(trusted):
            area.append(st.area[trusted[np.abs(np.arange(len(st.s))[:, None] - trusted).argmin(1)]])
        else:
            area.append(np.full(len(st.s), np.median(st.area)))
        held.append(untrusted)
    return area, held


def _paths(tree: Tree, stations: list[Stations], parents: list[int], depth: np.ndarray, leaves: list[int]) -> list[list[int]]:
    """Branches from root to each end: trees by root, then longest path first."""
    paths = []
    for leaf in leaves:
        path, branch = [], leaf
        while branch >= 0:
            path.append(branch)
            branch = parents[branch]
        paths.append(path[::-1])
    return sorted(paths, key=lambda path: (tree.branches[path[0]].root, -(depth[path[-1]] + stations[path[-1]].s[-1]), path[-1]))


def _reference(
    stations: list[Stations],
    parents: list[int],
    children: list[list[int]],
    held: list[np.ndarray],
    depth: np.ndarray,
    paths: list[list[int]],
    contact: list[np.ndarray] | None,
) -> list[np.ndarray]:
    """Healthy radius per station, each branch from the longest path through it.

    Along a path, r_ref = α_group + β·s, with one slope and an intercept per group, fitted
    robustly to trusted stations within 0.9-1.25 of the last fit. A branch starts its own group
    unless it is its parent's largest child with fewer than 8 candidates: a short continuation
    of the same vessel, so a diffuse narrowing there is still seen. A short end branch with
    candidates continues only if it is at least 0.9 of its parent's size; a smaller one is a
    daughter and keeps its own radius.
    """
    measured = [np.sqrt(st.area / np.pi) for st in stations]
    touching = contact if contact is not None else [np.zeros(len(st.s), dtype=bool) for st in stations]
    candidate = [~held[b] & ~st.spur & ~np.asarray(touching[b], dtype=bool) for b, st in enumerate(stations)]
    size = [float(np.median(values)) for values in measured]
    continues = [
        parents[b] >= 0
        and candidate[b].sum() < _STEP
        and b == max(children[parents[b]], key=lambda c: size[c])
        and (bool(children[b]) or not candidate[b].any() or size[b] >= _FIT_LOW * size[parents[b]])
        for b in range(len(parents))
    ]
    reference: list[np.ndarray | None] = [None] * len(parents)
    for path in paths:
        group = np.cumsum([index == 0 or not continues[b] for index, b in enumerate(path)]) - 1
        rows = [slice(0 if index == 0 else 1, None) for index in range(len(path))]
        s = np.concatenate([depth[b] + stations[b].s[row] for b, row in zip(path, rows)])
        r = np.concatenate([measured[b][row] for b, row in zip(path, rows)])
        good = np.concatenate([candidate[b][row] for b, row in zip(path, rows)])
        member = np.concatenate([np.full(len(stations[b].s[row]), g) for b, g, row in zip(path, group, rows)])
        keep = good
        for _ in range(_FIT_PASSES):
            line = _fit(s, r, good, keep, member, group[-1] + 1)
            fitted = good & (r >= _FIT_LOW * line(s, member)) & (r <= _FIT_HIGH * line(s, member))
            if (fitted == keep).all():
                break
            keep = fitted
        for b, g in zip(path, group):
            if reference[b] is None:
                reference[b] = line(depth[b] + stations[b].s, np.full(len(stations[b].s), g))
    return reference


def _fit(s, r, good, keep, member, groups):
    """r_ref(s, group): one slope and an intercept per group with enough kept stations, else the
    group's median radius (of its candidates, or of all its stations)."""
    constant = np.array([np.median(r[good & (member == g)]) if (good & (member == g)).any() else np.median(r[member == g]) for g in range(groups)])
    fitted = [g for g in range(groups) if (keep & (member == g)).sum() >= _FEW]
    slope, intercept = np.zeros(groups), constant.copy()
    if fitted:
        rows = keep & np.isin(member, fitted)
        design = np.column_stack([member[rows] == g for g in fitted] + [s[rows]]).astype(float)
        start = np.r_[[np.median(r[rows & (member == g)]) for g in fitted], 0.0]
        solution = least_squares(lambda p: design @ p - r[rows], start, loss="soft_l1", f_scale=0.05).x
        intercept[fitted], slope[fitted] = solution[:-1], solution[-1]
    return lambda at, group: np.maximum(intercept[group] + slope[group] * at, _FLOOR)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """First and last index of each run of True."""
    edges = np.flatnonzero(np.diff(np.concatenate([[0], mask.astype(int), [0]])))
    return list(zip(edges[::2], edges[1::2] - 1))


def _crossing(s: np.ndarray, ratio: np.ndarray, outside: int, inside: int) -> float:
    """Where the ratio crosses the lesion threshold between two stations, or the branch end."""
    if not 0 <= outside < len(s):
        return float(s[inside])
    return float(s[outside] + (ratio[outside] - _LESION) / (ratio[outside] - ratio[inside]) * (s[inside] - s[outside]))
