"""The MCP server's tools against a real app on the synthetic demo, without the MCP SDK."""

import numpy as np
import pytest
from fastapi.testclient import TestClient
from test_coronary import RUN, rig

from scan3d.api import create_app
from scan3d.config import Settings
from scripts.scan3d_mcp import ApiError, Scan3D, Tools, native_drawing


@pytest.fixture
def tools(tmp_path):
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", allowed_hosts=("127.0.0.1",))
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as http:
        api = Scan3D(http=http)
        api.poll_seconds = 0.05
        yield Tools(api)


def pixel(description, r, a):
    """The view_slice_image pixel of an axial RAS point, through the matrix the tool reports."""
    matrix = np.asarray(description["pixel_to_ras"])
    return [(r - matrix[0][3]) / matrix[0][0], (a - matrix[1][3]) / matrix[1][1]]


def test_view_mark_correct_save_and_undo(tools):
    case = tools.create_demo_case()
    case_id, first = case["id"], case["active_revision_id"]
    liver = next(s for s in tools.get_revision(case_id)["structures"] if s["name"] == "Liver")

    view = tools.view_slice_image(case_id, "axial", -55.0)
    assert view.png.startswith(b"\x89PNG\r\n\x1a\n")
    described = view.description
    assert described["edges"] == {"left": "R", "right": "L", "top": "A", "bottom": "P"}
    assert any(row["id"] == liver["id"] and row["area_mm2"] > 0 for row in described["visible_structures"])

    # A slice point lands on the voxel the image shows: the phantom liver is 105 HU.
    draft = tools.start_correction(case_id, liver["id"], method="direct")
    point = {"kind": "point", "slice": {"plane": "axial", "position_mm": -55.0, "x": pixel(described, 40, 5)[0], "y": pixel(described, 40, 5)[1]}}
    ras = tools.api.slice_to_ras(case_id, first, "axial", -55.0, [[point["slice"]["x"], point["slice"]["y"]]])[0]
    assert np.allclose(ras, [40, 5, -55], atol=1e-6)
    assert tools.probe(case_id, ras)["hu"] == 105

    # Paint outward from the liver's left edge, on the image's own pixels.
    stroke = [pixel(described, -20, 5), pixel(described, -40, 5)]
    draft = tools.add_marks(case_id, draft["id"], [{"kind": "scribble", "slice": {"plane": "axial", "position_mm": -55.0, "points": stroke}}])
    assert draft["marks"] == [{"id": "mcp-1", "kind": "scribble", "include": True}]
    ran = tools.run_correction(case_id, draft["id"])
    assert ran["job"]["status"] == "completed", ran
    summary = ran["draft"]["candidate"]["summary"]
    assert summary["added_voxels"] > 0 and summary["removed_voxels"] == 0
    # The default candidate view draws the changed mask, though a candidate gives it no bounds.
    compare = tools.view_slice_image(case_id, "axial", -55.0, revision_id=ran["draft"]["candidate"]["revision_id"], compare=True)
    row = next(row for row in compare.description["visible_structures"] if row["id"] == liver["id"])
    assert row["area_mm2"] > row["parent_area_mm2"]

    saved = tools.save_correction(case_id, draft["id"])
    assert saved["job"]["status"] == "completed", saved
    corrected = tools.get_revision(case_id)
    assert corrected["id"] == saved["draft"]["accepted_revision_id"] != first
    assert next(s for s in corrected["structures"] if s["id"] == liver["id"])["volume_ml"] > liver["volume_ml"]
    assert tools.activate_revision(case_id, first)["active_revision_id"] == first


def test_drawings_keep_the_view_plane_or_are_refused():
    demo = {"affine_ras": [[3, 0, 0, -120], [0, 3, 0, -120], [0, 0, 3, -168], [0, 0, 0, 1]], "shape": [80, 80, 112]}
    # A left-right stroke lies on an axial and a coronal native slice; drawn on an axial view, it stays axial.
    assert native_drawing(demo, [[-20, 5, -55], [-40, 5, -55]], "axial")[:2] == (2, 38)
    tilt = np.deg2rad(23)
    rotation = np.array([[np.cos(tilt), 0, np.sin(tilt)], [0, 1, 0], [-np.sin(tilt), 0, np.cos(tilt)]])
    tilted = {"affine_ras": np.block([[rotation * 3, np.zeros((3, 1))], [np.zeros((1, 3)), np.ones((1, 1))]]).tolist(), "shape": [80, 80, 112]}
    with pytest.raises(ApiError, match="one slice"):
        native_drawing(tilted, [[20, 5, 55], [40, 5, 55]], "axial")


def test_coronary_analysis_runs_lists_and_reads_one_branch_in_full(rig):
    http, service, case_id, revision_id, _ = rig
    tools = Tools(Scan3D(http=http))
    assert tools.run_coronary_analysis(case_id, revision_id=revision_id, wait_seconds=0, **RUN)["job"]["status"] == "queued"
    service.execute(service.store.claim("cpu"))
    # The same inputs return the same run, now complete, read without its station arrays.
    result = tools.run_coronary_analysis(case_id, revision_id=revision_id, wait_seconds=0, **RUN)
    assert result["notice"] == "Research use only. Not for diagnostic use." and len(result["branches"]) == 3 and "stations" not in result
    (listed,) = tools.list_coronary_analyses(case_id, revision_id)["analyses"]
    daughter = next(branch["id"] for branch in result["branches"] if branch["parent"] >= 0)
    one = tools.get_coronary_analysis(case_id, listed["analysis_id"], revision_id, station_branches=[daughter])
    assert list(one["stations"]) == [str(daughter)] and len(one["stations"][str(daughter)]["ffr"]) > 1
    with pytest.raises(ApiError, match="No such branch"):
        tools.get_coronary_analysis(case_id, listed["analysis_id"], revision_id, station_branches=[99])
