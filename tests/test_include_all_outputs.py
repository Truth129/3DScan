"""Restoring previously filtered labels must preserve reviewed mask revisions."""

import hashlib
import json
from pathlib import Path

import nibabel as nib
import numpy as np
import pytest

from scan3d import segmentation
from scan3d.config import Settings
from scan3d.service import Service, atomic_json
from scan3d.store import identifier


@pytest.fixture
def saved_run(tmp_path, monkeypatch):
    service = Service(Settings(tmp_path / "data", (tmp_path,)))
    case = service.new_case("Previously filtered native output fixture")
    case_dir = service.store.case_dir(case["id"])
    source = case_dir / "converted"
    source.mkdir()
    affine = np.array([[-2.0, 0, 0, 30], [0, 3.0, 0, -25], [0, 0, 4.0, -40], [0, 0, 0, 1]])
    shape = (18, 18, 18)
    ct = source / "reference.nii.gz"
    nib.save(nib.Nifti1Image(np.full(shape, 45, np.int16), affine), ct)
    run = case_dir / "inference" / ("a" * 64) / ("run-" + identifier())
    masks = run / "total"
    masks.mkdir(parents=True)
    native_liver = np.zeros(shape, np.uint8)
    native_liver[3:9, 3:9, 3:9] = 1
    skull = np.zeros(shape, np.uint8)
    skull[4:12, 4:12, 11:15] = 1
    for name, values in (("liver", native_liver), ("skull", skull), ("spleen", np.zeros(shape, np.uint8))):
        nib.save(nib.Nifti1Image(values, affine), masks / f"{name}.nii.gz")
    manifest = {
        "schema_version": 1,
        "engine": "TotalSegmentator",
        "status": "completed",
        "profile": "chest_abdomen",
        "run_id": run.name,
        "input_sha256": hashlib.sha256(ct.read_bytes()).hexdigest(),
        "tasks": [{"task": "total", "status": "completed", "labels": ["liver", "skull", "spleen"]}],
        "empty_labels": ["total:spleen"],
        "structure_count": 1,
        "qc": ["Empty label omitted: total:spleen; inspect scan coverage"],
    }
    atomic_json(run / "inference.json", manifest)
    group, color = segmentation._appearance("liver")
    liver = dict(id="total:liver", name="Liver", task="total", group=group, color=color, mask_file=str(masks / "liver.nii.gz"))
    acquisition = dict(profile="chest_abdomen", series_uid="fixture-series", phase="venous")
    original = service.build_revision(case["id"], ct, [liver], source="totalsegmentator", acquisition=acquisition, inference_manifest=manifest)
    service.store.commit_revision(original)

    # Simulate a saved Slicer correction that differs from the native model mask.
    correction = case_dir / "corrections" / "liver.nii.gz"
    correction.parent.mkdir()
    corrected_liver = native_liver.copy()
    corrected_liver[3:5] = 0
    nib.save(nib.Nifti1Image(corrected_liver, affine), correction)
    corrected = dict(liver, mask_file=str(correction))
    parent = service.build_revision(
        case["id"], ct, [corrected], source="slicer_correction", parent=original["id"], acquisition=acquisition, inference_manifest=manifest
    )
    parent["review_status"] = "corrected"
    atomic_json(Path(parent["directory"]) / "revision.json", parent)
    service.store.commit_revision(parent)

    def no_inference(*args, **kwargs):
        pytest.fail("Restoring saved outputs must never invoke model inference or GPU setup")

    monkeypatch.setattr(segmentation, "segment", no_inference)
    monkeypatch.setattr(segmentation, "model_fingerprint", no_inference)
    monkeypatch.setattr(segmentation.subprocess, "Popen", no_inference)
    return service, case, parent, original, run, manifest, corrected_liver


def execute_next(service, case_id):
    queued = service.include_all_outputs(case_id)
    claimed = service.store.claim()
    assert claimed["id"] == queued["id"] and claimed["kind"] == "include_all"
    service.execute(claimed)
    return service.store.get("jobs", queued["id"])


def test_saved_outputs_add_skull_without_replacing_corrections_and_allow_exact_undo(saved_run):
    service, case, parent, original, run, manifest, corrected_liver = saved_run
    parent_snapshot = service.store.revision(case["id"], parent["id"])
    native_before = {path: path.read_bytes() for path in run.rglob("*") if path.is_file()}
    correction_path = Path(parent["structures"][0]["mask_file"])
    correction_before = correction_path.read_bytes()

    job = execute_next(service, case["id"])
    assert job["status"] == "completed", job.get("error")
    revision = service.store.revision(case["id"], job["revision_id"])
    entries = {s["id"]: s for s in revision["structures"]}
    assert set(entries) == {"total:liver", "total:skull"}
    assert entries["total:liver"] == parent["structures"][0]
    assert entries["total:liver"]["volume_ml"] < original["structures"][0]["volume_ml"]
    assert entries["total:skull"]["group"] == "Bones"
    assert entries["total:skull"]["voxel_count"] == 8 * 8 * 4
    assert entries["total:skull"]["volume_ml"] == pytest.approx(8 * 8 * 4 * 24 / 1000)
    assert len(revision["surface_assets"]) == 2
    assert revision["source"] == "include_all_outputs"
    assert revision["parent_id"] == parent["id"]
    assert revision["review_status"] == "unreviewed"
    assert revision["acquisition"] == parent["acquisition"]
    assert revision["inference_manifest"] == manifest
    assert revision["operation"]["added_structure_ids"] == ["total:skull"]
    assert revision["operation"]["empty_labels"] == ["total:spleen"]
    assert revision["operation"]["source_run_id"] == run.name
    assert revision["operation"]["code_version"]
    assert len(revision["operation"]["code_sha256"]) == 64
    assert service.store.get("cases", case["id"])["active_revision_id"] == revision["id"]

    assert service.store.revision(case["id"], parent["id"]) == parent_snapshot
    assert correction_path.read_bytes() == correction_before
    assert np.array_equal(np.asarray(nib.load(correction_path).dataobj), corrected_liver)
    assert {path: path.read_bytes() for path in run.rglob("*") if path.is_file()} == native_before
    service.store.activate(case["id"], parent["id"])
    assert service.store.get("cases", case["id"])["active_revision_id"] == parent["id"]
    assert service.store.revision(case["id"], parent["id"]) == parent_snapshot


@pytest.mark.parametrize("missing", ["inference.json", "total/skull.nii.gz", "total/liver.nii.gz"])
def test_missing_native_source_fails_without_creating_a_revision(saved_run, missing):
    service, case, parent, _, run, _, _ = saved_run
    (run / missing).unlink()
    revisions_before = service.store.list("revisions", case["id"])
    job = execute_next(service, case["id"])
    assert job["status"] == "failed"
    assert "unavailable" in job["error"] and "no inference was rerun" in job["error"]
    assert service.store.list("revisions", case["id"]) == revisions_before
    assert service.store.get("cases", case["id"])["active_revision_id"] == parent["id"]


def test_manifest_for_another_ct_cannot_supply_extra_anatomy(saved_run):
    service, case, parent, _, run, manifest, _ = saved_run
    manifest["input_sha256"] = "0" * 64
    atomic_json(run / "inference.json", manifest)
    parent["inference_manifest"] = manifest
    service.store.save("revisions", parent)
    job = execute_next(service, case["id"])
    assert job["status"] == "failed" and "different reference CT" in job["error"]
    assert service.store.get("cases", case["id"])["active_revision_id"] == parent["id"]


def test_active_revision_changed_during_restore_is_not_overwritten(saved_run, monkeypatch):
    service, case, parent, original, _, _, _ = saved_run
    build_revision = service.build_revision

    def change_active_revision(*args, **kwargs):
        result = build_revision(*args, **kwargs)
        # A reviewer switches revisions while the worker is preparing surfaces.
        service.store.activate(case["id"], original["id"])
        return result

    monkeypatch.setattr(service, "build_revision", change_active_revision)
    revisions_before = service.store.list("revisions", case["id"])
    job = execute_next(service, case["id"])
    assert job["status"] == "failed" and "active revision changed" in job["error"].lower()
    assert service.store.get("cases", case["id"])["active_revision_id"] == original["id"]
    assert service.store.list("revisions", case["id"]) == revisions_before
