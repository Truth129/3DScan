"""CT preparation and explicitly requested inference are separate transactions, with deterministic converter and worker doubles."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest

from inference_path_stub import GPU_NAME, stand_in
from scan3d import dicom, imaging, segmentation, surfaces
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.store import identifier


@pytest.fixture
def rig(tmp_path, monkeypatch):
    source = tmp_path / "source-dicom"
    source.mkdir()
    original = source / "slice.dcm"
    original.write_bytes(b"synthetic DICOM indexing fixture; no patient data")
    values = np.arange(12 * 14 * 16, dtype=np.int16).reshape((12, 14, 16)) - 1000
    angle = np.deg2rad(23)
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-0.8, 1.4, 2.2])
    affine[:3, 3] = [31, -52, 87]
    calls = SimpleNamespace(conversions=[], fingerprints=[], segments=[], task_labels={"total": ["liver", "spleen"], "lung_vessels": ["lung_arteries"]})

    def index(source_dir):
        return [
            {
                "uid": "fixture-series",
                "description": "Synthetic oblique CT",
                "modality": "CT",
                "slice_count": values.shape[2],
                "spacing": [0.8, 1.4, 2.2],
                "warnings": [],
                "files": [str(Path(source_dir) / "slice.dcm")],
            }
        ]

    def convert(files, directory):
        calls.conversions.append(list(files))
        image = nib.Nifti1Image(values, affine)
        image.header.set_xyzt_units("mm")
        image.set_sform(affine, code=2)
        image.set_qform(affine, code=1)
        path = Path(directory) / "reference.nii.gz"
        nib.save(image, path)
        return path

    def fingerprint(profile, options=None):
        options = deepcopy(options)
        calls.fingerprints.append((profile, options))
        digest = hashlib.sha256(json.dumps([profile, options], sort_keys=True).encode()).hexdigest()
        return {
            "schema_version": 2,
            "versions": {"TotalSegmentator": "2.18.0", "nnunetv2": "test", "torch": "test"},
            "device_request": "gpu:1",
            "wrapper_sha256": "f" * 64,
            "weights_complete": True,
            "digest": digest,
            "options": options,
        }

    def segment(ct_path, output_dir, profile, progress, cancelled, options=None):
        assert options and len(options["tasks"]) == 1, "Only an explicitly selected task may run"
        assert not cancelled()
        options = deepcopy(options)
        calls.segments.append({"ct": str(ct_path), "profile": profile, "options": options})
        task = options["tasks"][0]
        run = Path(output_dir) / ("run-" + identifier())
        task_dir = run / task
        task_dir.mkdir(parents=True)
        reference = nib.load(ct_path)
        structures = []
        for n, label in enumerate(calls.task_labels[task]):
            mask = np.zeros(reference.shape, np.uint8)
            start = 2 + len(calls.segments) % 3
            mask[start : start + 3, 3 + n : 6 + n, 4:8] = 1
            path = task_dir / f"{label}.nii.gz"
            nib.save(nib.Nifti1Image(mask, reference.affine, reference.header.copy()), path)
            group, color = segmentation._appearance(label)
            structures.append(dict(id=f"{task}:{label}", task=task, name=label.replace("_", " ").capitalize(), group=group, color=color, mask_file=str(path)))
        manifest = {
            "schema_version": 1,
            "engine": "TotalSegmentator",
            "engine_version": "2.18.0",
            "status": "completed",
            "run_id": run.name,
            "profile": profile,
            "options": options,
            "input_sha256": hashlib.sha256(Path(ct_path).read_bytes()).hexdigest(),
            "runtime": {"selected_device": "gpu:1"},
            "qc": [],
            "empty_labels": [],
            "structure_count": len(structures),
            "model_fingerprint": fingerprint(profile, options),
            "tasks": [{"task": task, "status": "completed", "labels": list(calls.task_labels[task]), "options": options, "effective_options": options}],
        }
        for path in (Path(output_dir) / "inference.json", run / "inference.json"):
            path.write_text(json.dumps(manifest))
        progress("Synthetic task completed")
        return structures

    def no_gpu(*args, **kwargs):
        pytest.fail("Integration fixtures must never initialize a real GPU or model")

    monkeypatch.setattr(dicom, "index_dicom", index)
    monkeypatch.setattr(dicom, "convert_series", convert)
    monkeypatch.setattr(segmentation, "model_fingerprint", fingerprint)
    monkeypatch.setattr(segmentation, "segment", segment)
    monkeypatch.setattr(segmentation, "_resolve_device", no_gpu)
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    # Deliberately omit the lifespan context: claim/execute the real queue
    # synchronously so active-revision races are deterministic.
    http = TestClient(app, base_url="http://127.0.0.1")
    fixture = SimpleNamespace(http=http, service=app.state.service, calls=calls, source=source, values=values, affine=affine, original=original)
    yield fixture
    http.close()


def execute(rig, response):
    assert response.status_code == 200, response.text
    queued = response.json()
    claimed = rig.service.store.claim()
    assert claimed and claimed["id"] == queued["id"]
    rig.service.execute(claimed)
    job = rig.service.store.get("jobs", queued["id"])
    assert job["status"] == "completed", job.get("error")
    return rig.service.store.revision(job["case_id"], job["revision_id"])


def imported(rig):
    response = rig.http.post("/api/cases/import-local", json={"path": str(rig.source), "label": "Synthetic CT"})
    assert response.status_code == 200, response.text
    return response.json()


def prepared(rig, case=None):
    case = case or imported(rig)
    revision = execute(
        rig, rig.http.post(f"/api/cases/{case['id']}/process", json={"series_uid": "fixture-series", "profile": "chest_abdomen", "phase": "arterial"})
    )
    return case, revision


def requested(rig, case, revision, task="total", **options):
    return rig.http.post(
        f"/api/cases/{case['id']}/segment",
        json={"revision_id": revision["id"], "task": task, "robust_crop": False, "body_seg": False, "remove_small_blobs": False, **options},
    )


def failed(rig, response):
    assert response.status_code == 200, response.text
    claimed = rig.service.store.claim()
    assert claimed["id"] == response.json()["id"]
    rig.service.execute(claimed)
    job = rig.service.store.get("jobs", claimed["id"])
    assert job["status"] == "failed"
    return job


def test_import_and_prepare_are_CT_only_and_preserve_source_hu(rig, monkeypatch):
    before = rig.original.read_bytes()
    case = imported(rig)
    assert case["active_revision_id"] is None
    assert rig.service.store.list("jobs") == []
    assert not rig.calls.conversions and not rig.calls.fingerprints and not rig.calls.segments

    def no_surfaces(*args, **kwargs):
        pytest.fail("Preparing a CT must not calculate masks or build surfaces")

    monkeypatch.setattr(surfaces, "prepare_surface", no_surfaces)
    monkeypatch.setattr(surfaces, "build_model", no_surfaces)
    monkeypatch.setattr(imaging, "analyze_mask", no_surfaces)
    _, revision = prepared(rig, case)
    assert revision["structures"] == []
    assert revision["surface_assets"] == []
    assert not rig.calls.fingerprints and not rig.calls.segments
    assert len(rig.calls.conversions) == 1
    assert revision["geometry"]["shape"] == list(rig.values.shape)
    np.testing.assert_allclose(revision["geometry"]["affine_ras"], rig.affine, atol=1e-5)
    np.testing.assert_array_equal(nib.load(revision["ct_file"]).dataobj, rig.values)
    assert rig.original.read_bytes() == before
    assert Path(rig.calls.conversions[0][0]).read_bytes() == before
    public = rig.http.get(f"/api/cases/{case['id']}/revisions/{revision['id']}").json()
    assert public["structures"] == [] and "ct_file" not in public


def test_CT_only_revision_serves_frames_probe_and_raw_volume_without_masks(rig):
    case, revision = prepared(rig)
    prefix = f"/api/cases/{case['id']}/revisions/{revision['id']}"
    manifest = rig.http.get(prefix + "/model-manifest")
    assert manifest.status_code == 200 and manifest.json()["structures"] == []
    frame = rig.http.get(prefix + "/review-frame", params={"plane": "axial", "position_mm": revision["geometry"]["center_ras"][2]})
    assert frame.status_code == 200 and "X-CT-Frame" in frame.headers
    voxel = np.array([5, 6, 7])
    ras = np.asarray(revision["geometry"]["affine_ras"]) @ np.r_[voxel, 1]
    probe = rig.http.get(prefix + "/probe", params=dict(zip(("x", "y", "z"), ras[:3])))
    assert probe.status_code == 200 and probe.json()["hu"] == rig.values[tuple(voxel)]
    descriptor = rig.http.get(prefix + "/ct-volume").json()
    raw = b"".join(rig.http.get(chunk["url"]).content for chunk in descriptor["chunks"])
    values = np.frombuffer(raw, dtype=descriptor["dtype"]).reshape(descriptor["shape"], order="F")
    np.testing.assert_array_equal(values * descriptor["slope"] + descriptor["intercept"], rig.values)
    assert not rig.calls.segments and not rig.calls.fingerprints


def test_cardiac_acquisition_survives_explicit_segmentation_with_identical_task_flags(rig):
    case = imported(rig)
    acquisition = dict(series_uid="fixture-series", profile="corcta", phase="coronary_arterial", cardiac_phase="best_diastolic", rr_percent=69)
    ct = execute(rig, rig.http.post(f"/api/cases/{case['id']}/process", json=acquisition))
    assert not rig.calls.segments
    before = Path(ct["ct_file"]).read_bytes()
    segmented = execute(rig, requested(rig, case, ct, task="total"))
    assert segmented["acquisition"] == acquisition
    assert rig.calls.segments[0]["options"] == dict(tasks=["total"], robust_crop=False, body_seg=False, remove_small_blobs=False)
    assert len(rig.calls.segments) == 1 and Path(ct["ct_file"]).read_bytes() == before


@pytest.mark.parametrize("flag", ["robust_crop", "remove_small_blobs"])
def test_explicit_task_and_each_option_reach_worker_cache_and_provenance(rig, flag):
    case, ct = prepared(rig)
    ct_before = Path(ct["ct_file"]).read_bytes()
    first = execute(rig, requested(rig, case, ct))
    assert first["parent_id"] == ct["id"]
    assert {s["task"] for s in first["structures"]} == {"total"}
    assert len(rig.calls.segments) == 1
    expected = {"tasks": ["total"], "robust_crop": False, "body_seg": False, "remove_small_blobs": False}
    assert rig.calls.segments[-1]["options"] == expected
    first_snapshot = deepcopy(first)
    warm = execute(rig, requested(rig, case, first))
    assert warm["parent_id"] == first["id"] and len(rig.calls.segments) == 1
    assert warm["inference_manifest"] == first["inference_manifest"]
    changed = execute(rig, requested(rig, case, warm, **{flag: True}))
    expected[flag] = True
    assert len(rig.calls.segments) == 2
    assert rig.calls.segments[-1]["options"] == expected
    assert changed["inference_manifest"]["options"] == expected
    public = rig.http.get(f"/api/cases/{case['id']}/revisions/{changed['id']}").json()
    assert public["segmentation_settings"] == expected
    assert public["segmentation_runs"][-1]["options"] == expected
    assert public["segmentation_runs"][-1]["input_revision_id"] == warm["id"]
    assert changed["inference_manifest"]["model_fingerprint"]["digest"] != first["inference_manifest"]["model_fingerprint"]["digest"]
    assert rig.service.store.revision(case["id"], first["id"]) == first_snapshot
    assert Path(ct["ct_file"]).read_bytes() == ct_before


def test_new_task_preserves_other_task_correction_and_replaces_only_selected_task(rig):
    case, ct = prepared(rig)
    first = execute(rig, requested(rig, case, ct))
    both = execute(rig, requested(rig, case, first, task="lung_vessels"))
    assert {s["task"] for s in both["structures"]} == {"total", "lung_vessels"}
    assert rig.calls.segments[-1]["options"] == {"tasks": ["lung_vessels"], "robust_crop": True, "body_seg": False, "remove_small_blobs": False}
    lung = next(s for s in both["structures"] if s["task"] == "lung_vessels")
    correction = Path(both["directory"]) / "corrected-lung.nii.gz"
    corrected_values = np.asarray(nib.load(lung["mask_file"]).dataobj).copy()
    before_count = int(corrected_values.sum())
    corrected_values[np.nonzero(corrected_values)[0][0]] = 0
    assert 0 < corrected_values.sum() < before_count
    nib.save(nib.Nifti1Image(corrected_values, nib.load(ct["ct_file"]).affine), correction)
    corrected = {k: v for k, v in lung.items() if k not in ("volume_ml", "voxel_count", "centroid_ras", "qc")}
    corrected["mask_file"] = str(correction)
    parent = rig.service.build_revision(
        case["id"],
        Path(ct["ct_file"]),
        [corrected if s["id"] == lung["id"] else s for s in both["structures"]],
        source="slicer_correction",
        parent=both["id"],
        acquisition=both["acquisition"],
        inference_manifest=both["inference_manifest"],
    )
    rig.service.store.commit_revision(parent)
    snapshots = {r["id"]: deepcopy(r) for r in rig.service.store.list("revisions", case["id"])}
    masks = {s["mask_file"]: Path(s["mask_file"]).read_bytes() for r in snapshots.values() for s in r["structures"]}
    preserved = next(s for s in parent["structures"] if s["task"] == "lung_vessels")
    rig.calls.task_labels["total"] = ["liver"]
    result = execute(rig, requested(rig, case, parent, remove_small_blobs=True))
    assert result["parent_id"] == parent["id"]
    assert {s["id"] for s in result["structures"]} == {"total:liver", "lung_vessels:lung_arteries"}
    assert next(s for s in result["structures"] if s["task"] == "lung_vessels") == preserved
    assert {r: rig.service.store.revision(case["id"], r) for r in snapshots} == snapshots
    assert {path: Path(path).read_bytes() for path in masks} == masks
    rig.service.store.activate(case["id"], parent["id"])
    assert rig.service.store.revision(case["id"], parent["id"]) == snapshots[parent["id"]]


def test_total_v3_replaces_shared_anatomy_preserves_unique_labels_and_reuses_cache(rig):
    case, ct = prepared(rig)
    rig.calls.task_labels["total"] = ["liver", "spleen", "vertebrae_S1"]
    rig.calls.task_labels["total_v3"] = ["liver", "spleen", "vertebrae_L6"]
    first = execute(rig, requested(rig, case, ct))
    snapshots = {revision["id"]: deepcopy(revision) for revision in rig.service.store.list("revisions", case["id"])}
    files = {entry["mask_file"]: Path(entry["mask_file"]).read_bytes() for revision in snapshots.values() for entry in revision["structures"]}
    files[ct["ct_file"]] = Path(ct["ct_file"]).read_bytes()
    files[str(rig.original)] = rig.original.read_bytes()
    result = execute(rig, requested(rig, case, first, task="total_v3"))
    by_id = {entry["id"]: entry for entry in result["structures"]}
    assert set(by_id) == {"total_v3:liver", "total_v3:spleen", "total_v3:vertebrae_L6", "total:vertebrae_S1"}
    assert {asset["id"] for asset in result["surface_assets"]} == set(by_id)
    manifest = rig.http.get(f"/api/cases/{case['id']}/revisions/{result['id']}/model-manifest")
    assert manifest.status_code == 200, manifest.text
    assert {entry["id"] for entry in manifest.json()["structures"]} == set(by_id)
    assert by_id["total:vertebrae_S1"] == next(entry for entry in first["structures"] if entry["id"] == "total:vertebrae_S1")
    old_liver = next(entry for entry in first["structures"] if entry["id"] == "total:liver")
    assert not np.array_equal(nib.load(old_liver["mask_file"]).dataobj, nib.load(by_id["total_v3:liver"]["mask_file"]).dataobj)
    assert result["operation"]["replacement_policy"] == "same_task_or_exact_name_within_model_v2"
    assert set(result["operation"]["replaced_structure_ids"]) == {"total:liver", "total:spleen"}
    assert result["operation"]["replacement_id_map"] == {"total:liver": "total_v3:liver", "total:spleen": "total_v3:spleen"}
    assert result["segmentation_runs"][-1]["tasks"] == ["total_v3"]
    assert result["inference_manifest"]["tasks"][0]["task"] == "total_v3"
    assert len(rig.calls.segments) == 2

    # Restoring the original revision and selecting v3 again must apply the
    # same merge policy even when native inference outputs come from cache.
    rig.service.store.activate(case["id"], first["id"])
    warm = execute(rig, requested(rig, case, first, task="total_v3"))
    assert len(rig.calls.segments) == 2
    assert {entry["id"] for entry in warm["structures"]} == set(by_id)
    assert warm["operation"]["replacement_id_map"] == result["operation"]["replacement_id_map"]
    assert {revision_id: rig.service.store.revision(case["id"], revision_id) for revision_id in snapshots} == snapshots
    assert {path: Path(path).read_bytes() for path in files} == files
    rig.service.store.activate(case["id"], first["id"])
    assert rig.service.store.revision(case["id"], first["id"]) == snapshots[first["id"]]


def test_partial_new_task_replaces_only_output_names_it_actually_produces(rig):
    case, ct = prepared(rig)
    first = execute(rig, requested(rig, case, ct))
    rig.calls.task_labels["total_v3"] = ["liver"]
    result = execute(rig, requested(rig, case, first, task="total_v3"))
    assert {entry["id"] for entry in result["structures"]} == {"total_v3:liver", "total:spleen"}
    assert next(entry for entry in result["structures"] if entry["id"] == "total:spleen") == next(
        entry for entry in first["structures"] if entry["id"] == "total:spleen"
    )
    assert result["operation"]["replaced_structure_ids"] == ["total:liver"]


def with_custom_child(rig, case, ct, first, structures):
    liver = next(entry for entry in structures if entry["id"] == "total:liver")
    custom = {**liver, "id": "custom:finding", "task": "custom", "name": "Finding"}
    custom["relationship"] = {"kind": "subregion", "parent_id": "total:liver", "exclusive_with": []}
    parent = rig.service.build_revision(
        case["id"],
        Path(ct["ct_file"]),
        [*structures, custom],
        source="interactive_correction",
        parent=first["id"],
        acquisition=first["acquisition"],
        inference_manifest=first["inference_manifest"],
    )
    rig.service.store.commit_revision(parent)
    return parent, custom


def saved(rig, case):
    revisions = deepcopy(rig.service.store.list("revisions", case["id"]))
    files = {entry["mask_file"]: Path(entry["mask_file"]).read_bytes() for revision in revisions for entry in revision["structures"]}
    return rig.service.store.get("cases", case["id"])["active_revision_id"], revisions, files


def test_new_task_remaps_custom_parent_and_inherits_explicit_exclusivity(rig):
    case, ct = prepared(rig)
    first = execute(rig, requested(rig, case, ct))
    structures = deepcopy(first["structures"])
    for entry in structures:
        peer = "total:spleen" if entry["id"] == "total:liver" else "total:liver"
        entry["relationship"] = {"kind": "subdivision", "exclusive_with": [peer]}
    parent, custom = with_custom_child(rig, case, ct, first, structures)
    before, files = deepcopy(parent), saved(rig, case)[2]
    rig.calls.task_labels["total_v3"] = ["liver", "spleen"]
    by_id = {entry["id"]: entry for entry in execute(rig, requested(rig, case, parent, task="total_v3"))["structures"]}
    assert by_id["custom:finding"]["relationship"] == {"kind": "subregion", "parent_id": "total_v3:liver", "exclusive_with": []}
    assert by_id["total_v3:liver"]["relationship"] == {"kind": "subdivision", "exclusive_with": ["total_v3:spleen"]}
    assert by_id["total_v3:spleen"]["relationship"] == {"kind": "subdivision", "exclusive_with": ["total_v3:liver"]}
    assert (by_id["custom:finding"]["mask_file"], by_id["custom:finding"]["volume_ml"]) == (custom["mask_file"], custom["volume_ml"])
    assert rig.service.store.revision(case["id"], parent["id"]) == before and files.items() <= saved(rig, case)[2].items()


def test_duplicate_incoming_names_fail_without_publishing_or_mutating_saved_revision(rig):
    case, ct = prepared(rig)
    first = execute(rig, requested(rig, case, ct))
    before = saved(rig, case)
    rig.calls.task_labels["total_v3"] = ["liver", "LIVER"]
    failed(rig, requested(rig, case, first, task="total_v3"))
    assert saved(rig, case) == before


def test_task_rerun_cannot_publish_dangling_preserved_custom_relationship(rig):
    case, ct = prepared(rig)
    first = execute(rig, requested(rig, case, ct))
    parent, _ = with_custom_child(rig, case, ct, first, first["structures"])
    before = saved(rig, case)
    # A new run without a liver must not leave the saved custom child pointing to a missing parent.
    rig.calls.task_labels["total"] = ["spleen"]
    assert "relationship" in failed(rig, requested(rig, case, parent, remove_small_blobs=True))["error"].lower()
    assert saved(rig, case) == before


def test_stale_parent_is_rejected_before_enqueue_and_during_publish(rig, monkeypatch):
    case, ct = prepared(rig)
    current = execute(rig, requested(rig, case, ct))
    jobs_before = rig.service.store.list("jobs", case["id"])
    response = requested(rig, case, ct)
    assert response.status_code in (400, 409), response.text
    assert rig.service.store.list("jobs", case["id"]) == jobs_before
    queued = requested(rig, case, current, remove_small_blobs=True)
    assert queued.status_code == 200, queued.text
    before = rig.service.store.list("revisions", case["id"])
    build = rig.service.build_revision

    def changed_while_building(*args, **kwargs):
        revision = build(*args, **kwargs)
        rig.service.store.activate(case["id"], ct["id"])
        return revision

    monkeypatch.setattr(rig.service, "build_revision", changed_while_building)
    job = rig.service.store.claim()
    rig.service.execute(job)
    failed = rig.service.store.get("jobs", queued.json()["id"])
    assert failed["status"] == "failed" and "active revision changed" in failed["error"].lower()
    assert rig.service.store.get("cases", case["id"])["active_revision_id"] == ct["id"]
    assert rig.service.store.list("revisions", case["id"]) == before


def test_invalid_explicit_task_fails_before_job_or_model_access(rig):
    case, ct = prepared(rig)
    jobs = rig.service.store.list("jobs", case["id"])
    response = requested(rig, case, ct, task="unknown-task-not-a-shell-command")
    assert response.status_code in (400, 422), response.text
    assert rig.service.store.list("jobs", case["id"]) == jobs
    assert not rig.calls.segments and not rig.calls.fingerprints


# Inference paths, through the same queue and doubles


@pytest.fixture
def two_paths(rig, tmp_path, monkeypatch):
    """`rig` whose doubles differ by path as the real ones do, with one added path."""
    stub = stand_in(tmp_path)
    monkeypatch.setenv("SCAN3D_INFERENCE_PATH_DIR", str(stub.proof.parent))
    plain_fingerprint, plain_segment = segmentation.model_fingerprint, segmentation.segment

    def fingerprint(profile, options=None, *, inference_path=None):
        result = plain_fingerprint(profile, options)
        if inference_path:
            result.update(versions=inference_path.versions(), device_request=inference_path.identity)
            result["digest"] = hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
        return result

    def segment(ct_path, output_dir, profile, progress, cancelled, options=None, *, inference_path=None):
        structures = plain_segment(ct_path, output_dir, profile, progress, cancelled, options=options)
        rig.calls.segments[-1]["inference_path"] = inference_path
        manifest = Path(output_dir) / "inference.json"
        record = json.loads(manifest.read_text())
        record["model_fingerprint"] = fingerprint(profile, options, inference_path=inference_path)
        if inference_path:
            record.update(
                inference_path=inference_path.id, runtime={"selected_device": "gpu:0", "gpu_name": GPU_NAME, "torch": "2.0", "hip": None, "cuda": "13.0"}
            )
        manifest.write_text(json.dumps(record))
        return structures

    monkeypatch.setattr(segmentation, "model_fingerprint", fingerprint)
    monkeypatch.setattr(segmentation, "segment", segment)
    return rig


def test_each_path_keeps_its_own_results_and_a_revision_records_what_ran_only_when_a_path_was_named(two_paths):
    rig = two_paths
    case, ct = prepared(rig)
    response = requested(rig, case, ct)
    default = execute(rig, response)
    # No choice leaves a job as it was.
    assert "inference_path" not in response.json() and "inference_path" not in rig.service.store.get("jobs", response.json()["id"])
    added = execute(rig, requested(rig, case, default, inference_path="nvidia"))
    assert rig.calls.segments[0]["inference_path"] is None and rig.calls.segments[1]["inference_path"].id == "nvidia"
    measured = {"device": "gpu:0", "gpu_name": GPU_NAME, "torch_version": "2.0", "hip_version": None, "cuda_version": "13.0", "results_moved_to_cpu": False}
    assert "runtime" not in default["operation"]
    assert added["operation"]["runtime"] == {"inference_path": "nvidia", "reused": False, **measured}
    default_again = execute(rig, requested(rig, case, added))
    added_again = execute(rig, requested(rig, case, default_again, inference_path="nvidia"))
    response = requested(rig, case, added_again, inference_path="default")
    assert response.json()["inference_path"] == {"id": "default", "label": "Default device", "identity": "default"}
    named_default = execute(rig, response)
    assert len(rig.calls.segments) == 2
    assert "runtime" not in default_again["operation"]
    assert added_again["operation"]["runtime"] == {"inference_path": "nvidia", "reused": True, **measured}
    assert named_default["operation"]["runtime"]["inference_path"] == "default" and named_default["operation"]["runtime"]["reused"] is True
    assert default_again["inference_manifest"] == named_default["inference_manifest"] == default["inference_manifest"]
    assert added_again["inference_manifest"] == added["inference_manifest"] != default["inference_manifest"]
    assert added["inference_manifest"]["inference_path"] == "nvidia" and "inference_path" not in default["inference_manifest"]
    public = rig.http.get(f"/api/cases/{case['id']}/revisions/{added_again['id']}").json()
    assert public["operation"]["runtime"] == added_again["operation"]["runtime"]


def test_the_card_stays_off_everything_a_later_revision_inherits(two_paths):
    # A revision can hold masks from several runs, and DICOM SEG imports copy these lists too.
    rig = two_paths
    case, ct = prepared(rig)
    added = execute(rig, requested(rig, case, ct, inference_path="nvidia"))
    public = rig.http.get(f"/api/cases/{case['id']}/revisions/{added['id']}").json()
    child = rig.service.build_revision(
        case["id"], Path(ct["ct_file"]), added["structures"], source="interactive_correction", parent=added["id"], acquisition=added["acquisition"]
    )
    assert child["operation"] is None and child["segmentation_runs"] == added["segmentation_runs"]
    assert GPU_NAME not in json.dumps(added["segmentation_runs"]) and "nvidia" not in json.dumps(added["segmentation_runs"])
    assert GPU_NAME not in json.dumps(public["model_provenance"]) and "nvidia" not in json.dumps(public["model_provenance"])


def test_a_chosen_path_is_a_frozen_public_field_and_stays_out_of_the_payload(two_paths):
    rig = two_paths
    case, ct = prepared(rig)
    bound = {"id": "nvidia", "label": GPU_NAME, "identity": f"nvidia|gpu|{GPU_NAME}"}
    response = requested(rig, case, ct, inference_path="nvidia")
    assert response.json()["inference_path"] == bound
    stored = rig.service.store.get("jobs", response.json()["id"])
    assert set(stored["payload"]) == {"revision_id", "options", "reference_image_id"} and "inference_path" not in stored["payload"]["options"]
    execute(rig, response)
    finished = rig.http.get(f"/api/jobs/{stored['id']}").json()
    assert finished["inference_path"] == bound and "payload" not in finished


def test_a_path_that_cannot_run_is_refused_with_no_job_or_model_access(two_paths, tmp_path):
    rig = two_paths
    case, ct = prepared(rig)
    jobs = rig.service.store.list("jobs", case["id"])
    unknown = requested(rig, case, ct, inference_path="absent")
    assert (unknown.status_code, unknown.json()["detail"]) == (400, "This GPU is not set up on this workstation.")
    proof = tmp_path / "paths" / "nvidia.json"
    record = json.loads(proof.read_text())
    proof.write_text(json.dumps({**record, "gpu_ready": False}))
    unavailable = requested(rig, case, ct, inference_path="nvidia")
    assert (unavailable.status_code, unavailable.json()["detail"]) == (400, f"{GPU_NAME} is unavailable: Its last GPU check failed.")
    assert rig.service.store.list("jobs", case["id"]) == jobs
    assert not rig.calls.segments and not rig.calls.fingerprints


def test_an_empty_name_is_refused_rather_than_read_as_no_choice(two_paths):
    # The command line reaches this with no schema in front of it.
    rig = two_paths
    case, ct = prepared(rig)
    with pytest.raises(ValueError, match="not set up"):
        rig.service.segment_case(case["id"], ct["id"], "total", inference_path="")


@pytest.mark.parametrize("value", ["", "Upper", "../nvidia", 7])
def test_a_malformed_path_is_a_schema_error(two_paths, value):
    rig = two_paths
    case, ct = prepared(rig)
    assert requested(rig, case, ct, inference_path=value).status_code == 422


@pytest.mark.parametrize(
    "name, expected",
    [(None, "cannot read /home/someone/venv/bin/python"), ("nvidia", "cannot read <local path>"), ("default", "cannot read <local path>")],
    ids=["no-path-keeps-its-text", "added", "named-default"],
)
def test_a_job_with_a_path_keeps_local_paths_out_of_its_public_error(two_paths, monkeypatch, caplog, name, expected):
    rig = two_paths
    case, ct = prepared(rig)

    def broken(*args, **kwargs):
        raise OSError("cannot read /home/someone/venv/bin/python")

    monkeypatch.setattr(segmentation, "segment", broken)
    job = failed(rig, requested(rig, case, ct, inference_path=name))
    assert (job["error"], job["stage"]) == (expected, "Failed")
    assert rig.http.get(f"/api/jobs/{job['id']}").json()["error"] == expected
    assert "cannot read /home/someone/venv/bin/python" in caplog.text  # The journal keeps it all.
