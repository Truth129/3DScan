"""Research-only CT-FFR on a saved revision: a CPU-lane job, its stored result and the reading rules.

Research use only, not for diagnostic use. The job reads the revision's own binary masks on its CT
grid, never resampled, traces centrelines from the ostia, measures areas and solves hyperaemic
pressure (`vessel_geometry`, `ffr_physics`). It never makes a revision. Its result lives in
`cases/<case>/coronary/<key>/`: `result.json`, then `complete.json` last. The key covers the code,
the image, the masks and the inputs, so a later revision with the same masks reuses the folder,
and every read checks the folder again.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re

import numpy as np
import scipy
from scipy.sparse.csgraph import connected_components, dijkstra
from scipy.spatial import cKDTree

from . import identity, imaging, review_volume
from .errors import NotFound
from .ffr_physics import Physiology, analyse
from .fileio import atomic_json, atomic_text
from .identifiers import identifier
from .store import now, safe_id
from .vessel_geometry import centrelines, contact, lumen, measure, ostia

KIND = "coronary_analysis"
FORMAT = "scan3d-coronary-v1"
NOTICE = "Research use only. Not for diagnostic use."
SPACING = 0.5  # mm between stations
WINDOW = 1.0  # mm of lumen averaged into each station's area
REPORT_DIAMETER = 1.8  # mm; thinner stations, and lesions at them, are solved but not reported
REPORT_BEYOND = 20.0  # mm past a lesion's end where its FFR is read
_DAUGHTER = 3.0  # mm of each daughter whose median reference diameter picks the larger one
_SNAP = 3.0  # mm; a clicked ostium snaps to lumen this close, and is dropped this close to another root
_CONTACT = 1.5  # mm; lumen this close to the aorta is an ostium patch, as `ostia` uses
_OFF_AORTA = 5.0  # mm; a clicked point partway along lumen an aortic root reaches, farther than this from the aorta, is refused
_LOCAL = 2.0  # mm of lumen around a clicked point whose widest wall distance sizes the vessel there
_SHELL = 2.0  # mm; depth of the shell of lumen whose pieces say whether a point is partway along a vessel
_DENSITY = 1.05  # g/mL of myocardium
_LV_MASS = (20.0, 1000.0)  # g
_ROLES = ("lumen", "aorta", "myocardium")
_KEY = re.compile(r"[0-9a-f]{64}")
_CODE = [hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ("vessel_geometry.py", "ffr_physics.py", "coronary.py")]


def start(service, case_id, revision_id, inputs):
    """Queue a run, or return the queued, running or complete one for this revision and key."""
    _, normal, key = _prepare(service, case_id, revision_id, inputs)
    folder = _folder(service, case_id, key)
    with service.store.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute("SELECT data FROM jobs WHERE case_id=? AND json_extract(data,'$.kind')=? ORDER BY rowid DESC", (case_id, KIND)).fetchall()
        for row in rows:
            job = json.loads(row["data"])
            if job.get("revision_id") != revision_id or job["payload"].get("key") != key:
                continue
            # A run being cancelled is never handed back, and a complete job whose folder went is run
            # again rather than handed back to 404.
            if (job["status"] in ("queued", "running") and not job.get("cancel_requested")) or (
                job["status"] == "completed" and _stored(folder, key) is not None
            ):
                return job
        job = dict(
            id=identifier(),
            case_id=case_id,
            kind=KIND,
            status="queued",
            stage="Queued",
            error=None,
            revision_id=revision_id,
            created_at=now(),
            payload={"key": key, "inputs": normal},
            cancel_requested=False,
            requested_by=identity.actor_id(),
        )
        con.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job["id"], case_id, job["status"], json.dumps(job, allow_nan=False)))
    return job


def analyses(service, case_id, revision_id):
    """This revision's runs, newest first, with a summary once complete."""
    _saved(service, case_id, revision_id)
    jobs = [job for job in service.store.list("jobs", case_id) if job["kind"] == KIND and job.get("revision_id") == revision_id]
    return {
        "analyses": [
            {
                "analysis_id": job["id"],
                "state": job["status"],
                "stage": job["stage"],
                "created_at": job["created_at"],
                "finished_at": job.get("completed_at"),
                "error": job.get("error"),
                "inputs": job["payload"]["inputs"],
                "summary": (job.get("result") or {}).get("summary") if job["status"] == "completed" else None,
            }
            for job in jobs
        ]
    }


def read(service, case_id, revision_id, analysis_id):
    """A complete run's result, rechecked on disk, or NotFound."""
    _saved(service, case_id, revision_id)
    job = service.store.get("jobs", analysis_id)
    if job["case_id"] != case_id or job["kind"] != KIND or job.get("revision_id") != revision_id:
        raise NotFound("Coronary analysis not found")
    if job["status"] != "completed":
        raise NotFound("The coronary analysis has not finished")
    key = job["payload"]["key"]
    result = _stored(_folder(service, case_id, key), key)
    if result is None:
        raise NotFound("The coronary analysis result is missing or changed. Run it again.")
    # The folder may come from another revision's run with the same masks and inputs.
    result.update(analysis_id=job["id"], case_id=case_id, revision_id=revision_id)
    return result


def run(service, job):
    """The job: load, trace, measure, solve and save, or complete at once from a matching folder."""
    service.stage(job, "Loading masks")
    revision, inputs, key = _prepare(service, job["case_id"], job["revision_id"], job["payload"]["inputs"])
    if key != job["payload"]["key"]:
        # Saved masks never change, so in practice a restart onto new code or libraries did this.
        raise ValueError("The saved revision or the analysis software changed after this analysis was queued. Run it again.")
    folder = _folder(service, job["case_id"], key)
    result = _stored(folder, key)
    if result is None:
        result = _analyse(service, job, revision, inputs, key)
        body = json.dumps(result, allow_nan=False, separators=(",", ":"))
        atomic_text(folder / "result.json", body)
        atomic_json(folder / "complete.json", {"format": FORMAT, "key": key, "result_sha256": hashlib.sha256(body.encode()).hexdigest()})
    service.store.update_job(
        job["id"],
        status="completed",
        stage="Complete",
        error=None,
        completed_at=now(),
        result={"analysis_id": job["id"], "key": key, "summary": _summary(result)},
    )


def _saved(service, case_id, revision_id):
    # A candidate alias is an unsaved proposal, as agreement and cinematic references say.
    if revision_id.startswith("review-"):
        raise NotFound("Save the segmentation before reading a coronary analysis")
    return service.store.revision(case_id, revision_id)


def _prepare(service, case_id, revision_id, inputs):
    """The revision, normalised inputs and the key, refusing anything that cannot run."""
    if revision_id.startswith("review-"):
        raise ValueError("Save the segmentation before running a coronary analysis")
    revision = service.store.revision(case_id, revision_id)
    structures = {structure["id"]: structure for structure in revision["structures"]}
    normal = {role: inputs.get(role) for role in _ROLES}
    normal.update({name: None if inputs.get(name) is None else float(inputs[name]) for name in ("lv_mass", "sbp", "dbp", "heart_rate", "share")})
    normal["ostia"] = [[float(value) for value in point] for point in inputs.get("ostia") or ()]
    chosen = [normal[role] for role in _ROLES if normal[role]]
    for role in _ROLES:
        if normal[role] and normal[role] not in structures:
            raise ValueError(f"The {role} segment {normal[role]} is not in this revision")
    if len(set(chosen)) != len(chosen):
        raise ValueError("Choose a different segment for the lumen, the aorta and the myocardium")
    if not normal["lumen"]:
        raise ValueError("Choose the coronary lumen segment")
    if not normal["aorta"] and not normal["ostia"]:
        raise ValueError("Choose the aorta or add an ostium point")
    if normal["myocardium"] and normal["lv_mass"] is not None:
        raise ValueError("Give the LV mass or a myocardium segment, not both")
    if not normal["myocardium"] and normal["lv_mass"] is None:
        raise ValueError("Enter the LV mass or choose a myocardium segment")
    if len(normal["ostia"]) > 4 or not all(len(point) == 3 and all(map(math.isfinite, point)) for point in normal["ostia"]):
        raise ValueError("Add at most four ostium points, each a finite RAS point")
    # Any positive mass stands in for one the myocardium will give; only the pressures can fail here.
    _physiology(normal, normal["lv_mass"] or 150.0)
    asset = service.store.resolve_reference(revision)
    if asset["modality"] != "CT":
        raise ValueError("Coronary analysis needs a CT")
    try:
        masks = {role: review_volume._file_digest(structures[normal[role]]["mask_file"]) for role in _ROLES if normal[role]}
    except OSError:
        raise ValueError("A mask of this revision cannot be read") from None
    geometry = revision["geometry"]
    basis = {
        "format": "coronary-v1",
        "code": _CODE,
        "image": asset["content_sha256"],
        "shape": list(geometry["shape"]),
        "affine": geometry["affine_ras"],
        "masks": masks,
        "inputs": normal,
        "numpy": np.__version__,
        "scipy": scipy.__version__,
    }
    key = hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return revision, normal, key


def _physiology(inputs, lv_mass):
    physiology = Physiology(lv_mass, inputs["sbp"], inputs["dbp"], inputs["heart_rate"], inputs["share"])
    physiology.mean_pressure()
    return physiology


def _folder(service, case_id, key):
    # Never Store.case_dir, which would make the folder of a case being deleted.
    if not _KEY.fullmatch(key):
        raise ValueError("Invalid coronary analysis key")
    return service.settings.data_dir / "cases" / safe_id(case_id) / "coronary" / key


def _stored(folder, key):
    """The saved result when the folder is complete and its bytes match the marker, else None."""
    try:
        marker = json.loads((folder / "complete.json").read_text())
        body = (folder / "result.json").read_bytes()
        if not isinstance(marker, dict) or marker.get("format") != FORMAT or marker.get("key") != key:
            return None
        if marker.get("result_sha256") != hashlib.sha256(body).hexdigest():
            return None
        result = json.loads(body)
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) and result.get("format") == FORMAT and result.get("key") == key else None


def _summary(result):
    distal = [branch["distal"]["ffr"] for branch in result["branches"] if branch["distal"]["ffr"] is not None]
    return {
        "trees": len(result["roots"]),
        "branches": len(result["branches"]),
        "lesions": len(result["lesions"]),
        "lowest_distal_ffr": min(distal, default=None),
    }


def _analyse(service, job, revision, inputs, key):
    structures = {structure["id"]: structure for structure in revision["structures"]}
    image, affine = imaging._image(Path(revision["ct_file"]))

    def mask(role):
        values, _, _ = imaging._binary(Path(structures[inputs[role]]["mask_file"]), image, affine)
        return values

    flags = []
    lumen_mask = mask("lumen")
    if not lumen_mask.any():
        raise ValueError(f"The lumen segment {inputs['lumen']} is empty")
    found = lumen(lumen_mask, affine)
    aorta = _near(mask("aorta"), lumen_mask, affine) if inputs["aorta"] else np.empty((0, 3))
    del lumen_mask
    lv_mass, mass_source = inputs["lv_mass"], "typed"
    if inputs["myocardium"]:
        lv_mass = float(np.count_nonzero(mask("myocardium"))) * found.voxel_volume / 1000 * _DENSITY
        mass_source = "myocardium mask"
        if not _LV_MASS[0] <= lv_mass <= _LV_MASS[1]:
            raise ValueError(f"The myocardium segment gives an LV mass of {lv_mass:.0f} g, outside 20-1000 g. Type the LV mass instead.")
    physiology = _physiology(inputs, lv_mass)

    roots = list(ostia(found, aorta, _CONTACT)) if len(aorta) else []
    sources = ["aorta"] * len(roots)
    nearest = cKDTree(found.points)
    # A root splits the lumen it shares with another root, so a point on a vessel the aorta already
    # roots would cut it in two: the stretch above it becomes a stub left out of the flow split, and
    # the point's own tree starts at aortic pressure mid-vessel.
    component = connected_components(found.graph, directed=False)[1] if roots and inputs["ostia"] else None
    reached = set(component[roots]) if component is not None else set()
    for number, point in enumerate(inputs["ostia"], 1):
        distance, index = nearest.query(point)
        if distance > _SNAP:
            raise ValueError(f"Ostium point {number} is more than 3 mm from the lumen")
        if roots and np.linalg.norm(found.points[roots] - found.points[index], axis=1).min() <= _SNAP:
            flags.append(f"ostium point {number} is within 3 mm of another root and was dropped")
            continue
        if component is not None and component[index] in reached:
            off = float(cKDTree(aorta).query(found.points[index])[0])
            # Lumen that only touches an aortic tree joins its component too, so a vessel whose
            # start lies off the aorta keeps its point: only a point partway along a vessel is refused.
            if off > _OFF_AORTA and _partway(found, index):
                raise ValueError(
                    f"Ostium point {number} is {off:.0f} mm from the aorta, partway along a vessel already traced from the aorta, so it would split that vessel. Remove it."
                )
        roots.append(int(index))
        sources.append("ostium point")
    if not roots:
        raise ValueError("The lumen does not touch the aorta; add an ostium point")

    service.stage(job, "Centrelines")
    # Lumen the aorta cuts is the ostium face, never a vessel end, so no path starts there.
    covered = np.isfinite(cKDTree(aorta).query(found.points, distance_upper_bound=_CONTACT)[0]) if len(aorta) else None
    tree = centrelines(found, np.array(roots), SPACING, covered)
    if not tree.branches:
        raise ValueError("The lumen is too short to trace from its ostia")
    parents = [branch.parent for branch in tree.branches]

    service.stage(job, "Areas")
    stations, unassigned = measure(found, [branch.points for branch in tree.branches], WINDOW, parents)
    touching = contact(found, tree)

    service.stage(job, "Pressure")
    result = analyse(tree, stations, physiology, touching)

    service.stage(job, "Saving")
    depth = np.zeros(len(parents))
    for b, parent in enumerate(parents):
        if parent >= 0:
            depth[b] = depth[parent] + stations[parent].s[-1]
    diameter = [2 * reference for reference in result.reference]
    readings = Readings(
        parents, [depth[b] + st.s for b, st in enumerate(stations)], diameter, [reported(f, d) for f, d in zip(result.ffr, diameter)], result.reason
    )

    def ffr_at(branch, station):
        return None if station is None else _number(result.ffr[branch][station], 3)

    branches = []
    for b, (branch, st) in enumerate(zip(tree.branches, stations)):
        station, note = distal(readings.shown[b], result.reason[b])
        branches.append(
            {
                "id": b,
                "parent": int(branch.parent),
                "tree": int(branch.root),
                "gap": bool(branch.gap),
                "length": _number(st.s[-1], 2),
                "flow": _number(result.flow[b], 2),
                "reason": result.reason[b],
                "distal": {"ffr": ffr_at(b, station), "station": station, "note": note},
                "stations": {
                    "point": _column(st.points, 2),
                    "s": _column(st.s, 2),
                    "area": _column(st.area, 2),
                    "diameter": _column(2 * np.sqrt(st.area / np.pi), 2),
                    "reference_diameter": _column(diameter[b], 2),
                    "ffr": _column(result.ffr[b], 3),
                    "pressure": _column(result.pressure[b], 2),
                    "reported": readings.shown[b].tolist(),
                    "held": result.held[b].tolist(),
                    "junction": st.junction.tolist(),
                    "spur": st.spur.tolist(),
                    "contact": np.asarray(touching[b], dtype=bool).tolist(),
                },
            }
        )
    lesions = []
    for number, lesion in enumerate(listed(result.lesions, diameter)):
        after, after_station, note = beyond(readings, lesion.branch, lesion.end)
        prior, prior_station = before(readings, lesion.branch, lesion.start)
        drop = None
        if after is not None and prior is not None:
            drop = _number(result.ffr[prior][prior_station] - result.ffr[after][after_station], 3)
        lesions.append(
            {
                "id": number,
                "branch": int(lesion.branch),
                "throat": int(lesion.throat),
                "start": _number(lesion.start, 2),
                "end": _number(lesion.end, 2),
                "length": _number(lesion.length, 2),
                "mld": _number(lesion.mld, 2),
                "stenosis": _number(lesion.stenosis, 3),
                "held": bool(lesion.held),
                "beyond": {"ffr": ffr_at(after, after_station), "branch": after, "station": after_station, "note": note},
                "before": {"ffr": ffr_at(prior, prior_station), "branch": prior, "station": prior_station},
                "delta_ffr": drop,
            }
        )
    if unassigned:
        flags.append(f"{unassigned} lumen voxels are not reached from any root and are not measured")
    if touched := sum(int(np.count_nonzero(marks)) for marks in touching):
        flags.append(f"lumen touches lumen more than 10 mm away along the tree at {touched} stations; areas there may mix")
    constants = {name: getattr(physiology, name) for name in ("perfusion", "heart_factor", "tcri", "exponent", "venous", "viscosity", "density", "kt")}
    return {
        "format": FORMAT,
        "notice": NOTICE,
        "analysis_id": job["id"],
        "case_id": job["case_id"],
        "revision_id": job["revision_id"],
        "key": key,
        "created_at": now(),
        "inputs": {
            "lumen": inputs["lumen"],
            "aorta": inputs["aorta"],
            "myocardium": inputs["myocardium"],
            "lv_mass": _number(lv_mass, 1),
            "lv_mass_source": mass_source,
            "sbp": inputs["sbp"],
            "dbp": inputs["dbp"],
            "heart_rate": inputs["heart_rate"],
            "share": inputs["share"],
            "ostia": inputs["ostia"],
        },
        "constants": {**constants, "spacing": SPACING, "window": WINDOW, "report_diameter": REPORT_DIAMETER, "report_beyond": REPORT_BEYOND},
        "aortic_pressure": _number(result.aortic, 2),
        "assumed": list(physiology.assumed()),
        "flags": [*result.flags, *flags],
        "roots": [{"tree": k, "point": _column(point, 2), "source": source} for k, (point, source) in enumerate(zip(tree.roots, sources))],
        "pieces": [{"point": _column(point, 2), "volume": _number(volume, 2)} for point, volume in zip(tree.pieces, tree.piece_volume)],
        "branches": branches,
        "lesions": lesions,
    }


def _partway(found, index):
    """Whether lumen leaves this voxel in two or more directions, so it is not where a vessel starts.

    The shell is the lumen just over a vessel diameter away through the lumen, beyond the local
    cross-section: one piece of it at a vessel's start, two along a vessel, three at a junction.
    """
    near = dijkstra(found.graph, directed=False, indices=index, limit=_LOCAL)
    radius = found.wall.query(found.points[np.isfinite(near)])[0].max()
    inner = 2 * radius + found.edge
    reach = dijkstra(found.graph, directed=False, indices=index, limit=inner + _SHELL)
    shell = np.flatnonzero(np.isfinite(reach) & (reach >= inner))
    if not len(shell):
        return False
    pieces = np.bincount(connected_components(found.graph[shell][:, shell], directed=False)[1])
    return np.count_nonzero(pieces >= 3) >= 2


def _near(aorta, lumen_mask, affine):
    """RAS centres of the aorta voxels in the lumen's voxel bounds, widened to cover every voxel within the contact distance."""
    low, high = imaging._foreground_bounds(lumen_mask)
    # A step of d mm in RAS moves at most d·‖A⁻¹‖ voxels along any axis, on any grid.
    pad = int(np.ceil(_CONTACT * np.linalg.norm(np.linalg.inv(affine[:3, :3]), 2))) + 1
    low, high = np.maximum(low - pad, 0), np.minimum(high + pad + 1, aorta.shape)
    ijk = np.argwhere(aorta[tuple(slice(a, b) for a, b in zip(low, high))]) + low
    return imaging._world(affine, ijk)


def _number(value, digits):
    value = float(value)
    return round(value, digits) if math.isfinite(value) else None


def _column(values, digits):
    """Rounded values, with NaN and infinity as None, nested as given."""
    rounded = np.round(np.asarray(values, dtype=float), digits).tolist()

    def finite(item):
        return [finite(value) for value in item] if isinstance(item, list) else (item if math.isfinite(item) else None)

    return finite(rounded)


# Reading rules. Pure functions of the solved tree, so they are tested on hand-built ones.


@dataclass(frozen=True, eq=False)
class Readings:
    """What the reading rules see of a solved tree, per branch."""

    parents: list[int]  # -1 at a root; parents before children
    at: list[np.ndarray]  # mm from the ostium to each station, along the tree
    reference: list[np.ndarray]  # reference diameter per station, mm
    shown: list[np.ndarray]  # reported stations
    reason: list[str | None]  # why FFR is not reported on the branch


def reported(ffr, reference_diameter):
    """Stations whose FFR is read: solved, on a vessel at least 1.8 mm across."""
    return np.isfinite(ffr) & (np.asarray(reference_diameter) >= REPORT_DIAMETER)


def listed(lesions, reference_diameter):
    """Lesions whose reference diameter at the throat is at least 1.8 mm, as FFR is reported.

    A thinner one still has its expansion loss in the solve; it is just not listed.
    """
    return [lesion for lesion in lesions if reference_diameter[lesion.branch][lesion.throat] >= REPORT_DIAMETER]


def distal(shown, reason):
    """(station, note): a branch's last reported station, or None with why."""
    found = np.flatnonzero(shown)
    if len(found):
        return int(found[-1]), None
    return None, reason or "reference under 1.8 mm"


def beyond(readings, branch, end):
    """(branch, station, note) where a lesion ending `end` mm from the ostium is read.

    The station 20 mm on along the tree, taking the daughter with the larger reference at each
    junction. Where the vessel ends first, or that station is not reported, the last reported
    station walked past since the lesion's end, with a note saying how far on it is. With none,
    the note says why.
    """
    target, walked, b = end + REPORT_BEYOND, [], branch
    while True:
        at = readings.at[b]
        on = np.flatnonzero(at >= end - 1e-9)
        far = np.flatnonzero(at >= target)
        if len(far):
            walked.append((b, on[on <= far[0]]))
            if readings.shown[b][far[0]]:
                return b, int(far[0]), None
            why = readings.reason[b] or "under 1.8 mm"
            break
        walked.append((b, on))
        children = [child for child, parent in enumerate(readings.parents) if parent == b]
        if not children:
            # A leaf whose FFR is withheld, such as one ending at a lumen gap, says so.
            why = readings.reason[b] or "vessel ends"
            break
        b = max(children, key=lambda child: np.median(readings.reference[child][readings.at[child] - readings.at[child][0] <= _DAUGHTER]))
    for b, stations in reversed(walked):
        shown = stations[readings.shown[b][stations]]
        if len(shown):
            return b, int(shown[-1]), f"read {readings.at[b][shown[-1]] - end:.1f} mm beyond the lesion: {why}"
    return None, None, readings.reason[walked[-1][0]] or "no reported station beyond the lesion"


def before(readings, branch, start):
    """(branch, station): the last reported station before a lesion starting `start` mm from the ostium."""
    b, limit = branch, start
    while b >= 0:
        shown = np.flatnonzero(readings.shown[b] & (readings.at[b] < limit))
        if len(shown):
            return b, int(shown[-1])
        b, limit = readings.parents[b], math.inf
    return None, None
