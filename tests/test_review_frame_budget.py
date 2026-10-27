"""Reject oversized decoded review frames before allocating pixel or mask data."""

import gzip

import nibabel as nib
import numpy as np
import pytest

from scan3d import imaging, review_volume


def source(tmp_path):
    path = tmp_path / "source.nii"
    nib.save(nib.Nifti1Image(np.zeros((2, 2, 2), np.int16), np.eye(4)), path)
    return path


@pytest.mark.parametrize("overlay_count,compare", [(29, False), (15, True)])
def test_preflight_counts_all_layers_before_any_voxel_allocation(tmp_path, monkeypatch, overlay_count, compare):
    path = source(tmp_path)
    monkeypatch.setattr(imaging, "slice_geometry", lambda *a, **k: ({"width": 2048, "height": 4096}, np.eye(4)))
    monkeypatch.setattr(imaging, "_ct_array", lambda *a: pytest.fail("Oversized frame decoded the CT"))
    monkeypatch.setattr(imaging, "_reslice", lambda *a, **k: pytest.fail("Oversized frame allocated pixels"))
    overlays = ((str(i), path, path if compare else None) for i in range(overlay_count))
    assert review_volume._FRAME_MAX_BYTES == 256 * 1024**2
    with pytest.raises(ValueError, match="256 MiB decoded payload"):
        review_volume.review_frame(path, None, "axial", 0, overlays=overlays)


@pytest.mark.parametrize("with_generator", [False, True])
def test_exact_budget_accepts_generator_without_losing_layers(tmp_path, monkeypatch, with_generator):
    path = source(tmp_path)
    monkeypatch.setattr(review_volume, "_FRAME_MAX_BYTES", 24)
    monkeypatch.setattr(imaging, "slice_geometry", lambda *a, **k: ({"width": 2, "height": 2}, np.eye(4)))
    monkeypatch.setattr(imaging, "_ct_array", lambda *a: np.zeros((2, 2, 2)))
    monkeypatch.setattr(imaging, "_slice_mask", lambda *a: np.ones((2, 2, 2)))
    monkeypatch.setattr(imaging, "_reslice", lambda *a, **k: np.zeros((2, 2)))
    layers = [("a", path, None), ("b", path, None)]
    result = review_volume.review_frame(path, None, "axial", 0, overlays=(entry for entry in layers) if with_generator else layers)
    assert len(gzip.decompress(result["body"])) == 24
    assert [entry["id"] for entry in result["frame"]["overlays"]] == ["a", "b"]
    with pytest.raises(ValueError, match="payload limit"):
        review_volume.review_frame(path, None, "axial", 0, overlays=[("a", path, path), ("b", path, None)])


def test_legacy_frame_counts_current_parent_and_rejects_parent_only_early(tmp_path, monkeypatch):
    path = source(tmp_path)
    monkeypatch.setattr(review_volume, "_FRAME_MAX_BYTES", 23)
    monkeypatch.setattr(imaging, "slice_geometry", lambda *a, **k: ({"width": 2, "height": 2}, np.eye(4)))
    monkeypatch.setattr(imaging, "_ct_array", lambda *a: pytest.fail("Invalid frame decoded the CT"))
    with pytest.raises(ValueError, match="payload limit"):
        review_volume.review_frame(path, path, "axial", 0, path)
    with pytest.raises(ValueError, match="current mask is required"):
        review_volume.review_frame(path, None, "axial", 0, path)
