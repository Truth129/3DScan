"""Real static MR import, preparation and reference isolation without inference."""

from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pydicom
import pytest

from scan3d import segmentation, surfaces
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.image_assets import bind_structures, create_image_asset
from scan3d.segmentation_options import normalize_options
from scan3d.store import identifier
from test_mr_dicom import write_mr_series
from test_enhanced_mr import write_enhanced_mr


@pytest.fixture
def mr_rig(tmp_path, monkeypatch):
    import shutil

    if not shutil.which("dcm2niix"):
        pytest.skip("Native dcm2niix is not installed")
    source = write_mr_series(tmp_path / "source")
    original = {path.name: path.read_bytes() for path in source.glob("*.dcm")}

    def no_model(*args, **kwargs):
        pytest.fail("Static MR preparation must not access a model or GPU")

    monkeypatch.setattr(segmentation, "_resolve_device", no_model)
    monkeypatch.setattr(segmentation, "model_fingerprint", no_model)
    monkeypatch.setattr(segmentation, "segment", no_model)
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "web"))
    http = TestClient(app, base_url="http://127.0.0.1")
    # Use the real queue synchronously, with no worker lifespan or model access.
    yield SimpleNamespace(http=http, service=app.state.service, source=source, original=original, tmp_path=tmp_path)
    http.close()


def import_mr(rig, source=None):
    response = rig.http.post("/api/cases/import-local", json={"path": str(source or rig.source), "label": "Synthetic MR"})
    assert response.status_code == 200, response.text
    return response.json()


def prepare_mr(rig, case=None, *, series_uid=None):
    case = case or import_mr(rig)
    selected = series_uid or case["series"][0]["uid"]
    response = rig.http.post(f"/api/cases/{case['id']}/process", json={"series_uid": selected, "profile": "other", "phase": "unknown"})
    assert response.status_code == 200, response.text
    job = rig.service.store.claim()
    assert job and job["id"] == response.json()["id"]
    rig.service.execute(job)
    completed = rig.service.store.get("jobs", job["id"])
    assert completed["status"] == "completed", completed.get("error")
    return case, rig.service.store.revision(case["id"], completed["revision_id"])


def test_real_mr_api_import_prepare_and_scalar_transport_preserve_source_policy(mr_rig, monkeypatch):
    rig = mr_rig
    case = import_mr(rig)
    assert case["active_revision_id"] is None and not rig.service.store.list("jobs")
    selected = case["series"][0]
    assert selected["modality"] == "MR" and selected["supported"] and selected["unsupported_reason"] is None
    assert "files" not in selected and "source_dir" not in case
    assert selected["echo_time_ms"] == 3 and selected["sequence_name"] == "synthetic_t1"
    monkeypatch.setattr(surfaces, "prepare_surface", lambda *args, **kwargs: pytest.fail("Preparation built a surface"))
    case, revision = prepare_mr(rig, case)
    assert revision["kind"] == "image" and revision["structures"] == []
    assert revision["surface_assets"] == [] and revision["inference_manifest"] is None
    asset = rig.service.store.resolve_reference(revision)
    assert asset["id"] == revision["reference_image_id"]
    assert asset["modality"] == "MR" and asset["scalar"]["units"] == "signal"
    assert asset["source"]["series_uid"] == selected["uid"]
    assert asset["presentation"]["default_window"] == 249.5 and asset["presentation"]["default_level"] == 125.25
    assert set(asset["capabilities"]) == {"mpr", "segmentation", "voxel_measurements", "volume_rendering"}
    prefix = f"/api/cases/{case['id']}/revisions/{revision['id']}"
    public = rig.http.get(prefix).json()
    assert public["image_asset"]["id"] == asset["id"] and "ct_file" not in public
    images = rig.http.get(f"/api/cases/{case['id']}/images").json()
    assert [item["id"] for item in images] == [asset["id"]]
    assert "file_path" not in images[0]
    expected = np.asarray(nib.load(asset["file_path"]).dataobj)
    for endpoint in ("image-volume", "ct-volume"):
        response = rig.http.get(prefix + "/" + endpoint)
        assert response.status_code == 200, response.text
        descriptor = response.json()
        assert descriptor["modality"] == "MR" and descriptor["scalar"]["units"] == "signal"
        assert descriptor["reference_image_id"] == asset["id"] and descriptor["presentation"] == asset["presentation"]
        raw = b"".join(rig.http.get(item["url"]).content for item in descriptor["chunks"])
        samples = np.frombuffer(raw, dtype=descriptor["dtype"]).reshape(descriptor["shape"], order="F")
        np.testing.assert_allclose(samples * descriptor["slope"] + descriptor["intercept"], expected, rtol=0, atol=1e-6)
    preview = rig.http.get(prefix + "/image-volume", params={"quality": "preview"}).json()
    assert preview["dtype"] == "float32"
    preview_raw = b"".join(rig.http.get(item["url"]).content for item in preview["chunks"])
    preview_values = np.frombuffer(preview_raw, dtype=preview["dtype"]).reshape(preview["shape"], order="F")
    np.testing.assert_allclose(preview_values, expected, rtol=0, atol=1e-6)
    voxel = np.array([3, 4, 2])
    ras = np.asarray(asset["geometry"]["affine_ras"]) @ np.r_[voxel, 1]
    probe = rig.http.get(prefix + "/probe", params=dict(zip(("x", "y", "z"), ras[:3])))
    assert probe.status_code == 200, probe.text
    sample = probe.json()
    assert sample["value"] == pytest.approx(expected[tuple(voxel)]) and sample["units"] == "signal"
    assert sample["reference_image_id"] == asset["id"]
    assert {path.name: path.read_bytes() for path in rig.source.glob("*.dcm")} == rig.original
    copied = Path(rig.service.store.get("cases", case["id"])["source_dir"])
    assert {path.name: path.read_bytes() for path in copied.glob("*.dcm")} == rig.original


def test_mr_api_catalog_defaults_and_wrong_modality_rejection_do_not_touch_gpu(mr_rig):
    rig = mr_rig
    case, revision = prepare_mr(rig)
    catalog = rig.http.get("/api/segmentation-options", params={"modality": "MR"})
    assert catalog.status_code == 200, catalog.text
    options = catalog.json()
    assert options["defaults"]["task"] == "total_mr"
    assert {item["id"] for item in options["tasks"]} >= {"total_mr", "body_mr", "vertebrae_mr"}
    assert all(item["modality"] == "MR" and not item["body_seg_supported"] for item in options["tasks"])
    assert normalize_options("other", modality="MR")["tasks"] == ["total_mr"]
    capabilities = rig.http.get("/api/imaging-capabilities").json()
    assert {item["modality"] for item in capabilities["modalities"]} == {"CT", "MR"}
    before = deepcopy(rig.service.store.list("jobs"))
    response = rig.http.post(f"/api/cases/{case['id']}/segment", json={"revision_id": revision["id"], "task": "total"})
    assert response.status_code == 400 and "MR" in response.json()["detail"]
    assert rig.service.store.list("jobs") == before
    response = rig.http.post(f"/api/cases/{case['id']}/segment", json={"revision_id": revision["id"], "task": "total_mr", "body_seg": True})
    assert response.status_code == 400
    assert rig.service.store.list("jobs") == before
    response = rig.http.post(f"/api/cases/{case['id']}/segment", json={"revision_id": revision["id"], "task": "total_mr"})
    assert response.status_code == 200, response.text
    queued = rig.service.store.get("jobs", response.json()["id"])
    assert queued["payload"]["reference_image_id"] == revision["reference_image_id"]
    assert queued["payload"]["options"]["tasks"] == ["total_mr"]


def test_mr_sigmoid_source_prepares_computed_linear_window_without_changing_pixels(mr_rig):
    rig = mr_rig
    for path in rig.source.glob("*.dcm"):
        ds = pydicom.dcmread(path)
        ds.PhotometricInterpretation = "MONOCHROME1"
        ds.VOILUTFunction = "SIGMOID"
        ds.save_as(path, enforce_file_format=True)
    original = {path.name: path.read_bytes() for path in rig.source.glob("*.dcm")}
    case, revision = prepare_mr(rig)
    selected = case["series"][0]
    assert selected["voi_lut_function"] == "SIGMOID"
    assert selected["window_center"] is None and selected["window_width"] is None
    policy = revision["image_asset"]["presentation"]
    values = np.asarray(nib.load(revision["ct_file"]).dataobj)
    low, high = np.percentile(values[values != 0], [1, 99])
    assert policy["default_level"] == pytest.approx((low + high) / 2)
    assert policy["default_window"] == pytest.approx(high - low)
    assert policy["photometric_interpretation"] == "MONOCHROME1"
    assert {path.name: path.read_bytes() for path in rig.source.glob("*.dcm")} == original
    copied = Path(rig.service.store.get("cases", case["id"])["source_dir"])
    assert {path.name: path.read_bytes() for path in copied.glob("*.dcm")} == original


def test_enhanced_mr_api_prepares_one_instance_and_binds_actual_phase_selector(mr_rig):
    rig = mr_rig
    source = write_enhanced_mr(rig.tmp_path / "enhanced-source" / "volume.dcm", temporal_indices=[6] * 5)
    before = source.read_bytes()
    ds = pydicom.dcmread(source, stop_before_pixels=True)
    case, revision = prepare_mr(rig, import_mr(rig, source.parent))
    selected = case["series"][0]
    assert selected["storage_kind"] == "enhanced_mr" and selected["acquired_frame_count"] == 5
    assert selected["temporal_position_index"] == 6 and selected["slice_count"] == 5
    asset = rig.service.store.resolve_reference(revision)
    assert asset["source"]["series_uid"] == ds.SeriesInstanceUID
    assert asset["source"]["sop_class_uids"] == [str(ds.SOPClassUID)]
    assert asset["frame_selector"] == {"source_sop_instance_uid": ds.SOPInstanceUID, "stack_id": "1", "temporal_position_index": 6}
    descriptor = rig.http.get(f"/api/cases/{case['id']}/revisions/{revision['id']}/image-volume").json()
    assert descriptor["reference_image_id"] == asset["id"] and descriptor["modality"] == "MR"
    assert descriptor["scalar"]["units"] == "signal" and source.read_bytes() == before
    assert len(rig.service.store.get("cases", case["id"])["series"][0]["files"]) == 1


def test_mr_rejects_cardiac_ct_timing_without_queuing(mr_rig):
    rig = mr_rig
    case = import_mr(rig)
    response = rig.http.post(
        f"/api/cases/{case['id']}/process", json={"series_uid": case["series"][0]["uid"], "profile": "corcta", "phase": "unknown", "rr_percent": 70}
    )
    assert response.status_code == 400 and "static MR" in response.json()["detail"]
    assert not rig.service.store.list("jobs")


def test_mr_derived_masks_keep_source_policy_and_reject_cross_sequence_bindings(mr_rig):
    rig = mr_rig
    case, parent = prepare_mr(rig)
    asset = rig.service.store.resolve_reference(parent)
    other = create_image_asset(case["id"], asset["file_path"], modality="MR", source={**asset["source"], "series_uid": "other-mr-sequence"})
    assert other["id"] != asset["id"] and other["geometry"] == asset["geometry"]
    values = np.zeros(asset["geometry"]["shape"], np.uint8)
    values[2:5, 2:6, 1:4] = 1
    mask = rig.tmp_path / "roi.nii.gz"
    nib.save(nib.Nifti1Image(values, np.asarray(asset["geometry"]["affine_ras"])), mask)
    structure = dict(id="custom:roi", name="Synthetic MR ROI", task="custom", group="Other", color="#cb78b0", mask_file=str(mask))
    with pytest.raises(ValueError, match="another reference"):
        rig.service.build_revision(
            case["id"],
            Path(asset["file_path"]),
            bind_structures([structure], other["id"]),
            source="invalid_cross_sequence",
            parent=parent["id"],
            acquisition=parent["acquisition"],
        )
    with pytest.raises(ValueError, match="series"):
        rig.service.build_revision(
            case["id"],
            Path(asset["file_path"]),
            [structure],
            source="invalid_cross_sequence",
            parent=parent["id"],
            acquisition={**parent["acquisition"], "series_uid": "other-mr-sequence"},
        )
    child = rig.service.build_revision(
        case["id"],
        Path(asset["file_path"]),
        [structure],
        source="synthetic_correction",
        parent=parent["id"],
        acquisition=parent["acquisition"],
        image_source=asset["source"],
    )
    rig.service.store.commit_revision(child)
    assert child["image_asset"] == parent["image_asset"]
    assert child["reference_image_id"] == parent["reference_image_id"]
    assert child["structures"][0]["reference_image_id"] == asset["id"]
    expected_ml = np.count_nonzero(values) * abs(np.linalg.det(np.asarray(asset["geometry"]["affine_ras"])[:3, :3])) / 1000
    assert child["structures"][0]["volume_ml"] == pytest.approx(expected_ml)


def test_mr_view_only_capability_blocks_segmentation_before_enqueue(mr_rig):
    rig = mr_rig
    _, parent = prepare_mr(rig)
    case = rig.service.new_case("Synthetic view-only MR")
    asset = create_image_asset(case["id"], parent["ct_file"], modality="MR", acquisition=parent["acquisition"])
    asset["capabilities"].remove("segmentation")
    rig.service.store.register_image_asset(asset)
    revision = rig.service.build_image_revision(case["id"], Path(parent["ct_file"]), modality="MR", acquisition=parent["acquisition"])
    rig.service.store.commit_revision(revision)
    response = rig.http.post(f"/api/cases/{case['id']}/segment", json={"revision_id": revision["id"], "task": "total_mr"})
    assert response.status_code == 400 and "segmentation" in response.json()["detail"]
    assert not rig.service.store.list("jobs", case["id"])


def test_mr_saved_output_restoration_keeps_existing_corrections_and_reference(mr_rig):
    rig = mr_rig
    case, prepared = prepare_mr(rig)
    source = nib.load(prepared["ct_file"])
    run_id = "run-" + identifier()
    run = rig.service.store.case_dir(case["id"]) / "inference" / "synthetic-mr-cache" / run_id
    native = run / "total_mr"
    native.mkdir(parents=True)
    masks = {}
    for name, region in (("liver", (slice(1, 4), slice(2, 5), slice(1, 3))), ("spleen", (slice(5, 8), slice(3, 6), slice(2, 4)))):
        values = np.zeros(source.shape, np.uint8)
        values[region] = 1
        path = native / f"{name}.nii.gz"
        nib.save(nib.Nifti1Image(values, source.affine), path)
        masks[name] = values
    manifest = {
        "status": "completed",
        "run_id": run_id,
        "modality": "MR",
        "input_sha256": hashlib.sha256(Path(prepared["ct_file"]).read_bytes()).hexdigest(),
        "tasks": [{"task": "total_mr", "status": "completed", "labels": ["liver", "spleen"]}],
    }
    (run / "inference.json").write_text(json.dumps(manifest))
    # An existing corrected liver must survive while a saved spleen is restored.
    corrected_values = masks["liver"].copy()
    corrected_values[1, 2, 1] = 0
    corrected_path = rig.tmp_path / "corrected-liver.nii.gz"
    nib.save(nib.Nifti1Image(corrected_values, source.affine), corrected_path)
    corrected = rig.service.build_revision(
        case["id"],
        Path(prepared["ct_file"]),
        [dict(id="total_mr:liver", name="Liver", task="total_mr", group="Abdominal organs", color="#d88a72", mask_file=str(corrected_path))],
        source="synthetic_mr_correction",
        parent=prepared["id"],
        acquisition=prepared["acquisition"],
        inference_manifest=manifest,
    )
    rig.service.store.commit_revision(corrected)
    original_bytes = corrected_path.read_bytes()
    queued = rig.service.include_all_outputs(case["id"])
    job = rig.service.store.claim()
    assert job and job["id"] == queued["id"]
    rig.service.execute(job)
    completed = rig.service.store.get("jobs", job["id"])
    assert completed["status"] == "completed", completed.get("error")
    restored = rig.service.store.revision(case["id"], completed["revision_id"])
    assert restored["reference_image_id"] == prepared["reference_image_id"]
    assert restored["image_asset"] == prepared["image_asset"]
    assert {item["id"] for item in restored["structures"]} == {"total_mr:liver", "total_mr:spleen"}
    for item in restored["structures"]:
        expected = corrected_values if item["id"] == "total_mr:liver" else masks["spleen"]
        np.testing.assert_array_equal(nib.load(item["mask_file"]).dataobj, expected)
    assert corrected_path.read_bytes() == original_bytes


def test_ct_only_deployment_keeps_prepared_mr_but_refuses_new_mr_work(mr_rig):
    rig = mr_rig
    case, revision = prepare_mr(rig)
    ct_only = TestClient(
        create_app(Settings(rig.tmp_path / "data", (rig.tmp_path,), web_dir=rig.tmp_path / "web", modalities=("CT",))), base_url="http://127.0.0.1"
    )
    reason = "MR images are not enabled on this workstation"
    series = ct_only.get(f"/api/cases/{case['id']}").json()["series"][0]
    assert series["supported"] is False and series["unsupported_reason"] == reason
    segment = ct_only.post(f"/api/cases/{case['id']}/segment", json={"revision_id": revision["id"], "task": "total_mr"})
    process = ct_only.post(f"/api/cases/{case['id']}/process", json={"series_uid": series["uid"], "profile": "other", "phase": "unknown"})
    assert segment.status_code == process.status_code == 400
    assert segment.json()["detail"] == process.json()["detail"] == reason
    assert len(rig.service.store.list("jobs", case["id"])) == 1  # Only the original preparation.
    assert ct_only.get(f"/api/cases/{case['id']}/revisions/{revision['id']}").status_code == 200
    ct_only.close()
