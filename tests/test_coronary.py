"""Research-only coronary analysis: the job on a synthetic Y over HTTP, and the reading rules."""

from dataclasses import replace
import json
from pathlib import Path

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest
from synthetic_vessels import GRIDS, THICK, Phantom, Scan, Stenosis, Vessel

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.coronary import Readings, before, beyond, distal, listed, reported
from scan3d.ffr_physics import Lesion

Y = Phantom(
    (Vessel(1.5, 15), Vessel(1.2, 20, start=(15, 0, 0), heading=35), Vessel(1.2, 20, start=(15, 0, 0), heading=-35)),
    joints=(((15, 0, 0), 1.5),),
)
NARROWED = Phantom(
    (Vessel(1.5, 15), Vessel(1.2, 20, start=(15, 0, 0), heading=35, stenoses=(Stenosis(8, 6, 0.6),)), Vessel(1.2, 20, start=(15, 0, 0), heading=-35)),
    joints=(((15, 0, 0), 1.5),),
)
STUBBED = Phantom((Vessel(1.5, 40), Vessel(1.3, 4, start=(12, 0, 0), heading=60)), joints=(((12, 0, 0), 1.3),))
LUMEN, AORTA, MYOCARDIUM = "imagecasx_coronary:coronary_arteries", "total:aorta", "heartchambers_highres:heart_myocardium"
RUN = {"lumen": LUMEN, "aorta": AORTA, "lv_mass": 150, "sbp": 120, "dbp": 80, "heart_rate": 60, "share": 1 / 3}


@pytest.fixture
def rig(tmp_path, request):
    """A saved revision holding a phantom's lumen, its aorta box and the rest as myocardium on a sheared grid.

    The phantom is the Y unless the test passes another. Its CT is the masks at two flat values, or
    with a `Scan` the phantom as imaged, under the tight mask a model might draw, on another grid
    if one follows. No lane runs; the test does.
    """
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", allowed_hosts=("127.0.0.1",)))
    service = app.state.service
    case = service.new_case("Synthetic coronary Y")
    folder = service.store.case_dir(case["id"]) / "synthetic"
    folder.mkdir()
    phantom, grid = getattr(request, "param", Y), GRIDS["0.35x0.35x0.5 sheared"]
    if isinstance(phantom, tuple):
        image = phantom[0].image(phantom[2] if len(phantom) > 2 else grid, phantom[1])
        lumen, aorta, affine, hu = image.thin, image.aorta, image.affine, image.ct
    else:
        lumen, aorta, affine = phantom.masks(grid)
        hu = np.where(lumen | aorta, 400, -50).astype(np.int16)

    def save(name, values):
        image = nib.Nifti1Image(values, affine)
        image.header.set_xyzt_units("mm")
        nib.save(image, folder / name)
        return folder / name

    ct = save("reference.nii.gz", hu)
    structures = [
        dict(id=sid, name=sid, task=sid.split(":")[0], group="Vessels", color="#d04040", mask_file=str(save(f"{n}.nii.gz", mask.astype(np.uint8))))
        for n, (sid, mask) in enumerate(((LUMEN, lumen), (AORTA, aorta), (MYOCARDIUM, ~(lumen | aorta))))
    ]
    revision = service.build_revision(case["id"], ct, structures, "synthetic_demo")
    service.store.commit_revision(revision)
    route = f"/api/cases/{case['id']}/revisions/{revision['id']}/coronary-analysis"
    return TestClient(app, base_url="http://127.0.0.1"), service, case["id"], revision["id"], route


def test_a_y_runs_once_reads_both_daughters_and_makes_no_revision(rig):
    http, service, case_id, revision_id, route = rig
    revisions = service.store.list("revisions", case_id)
    queued = http.post(route, json=RUN).json()
    assert queued["kind"] == "coronary_analysis" and queued["revision_id"] == revision_id and "payload" not in queued
    assert http.post(route, json=RUN).json()["id"] == queued["id"]
    claimed = service.store.claim("cpu")
    assert claimed["id"] == queued["id"]
    service.execute(claimed)
    job = service.store.get("jobs", queued["id"])
    assert job["status"] == "completed", job["error"]
    assert http.post(route, json=RUN).json()["id"] == queued["id"]

    response = http.get(f"{route}/{queued['id']}")
    assert response.status_code == 200 and "no-store" in response.headers["cache-control"]
    result = response.json()
    assert result["format"] == "scan3d-coronary-v1" and result["notice"] == "Research use only. Not for diagnostic use."
    assert (result["analysis_id"], result["revision_id"], result["inputs"]["lv_mass_source"]) == (queued["id"], revision_id, "typed")
    assert [root["source"] for root in result["roots"]] == ["aorta"] and result["lesions"] == []
    daughters = [branch for branch in result["branches"] if branch["parent"] >= 0]
    assert len(result["branches"]) == 3 and all(0.9 < branch["distal"]["ffr"] <= 1 for branch in daughters)
    (listed,) = http.get(route).json()["analyses"]
    assert listed["analysis_id"] == queued["id"] and listed["summary"] == job["result"]["summary"]
    assert listed["summary"]["branches"] == 3 and listed["summary"]["lowest_distal_ffr"] > 0.9
    assert service.store.list("revisions", case_id) == revisions
    assert service.store.get("cases", case_id)["active_revision_id"] == revision_id


def test_one_tree_from_an_ostium_point_and_myocardium_reports_no_ffr_without_a_share(rig):
    http, service, _, _, route = rig
    root = Y.vessels[0].axis(np.array([0.0]))[0].tolist()
    run = {"lumen": LUMEN, "myocardium": MYOCARDIUM, "ostia": [root]}
    queued = http.post(route, json=run).json()
    service.execute(service.store.claim("cpu"))
    assert service.store.get("jobs", queued["id"])["status"] == "completed"
    result = http.get(f"{route}/{queued['id']}").json()
    lumen, aorta, affine = Y.masks(GRIDS["0.35x0.35x0.5 sheared"])
    mass = round(np.count_nonzero(~(lumen | aorta)) * abs(np.linalg.det(affine[:3, :3])) / 1000 * 1.05, 1)
    assert (result["inputs"]["lv_mass"], result["inputs"]["lv_mass_source"]) == (mass, "myocardium mask")
    assert [root["source"] for root in result["roots"]] == ["ostium point"]
    # Unreported FFR is null, never 0, which a reader would take for severe ischaemia.
    assert {(branch["distal"]["ffr"], branch["distal"]["note"]) for branch in result["branches"]} == {(None, "one coronary tree only")}
    assert all(set(branch["stations"]["ffr"]) == {None} for branch in result["branches"])
    assert http.post(route, json={**run, "share": 0.5}).json()["id"] != queued["id"]


@pytest.mark.parametrize("rig", [NARROWED], indirect=True, ids=["narrowed daughter"])
def test_a_narrowed_daughter_reads_a_drop_across_its_lesion(rig):
    http, service, _, _, route = rig
    queued = http.post(route, json=RUN).json()
    service.execute(service.store.claim("cpu"))
    assert service.store.get("jobs", queued["id"])["status"] == "completed"
    (lesion,) = http.get(f"{route}/{queued['id']}").json()["lesions"]
    assert lesion["beyond"]["ffr"] is not None and lesion["delta_ffr"] > 0
    for read in (lesion["beyond"], lesion["before"]):
        assert read["low"] <= read["ffr"] <= read["high"] and read["indeterminate"] == (read["low"] <= 0.8 < read["high"])


@pytest.mark.parametrize("rig", [(Phantom((Vessel(1.7, 25),)), Scan(seed=6))], indirect=True, ids=["imaged vessel under a tight mask"])
def test_the_ct_corrects_the_masks_widths_unless_the_mask_is_asked_for(rig):
    http, service, case_id, _, route = rig
    jobs, read = {}, {}
    for width in ("ct", "mask"):
        jobs[width] = http.post(route, json={**RUN, "lumen_width": width}).json()["id"]
        service.execute(service.store.claim("cpu"))
        read[width] = http.get(f"{route}/{jobs[width]}").json()
        assert read[width]["inputs"]["lumen_width"] == read[width]["lumen"]["width"] == width
        assert not [flag for flag in read[width]["flags"] if "mask's own" in flag or "calcium" in flag]
        # NaN is not JSON: a station the CT did not read says null.
        saved = service.store.case_dir(case_id) / "coronary" / read[width]["key"] / "result.json"
        json.loads(saved.read_text(), parse_constant=lambda name: pytest.fail(f"{name} in the saved result"))
    # The CT is the default, and each choice is its own run.
    assert http.post(route, json=RUN).json()["id"] == jobs["ct"] != jobs["mask"]
    ct, mask = (read[width]["branches"][0]["stations"] for width in ("ct", "mask"))
    count = len(mask["s"])
    assert read["mask"]["lumen"] == {"width": "mask", "calibrated": False, "offset": None, "blur": None, "read": 0, "stations": count}
    assert mask["diameter"] == mask["mask_diameter"] == ct["mask_diameter"] and mask["ct_diameter"] == [None] * count and not any(mask["calcium"])
    found = read["ct"]["lumen"]
    assert found["calibrated"] and found["stations"] == count and found["read"] == sum(value is not None for value in ct["ct_diameter"]) >= 40
    assert None in ct["ct_diameter"] and not any(ct["calcium"])
    # The mask is drawn 0.2 mm inside the 3.4 mm vessel's blurred edge all round, which is most of the offset the CT finds.
    assert 0.4 < found["offset"] < 0.8 and 0.9 < found["blur"] < 1.5
    trusted = ~np.array(ct["held"])
    assert np.array(ct["diameter"])[trusted] - np.array(ct["mask_diameter"])[trusted] == pytest.approx(found["offset"], abs=0.15)
    assert np.median(np.array(ct["diameter"])[trusted]) == pytest.approx(3.4, abs=0.15)
    assert np.median(np.array(mask["diameter"])[trusted]) < 3.0
    assert http.post(route, json={**RUN, "lumen_width": "voxels"}).status_code == 422


@pytest.mark.parametrize("rig", [Phantom((Vessel(1.5, 9),))], indirect=True, ids=["a vessel too short to correct"])
def test_too_few_stations_for_the_ct_leave_the_masks_widths_and_say_so(rig):
    http, service, _, _, route = rig
    queued = http.post(route, json=RUN).json()
    service.execute(service.store.claim("cpu"))
    result = http.get(f"{route}/{queued['id']}").json()
    found, (branch,) = result["lumen"], result["branches"]
    assert found["read"] < 20 and found["stations"] == len(branch["stations"]["s"])
    assert found == {"width": "ct", "calibrated": False, "offset": None, "blur": None, "read": found["read"], "stations": found["stations"]}
    why = f"the CT gave the lumen's edge at only {found['read']} of {found['stations']} stations, too few to correct them"
    assert f"lumen widths are the mask's own: {why}" in result["flags"]
    assert branch["stations"]["diameter"] == branch["stations"]["mask_diameter"]


TWICE = Phantom(
    (Vessel(1.8, 16), Vessel(1.3, 24, start=(16, 0, 0), heading=35), Vessel(1.05, 24, start=(16, 0, 0), heading=-35)),
    joints=(((16, 0, 0), 1.8),),
)


@pytest.mark.parametrize("rig", [(TWICE, Scan(seed=21), THICK)], indirect=True, ids=["a daughter lying in 1 mm slices"])
def test_the_ct_is_not_read_where_lumen_touches_lumen_far_along_the_tree(rig):
    """The daughter is traced as two branches that each hold part of its lumen, which the contact rule marks: read, each would take its whole width."""
    http, service, _, _, route = rig
    queued = http.post(route, json=RUN).json()
    service.execute(service.store.claim("cpu"))
    result = http.get(f"{route}/{queued['id']}").json()
    marks = [pair for branch in result["branches"] for pair in zip(branch["stations"]["contact"], branch["stations"]["ct_diameter"])]
    assert result["lumen"]["calibrated"] and sum(touching for touching, _ in marks) > 10
    assert not [read for touching, read in marks if touching and read is not None]


@pytest.mark.parametrize("rig", [STUBBED], indirect=True, ids=["clipped side stub"])
def test_a_clipped_side_stub_is_left_out_and_flagged(rig):
    http, service, _, _, route = rig
    queued = http.post(route, json=RUN).json()
    service.execute(service.store.claim("cpu"))
    result = http.get(f"{route}/{queued['id']}").json()
    assert len(result["branches"]) == 1
    assert any(
        flag.startswith("end branches under 8 mm and short for their width, left out of the tree and the flow split as clipped side branches: 1 (")
        for flag in result["flags"]
    )


def test_a_run_with_no_aorta_and_no_ostium_is_refused(rig):
    http, service, _, _, route = rig
    response = http.post(route, json={"lumen": LUMEN, "lv_mass": 150})
    assert response.status_code == 400 and response.json()["detail"] == "Choose the aorta or add an ostium point"
    assert http.post(route, json={**RUN, "aorta": "total:heart"}).status_code == 400
    assert http.post(route, json={**RUN, "sbp": 70, "dbp": 90}).status_code == 400
    assert service.store.claim("cpu") is None


def test_an_ostium_point_down_a_vessel_the_aorta_roots_is_refused(rig):
    """A root 10 mm down the trunk would split it and trace the stretch above it backwards."""
    http, service, _, _, route = rig
    point = Y.vessels[0].axis(np.array([10.0]))[0].tolist()
    queued = http.post(route, json={**RUN, "ostia": [point]}).json()
    service.execute(service.store.claim("cpu"))
    job = service.store.get("jobs", queued["id"])
    assert job["status"] == "failed" and "partway along a vessel already traced from the aorta, so it would split that vessel" in job["error"]


def test_reading_rules_on_a_hand_built_tree():
    """A 20 mm trunk forks into a daughter 3.0 mm across that thins under 1.8 mm 4 mm on, and a 2.0 mm one past a lumen gap."""
    s = [np.arange(21.0), np.arange(11.0), np.arange(16.0)]
    reference = [np.full(21, 3.0), np.where(s[1] <= 4, 3.0, 1.6), np.full(16, 2.0)]
    ffr = [1 - 0.005 * s[0], 0.9 - 0.01 * s[1], np.full(16, np.nan)]
    shown = [reported(f, r) for f, r in zip(ffr, reference)]
    readings = Readings([-1, 0, 0], [s[0], 20 + s[1], 20 + s[2]], reference, shown, [None, None, "FFR not reported: lumen gap"])
    assert [distal(shown[b], readings.reason[b]) for b in range(3)] == [(20, None), (4, None), (None, "FFR not reported: lumen gap")]
    assert beyond(readings, 0, 3.0) == (1, 3, None)
    assert beyond(readings, 0, 8.0) == (1, 4, "read 16.0 mm beyond the lesion: under 1.8 mm")
    assert beyond(readings, 0, 18.5) == (1, 4, "read 5.5 mm beyond the lesion: vessel ends")
    assert beyond(readings, 2, 21.0) == (None, None, "FFR not reported: lumen gap")
    assert beyond(readings, 1, 25.0) == (None, None, "no reported station beyond the lesion")
    # With the gap daughter the larger, the walk ends at the gap and the note says so.
    wider = replace(readings, reference=[*reference[:2], np.full(16, 4.0)])
    assert beyond(wider, 0, 18.5) == (0, 20, "read 1.5 mm beyond the lesion: FFR not reported: lumen gap")
    assert before(readings, 0, 5.5) == (0, 5)
    assert before(readings, 2, 21.0) == (0, 20)


def test_lesions_on_vessels_under_1_8_mm_are_not_listed():
    reference = [np.full(21, 3.0), np.where(np.arange(11.0) <= 4, 3.0, 1.6)]
    found = [Lesion(branch, throat, 0.0, 1.0, 1.0, 1.0, 0.5, False) for branch, throat in ((0, 5), (1, 8), (1, 2))]
    assert [(lesion.branch, lesion.throat) for lesion in listed(found, reference)] == [(0, 5), (1, 2)]
