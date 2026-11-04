"""Modality policies separate representation from unsupported import workflows."""

import gzip

import nibabel as nib
import numpy as np
import pytest

from scan3d.modalities import get_modality_adapter
from scan3d.review_volume import scalar_volume, ct_volume, chunk_path
from scan3d.segmentation_options import normalize_options, segmentation_catalog, task_ids


def decode(descriptor, cache):
    raw = b"".join(gzip.decompress(chunk_path(cache, descriptor, descriptor["key"], item["index"]).read_bytes()) for item in descriptor["chunks"])
    return np.frombuffer(raw, dtype=descriptor["dtype"]).reshape(descriptor["shape"], order="F")


def test_current_ct_adapter_owns_unchanged_presentation_and_capabilities():
    adapter = get_modality_adapter("CT")
    assert adapter.modality == "CT" and adapter.kind == "volume3d" and adapter.units == "HU"
    policy = adapter.presentation()
    assert policy["background_value"] == -1024
    assert policy["default_window"] == 400 and policy["default_level"] == 40
    assert policy["units"] == "HU" and policy["photometric_interpretation"] == "MONOCHROME2"
    assert policy["preset_group"] == "CT" and policy["preview_quantization_step"] == 1
    policy["default_window"] = 1
    assert adapter.presentation()["default_window"] == 400


@pytest.mark.parametrize("modality", ["XA", "US", "not-a-modality"])
def test_unimplemented_modality_routes_are_rejected_explicitly(modality):
    with pytest.raises(ValueError, match="(?i)(unsupported|not.*support|modality)"):
        get_modality_adapter(modality)
    for operation in (
        lambda: task_ids(modality=modality),
        lambda: segmentation_catalog(modality=modality),
        lambda: normalize_options("chest_abdomen", {"tasks": ["total"]}, modality=modality),
    ):
        with pytest.raises(ValueError, match="(?i)(unsupported|not.*support|modality)"):
            operation()


def test_scalar_preview_can_preserve_fractional_signal_without_ct_quantization(tmp_path):
    values = (np.arange(7 * 9 * 11).reshape(7, 9, 11) / 1000 + 0.125).astype(np.float32)
    source = tmp_path / "fractional-signal.nii"
    nib.save(nib.Nifti1Image(values, np.diag([0.8, 1.4, 2.2, 1.0])), source)
    policy = {**get_modality_adapter("CT").presentation(), "units": "signal", "background_value": 0, "preview_quantization_step": 0, "preset_group": "generic"}
    cache = tmp_path / "cache"
    generic = scalar_volume(source, cache, "preview", presentation=policy)
    assert generic["dtype"] == "float32"
    np.testing.assert_array_equal(decode(generic, cache), values)
    legacy = ct_volume(source, cache, "preview")
    assert generic["key"] != legacy["key"], "Intensity policy must be part of the presentation cache identity"
    assert legacy["dtype"] == "int16"
    np.testing.assert_array_equal(decode(legacy, cache), np.rint(values).astype(np.int16))
    np.testing.assert_array_equal(nib.load(source).dataobj, values)


def test_legacy_ct_wrapper_matches_generic_ct_policy_without_changing_samples(tmp_path):
    values = np.arange(7 * 9 * 11, dtype=np.uint16).reshape(7, 9, 11)
    source = tmp_path / "stored-samples.nii"
    image = nib.Nifti1Image(values, np.diag([-0.8, 1.4, 2.2, 1.0]))
    image.header.set_slope_inter(0.25, -1024)
    nib.save(image, source)
    cache = tmp_path / "cache"
    before = source.read_bytes()
    legacy = ct_volume(source, cache)
    generic = scalar_volume(source, cache, presentation=get_modality_adapter("CT").presentation())
    assert generic["key"] == legacy["key"] and generic["dtype"] == legacy["dtype"] == "uint16"
    assert generic["slope"] == legacy["slope"] == 0.25 and generic["intercept"] == legacy["intercept"] == -1024
    np.testing.assert_array_equal(decode(generic, cache), values)
    assert source.read_bytes() == before
