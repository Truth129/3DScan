"""Lossless portable rendering payloads, layer isolation and bounded export."""

import base64
from copy import deepcopy
import gzip
import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient
import nibabel as nib
import numpy as np
import pytest
from pydantic import ValidationError

from scan3d import imaging, portable_export, review_volume
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.image_assets import bind_structures, create_image_asset, public_image_asset
from scan3d.portable_export import ExportRequest, ExportTooLarge, prepare_export
from scan3d.store import identifier


@pytest.fixture
def portable_case(tmp_path):
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist")
    settings.web_dir.mkdir()
    license_text = Path("web/public/licenses/Slicer-LICENSE.txt").read_text()
    (settings.web_dir / "offline-viewer.js").write_text("/*! " + license_text + " */\nvar Scan3DOffline={initOffline(){}};")
    (settings.web_dir / "offline-viewer.css").write_text("body{background:#111}")
    source = tmp_path / "PRIVATE-PATIENT-NAME"
    source.mkdir()
    affine = np.array([[-0.8, 0.12, 0, 31.0], [0.1, 1.4, 0.2, -52.0], [0, 0, 2.2, 87.0], [0, 0, 0, 1.0]])
    values = (np.arange(7 * 9 * 35).reshape((7, 9, 35)) - 1100).astype(np.int16)
    ct = nib.Nifti1Image(values, affine)
    ct.header.set_xyzt_units("mm")
    ct.header.set_slope_inter(0.3, -1024.3)
    ct_path = source / "PRIVATE-CT.nii.gz"
    nib.save(ct, ct_path)
    structures = []
    for name, bounds in (("one", (slice(2, 6), slice(3, 8), slice(5, 29))), ("two", (slice(1, 4), slice(2, 5), slice(2, 10)))):
        mask = np.zeros(values.shape, np.uint8)
        mask[bounds] = 1
        path = source / f"PRIVATE-{name}.nii.gz"
        imaging._save_mask(mask, ct, path)
        structures.append(
            dict(
                id=f"fixture:{name}",
                name=name,
                task="fixture",
                group="Fixture",
                color="#ba9876",
                mask_file=str(path),
                volume_ml=42.5,
                voxel_count=int(mask.sum()),
                centroid_ras=None,
                bounds_ras=None,
                qc=["PRIVATE-QC-PATIENT-NAME"],
                other_metadata="PRIVATE-UNKNOWN-FIELD",
                relationship={"kind": "independent", "exclusive_with": [], "private_path": str(path)},
            )
        )
    directory = source / "revision"
    directory.mkdir()
    glb = b"glTF" + bytes(range(256)) * 7
    (directory / "model.glb").write_bytes(glb)
    geometry = imaging.geometry(ct_path)
    geometry["private_path"] = str(source)
    revision = dict(
        id=identifier(),
        case_id=identifier(),
        parent_id=identifier(),
        created_at="PRIVATE-DATE",
        directory=str(directory),
        ct_file=str(ct_path),
        geometry=geometry,
        structures=structures,
        review_status="corrected",
        synthetic=False,
        source="PRIVATE-SOURCE-NAME",
        qc=["PRIVATE-QC"],
        acquisition={"series_uid": "PRIVATE-SERIES-UID", "phase": "PRIVATE-PHASE"},
        operation={"secret": "PRIVATE-OPERATION", "runtime": {"inference_path": "PRIVATE-PATH", "gpu_name": "PRIVATE-VENDOR", "reused": True}},
        source_dir=str(source),
        label="PRIVATE-CASE-NAME",
        inference_manifest={
            "engine": "TotalSegmentator",
            "engine_version": "2.18.0",
            "resolution": "standard_full",
            "license_number": "PRIVATE-LICENSE-NUMBER",
            "runtime": {"gpu_name": "PRIVATE-VENDOR"},
            "inference_path": "PRIVATE-PATH",
            "model_fingerprint": {"device_request": "PRIVATE-PATH|gpu|PRIVATE-VENDOR", "versions": {"PRIVATE-PACKAGE": "1"}},
            "tasks": [{"task": "total", "secret": "PRIVATE-TASK-SECRET", "results_moved_to_cpu": True}],
        },
        pipeline_version="test",
    )
    return settings, revision, glb


@pytest.fixture
def portable_mr(portable_case):
    settings, revision, glb = portable_case
    image = nib.load(revision["ct_file"])
    signal = np.linspace(-0.2, 0.8, np.prod(image.shape), dtype=np.float32).reshape(image.shape)
    mr = nib.Nifti1Image(signal, image.affine)
    mr.header.set_xyzt_units("mm")
    mr.header.set_slope_inter(0.37, -0.1333)
    nib.save(mr, revision["ct_file"])
    asset = create_image_asset(
        revision["case_id"],
        revision["ct_file"],
        modality="MR",
        source={"series_uid": "PRIVATE-MR-SERIES", "study_uid": "PRIVATE-STUDY", "frame_of_reference_uid": "PRIVATE-FRAME", "sop_class_uids": ["PRIVATE-SOP"]},
        presentation_metadata={"window_width": 0.125, "window_center": -0.05, "photometric_interpretation": "MONOCHROME1"},
    )
    revision["image_asset"] = public_image_asset(asset)
    revision["reference_image_id"] = asset["id"]
    revision["structures"] = bind_structures(revision["structures"], asset["id"])
    return settings, revision, glb


def decoded_document(plan):
    document = b"".join(plan.chunks())
    assert len(document) == plan.byte_length
    text = document.decode()
    call = text.rsplit("Scan3DOffline.initOffline(", 1)[1]
    values = []
    parser = json.JSONDecoder()
    while True:
        value, end = parser.raw_decode(call)
        values.append(value)
        call = call[end:]
        if not call.startswith(","):
            assert call.startswith(");</script>")
            break
        call = call[1:]
    return text, values


def decode_volume(volume):
    descriptor = volume["descriptor"]
    chunks = {item["index"]: item["gzip_base64"] for item in volume["chunks"]}
    raw = b"".join(gzip.decompress(base64.b64decode(chunks[chunk["index"]])) for chunk in descriptor["chunks"])
    assert len(raw) == descriptor["byte_length"]
    return np.frombuffer(raw, dtype=np.dtype(descriptor["dtype"]).newbyteorder("<")).reshape(descriptor["shape"], order="F")


@pytest.mark.parametrize("segmentation,volume,plane", [values for values in itertools.product([False, True], repeat=3) if any(values)])
def test_independent_layer_combinations_embed_only_requested_artifacts(portable_case, segmentation, volume, plane):
    settings, revision, glb = portable_case
    request = ExportRequest.model_validate(
        dict(
            layers=dict(segmentation=segmentation, ct_volume=volume, ct_plane=plane),
            state={"plane": {"overlays": ["fixture:one"]}, "volume_mask": {"enabled": True, "ids": ["fixture:two"]}},
        )
    )
    original = deepcopy(revision)
    text, (manifest, model, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    assert (base64.b64decode(model) if model is not None else None) == (glb if segmentation else None)
    assert payload["layers"] == request.layers.model_dump()
    assert ("ct" in payload) == (volume or plane)
    native = [item for item in payload["masks"] if not item["descriptor"].get("volume_mask_union")]
    unions = [item for item in payload["masks"] if item["descriptor"].get("volume_mask_union")]
    assert {item["id"] for item in native} == ({"fixture:one"} if plane else set())
    assert len(unions) == int(volume)
    if volume:
        assert unions[0]["descriptor"]["selected_structure_ids"] == ["fixture:two"]
        assert unions[0]["id"] == "volume-mask-union:" + unions[0]["descriptor"]["key"]
    assert payload["schema_version"] == 1
    assert "PRIVATE-" not in text
    assert "mask_file" not in text and "ct_file" not in text and "/api/" not in text
    assert "connect-src 'none'" in text and "worker-src blob:" in text
    assert Path("web/public/licenses/Slicer-LICENSE.txt").read_text() in text
    assert manifest["label"] == "Scan3D case " + revision["case_id"][:8]
    assert manifest["review_status"] == "corrected"
    assert manifest["model_provenance"] == {"engine": "TotalSegmentator", "engine_version": "2.18.0", "resolution": "standard_full", "tasks": ["total"]}
    assert revision == original


def test_ct_hu_and_mask_crops_reconstruct_exactly_on_oblique_reflected_grid(portable_case):
    settings, revision, _ = portable_case
    request = ExportRequest.model_validate(
        {
            "layers": {"segmentation": False, "ct_volume": True, "ct_plane": True},
            "state": {"plane": {"index": 12, "overlays": ["fixture:one", "fixture:two"]}, "volume_mask": {"enabled": True, "ids": ["fixture:one"]}},
        }
    )
    _, (manifest, model, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    assert model is None
    ct = decode_volume(payload["ct"])
    descriptor = payload["ct"]["descriptor"]
    hu = (ct.astype(np.float64) * descriptor["slope"] + descriptor["intercept"]).astype(np.float32)
    np.testing.assert_array_equal(hu, np.asarray(nib.load(revision["ct_file"]).dataobj, dtype=np.float32))
    np.testing.assert_allclose(descriptor["affine_ras"], imaging.geometry(Path(revision["ct_file"]))["affine_ras"], atol=1e-6)
    assert np.linalg.det(np.asarray(descriptor["affine_ras"])[:3, :3]) < 0
    assert descriptor["quality"] == "full" and not descriptor["approximate"]
    assert len(payload["masks"]) == 3
    for embedded in payload["masks"]:
        if embedded["descriptor"].get("volume_mask_union"):
            assert embedded["descriptor"]["presentation_only"] is True
            assert embedded["descriptor"]["byte_length"] <= 8 * 1024**2
            continue
        source = next(item for item in revision["structures"] if item["id"] == embedded["id"])
        mask = decode_volume(embedded)
        crop = embedded["descriptor"]
        result = np.zeros(ct.shape, dtype=np.uint8)
        result[tuple(slice(low, low + size) for low, size in zip(crop["origin_ijk"], crop["shape"]))] = mask
        np.testing.assert_array_equal(result, np.asanyarray(nib.load(source["mask_file"]).dataobj))
        expected_affine = np.asarray(descriptor["affine_ras"]).copy()
        expected_affine[:3, 3] = imaging._world(expected_affine, crop["origin_ijk"])
        np.testing.assert_allclose(crop["affine_ras"], expected_affine, atol=1e-6)
        assert all("url" not in chunk for chunk in crop["chunks"])
    assert manifest["geometry"]["shape"] == list(ct.shape)


def test_display_margin_embeds_separate_volume_alias_and_exact_plane_overlay(portable_case):
    settings, revision, _ = portable_case
    before = deepcopy(revision)
    request = ExportRequest.model_validate(
        {
            "layers": {"segmentation": False, "ct_plane": True, "ct_volume": True},
            "state": {"plane": {"overlays": ["fixture:one"]}, "volume_mask": {"enabled": True, "ids": ["fixture:one"], "margin_mm": 2.5}},
        }
    )
    _, (_, _, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    masks = {item["id"]: item for item in payload["masks"]}
    union = next(item for item in payload["masks"] if item["descriptor"].get("volume_mask_union"))
    assert set(masks) == {"fixture:one", union["id"]}
    assert union["descriptor"]["selected_structure_ids"] == ["fixture:one"]
    assert payload["state"]["volume_mask"]["ids"] == ["fixture:one"]
    assert payload["state"]["volume_mask"]["margin_mm"] == 2.5
    exact = masks["fixture:one"]
    padded = union
    assert exact["descriptor"]["key"] != padded["descriptor"]["key"]
    assert padded["descriptor"]["display_margin_mm"] == 2.5
    assert int(decode_volume(padded).sum()) > int(decode_volume(exact).sum())
    assert revision == before
    assert ExportRequest.model_validate({"state": {"volume_mask": {"margin_mm": 0.0625}}}).state.volume_mask.margin_mm == 0.063
    for margin in [-0.01, 5.01, float("nan"), float("inf")]:
        with pytest.raises(ValidationError):
            ExportRequest.model_validate({"state": {"volume_mask": {"margin_mm": margin}}})


def test_no_segmentation_model_access_when_only_ct_is_selected(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    (Path(revision["directory"]) / "model.glb").unlink()
    original = Path.open

    def guarded(path, *args, **kwargs):
        if path.name == "model.glb":
            pytest.fail("An excluded segmentation model must never be opened")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_plane": True}})
    _, (_, model, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    assert model is None and payload["ct"] and payload["masks"] == []


def test_prepared_ct_without_segmentation_exports_ct_and_legacy_get_remains_segmentation_only(portable_case):
    settings, revision, glb = portable_case
    _, arguments = decoded_document(prepare_export(revision, revision["case_id"], settings, ExportRequest(), legacy=True))
    assert len(arguments) == 2 and base64.b64decode(arguments[1]) == glb
    revision["structures"] = []
    request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_volume": True}})
    _, (manifest, model, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    assert manifest["structures"] == [] and model is None and "ct" in payload
    with pytest.raises(ValueError, match="no segmentation"):
        prepare_export(revision, revision["case_id"], settings, ExportRequest())


def test_names_are_escaped_and_no_application_metadata_or_credentials_are_embedded(portable_case):
    settings, revision, _ = portable_case
    revision["structures"][0]["name"] = '</script><script>alert("anatomy")</script>'
    text, (manifest, _) = decoded_document(prepare_export(revision, revision["case_id"], settings, ExportRequest(), legacy=True))
    assert manifest["structures"][0]["name"] == '</script><script>alert("anatomy")</script>'
    assert "\\u003c/script>" in text and '<script>alert("anatomy")' not in text
    assert "PRIVATE-" not in text
    assert not {"case_id", "id", "created_at", "parent_id", "model_url", "directory", "ct_file", "operation", "acquisition"} & manifest.keys()
    assert "private_path" not in json.dumps(manifest)


def test_header_limits_reject_oversized_ct_before_dense_decode(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    image = SimpleNamespace(shape=(512, 512, 2048), get_data_dtype=lambda: np.dtype("int16"))
    monkeypatch.setattr(imaging, "_image", lambda _: (image, np.eye(4)))
    # Ample memory, so the header limit rejects it rather than this machine's free RAM.
    monkeypatch.setattr(portable_export, "_available_memory", lambda: 1024**4)
    monkeypatch.setattr(review_volume, "scalar_volume", lambda *args, **kwargs: pytest.fail("Oversized CT must be rejected before decoding"))
    request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_volume": True}})
    with pytest.raises(ExportTooLarge, match="512 MiB"):
        prepare_export(revision, revision["case_id"], settings, request)


def test_low_memory_rejected_before_dense_decode(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    monkeypatch.setattr(portable_export, "_available_memory", lambda: 1)
    monkeypatch.setattr(review_volume, "scalar_volume", lambda *args, **kwargs: pytest.fail("Low-memory export must be rejected before decoding"))
    request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_plane": True}})
    with pytest.raises(ExportTooLarge, match="insufficient available memory"):
        prepare_export(revision, revision["case_id"], settings, request)


def test_mask_and_html_budget_reject_before_streaming(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_plane": True}, "state": {"plane": {"overlays": ["fixture:one"]}}})
    monkeypatch.setattr(portable_export, "MAX_MASK_BYTES", 1)
    with pytest.raises(ExportTooLarge, match="mask crops"):
        prepare_export(revision, revision["case_id"], settings, request)
    monkeypatch.setattr(portable_export, "MAX_MASK_BYTES", 192 * 1024**2)
    monkeypatch.setattr(portable_export, "MAX_HTML_BYTES", 1)
    with pytest.raises(ExportTooLarge, match="size limit|download limit"):
        prepare_export(revision, revision["case_id"], settings, ExportRequest())


def test_unknown_segment_and_out_of_range_plane_are_rejected_before_decode(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    monkeypatch.setattr(review_volume, "scalar_volume", lambda *args, **kwargs: pytest.fail("Invalid references must be rejected before decoding"))
    for state in (
        {"plane": {"overlays": ["other:secret"]}},
        {"volume_mask": {"enabled": True, "ids": ["other:secret"]}},
        {"display": {"other:secret": {"visible": True, "opacity": 1}}},
        {"selected": "other:secret"},
    ):
        request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_plane": True}, "state": state})
        with pytest.raises(ValueError, match="outside this saved revision"):
            prepare_export(revision, revision["case_id"], settings, request)
    request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_plane": True}, "state": {"plane": {"index": 999}}})
    with pytest.raises(ValueError, match="outside the reference CT"):
        prepare_export(revision, revision["case_id"], settings, request)


@pytest.mark.parametrize(
    "changes",
    [
        {"layers": {"segmentation": False, "ct_volume": False, "ct_plane": False}},
        {"layers": {"ct_volume": "true"}},
        {"state": {"path": "/private"}},
        {"state": {"volume": {"density": 20}}},
        {"state": {"volume": {"shift": float("nan")}}},
        {"state": {"volume": {"preset": "unknown-volume-preset"}}},
        {"state": {"plane": {"axis": True}}},
        {"state": {"plane": {"axis": 1, "plane": "axial"}}},
        {"state": {"volume": {"crop": [[0, 0, 0], [0, 1, 1]]}}},
        {"state": {"display": {"fixture:one": {"opacity": -1}}}},
    ],
)
def test_render_state_is_strict_finite_and_bounded(changes):
    with pytest.raises(ValidationError):
        ExportRequest.model_validate(changes)


def test_encoded_files_stream_in_bounded_blocks_with_exact_base64_roundtrip(tmp_path):
    data = bytes(range(256)) * 1800 + b"end!"
    path = tmp_path / "binary"
    path.write_bytes(data)
    file = portable_export.EncodedFile(path, len(data))
    chunks = list(file.chunks())
    assert max(map(len, chunks)) <= portable_export._BASE64_READ_BYTES * 4 // 3
    assert len(b"".join(chunks)) == file.encoded_length
    assert base64.b64decode(b"".join(chunks)) == data


def test_empty_mask_and_required_id_union_remain_explicit_without_duplicate_data(portable_case):
    settings, revision, _ = portable_case
    image, _ = imaging._image(Path(revision["ct_file"]))
    imaging._save_mask(np.zeros(image.shape, np.uint8), image, Path(revision["structures"][0]["mask_file"]))
    request = ExportRequest.model_validate(
        {
            "layers": {"segmentation": False, "ct_volume": True, "ct_plane": True},
            "state": {"plane": {"overlays": ["fixture:one", "fixture:one"]}, "volume_mask": {"enabled": True, "mode": "outside", "ids": ["fixture:one"]}},
        }
    )
    _, (_, _, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    assert len(payload["masks"]) == 2
    embedded = next(item for item in payload["masks"] if item["descriptor"].get("volume_mask_union"))
    assert embedded["descriptor"]["empty"] is True
    assert embedded["descriptor"]["shape"] == [1, 1, 1]
    assert decode_volume(embedded).sum() == 0
    assert payload["state"]["volume_mask"]["mode"] == "outside"


def test_model_limit_is_checked_without_reading_sparse_large_file(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    model = Path(revision["directory"]) / "model.glb"
    with model.open("wb") as stream:
        stream.truncate(portable_export.MAX_MODEL_BYTES + 1)
    original = Path.open

    def guarded(path, *args, **kwargs):
        if path == model:
            pytest.fail("An oversized model must be rejected before reading it")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded)
    with pytest.raises(ExportTooLarge, match="segmentation model"):
        prepare_export(revision, revision["case_id"], settings, ExportRequest())


def test_camera_state_is_finite_owned_and_direction_normalized():
    data = {"state": {"camera": {"position_ras": [0, -200, 0], "target_ras": [0, 0, 0], "up_ras": [0, 0, 4], "height_mm": 350, "zoom": 1.25}}}
    request = ExportRequest.model_validate(data)
    assert request.state.camera.up_ras == (0, 0, 1)
    assert data["state"]["camera"]["up_ras"] == [0, 0, 4]
    data["state"]["camera"]["up_ras"] = [0, 0, 0]
    with pytest.raises(ValidationError, match="nonzero"):
        ExportRequest.model_validate(data)


def test_supported_volume_presets_match_generated_frontend_catalog():
    generated = Path("web/src/lib/slicer-volume-presets.generated.ts").read_text()
    ids = set(re.findall(r'^  "([^"\n]+)": \{', generated, re.M))
    assert len(ids) == 31
    ids.remove("uCT-Skull")
    assert portable_export.SUPPORTED_VOLUME_PRESETS == ids | {"angiography", "bone", "soft-tissue", "lung", "CT-Cinematic-Heart", "MR-SignalWindow"}
    for preset in portable_export.SUPPORTED_VOLUME_PRESETS:
        assert ExportRequest.model_validate({"state": {"volume": {"preset": preset}}}).state.volume.preset == preset


@pytest.mark.parametrize("legacy", [False, True])
def test_mr_surface_export_retains_only_safe_source_display_context(portable_mr, legacy):
    settings, revision, glb = portable_mr
    original = deepcopy(revision)
    text, arguments = decoded_document(prepare_export(revision, revision["case_id"], settings, ExportRequest(), legacy=legacy))
    manifest, model = arguments[:2]
    assert base64.b64decode(model) == glb
    context = manifest["image_context"]
    assert set(context) == {"reference_image_id", "modality", "scalar", "presentation"}
    assert context["reference_image_id"] == revision["reference_image_id"]
    assert context["modality"] == "MR" and context["scalar"]["units"] == "signal"
    assert context["presentation"] == revision["image_asset"]["presentation"]
    assert "PRIVATE-" not in text and "source_uid" not in text and "content_sha256" not in text
    assert "image_asset" not in manifest and "case_id" not in context and "source" not in context
    assert revision == original
    if not legacy:
        assert "ct" not in arguments[2]
        assert arguments[2]["state"]["volume"]["preset"] == "MR-SignalWindow"


def test_mr_layers_preserve_fractional_signal_and_bound_mask_identity(portable_mr):
    settings, revision, _ = portable_mr
    request = ExportRequest.model_validate(
        {
            "layers": {"segmentation": False, "ct_volume": True, "ct_plane": True},
            "state": {"plane": {"overlays": ["fixture:one"], "index": 12}, "volume_mask": {"enabled": True, "ids": ["fixture:two"], "margin_mm": 1}},
        }
    )
    original_request, original_revision = request.model_dump(), deepcopy(revision)
    text, (manifest, model, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    assert model is None and "PRIVATE-" not in text
    context, descriptor = manifest["image_context"], payload["ct"]["descriptor"]
    assert {key: descriptor[key] for key in context} == context
    assert descriptor["dtype"] == "float32" and descriptor["slope"] == 1 and descriptor["intercept"] == 0
    signal = decode_volume(payload["ct"])
    np.testing.assert_array_equal(signal, np.asarray(nib.load(revision["ct_file"]).dataobj, dtype=np.float32))
    assert np.any(signal < 0) and np.any(signal != np.rint(signal))
    assert payload["state"]["volume"]["preset"] == "MR-SignalWindow"
    assert payload["state"]["volume"]["signal_window"] == {"window": 0.125, "level": -0.05, "invert": True}
    assert payload["state"]["plane"]["window"] == 0.125 and payload["state"]["plane"]["level"] == -0.05
    assert {item["descriptor"]["reference_image_id"] for item in payload["masks"]} == {revision["reference_image_id"]}
    unbound = review_volume.scalar_volume(revision["ct_file"], settings.data_dir / "review-cache", "full", presentation=context["presentation"])
    assert unbound["key"] != descriptor["key"]
    exact = next(item for item in payload["masks"] if item["id"] == "fixture:one")
    unbound_mask = review_volume.mask_volume(revision["ct_file"], revision["structures"][0]["mask_file"], settings.data_dir / "review-cache", "full")
    assert unbound_mask["key"] != exact["descriptor"]["key"]
    assert request.model_dump() == original_request and revision == original_revision


def test_mr_export_keeps_explicit_fractional_signal_and_plane_windows(portable_mr):
    settings, revision, _ = portable_mr
    window = {"window": 0.03125, "level": -0.0075, "invert": False}
    request = ExportRequest.model_validate(
        {
            "layers": {"segmentation": False, "ct_volume": True, "ct_plane": True},
            "state": {"volume": {"preset": "MR-SignalWindow", "signal_window": window}, "plane": {"window": 0.07, "level": 0.02}},
        }
    )
    _, (_, _, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    assert payload["state"]["volume"]["signal_window"] == window
    assert payload["state"]["plane"]["window"] == 0.07 and payload["state"]["plane"]["level"] == 0.02
    assert payload["ct"]["descriptor"]["presentation"]["default_window"] == 0.125


@pytest.mark.parametrize(
    "volume", [{"preset": "CT-Cinematic-Heart"}, {"preset": "angiography"}, {"preset": "CT-Bone"}, {"preset": "MR-SignalWindow", "shift": 25}]
)
def test_mr_rejects_ct_calibration_before_volume_decode(portable_mr, monkeypatch, volume):
    settings, revision, _ = portable_mr
    monkeypatch.setattr(review_volume, "scalar_volume", lambda *args, **kwargs: pytest.fail("Invalid appearance decoded MRI"))
    request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_volume": True}, "state": {"volume": volume}})
    with pytest.raises(ValueError, match="MR .*CT"):
        prepare_export(revision, revision["case_id"], settings, request)


@pytest.mark.parametrize("field", ["reference_image_id", "structure_reference_image_id"])
def test_mr_export_rejects_cross_source_binding_before_decode(portable_mr, monkeypatch, field):
    settings, revision, _ = portable_mr
    if field == "reference_image_id":
        revision["reference_image_id"] = identifier()
    else:
        revision["structures"][0]["reference_image_id"] = identifier()
    monkeypatch.setattr(review_volume, "scalar_volume", lambda *args, **kwargs: pytest.fail("Wrong source decoded MRI"))
    with pytest.raises(ValueError, match="reference image"):
        prepare_export(revision, revision["case_id"], settings, ExportRequest())


@pytest.mark.parametrize(
    "window",
    [
        {"window": 0, "level": 0},
        {"window": -1, "level": 0},
        {"window": float("inf"), "level": 0},
        {"window": 1, "level": float("nan")},
        {"window": True, "level": 0},
        {"window": 1, "level": False},
        {"window": 1, "level": 0, "invert": "false"},
        {"window": 1, "level": 0, "source_file": "private.nii.gz"},
    ],
)
def test_signal_window_requires_finite_numbers_positive_width_and_strict_inversion(window):
    with pytest.raises(ValidationError):
        ExportRequest.model_validate({"state": {"volume": {"preset": "MR-SignalWindow", "signal_window": window}}})


@pytest.mark.parametrize("window", [None, {"window": 0.125, "level": -0.05, "invert": False}])
def test_optional_signal_window_serialization_preserves_ct_shape_and_mr_values(window):
    request = ExportRequest.model_validate({"state": {"volume": {"signal_window": window}}})
    serialized = [
        request.state.volume.model_dump(),
        json.loads(request.state.volume.model_dump_json()),
        request.model_dump()["state"]["volume"],
        json.loads(request.model_dump_json())["state"]["volume"],
    ]
    for volume in serialized:
        if window is None:
            assert "signal_window" not in volume
        else:
            assert volume["signal_window"] == window


def test_legacy_ct_surface_export_does_not_require_source_pixels(portable_case):
    settings, revision, glb = portable_case
    Path(revision["ct_file"]).unlink()
    _, (manifest, model) = decoded_document(prepare_export(revision, revision["case_id"], settings, ExportRequest(), legacy=True))
    assert base64.b64decode(model) == glb and "image_context" not in manifest


def test_bound_ct_export_keeps_hu_pixels_and_legacy_appearance_defaults(portable_case):
    settings, revision, _ = portable_case
    asset = create_image_asset(revision["case_id"], revision["ct_file"], acquisition=revision["acquisition"])
    revision["image_asset"] = public_image_asset(asset)
    revision["reference_image_id"] = asset["id"]
    request = ExportRequest.model_validate({"layers": {"segmentation": False, "ct_volume": True, "ct_plane": True}})
    text, (manifest, _, payload) = decoded_document(prepare_export(revision, revision["case_id"], settings, request))
    descriptor = payload["ct"]["descriptor"]
    assert descriptor["modality"] == "CT" and descriptor["scalar"]["units"] == "HU"
    assert descriptor["reference_image_id"] == asset["id"]
    raw = decode_volume(payload["ct"])
    pixels = (raw.astype(np.float64) * descriptor["slope"] + descriptor["intercept"]).astype(np.float32)
    np.testing.assert_array_equal(pixels, np.asarray(nib.load(revision["ct_file"]).dataobj, dtype=np.float32))
    assert payload["state"] == request.state.model_dump(mode="json", exclude_none=True)
    assert manifest["image_context"]["presentation"]["default_window"] == 400
    assert "PRIVATE-" not in text


def test_changed_export_file_cannot_silently_truncate_or_extend_stream(tmp_path):
    path = tmp_path / "artifact"
    path.write_bytes(b"abc")
    planned = portable_export.EncodedFile(path, 3)
    path.write_bytes(b"ab")
    with pytest.raises(ValueError, match="artifact changed"):
        b"".join(planned.chunks())
    path.write_bytes(b"abcd")
    with pytest.raises(ValueError, match="artifact changed"):
        b"".join(planned.chunks())


def test_export_api_headers_legacy_post_authorization_and_limits(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    app = create_app(settings)
    service = app.state.service
    case = service.new_case("PRIVATE-PATIENT-NAME")
    revision["case_id"] = case["id"]
    service.store.save("revisions", revision)
    original = deepcopy(service.store.revision(case["id"], revision["id"]))
    url = f"/api/cases/{case['id']}/revisions/{revision['id']}/export.html"
    client = TestClient(app, base_url="http://127.0.0.1")
    try:
        legacy = client.get(url)
        assert legacy.status_code == 200, legacy.text
        assert legacy.headers["cache-control"] == "private, no-store"
        assert legacy.headers["content-disposition"].endswith('.html"')
        body = {"layers": {"segmentation": False, "ct_volume": True, "ct_plane": True}, "state": {"plane": {"overlays": ["fixture:one"]}}}
        response = client.post(url, json=body)
        assert response.status_code == 200, response.text
        assert int(response.headers["content-length"]) == len(response.content)
        assert response.headers["content-type"].startswith("text/html")
        assert "PRIVATE-" not in response.text
        assert client.post(url, json=body, headers={"Origin": "https://other.example"}).status_code == 403
        wrong = url.replace(case["id"], identifier())
        assert client.post(wrong, json=body).status_code == 404
        assert client.post(url, json={"layers": {"segmentation": False, "ct_volume": False, "ct_plane": False}}).status_code == 422
        monkeypatch.setattr(portable_export, "MAX_HTML_BYTES", 1)
        assert client.post(url, json={}).status_code == 413
        assert service.store.revision(case["id"], revision["id"]) == original
        assert service.store.list("jobs") == []
    finally:
        client.close()


def test_new_partial_segment_display_defaults_to_fifty_percent():
    request = ExportRequest.model_validate({"state": {"display": {"fixture:one": {"visible": True}}}})
    assert request.state.display["fixture:one"].opacity == 0.5
    explicit = ExportRequest.model_validate({"state": {"display": {"fixture:one": {"visible": True, "opacity": 1}}}})
    assert explicit.state.display["fixture:one"].opacity == 1
