"""Geometry gates run without adding highdicom to application dependencies."""

import numpy as np
import pytest

import osirix_seg_smoke as smoke


def test_native_mask_projection_preserves_every_source_pixel_and_overlap():
    images, pixels, truth = smoke.source_fixture()
    native_pixels, masks, affine = smoke.native_grid(images, pixels, truth)
    assert native_pixels.shape == (64, 12, 48) and truth.shape == (12, 48, 64, 2)
    projected, error = smoke.project_masks(images, masks, affine)
    assert np.array_equal(projected, truth) and error < 1e-10
    assert np.count_nonzero(projected[..., 0] & projected[..., 1]) > 0
    assert not projected[0].any() and not projected[-1].any()
    assert not projected[5, 21, 23, 0] and projected[5, 21, 19, 0]
    reversed_pixels, _ = smoke.project_masks(images[::-1], masks, affine)
    assert np.array_equal(reversed_pixels, truth[::-1])


def test_fresh_source_identity_and_exact_oblique_anisotropic_metadata():
    images, _, _ = smoke.source_fixture()
    other, _, _ = smoke.source_fixture()
    assert images[0].StudyInstanceUID != other[0].StudyInstanceUID and images[0].PatientID != other[0].PatientID
    assert len({image.SOPInstanceUID for image in images}) == len(images)
    for image in images:
        assert image.PatientName == "SYNTHETIC^SEGRETURN" and image.Modality == "CT"
        assert image.PixelSpacing == [1.1, 0.8] and image.Rows != image.Columns
        assert image.file_meta.MediaStorageSOPInstanceUID == image.SOPInstanceUID
        assert image.ImageOrientationPatient[2] != 0
        assert image.PatientBirthDate == image.PatientSex == image.ReferringPhysicianName == ""
        assert image.Manufacturer == "3DScan" and "KVP" in image and image.KVP == ""
        assert "NumberOfTemporalPositions" not in image
        for element in image.iterall():
            if element.VR == "DS":
                values = element.value if element.VM > 1 else [element.value]
                assert all(len(str(value)) <= 16 for value in values)


@pytest.mark.parametrize("offset", [0.2, 100])
def test_mismatched_grid_is_rejected_instead_of_resampling(offset):
    images, pixels, truth = smoke.source_fixture()
    _, masks, affine = smoke.native_grid(images, pixels, truth)
    affine[:3, 3] += offset * affine[:3, 0]
    with pytest.raises(ValueError, match="integer|outside"):
        smoke.project_masks(images, masks, affine)


def test_invalid_output_never_creates_files(tmp_path):
    output = tmp_path / "not-a-smoke-fixture"
    with pytest.raises(ValueError, match="scan3d-osirix-seg"):
        smoke.prepare(output)
    assert not output.exists()
