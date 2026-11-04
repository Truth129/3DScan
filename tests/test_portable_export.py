"""Lossless portable rendering payloads, layer isolation and bounded export."""

import base64
from copy import deepcopy
import gzip
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


def export(settings, revision, request=None, legacy=False):
    """Prepare and decode an export, as (document text, initOffline arguments)."""
    request = request if isinstance(request, ExportRequest) else ExportRequest.model_validate(request or {})
    return decoded_document(prepare_export(revision, revision["case_id"], settings, request, legacy=legacy))


def decode_volume(volume):
    descriptor = volume["descriptor"]
    chunks = {item["index"]: item["gzip_base64"] for item in volume["chunks"]}
    raw = b"".join(gzip.decompress(base64.b64decode(chunks[chunk["index"]])) for chunk in descriptor["chunks"])
    assert len(raw) == descriptor["byte_length"]
    return np.frombuffer(raw, dtype=np.dtype(descriptor["dtype"]).newbyteorder("<")).reshape(descriptor["shape"], order="F")


def source_values(revision):
    return np.asarray(nib.load(revision["ct_file"]).dataobj, dtype=np.float32)


def forbid_decode(monkeypatch):
    monkeypatch.setattr(review_volume, "scalar_volume", lambda *args, **kwargs: pytest.fail("The export decoded pixels before refusing"))


def forbid_open(monkeypatch, name):
    original = Path.open
    monkeypatch.setattr(Path, "open", lambda path, *args, **kwargs: pytest.fail(f"{name} was opened") if path.name == name else original(path, *args, **kwargs))


PLANE_AND_VOLUME = {"segmentation": False, "ct_volume": True, "ct_plane": True}


@pytest.mark.parametrize("segmentation,volume,plane", [(True, False, False), (False, False, True), (False, True, True)])
def test_independent_layer_combinations_embed_only_requested_artifacts(portable_case, segmentation, volume, plane):
    settings, revision, glb = portable_case
    request = ExportRequest.model_validate(
        dict(
            layers=dict(segmentation=segmentation, ct_volume=volume, ct_plane=plane),
            state={"plane": {"overlays": ["fixture:one"]}, "volume_mask": {"enabled": True, "ids": ["fixture:two"]}},
        )
    )
    original = deepcopy(revision)
    text, (manifest, model, payload) = export(settings, revision, request)
    assert (base64.b64decode(model) if model is not None else None) == (glb if segmentation else None)
    assert payload["layers"] == request.layers.model_dump() and ("ct" in payload) == (volume or plane)
    native = [item for item in payload["masks"] if not item["descriptor"].get("volume_mask_union")]
    unions = [item for item in payload["masks"] if item["descriptor"].get("volume_mask_union")]
    assert {item["id"] for item in native} == ({"fixture:one"} if plane else set())
    assert len(unions) == int(volume)
    if volume:
        assert unions[0]["descriptor"]["selected_structure_ids"] == ["fixture:two"]
        assert unions[0]["id"] == "volume-mask-union:" + unions[0]["descriptor"]["key"]
    assert "PRIVATE-" not in text and "mask_file" not in text and "ct_file" not in text and "/api/" not in text
    assert "connect-src 'none'" in text and "worker-src blob:" in text
    assert Path("web/public/licenses/Slicer-LICENSE.txt").read_text() in text
    assert manifest["label"] == "Scan3D case " + revision["case_id"][:8] and manifest["review_status"] == "corrected"
    assert manifest["model_provenance"] == {"engine": "TotalSegmentator", "engine_version": "2.18.0", "resolution": "standard_full", "tasks": ["total"]}
    assert revision == original


def test_ct_hu_and_mask_crops_reconstruct_exactly_on_oblique_reflected_grid(portable_case):
    settings, revision, _ = portable_case
    state = {"plane": {"index": 12, "overlays": ["fixture:one", "fixture:two"]}, "volume_mask": {"enabled": True, "ids": ["fixture:one"]}}
    _, (manifest, model, payload) = export(settings, revision, {"layers": PLANE_AND_VOLUME, "state": state})
    assert model is None
    ct = decode_volume(payload["ct"])
    descriptor = payload["ct"]["descriptor"]
    np.testing.assert_array_equal((ct.astype(np.float64) * descriptor["slope"] + descriptor["intercept"]).astype(np.float32), source_values(revision))
    np.testing.assert_allclose(descriptor["affine_ras"], imaging.geometry(Path(revision["ct_file"]))["affine_ras"], atol=1e-6)
    assert np.linalg.det(np.asarray(descriptor["affine_ras"])[:3, :3]) < 0
    assert descriptor["quality"] == "full" and not descriptor["approximate"] and len(payload["masks"]) == 3
    for embedded in payload["masks"]:
        crop = embedded["descriptor"]
        if crop.get("volume_mask_union"):
            assert crop["presentation_only"] is True and crop["byte_length"] <= 8 * 1024**2
            continue
        source = next(item for item in revision["structures"] if item["id"] == embedded["id"])
        result = np.zeros(ct.shape, dtype=np.uint8)
        result[tuple(slice(low, low + size) for low, size in zip(crop["origin_ijk"], crop["shape"]))] = decode_volume(embedded)
        np.testing.assert_array_equal(result, np.asanyarray(nib.load(source["mask_file"]).dataobj))
        expected_affine = np.asarray(descriptor["affine_ras"]).copy()
        expected_affine[:3, 3] = imaging._world(expected_affine, crop["origin_ijk"])
        np.testing.assert_allclose(crop["affine_ras"], expected_affine, atol=1e-6)
        assert all("url" not in chunk for chunk in crop["chunks"])
    assert manifest["geometry"]["shape"] == list(ct.shape)
    # A revision bound to a registered image exports the same HU, named as CT with its legacy defaults.
    asset = create_image_asset(revision["case_id"], revision["ct_file"], acquisition=revision["acquisition"])
    revision["image_asset"], revision["reference_image_id"] = public_image_asset(asset), asset["id"]
    text, (manifest, _, payload) = export(settings, revision, {"layers": PLANE_AND_VOLUME})
    bound = payload["ct"]["descriptor"]
    assert (bound["modality"], bound["scalar"]["units"], bound["reference_image_id"]) == ("CT", "HU", asset["id"])
    np.testing.assert_array_equal(
        (decode_volume(payload["ct"]).astype(np.float64) * bound["slope"] + bound["intercept"]).astype(np.float32), source_values(revision)
    )
    assert manifest["image_context"]["presentation"]["default_window"] == 400 and "PRIVATE-" not in text


def test_display_margin_and_empty_masks_get_their_own_volume_union(portable_case):
    settings, revision, _ = portable_case
    before = deepcopy(revision)
    state = {"plane": {"overlays": ["fixture:one"]}, "volume_mask": {"enabled": True, "ids": ["fixture:one"], "margin_mm": 2.5}}
    _, (_, _, payload) = export(settings, revision, {"layers": PLANE_AND_VOLUME, "state": state})
    masks = {item["id"]: item for item in payload["masks"]}
    union = next(item for item in payload["masks"] if item["descriptor"].get("volume_mask_union"))
    assert set(masks) == {"fixture:one", union["id"]} and payload["state"]["volume_mask"]["margin_mm"] == 2.5
    exact = masks["fixture:one"]
    assert exact["descriptor"]["key"] != union["descriptor"]["key"] and union["descriptor"]["display_margin_mm"] == 2.5
    assert int(decode_volume(union).sum()) > int(decode_volume(exact).sum())
    assert revision == before
    assert ExportRequest.model_validate({"state": {"volume_mask": {"margin_mm": 0.0625}}}).state.volume_mask.margin_mm == 0.063
    # An empty mask, named twice, still gives one explicit empty union and no duplicate data.
    image, _ = imaging._image(Path(revision["ct_file"]))
    imaging._save_mask(np.zeros(image.shape, np.uint8), image, Path(revision["structures"][0]["mask_file"]))
    state = {"plane": {"overlays": ["fixture:one", "fixture:one"]}, "volume_mask": {"enabled": True, "mode": "outside", "ids": ["fixture:one"]}}
    _, (_, _, payload) = export(settings, revision, {"layers": PLANE_AND_VOLUME, "state": state})
    union = next(item for item in payload["masks"] if item["descriptor"].get("volume_mask_union"))
    assert len(payload["masks"]) == 2 and union["descriptor"]["empty"] is True and union["descriptor"]["shape"] == [1, 1, 1]
    assert decode_volume(union).sum() == 0 and payload["state"]["volume_mask"]["mode"] == "outside"


def test_segmentation_layer_alone_decides_whether_the_model_is_read(portable_case, monkeypatch):
    settings, revision, glb = portable_case
    _, arguments = export(settings, revision, legacy=True)
    assert len(arguments) == 2 and base64.b64decode(arguments[1]) == glb
    # The legacy surface export needs no source pixels.
    pixels = Path(revision["ct_file"]).read_bytes()
    Path(revision["ct_file"]).unlink()
    assert "image_context" not in export(settings, revision, legacy=True)[1][0]
    Path(revision["ct_file"]).write_bytes(pixels)
    (Path(revision["directory"]) / "model.glb").unlink()
    forbid_open(monkeypatch, "model.glb")
    _, (_, model, payload) = export(settings, revision, {"layers": {"segmentation": False, "ct_plane": True}})
    assert model is None and payload["ct"] and payload["masks"] == []
    revision["structures"] = []
    _, (manifest, model, payload) = export(settings, revision, {"layers": {"segmentation": False, "ct_volume": True}})
    assert manifest["structures"] == [] and model is None and "ct" in payload
    with pytest.raises(ValueError, match="no segmentation"):
        export(settings, revision)


def test_names_are_escaped_and_no_application_metadata_or_credentials_are_embedded(portable_case):
    settings, revision, _ = portable_case
    revision["structures"][0]["name"] = '</script><script>alert("anatomy")</script>'
    text, (manifest, _) = export(settings, revision, legacy=True)
    assert manifest["structures"][0]["name"] == '</script><script>alert("anatomy")</script>'
    assert "\\u003c/script>" in text and '<script>alert("anatomy")' not in text and "PRIVATE-" not in text
    assert not {"case_id", "id", "created_at", "parent_id", "model_url", "directory", "ct_file", "operation", "acquisition"} & manifest.keys()
    assert "private_path" not in json.dumps(manifest)


def test_size_and_memory_limits_refuse_before_decoding_or_reading(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    with monkeypatch.context() as patched:
        forbid_decode(patched)
        patched.setattr(portable_export, "_available_memory", lambda: 1)
        with pytest.raises(ExportTooLarge, match="insufficient available memory"):
            export(settings, revision, {"layers": {"segmentation": False, "ct_plane": True}})
        # With ample memory, the header limit refuses a CT too large to embed.
        patched.setattr(portable_export, "_available_memory", lambda: 1024**4)
        image = SimpleNamespace(shape=(512, 512, 2048), get_data_dtype=lambda: np.dtype("int16"))
        patched.setattr(imaging, "_image", lambda _: (image, np.eye(4)))
        with pytest.raises(ExportTooLarge, match="512 MiB"):
            export(settings, revision, {"layers": {"segmentation": False, "ct_volume": True}})
    monkeypatch.setattr(portable_export, "MAX_MASK_BYTES", 1)
    with pytest.raises(ExportTooLarge, match="mask crops"):
        export(settings, revision, {"layers": {"segmentation": False, "ct_plane": True}, "state": {"plane": {"overlays": ["fixture:one"]}}})
    monkeypatch.setattr(portable_export, "MAX_HTML_BYTES", 1)
    with pytest.raises(ExportTooLarge, match="size limit|download limit"):
        export(settings, revision)
    model = Path(revision["directory"]) / "model.glb"
    with model.open("wb") as stream:
        stream.truncate(portable_export.MAX_MODEL_BYTES + 1)
    forbid_open(monkeypatch, "model.glb")
    with pytest.raises(ExportTooLarge, match="segmentation model"):
        export(settings, revision)


def test_unknown_segment_and_out_of_range_plane_are_rejected_before_decode(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    forbid_decode(monkeypatch)
    for state in ({"plane": {"overlays": ["other:secret"]}}, {"display": {"other:secret": {"visible": True, "opacity": 1}}}):
        with pytest.raises(ValueError, match="outside this saved revision"):
            export(settings, revision, {"layers": {"segmentation": False, "ct_plane": True}, "state": state})
    with pytest.raises(ValueError, match="outside the reference CT"):
        export(settings, revision, {"layers": {"segmentation": False, "ct_plane": True}, "state": {"plane": {"index": 999}}})


@pytest.mark.parametrize(
    "changes",
    [
        {"layers": {"segmentation": False, "ct_volume": False, "ct_plane": False}},
        {"layers": {"ct_volume": "true"}},
        {"state": {"path": "/private"}},
        {"state": {"volume": {"shift": float("nan")}}},
        {"state": {"volume": {"preset": "unknown-volume-preset"}}},
        {"state": {"volume": {"crop": [[0, 0, 0], [0, 1, 1]]}}},
        {"state": {"volume_mask": {"margin_mm": 5.01}}},
        {"state": {"camera": {"position_ras": [0, -200, 0], "target_ras": [0, 0, 0], "up_ras": [0, 0, 0], "height_mm": 350, "zoom": 1}}},
        {"state": {"volume": {"preset": "MR-SignalWindow", "signal_window": {"window": 0, "level": 0}}}},
        {"state": {"volume": {"preset": "MR-SignalWindow", "signal_window": {"window": 1, "level": float("nan")}}}},
        {"state": {"volume": {"preset": "MR-SignalWindow", "signal_window": {"window": 1, "level": 0, "invert": "false"}}}},
        {"state": {"volume": {"preset": "MR-SignalWindow", "signal_window": {"window": 1, "level": 0, "source_file": "private.nii.gz"}}}},
    ],
)
def test_render_state_is_strict_finite_and_bounded(changes):
    with pytest.raises(ValidationError):
        ExportRequest.model_validate(changes)


def test_render_state_normalizes_camera_and_leaves_out_an_unset_signal_window():
    data = {"state": {"camera": {"position_ras": [0, -200, 0], "target_ras": [0, 0, 0], "up_ras": [0, 0, 4], "height_mm": 350, "zoom": 1.25}}}
    assert ExportRequest.model_validate(data).state.camera.up_ras == (0, 0, 1) and data["state"]["camera"]["up_ras"] == [0, 0, 4]
    assert "signal_window" not in json.loads(ExportRequest().model_dump_json())["state"]["volume"]
    window = {"window": 0.125, "level": -0.05, "invert": False}
    assert ExportRequest.model_validate({"state": {"volume": {"signal_window": window}}}).model_dump()["state"]["volume"]["signal_window"] == window


def test_encoded_files_stream_in_bounded_blocks_and_refuse_a_changed_file(tmp_path):
    data = bytes(range(256)) * 1800 + b"end!"
    path = tmp_path / "binary"
    path.write_bytes(data)
    file = portable_export.EncodedFile(path, len(data))
    chunks = list(file.chunks())
    assert max(map(len, chunks)) <= portable_export._BASE64_READ_BYTES * 4 // 3
    assert len(b"".join(chunks)) == file.encoded_length and base64.b64decode(b"".join(chunks)) == data
    for changed in (data[:-1], data + b"!"):
        path.write_bytes(changed)
        with pytest.raises(ValueError, match="artifact changed"):
            b"".join(file.chunks())


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
    text, arguments = export(settings, revision, legacy=legacy)
    manifest, model = arguments[:2]
    context = manifest["image_context"]
    assert base64.b64decode(model) == glb and set(context) == {"reference_image_id", "modality", "scalar", "presentation"}
    assert context["reference_image_id"] == revision["reference_image_id"] and context["presentation"] == revision["image_asset"]["presentation"]
    assert context["modality"] == "MR" and context["scalar"]["units"] == "signal"
    assert "PRIVATE-" not in text and "source_uid" not in text and "content_sha256" not in text and "image_asset" not in manifest
    assert revision == original
    if not legacy:
        assert "ct" not in arguments[2] and arguments[2]["state"]["volume"]["preset"] == "MR-SignalWindow"


def test_mr_layers_preserve_fractional_signal_windows_and_bound_mask_identity(portable_mr):
    settings, revision, _ = portable_mr
    request = ExportRequest.model_validate(
        {
            "layers": PLANE_AND_VOLUME,
            "state": {"plane": {"overlays": ["fixture:one"], "index": 12}, "volume_mask": {"enabled": True, "ids": ["fixture:two"], "margin_mm": 1}},
        }
    )
    original_request, original_revision = request.model_dump(), deepcopy(revision)
    text, (manifest, model, payload) = export(settings, revision, request)
    assert model is None and "PRIVATE-" not in text
    context, descriptor = manifest["image_context"], payload["ct"]["descriptor"]
    assert {key: descriptor[key] for key in context} == context
    assert descriptor["dtype"] == "float32" and descriptor["slope"] == 1 and descriptor["intercept"] == 0
    signal = decode_volume(payload["ct"])
    np.testing.assert_array_equal(signal, source_values(revision))
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
    # Explicit fractional windows are kept as given.
    window = {"window": 0.03125, "level": -0.0075, "invert": False}
    state = {"volume": {"preset": "MR-SignalWindow", "signal_window": window}, "plane": {"window": 0.07, "level": 0.02}}
    _, (_, _, payload) = export(settings, revision, {"layers": PLANE_AND_VOLUME, "state": state})
    assert payload["state"]["volume"]["signal_window"] == window and payload["state"]["plane"]["window"] == 0.07
    assert payload["ct"]["descriptor"]["presentation"]["default_window"] == 0.125


@pytest.mark.parametrize("volume", [{"preset": "CT-Bone"}, {"preset": "MR-SignalWindow", "shift": 25}])
def test_mr_rejects_ct_calibration_before_volume_decode(portable_mr, monkeypatch, volume):
    settings, revision, _ = portable_mr
    forbid_decode(monkeypatch)
    with pytest.raises(ValueError, match="MR .*CT"):
        export(settings, revision, {"layers": {"segmentation": False, "ct_volume": True}, "state": {"volume": volume}})


@pytest.mark.parametrize("field", ["reference_image_id", "structure_reference_image_id"])
def test_mr_export_rejects_cross_source_binding_before_decode(portable_mr, monkeypatch, field):
    settings, revision, _ = portable_mr
    (revision if field == "reference_image_id" else revision["structures"][0])["reference_image_id"] = identifier()
    forbid_decode(monkeypatch)
    with pytest.raises(ValueError, match="reference image"):
        export(settings, revision)


def test_export_api_headers_legacy_post_authorization_and_limits(portable_case, monkeypatch):
    settings, revision, _ = portable_case
    app = create_app(settings)
    service = app.state.service
    case = service.new_case("PRIVATE-PATIENT-NAME")
    revision["case_id"] = case["id"]
    service.store.save("revisions", revision)
    original = deepcopy(service.store.revision(case["id"], revision["id"]))
    url = f"/api/cases/{case['id']}/revisions/{revision['id']}/export.html"
    with TestClient(app, base_url="http://127.0.0.1") as client:
        legacy = client.get(url)
        assert legacy.status_code == 200 and legacy.headers["cache-control"] == "private, no-store", legacy.text
        assert legacy.headers["content-disposition"].endswith('.html"')
        body = {"layers": PLANE_AND_VOLUME, "state": {"plane": {"overlays": ["fixture:one"]}}}
        response = client.post(url, json=body)
        assert response.status_code == 200 and int(response.headers["content-length"]) == len(response.content), response.text
        assert response.headers["content-type"].startswith("text/html") and "PRIVATE-" not in response.text
        assert client.post(url, json=body, headers={"Origin": "https://other.example"}).status_code == 403
        assert client.post(url.replace(case["id"], identifier()), json=body).status_code == 404
        assert client.post(url, json={"layers": {"segmentation": False, "ct_volume": False, "ct_plane": False}}).status_code == 422
        monkeypatch.setattr(portable_export, "MAX_HTML_BYTES", 1)
        assert client.post(url, json={}).status_code == 413
    assert service.store.revision(case["id"], revision["id"]) == original and service.store.list("jobs") == []
