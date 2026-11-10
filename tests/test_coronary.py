"""Research-only coronary analysis: the job on a synthetic Y over HTTP, and the reading rules."""

from dataclasses import replace
from pathlib import Path

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest
from synthetic_vessels import GRIDS, Phantom, Stenosis, Vessel

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
LUMEN, AORTA, MYOCARDIUM = "imagecasx_coronary:coronary_arteries", "total:aorta", "heartchambers_highres:heart_myocardium"
RUN = {"lumen": LUMEN, "aorta": AORTA, "lv_mass": 150, "sbp": 120, "dbp": 80, "heart_rate": 60, "share": 1 / 3}


@pytest.fixture
def rig(tmp_path, request):
    """A saved revision holding a phantom's lumen, its aorta box and the rest as myocardium on a sheared grid.

    The phantom is the Y unless the test passes another. No lane runs; the test does.
    """
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", allowed_hosts=("127.0.0.1",)))
    service = app.state.service
    case = service.new_case("Synthetic coronary Y")
    folder = service.store.case_dir(case["id"]) / "synthetic"
    folder.mkdir()
    lumen, aorta, affine = getattr(request, "param", Y).masks(GRIDS["0.35x0.35x0.5 sheared"])

    def save(name, values):
        image = nib.Nifti1Image(values, affine)
        image.header.set_xyzt_units("mm")
        nib.save(image, folder / name)
        return folder / name

    ct = save("reference.nii.gz", np.where(lumen | aorta, 400, -50).astype(np.int16))
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
