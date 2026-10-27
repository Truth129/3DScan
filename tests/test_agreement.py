"""Model-output agreement: exact metrics, lineage, caching and read-only HTTP use.

Masks are synthetic boxes and balls on isotropic, anisotropic, oblique, reflected
and sheared grids. Segmentation runs use a deterministic double of the worker
adapter around the real segment, refinement and direct-edit jobs; no model, GPU
or patient data is involved.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import time
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from scan3d import agreement, imaging, review_volume, segmentation
from scan3d.agreement import aggregate_agreement, mask_agreement, revision_agreement
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.demo import create_demo
from scan3d.service import Service
from scan3d.store import identifier


def physical(matrix):
    affine = np.eye(4)
    affine[:3, :3] = matrix
    affine[:3, 3] = (17., -31., 52.)
    return affine


def rotation(x, z):
    turn = np.array([[np.cos(z), -np.sin(z), 0], [np.sin(z), np.cos(z), 0], [0, 0, 1]])
    tilt = np.array([[1, 0, 0], [0, np.cos(x), -np.sin(x)], [0, np.sin(x), np.cos(x)]])
    return turn @ tilt


ORTHOGONAL = {"isotropic": np.eye(3) * .8, "anisotropic": np.diag([.7, 1.9, 3.1]),
              "oblique": rotation(.3, .4) @ np.diag([.7, 1.9, 3.1]), "reflected": np.diag([-.8, 1.2, -2.5])}
SHEARED = np.array([[1., .6, 0], [0, 1., .4], [0, 0, 2.]])


def box(low, high, shape=(20, 20, 20)):
    mask = np.zeros(shape, bool)
    mask[tuple(slice(int(a), int(b)) for a, b in zip(low, high))] = True
    return mask


def ball(center, radius, shape=(20, 20, 20)):
    return np.linalg.norm(np.moveaxis(np.indices(shape), 0, -1) - center, axis=-1) <= radius


def brute_hd95(original, current, matrix):
    """All-pairs reference with an independently written face-neighbour surface."""
    def surface(mask):
        padded = np.pad(mask, 1)
        exposed = np.zeros_like(mask)
        for axis in range(3):
            for step in (-1, 1):
                exposed |= ~np.roll(padded, step, axis)[1:-1, 1:-1, 1:-1]
        return np.argwhere(mask & exposed) @ matrix.T

    first, second = surface(original), surface(current)
    distances = np.linalg.norm(first[:, None] - second[None], axis=-1)
    return max(np.percentile(distances.min(axis=1), 95), np.percentile(distances.min(axis=0), 95))


@pytest.mark.parametrize("grid", ORTHOGONAL)
def test_shifted_box_has_known_dice_volumes_and_hd95_on_orthogonal_grids(grid):
    matrix = ORTHOGONAL[grid]
    original, current = box((4, 5, 6), (14, 15, 16)), box((6, 5, 6), (16, 15, 16))
    result = mask_agreement(original, current, physical(matrix))
    voxel_ml = abs(np.linalg.det(matrix)) / 1000
    assert result["dice"] == pytest.approx(.8)
    assert (result["added_voxels"], result["removed_voxels"], result["unchanged"]) == (200, 200, False)
    assert result["original_volume_ml"] == pytest.approx(1000 * voxel_ml) == result["current_volume_ml"]
    assert result["volume_change_ml"] == 0
    # Each trailing face is exactly two columns from the other surface; the
    # remaining surface distances are smaller, so both 95th percentiles are 2.
    assert result["hd95_mm"] == pytest.approx(2 * np.linalg.norm(matrix[:, 0]))
    assert result["hd95_mm"] == pytest.approx(brute_hd95(original, current, matrix))


def test_sheared_grid_uses_the_full_affine_metric_rather_than_axis_spacing():
    spacing = np.linalg.norm(SHEARED, axis=0)
    single = mask_agreement(box((5, 5, 5), (6, 6, 6)), box((8, 8, 5), (9, 9, 6)), physical(SHEARED))
    assert single["hd95_mm"] == pytest.approx(np.linalg.norm(SHEARED @ [3, 3, 0]))
    assert single["hd95_mm"] != pytest.approx(np.hypot(3 * spacing[0], 3 * spacing[1]), rel=.01)
    original, current = box((4, 5, 6), (14, 15, 16)), box((6, 5, 6), (16, 15, 16))
    shifted = mask_agreement(original, current, physical(SHEARED))["hd95_mm"]
    assert shifted == pytest.approx(brute_hd95(original, current, SHEARED))
    # The nearest sheared surface is not along a voxel axis at all.
    assert shifted < 2 * spacing[0] - .1


@pytest.mark.parametrize("matrix", [SHEARED, ORTHOGONAL["oblique"], np.array([[-.9, .5, .2], [.1, 1.3, -.4], [.3, 0, 2.2]])])
def test_irregular_masks_match_an_all_pairs_reference(matrix):
    original = ball((9, 10, 9), 5.5) | box((3, 3, 3), (7, 6, 12))
    current = (ball((10, 9, 10), 4.5) | box((12, 12, 4), (16, 15, 8))) & ~box((0, 0, 0), (20, 20, 7))
    result = mask_agreement(original, current, physical(matrix))
    overlap = np.count_nonzero(original & current)
    assert result["dice"] == pytest.approx(2 * overlap / (original.sum() + current.sum()))
    assert result["added_voxels"] == np.count_nonzero(current & ~original)
    assert result["removed_voxels"] == np.count_nonzero(original & ~current)
    assert result["hd95_mm"] == pytest.approx(brute_hd95(original, current, matrix), rel=1e-12)


def test_identical_trimmed_and_empty_masks():
    affine = physical(ORTHOGONAL["anisotropic"])
    mask = box((4, 4, 4), (14, 14, 14))
    same = mask_agreement(mask, mask.copy(), affine)
    assert (same["dice"], same["hd95_mm"], same["unchanged"]) == (1, 0, True)
    trimmed = mask.copy()
    trimmed[:, :, 13] = False
    result = mask_agreement(mask, trimmed, affine)
    assert result["dice"] == pytest.approx(2 * 900 / 1900)
    assert (result["added_voxels"], result["removed_voxels"]) == (0, 100)
    assert result["hd95_mm"] == pytest.approx(brute_hd95(mask, trimmed, ORTHOGONAL["anisotropic"]))
    erased = mask_agreement(mask, np.zeros_like(mask), affine)
    assert erased["dice"] == 0 and erased["hd95_mm"] is None and erased["removed_voxels"] == 1000
    assert erased["volume_change_ml"] == pytest.approx(-erased["original_volume_ml"])
    empty = mask_agreement(np.zeros_like(mask), np.zeros_like(mask), affine)
    assert (empty["dice"], empty["hd95_mm"], empty["unchanged"]) == (1, None, True)
    with pytest.raises(ValueError, match="one three-dimensional grid"):
        mask_agreement(mask, mask[:-1], affine)


# A sheared, anisotropic, partly reflected reference grid for the case history.
SHAPE = (16, 17, 18)
GRID = np.array([[-1.4, .6, .1, 25.], [.2, 2.1, .3, -44.], [0., .1, 2.8, 81.], [0., 0., 0., 1.]])
OUTPUTS = {"total": {"liver": ((3, 4, 4), (10, 11, 12)), "spleen": ((10, 3, 9), (14, 8, 15)),
                     "kidney_left": ((3, 11, 3), (7, 15, 8))},
           "lung_vessels": {"lung_arteries": ((11, 11, 11), (15, 16, 16))}}
PAINT = dict(id="paint", kind="scribble", include=True, axis=2, index=12, vertices=[[6., 4.], [8., 6.]], radius_mm=2.5)


@pytest.fixture
def history(tmp_path, monkeypatch):
    """A case whose real segment, refinement and review jobs use a deterministic worker."""
    service = Service(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    runs = []

    def fingerprint(profile, options=None, **source):
        digest = hashlib.sha256(json.dumps([profile, options], sort_keys=True).encode()).hexdigest()
        return {"versions": {"TotalSegmentator": "2.18.0"}, "device_request": "gpu:1",
                "wrapper_sha256": "f" * 64, "weights_complete": True, "digest": digest}

    def segment(ct_path, output_dir, profile, progress, cancelled, options=None, **source):
        task = options["tasks"][0]
        run = Path(output_dir) / f"run-{identifier()}"
        runs.append(run)
        reference = nib.load(ct_path)
        structures = []
        for label, (low, high) in OUTPUTS[task].items():
            # Each run proposes a slightly different liver, as a newer model might.
            shift = (len(runs) - 1 if label == "liver" else 0, 0, 0)
            path = run / task / f"{label}.nii.gz"
            imaging._save_mask(box(np.add(low, shift), np.add(high, shift), SHAPE), reference, path)
            structures.append(dict(id=f"{task}:{label}", name=label.replace("_", " ").capitalize(), task=task,
                                   group="Fixture", color="#a1b2c3", mask_file=str(path)))
        manifest = {"schema_version": 1, "engine": "TotalSegmentator", "engine_version": "2.18.0",
                    "status": "completed", "run_id": run.name, "options": options, "qc": [],
                    "model_fingerprint": fingerprint(profile, options),
                    "tasks": [{"task": task, "status": "completed", "labels": sorted(OUTPUTS[task])}]}
        (Path(output_dir) / "inference.json").write_text(json.dumps(manifest))
        return structures

    monkeypatch.setattr(segmentation, "model_fingerprint", fingerprint)
    monkeypatch.setattr(segmentation, "segment", segment)
    monkeypatch.setattr(segmentation, "_device_info", lambda device: pytest.fail("Agreement tests never use a GPU"))
    monkeypatch.setattr(service, "_interactive", lambda: pytest.fail("Direct edits need no AI runtime"))

    def image_case(label):
        case = service.new_case(label)
        source = service.store.case_dir(case["id"]) / "converted"
        source.mkdir()
        ct = nib.Nifti1Image(np.full(SHAPE, 40, np.int16), GRID)
        ct.header.set_xyzt_units("mm")
        nib.save(ct, source / "reference.nii.gz")
        image = service.build_ct_revision(case["id"], source / "reference.nii.gz")
        service.store.commit_revision(image)
        return service.store.get("cases", case["id"]), image

    case, image = image_case("Synthetic agreement history")
    return SimpleNamespace(service=service, case=case, image=image, runs=runs, image_case=image_case)


def execute(service, queued):
    claimed = service.store.claim()
    assert claimed and claimed["id"] == queued["id"]
    service.execute(claimed)
    job = service.store.get("jobs", queued["id"])
    assert job["status"] == "completed", job.get("error")
    return job


def produced(service, queued):
    job = execute(service, queued)
    return service.store.revision(job["case_id"], job["revision_id"])


def segment_task(history, revision, task, **options):
    return produced(history.service, history.service.segment_case(history.case["id"], revision["id"], task, **options))


def erase(history, revision, structure_id):
    """Erase a structure's lower patient-space octant with the real refinement job."""
    entry = next(s for s in revision["structures"] if s["id"] == structure_id)
    operation = {"type": "erase_region", "bounds_ras": [entry["bounds_ras"][0], entry["centroid_ras"]]}
    return produced(history.service, history.service.store.enqueue(history.case["id"], "refine", {
        "revision_id": revision["id"], "structure_id": structure_id, "operation": operation}))


def paint(history, revision, structure_id=None, *, name=None):
    """Accept a direct native-slice paint stroke, optionally as a reviewer-created segment."""
    reviews, case_id = history.service.reviews, history.case["id"]
    draft = (reviews.create_structure(case_id, revision["id"], name, method="direct") if name
             else reviews.create(case_id, revision["id"], structure_id, method="direct"))
    draft = reviews.update(case_id, draft["id"], draft["generation"], [PAINT])
    execute(history.service, reviews.predict(case_id, draft["id"], draft["generation"]))
    draft = reviews.get(case_id, draft["id"])
    return produced(history.service, reviews.accept(case_id, draft["id"], draft["generation"]))


def mask(path):
    return np.asanyarray(nib.load(path).dataobj).astype(bool)


def entry(revision, structure_id):
    return next(s for s in revision["structures"] if s["id"] == structure_id)


def rows(report):
    return {row["id"]: row for row in report["structures"]}


def test_originals_follow_corrections_custom_segments_and_later_runs(history):
    service, case_id = history.service, history.case["id"]
    first = segment_task(history, history.image, "total")
    refined = erase(history, first, "total:liver")
    edited = paint(history, refined, "total:spleen")
    custom = paint(history, edited, name="Lesion")
    vessels = segment_task(history, custom, "lung_vessels")
    run_a, run_b = (run.name for run in history.runs)
    report = revision_agreement(service, case_id, vessels["id"])
    found = rows(report)
    assert report["grid"]["sheared"] and report["hd95_exact"] and report["synthetic"] is False
    assert {key: report["summary"][key] for key in ("structures", "compared", "unchanged", "changed", "excluded")} == {
        "structures": 5, "compared": 4, "unchanged": 2, "changed": 2, "excluded": 1}
    assert report["summary"]["dice_median"] == pytest.approx(np.median([row["dice"] for row in report["structures"]]))
    # A refinement, then an unrelated task's run, keep the first model run as the original.
    assert found["total:liver"]["origin"] == {"kind": "model_output", "revision_id": first["id"],
                                              "run_id": run_a, "task": "total", "engine_version": "2.18.0"}
    expected = mask_agreement(mask(entry(first, "total:liver")["mask_file"]),
                              mask(entry(vessels, "total:liver")["mask_file"]), GRID)
    assert {key: found["total:liver"][key] for key in expected} == pytest.approx(expected)
    assert found["total:liver"]["removed_voxels"] > 0 == found["total:liver"]["added_voxels"]
    assert found["total:liver"]["volume_change_ml"] < 0 and found["total:liver"]["basis"] == "voxels"
    # Direct-edit review acceptance is a correction too.
    assert found["total:spleen"]["origin"]["revision_id"] == first["id"]
    assert found["total:spleen"]["added_voxels"] > 0 and found["total:spleen"]["dice"] < 1
    assert found["total:kidney_left"] | {"origin": None} == found["total:kidney_left"] | {
        "origin": None, "basis": "same_file", "unchanged": True, "dice": 1.0, "hd95_mm": 0.0,
        "volume_change_ml": 0.0, "added_voxels": 0, "removed_voxels": 0}
    assert found["total:kidney_left"]["original_volume_ml"] == pytest.approx(entry(vessels, "total:kidney_left")["volume_ml"])
    assert found["lung_vessels:lung_arteries"]["origin"]["revision_id"] == vessels["id"]
    assert found["lung_vessels:lung_arteries"]["origin"]["run_id"] == run_b
    lesion = next(s for s in vessels["structures"] if s["task"] == "custom")
    assert report["excluded"] == [{"id": lesion["id"], "name": "Lesion", "task": "custom", "group": "Custom",
                                   "code": "reviewer_created", "reason": agreement._REASONS["reviewer_created"]}]

    # A later run of the same task replaces corrected anatomy; it is the new original.
    rerun = segment_task(history, vessels, "total", remove_small_blobs=True)
    run_c = history.runs[2].name
    found = rows(revision_agreement(service, case_id, rerun["id"]))
    assert rerun["operation"]["replacement_id_map"]["total:liver"] == "total:liver"
    assert all(found[sid]["unchanged"] and found[sid]["origin"]["run_id"] == run_c
               for sid in ("total:liver", "total:spleen", "total:kidney_left"))
    assert found["lung_vessels:lung_arteries"]["origin"]["run_id"] == run_b
    corrected = erase(history, rerun, "total:liver")
    liver = rows(revision_agreement(service, case_id, corrected["id"]))["total:liver"]
    assert liver["origin"]["revision_id"] == rerun["id"] and liver["origin"]["run_id"] == run_c
    expected = mask_agreement(mask(entry(rerun, "total:liver")["mask_file"]),
                              mask(entry(corrected, "total:liver")["mask_file"]), GRID)
    assert liver["dice"] == pytest.approx(expected["dice"]) and liver["dice"] < 1
    assert liver["hd95_mm"] == pytest.approx(expected["hd95_mm"])


def test_unchanged_masks_need_no_decode_and_changed_pairs_are_cached(history, monkeypatch):
    service, case_id = history.service, history.case["id"]
    first = segment_task(history, history.image, "total")
    refined = erase(history, first, "total:liver")
    # Slicer rewrites every segment; an unchanged one can keep identical bytes.
    copy = service.store.case_dir(case_id) / "corrections" / "fixture" / "kidney.nii.gz"
    copy.parent.mkdir(parents=True)
    shutil.copyfile(entry(refined, "total:kidney_left")["mask_file"], copy)
    structures = [dict(s, mask_file=str(copy)) if s["id"] == "total:kidney_left" else s for s in refined["structures"]]
    rewritten = service.build_revision(case_id, Path(refined["ct_file"]), structures, source="slicer_correction",
                                       parent=refined["id"], inference_manifest=refined["inference_manifest"])
    service.store.commit_revision(rewritten)
    decoded, hashed = [], []
    crop, digest = agreement._crop, review_volume._file_digest
    monkeypatch.setattr(agreement, "_crop", lambda path, *args: decoded.append(Path(path)) or crop(path, *args))
    monkeypatch.setattr(review_volume, "_file_digest", lambda path: hashed.append(Path(path)) or digest(path))
    report = revision_agreement(service, case_id, rewritten["id"])
    found = rows(report)
    assert found["total:spleen"]["basis"] == "same_file" and found["total:kidney_left"]["basis"] == "same_digest"
    assert found["total:kidney_left"]["unchanged"] and found["total:kidney_left"]["dice"] == 1
    assert found["total:liver"]["basis"] == "voxels" and not found["total:liver"]["unchanged"]
    liver = {Path(entry(first, "total:liver")["mask_file"]), Path(entry(refined, "total:liver")["mask_file"])}
    assert set(decoded) == liver and len(decoded) == 2
    assert set(hashed) == liver | {copy, Path(entry(first, "total:kidney_left")["mask_file"])}
    assert report["cache"] == {"hits": 0, "computed": 1}

    monkeypatch.setattr(agreement, "_crop", lambda *args: pytest.fail("A cached pair must not be decoded"))
    cached = revision_agreement(service, case_id, rewritten["id"])
    assert cached["cache"] == {"hits": 1, "computed": 0} and cached["structures"] == report["structures"]
    records = list((service.settings.data_dir / "agreement-cache").rglob("*.json"))
    assert len(records) == 1
    # Damaged records and other implementations are measured again, never trusted.
    monkeypatch.setattr(agreement, "_crop", crop)
    records[0].write_text(json.dumps({**json.loads(records[0].read_text()), "agreement": {"dice": 1}}))
    repaired = revision_agreement(service, case_id, rewritten["id"])
    assert repaired["cache"] == {"hits": 0, "computed": 1} and repaired["structures"] == report["structures"]
    monkeypatch.setattr(agreement, "_IMPLEMENTATION", "mask-agreement-test")
    assert revision_agreement(service, case_id, rewritten["id"])["cache"] == {"hits": 0, "computed": 1}
    assert len(list((service.settings.data_dir / "agreement-cache").rglob("*.json"))) == 2


def test_typical_case_decodes_only_its_few_changed_structures(history, monkeypatch):
    service, case_id = history.service, history.case["id"]
    first = segment_task(history, history.image, "total")
    reference = nib.load(first["ct_file"])
    run = service.store.case_dir(case_id) / "inference" / ("b" * 64) / f"run-{identifier()}"
    corrections = service.store.case_dir(case_id) / "corrections" / "typical"
    voxel_ml = abs(np.linalg.det(GRID[:3, :3])) / 1000
    model, current = [], []
    for index in range(117):
        low = np.array([index % 13, index // 13 % 13, 2 + index % 11])
        path = run / "total" / f"label_{index:03d}.nii.gz"
        imaging._save_mask(box(low, low + 3, SHAPE), reference, path)
        item = dict(id=f"total:label_{index:03d}", name=f"Label {index}", task="total", group="Fixture",
                    color="#a1b2c3", mask_file=str(path), voxel_count=27, volume_ml=27 * voxel_ml)
        model.append(item)
        if index in (5, 60, 116):
            path = corrections / f"{index}.nii.gz"
            imaging._save_mask(box(low, low + (4, 3, 3), SHAPE), reference, path)
            item = dict(item, mask_file=str(path), voxel_count=36, volume_ml=36 * voxel_ml)
        current.append(item)
    ai = {**deepcopy(first), "id": identifier(), "parent_id": history.image["id"], "structures": model}
    reviewed = {**deepcopy(ai), "id": identifier(), "parent_id": ai["id"], "structures": current,
                "source": "nninteractive_correction", "review_status": "corrected"}
    service.store.save("revisions", ai)
    service.store.save("revisions", reviewed)
    decoded, hashed = [], []
    crop, digest = agreement._crop, review_volume._file_digest
    monkeypatch.setattr(agreement, "_crop", lambda path, *args: decoded.append(path) or crop(path, *args))
    monkeypatch.setattr(review_volume, "_file_digest", lambda path: hashed.append(path) or digest(path))
    started = time.perf_counter()
    report = revision_agreement(service, case_id, reviewed["id"])
    elapsed = time.perf_counter() - started
    assert {key: report["summary"][key] for key in ("structures", "compared", "unchanged", "changed", "excluded")} == {
        "structures": 117, "compared": 117, "unchanged": 114, "changed": 3, "excluded": 0}
    assert len(decoded) == len(hashed) == 6
    changed = [row for row in report["structures"] if not row["unchanged"]]
    assert all(row["dice"] == pytest.approx(2 * 27 / 63) and row["added_voxels"] == 9 for row in changed)
    assert all(row["origin"]["revision_id"] == ai["id"] for row in report["structures"])
    assert elapsed < 30  # A generous bound; README documents measured timings.


def test_missing_or_unreadable_masks_are_excluded_without_recomputing(history):
    service, case_id = history.service, history.case["id"]
    first = segment_task(history, history.image, "total")
    refined = erase(history, erase(history, first, "total:liver"), "total:kidney_left")
    Path(entry(first, "total:liver")["mask_file"]).unlink()
    Path(entry(refined, "total:kidney_left")["mask_file"]).write_bytes(b"not a NIfTI mask")
    report = revision_agreement(service, case_id, refined["id"])
    assert {row["id"]: row["code"] for row in report["excluded"]} == {
        "total:liver": "model_output_unavailable", "total:kidney_left": "unreadable"}
    assert rows(report)["total:spleen"]["unchanged"]
    assert str(service.settings.data_dir) not in json.dumps(report) and "mask_file" not in json.dumps(report)
    assert len(history.runs) == 1


def test_originals_from_another_source_image_are_not_compared(history):
    service, case_id = history.service, history.case["id"]
    first = segment_task(history, history.image, "total")
    # A second reconstruction on the same grid is still a different image.
    source = service.store.case_dir(case_id) / "converted-second"
    source.mkdir()
    other = nib.Nifti1Image(np.full(SHAPE, 90, np.int16), GRID)
    other.header.set_xyzt_units("mm")
    nib.save(other, source / "reference.nii.gz")
    second = service.build_ct_revision(case_id, source / "reference.nii.gz")
    service.store.commit_revision(second)
    reused = [{key: value for key, value in s.items() if key != "reference_image_id"} for s in first["structures"]]
    stray = {**deepcopy(second), "id": identifier(), "parent_id": first["id"], "kind": "segmentation", "structures": reused}
    service.store.save("revisions", stray)
    report = revision_agreement(service, case_id, stray["id"])
    assert not report["structures"] and {row["code"] for row in report["excluded"]} == {"different_image"}


def test_reports_never_modify_masks_revisions_or_provenance(history):
    service, case_id = history.service, history.case["id"]
    first = segment_task(history, history.image, "total")
    corrected = paint(history, erase(history, first, "total:liver"), "total:spleen")
    service.store.set_review_status(case_id, corrected["id"], "corrected")
    data = service.settings.data_dir

    def files():
        return {path: path.read_bytes() for path in data.rglob("*")
                if path.is_file() and not path.name.startswith("scan3d.sqlite3")}

    def records():
        return {table: service.store.list(table) for table in ("cases", "revisions", "jobs", "review_sessions", "image_assets")}

    before_files, before_records = files(), records()
    revision_agreement(service, case_id, corrected["id"])
    aggregate_agreement(service)
    after = files()
    assert records() == before_records
    assert {path: after[path] for path in before_files} == before_files
    assert all("agreement-cache" in path.parts or path.name == "agreement.lock" for path in set(after) - set(before_files))


def test_aggregate_counts_decided_active_revisions_and_keeps_synthetic_cases_apart(history):
    service, case_id = history.service, history.case["id"]
    first = segment_task(history, history.image, "total")
    refined = erase(history, first, "total:liver")
    service.store.set_review_status(case_id, refined["id"], "reviewed")
    demo = create_demo(service)
    service.store.set_review_status(demo["id"], demo["active_revision_id"], "reviewed")
    history.image_case("Prepared image without segmentation")
    service.new_case("Imported series, not yet prepared")
    summary = aggregate_agreement(service)
    assert summary["selection"] == {"revision": "active", "review_statuses": ["reviewed", "corrected"]}
    assert summary["cases"] == {"total": 4, "included": 2, "skipped": {
        "no_active_revision": 1, "no_segmentation": 1, "unreviewed": 0, "unavailable": 0}}
    real, synthetic = summary["pools"]["real"], summary["pools"]["synthetic"]
    assert real["cases"] == 1 and [task["task"] for task in real["tasks"]] == ["total"]
    total = real["tasks"][0]
    found = {row["id"]: row for row in total["structures"]}
    liver = rows(revision_agreement(service, case_id, refined["id"]))["total:liver"]
    assert found["total:liver"] == {
        "id": "total:liver", "name": "Liver", "task": "total", "reviewed": 1, "unchanged": 0,
        "unchanged_percent": 0.0, "dice_mean": liver["dice"], "dice_median": liver["dice"],
        "abs_volume_change_ml_mean": abs(liver["volume_change_ml"])}
    assert found["total:spleen"]["unchanged_percent"] == 100
    assert (total["reviewed"], total["unchanged"]) == (3, 2)
    assert total["unchanged_percent"] == pytest.approx(200 / 3)
    assert total["dice_mean"] == pytest.approx((2 + liver["dice"]) / 3) and total["dice_median"] == 1
    assert total["abs_volume_change_ml_mean"] == pytest.approx(abs(liver["volume_change_ml"]) / 3)
    assert real["summary"] == {key: total[key] for key in real["summary"]}
    assert synthetic["cases"] == 1 and synthetic["summary"]["reviewed"] == 7
    assert synthetic["summary"]["unchanged_percent"] == 100
    assert [task["task"] for task in synthetic["tasks"]] == ["synthetic"]
    assert {(item["case_id"], item["pool"]) for item in summary["revisions"]} == {(case_id, "real"), (demo["id"], "synthetic")}
    # A newer, undecided revision withdraws the case until it is reviewed.
    segment_task(history, refined, "lung_vessels")
    summary = aggregate_agreement(service)
    assert summary["cases"]["skipped"]["unreviewed"] == 1
    assert summary["pools"]["real"] == {"cases": 0, "excluded": 0, "summary": None, "tasks": []}


@pytest.fixture
def client(tmp_path):
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", allowed_hosts=("127.0.0.1",))
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        yield client, app.state.service


def completed(http, job_id):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = http.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            assert job["status"] == "completed", job
            return job
        time.sleep(.05)
    pytest.fail("Worker did not finish")


def test_agreement_routes_label_synthetic_baselines_and_reject_unknown_ids_and_candidates(client):
    http, service = client
    case = http.post("/api/cases/demo").json()
    prefix = f"/api/cases/{case['id']}"
    phantom = case["active_revision_id"]
    response = http.get(f"{prefix}/revisions/{phantom}/agreement")
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    report = response.json()
    assert report["synthetic"] is True and report["active"] is True and report["aggregate_pool"] is None
    assert report["summary"]["compared"] == report["summary"]["unchanged"] == 7
    assert {(row["origin"]["kind"], row["basis"]) for row in report["structures"]} == {("synthetic_phantom", "same_file")}
    assert "mask_file" not in response.text and str(service.settings.data_dir) not in response.text

    job = http.post(f"{prefix}/refinements", json={"revision_id": phantom, "structure_id": "synthetic:liver",
        "operation": {"type": "erase_region", "bounds_ras": [[0, -80, -100], [37, 80, 0]]}}).json()
    refined = completed(http, job["id"])["revision_id"]
    liver = rows(http.get(f"{prefix}/revisions/{refined}/agreement").json())["synthetic:liver"]
    assert 0 < liver["dice"] < 1 and liver["hd95_mm"] > 0 and liver["origin"]["revision_id"] == phantom
    assert liver["added_voxels"] == 0 < liver["removed_voxels"]
    assert liver["volume_change_ml"] == pytest.approx(liver["current_volume_ml"] - liver["original_volume_ml"])
    assert liver["volume_change_ml"] < 0

    summary = http.get("/api/agreement")
    assert summary.status_code == 200 and summary.headers["cache-control"] == "private, no-store"
    assert summary.json()["cases"]["skipped"]["unreviewed"] == 1
    assert http.patch(f"{prefix}/revisions/{refined}", json={"review_status": "corrected"}).status_code == 200
    assert http.get(f"{prefix}/revisions/{refined}/agreement").json()["aggregate_pool"] == "synthetic"
    pools = http.get("/api/agreement").json()["pools"]
    assert pools["real"] == {"cases": 0, "excluded": 0, "summary": None, "tasks": []}
    assert pools["synthetic"]["cases"] == 1 and pools["synthetic"]["summary"]["unchanged"] == 6

    # A real, servable correction candidate is still not an accepted revision.
    draft = http.post(f"{prefix}/reviews", json={"revision_id": refined, "structure_id": "synthetic:spine",
                                                  "method": "direct"}).json()
    stroke = dict(id="paint", kind="scribble", include=True, axis=2, index=40, vertices=[[36., 18.], [44., 22.]], radius_mm=6)
    draft = http.patch(f"{prefix}/reviews/{draft['id']}", json={"expected_generation": draft["generation"],
                                                               "prompts": [stroke]}).json()
    completed(http, http.post(f"{prefix}/reviews/{draft['id']}/predict", json={"generation": draft["generation"]}).json()["id"])
    alias = http.get(f"{prefix}/reviews/{draft['id']}").json()["candidate"]["revision_id"]
    assert http.get(f"{prefix}/revisions/{alias}").status_code == 200
    rejected = http.get(f"{prefix}/revisions/{alias}/agreement")
    assert rejected.status_code == 404 and "Save the correction" in rejected.json()["detail"]

    other = http.post("/api/cases/demo").json()
    assert http.get(f"{prefix}/revisions/{other['active_revision_id']}/agreement").status_code == 404
    assert http.get(f"{prefix}/revisions/{'0' * 32}/agreement").status_code == 404
    assert http.get(f"/api/cases/{'0' * 32}/revisions/{phantom}/agreement").status_code == 404
