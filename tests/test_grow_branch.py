"""Grow branch on synthetic volumes: the scored rule, its limits, its refusals, and the stored probability."""

import json
import sys

import nibabel as nib
import numpy as np
import pytest
from scipy import ndimage as ndi
from fastapi.testclient import TestClient

from scan3d import grow_branch, nnunet_models, nnunet_worker
from scan3d.api import create_app
from scan3d.config import Settings

AFFINE = np.diag([0.5, 0.5, 0.5, 1.0])


def level(p):
    return round(p * 255)


@pytest.fixture
def tube():
    """A trunk along k, a branch along j with a weak gap, and a bright blob beside the branch's far end."""
    mask = np.zeros((40, 40, 40), bool)
    mask[19:22, 19:22, :] = True  # 0.5 mm voxels, so the trunk's surface is at j = 21.
    probability = np.zeros(mask.shape, np.uint8)
    probability[mask] = level(0.9)
    probability[20, 22:36, 20] = level(0.3)
    probability[20, 28, 20] = level(0.07)  # Under 10 %, over 5 %.
    probability[20, 31:34, 22] = level(0.9)  # One empty voxel away from the branch at k = 21.
    return mask, probability


def grown_j(region):
    assert region is not None and set(map(tuple, np.argwhere(region)[:, [0, 2]])) == {(20, 20)}
    return sorted(np.argwhere(region)[:, 1].tolist())


def test_the_rule_stops_at_a_gap_skips_the_mask_halo_and_leaves_a_distractor(tube):
    mask, probability = tube
    # j = 22 and 23 lie within 1 mm of the trunk, so the branch starts at j = 24; the gap at 28 stops 10 %.
    assert grown_j(grow_branch.grow(mask, probability, AFFINE, (20, 25, 20), 0.10)) == [24, 25, 26, 27]
    assert grown_j(grow_branch.grow(mask, probability, AFFINE, (20, 25, 20), 0.05)) == list(range(24, 36))
    # A click inside the excluded halo seeds from the best allowed voxel within 1.5 mm.
    assert grown_j(grow_branch.grow(mask, probability, AFFINE, (20, 22, 20), 0.10)) == [24, 25, 26, 27]
    # Nothing near enough: no seed, nothing grows.
    assert grow_branch.grow(mask, probability, AFFINE, (5, 5, 5), 0.10) is None


def test_the_exclusion_is_one_millimetre_through_the_affine():
    mask = np.zeros((15, 15, 15), bool)
    mask[7, 7, 7] = True
    for affine in (np.diag([0.5, 0.5, 2.0, 1.0]), np.diag([0.3, 0.7, 0.45, 1.0])):
        spacing = np.diag(affine)[:3]
        expected = ndi.distance_transform_edt(~mask, sampling=spacing) <= 1.0
        assert np.array_equal(grow_branch._near(mask, affine, 1.0), expected)
    # A sheared grid: a voxel's mm distance comes from the full matrix, never from the axis spacings.
    sheared = np.array([[0.5, 0.4, 0, 0], [0, 0.5, 0, 0], [0, 0, 0.5, 0], [0, 0, 0, 1.0]])
    near = grow_branch._near(mask, sheared, 1.0)
    assert near[6, 8, 7] and not near[8, 8, 7]  # 0.51 mm and 1.03 mm from the centre.


def write(path, values, affine=AFFINE):
    nib.save(nib.Nifti1Image(values.astype(np.uint8), affine), path)
    return path


def test_grow_mask_refuses_nothing_too_much_and_a_missing_probability(tube, tmp_path):
    mask, probability = tube
    ct, mask_path = write(tmp_path / "ct.nii.gz", np.zeros(mask.shape)), write(tmp_path / "mask.nii.gz", mask)
    blob = probability.copy()
    blob[0:17, 0:17, 0:17] = level(0.5)  # 4,913 voxels of 0.125 µL: 0.61 mL.
    p_path = write(tmp_path / "p.nii.gz", blob)
    point = lambda ijk: (np.asarray(ijk) * 0.5).tolist()  # noqa: E731
    with pytest.raises(ValueError, match="CAS-Net saw nothing here"):
        grow_branch.grow_mask(ct, mask_path, p_path, point((30, 5, 30)), 0.10, tmp_path / "out.nii.gz")
    with pytest.raises(ValueError, match=r"would add 0\.61 mL, over its 0\.5 mL limit"):
        grow_branch.grow_mask(ct, mask_path, p_path, point((5, 5, 5)), 0.10, tmp_path / "out.nii.gz")
    with pytest.raises(ValueError, match="Run CAS-Net on this scan again"):
        grow_branch.grow_mask(ct, mask_path, None, point((20, 25, 20)), 0.10, tmp_path / "out.nii.gz")
    assert not (tmp_path / "out.nii.gz").exists()
    result = grow_branch.grow_mask(ct, mask_path, p_path, point((20, 25, 20)), 0.10, tmp_path / "out.nii.gz")
    saved = np.asarray(nib.load(tmp_path / "out.nii.gz").dataobj).astype(bool)
    expected = mask.copy()
    expected[20, 24:28, 20] = True
    assert result["added_voxels"] == 4 and np.array_equal(saved, expected)


def test_the_worker_stores_cas_net_probability_on_the_reference_grid_beside_an_unchanged_mask(tmp_path, monkeypatch):
    affine = np.array([[-0.7, -0.15, 0.0, 34.0], [0.1, 0.9, 0.2, -71.0], [0.0, 0.1, 1.8, 14.0], [0.0, 0.0, 0.0, 1.0]])
    shape = (6, 7, 8)
    nib.save(nib.Nifti1Image(np.zeros(shape, np.int16), affine), tmp_path / "image.nii.gz")
    rng = np.random.default_rng(3)
    native = rng.integers(0, 256, shape).astype(np.uint8)
    model = nnunet_models.MODELS["imagecasx_cas_net"]
    _, _, back = nnunet_worker.model_input(nib.load(tmp_path / "image.nii.gz"), affine, model.axes)
    # The model grid's values, which the worker turns back to the file's voxel order.
    forward = nnunet_worker.model_input(nib.Nifti1Image(native.astype(np.float32), affine), affine, model.axes)[0][0]
    monkeypatch.setattr(nnunet_worker, "run", lambda *_: ((forward > 127).astype(np.uint8), "test", forward.astype(np.uint8)))
    out = tmp_path / "out"
    out.mkdir()
    argv = ["-i", str(tmp_path / "image.nii.gz"), "-o", str(out), "--task", model.id, "--model-folder", str(tmp_path), "--device", "cpu"]
    argv += ["--report", str(tmp_path / "report.json"), "--probability", str(tmp_path / "p.nii.gz")]
    monkeypatch.setattr(sys, "argv", ["nnunet_worker", *argv])
    nnunet_worker.main()
    stored = nib.load(tmp_path / "p.nii.gz")
    assert stored.get_data_dtype() == np.uint8 and np.allclose(stored.affine, affine)
    assert np.array_equal(np.asarray(stored.dataobj), native)
    # The mask is the network's own, written as before.
    mask = np.asarray(nib.load(out / f"{model.labels[0]}.nii.gz").dataobj)
    assert np.array_equal(mask, nnunet_worker.without_small_pieces(native > 127, model.smallest_piece))
    assert json.loads((tmp_path / "report.json").read_text())["classes"] == {"1": model.labels[0]}


def completed(client, job_id):
    import time

    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            assert job["status"] == "completed", job
            return job
        time.sleep(0.05)
    pytest.fail("Worker did not finish")


def test_grow_branch_saves_a_revision_only_for_a_segment_with_a_stored_probability(tmp_path):
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist")
    with TestClient(create_app(settings), base_url="http://127.0.0.1") as http:
        service = http.app.state.service
        case = http.post("/api/cases/demo").json()
        prefix, rid = f"/api/cases/{case['id']}", case["active_revision_id"]
        body = {"revision_id": rid, "structure_id": "synthetic:aorta", "point_ras": [6.0, -27.0, 0.0]}
        refused = http.post(f"{prefix}/grow-branch", json=body)
        assert refused.status_code == 400 and "no stored CAS-Net probability" in refused.json()["detail"]
        # The demo's aorta ends at i = 40 on row j = 31, k = 56; a three-voxel branch leaves it there.
        revision = service.store.revision(case["id"], rid)
        reference = nib.load(revision["ct_file"])
        probability = np.zeros(reference.shape, np.uint8)
        probability[41:44, 31, 56] = level(0.3)
        p_path = write(tmp_path / "p.nii.gz", probability, reference.affine)
        aorta = next(s for s in revision["structures"] if s["id"] == "synthetic:aorta")
        aorta["probability_file"] = str(p_path)
        service.store.save("revisions", revision)
        shown = next(s for s in http.get(f"{prefix}/revisions/{rid}").json()["structures"] if s["id"] == "synthetic:aorta")
        assert shown["growable"] is True and "probability_file" not in shown
        nothing = http.post(f"{prefix}/grow-branch", json={**body, "point_ras": [-60.0, 30.0, 0.0]})
        assert nothing.status_code == 400 and nothing.json()["detail"] == grow_branch.NOTHING
        job = completed(http, http.post(f"{prefix}/grow-branch", json={**body, "threshold": 0.05}).json()["id"])
        grown = http.get(f"{prefix}/revisions/{job['revision_id']}").json()
        before = next(s for s in http.get(f"{prefix}/revisions/{rid}").json()["structures"] if s["id"] == "synthetic:aorta")
        after = next(s for s in grown["structures"] if s["id"] == "synthetic:aorta")
        assert grown["source"] == "grow_branch" and grown["parent_id"] == rid and after["growable"] is True
        assert after["voxel_count"] == before["voxel_count"] + 3
        assert grown["operation"]["provenance"] == "Grow branch, CAS-Net probability ≥ 0.05"
        assert http.get(prefix).json()["active_revision_id"] == grown["id"]
