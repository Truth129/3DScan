"""Inference presence checks preserve mask validation without dense reordering.

Only synthetic NIfTI files are used. Numerical consumers still receive the
contiguous boolean arrays needed by QC, edits and contour extraction.
"""

import nibabel as nib
import numpy as np
import pytest

from scan3d import imaging


def oblique_reflected_affine():
    angle = np.deg2rad(27)
    rotation = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    affine = np.eye(4)
    affine[:3, :3] = rotation @ np.diag([-0.8, 1.3, 2.1])
    affine[:3, 3] = [31, -52, 87]
    return affine


def save(path, values, affine=None, *, units="mm", slope=None, intercept=None):
    affine = oblique_reflected_affine() if affine is None else affine
    image = nib.Nifti1Image(values, np.eye(4))
    # An explicit sform also permits deliberately invalid transform fixtures
    # without asking NIfTI's quaternion encoder to repair singular matrices.
    image.set_sform(affine, code=2)
    image.set_qform(None, code=0)
    image.header.set_xyzt_units(units)
    if slope is not None:
        image.header.set_slope_inter(slope, intercept)
    nib.save(image, path)
    return path


@pytest.mark.parametrize("suffix", [".nii", ".nii.gz"])
@pytest.mark.parametrize("populated", [False, True])
def test_presence_and_numerical_masks_preserve_oblique_reflected_grid(tmp_path, suffix, populated):
    values = np.zeros((9, 11, 7), np.uint8)
    if populated:
        values[2:6, 3:8, 1:4] = 1
        values[-1, -1, -1] = 1
    ct_path = save(tmp_path / ("ct" + suffix), np.zeros(values.shape, np.int16))
    mask_path = save(tmp_path / ("mask" + suffix), values)
    reference, affine = imaging._image(ct_path)

    assert imaging._binary_nonempty(mask_path, reference, affine) is populated
    result, image, result_affine = imaging._binary(mask_path, reference, affine)
    assert result.dtype == np.bool_
    assert result.flags.c_contiguous
    assert image.shape == values.shape
    np.testing.assert_array_equal(result, values.astype(bool))
    np.testing.assert_array_equal(result_affine, affine)
    assert np.linalg.det(result_affine[:3, :3]) < 0


@pytest.mark.parametrize("suffix", [".nii", ".nii.gz"])
def test_inference_presence_does_not_make_contiguous_dense_copy(tmp_path, monkeypatch, suffix):
    values = np.zeros((13, 17, 19), np.uint8)
    values[-1, -1, -1] = 1
    path = save(tmp_path / ("mask" + suffix), values)
    assert np.asanyarray(nib.load(path).dataobj).flags.f_contiguous
    original = np.ascontiguousarray

    def reject_dense_reorder(value, *args, **kwargs):
        if getattr(value, "shape", None) == values.shape:
            pytest.fail("Presence validation must not reorder the dense mask")
        return original(value, *args, **kwargs)

    monkeypatch.setattr(imaging.np, "ascontiguousarray", reject_dense_reorder)
    assert imaging._binary_nonempty(path) is True


@pytest.mark.parametrize("dtype", [np.uint8, np.int16, np.float32, np.float64])
def test_binary_numeric_types_are_validated_without_changing_voxels(tmp_path, dtype):
    values = np.zeros((3, 4, 5), dtype)
    values[1, 2, 3] = 1
    path = save(tmp_path / "mask.nii.gz", values)
    assert imaging._binary_nonempty(path) is True
    result, _, _ = imaging._binary(path)
    np.testing.assert_array_equal(result, values.astype(bool))


@pytest.mark.parametrize("invalid", [-1, 2, 0.5, np.nan, np.inf, -np.inf])
def test_presence_checks_all_voxels_before_reporting_nonempty(tmp_path, invalid):
    # Valid min/max alone would miss a fractional voxel in this fixture.
    values = np.zeros((5, 6, 7), np.float32)
    values[0, 0, 0] = 1
    values[-1, -1, -1] = invalid
    path = save(tmp_path / "mask.nii.gz", values)
    for read in (imaging._binary_nonempty, imaging._binary):
        with pytest.raises(ValueError, match="only binary values"):
            read(path)


def test_complex_mask_is_rejected_instead_of_coerced(tmp_path):
    values = np.zeros((3, 4, 5), np.complex64)
    values[1, 2, 3] = 1 + 1j
    path = save(tmp_path / "mask.nii", values)
    for read in (imaging._binary_nonempty, imaging._binary):
        with pytest.raises(ValueError, match="only binary values"):
            read(path)


@pytest.mark.parametrize("slope,intercept", [(0.5, 0), (1, -1)])
def test_scaled_nifti_validates_effective_values(tmp_path, slope, intercept):
    expected = np.zeros((5, 6, 7), np.uint8)
    expected[1:4, 2:5, 3:6] = 1
    raw = ((expected.astype(np.float64) - intercept) / slope).astype(np.int16)
    path = save(tmp_path / "scaled.nii.gz", raw, slope=slope, intercept=intercept)
    assert imaging._binary_nonempty(path) is True
    result, _, _ = imaging._binary(path)
    np.testing.assert_array_equal(result, expected.astype(bool))


def test_scaled_fractional_mask_is_rejected(tmp_path):
    raw = np.zeros((5, 6, 7), np.int16)
    raw[0, 0, 0] = 4
    raw[-1, -1, -1] = 2
    path = save(tmp_path / "scaled.nii.gz", raw, slope=0.25, intercept=0)
    for read in (imaging._binary_nonempty, imaging._binary):
        with pytest.raises(ValueError, match="only binary values"):
            read(path)


@pytest.mark.parametrize("mismatch", ["shape", "origin", "direction"])
def test_presence_rejects_wrong_reference_geometry(tmp_path, mismatch):
    shape = (5, 6, 7)
    affine = oblique_reflected_affine()
    ct = save(tmp_path / "ct.nii.gz", np.zeros(shape, np.int16), affine)
    reference, reference_affine = imaging._image(ct)
    if mismatch == "shape":
        shape = (5, 6, 8)
    elif mismatch == "origin":
        affine[0, 3] += 0.1
    else:
        affine[:3, 0] *= -1
    path = save(tmp_path / "mask.nii.gz", np.ones(shape, np.uint8), affine)
    for read in (imaging._binary_nonempty, imaging._binary):
        with pytest.raises(ValueError, match="does not match the reference CT"):
            read(path, reference, reference_affine)


def test_presence_respects_equivalent_spatial_units(tmp_path):
    affine = oblique_reflected_affine()
    shape = (5, 6, 7)
    ct = save(tmp_path / "ct.nii.gz", np.zeros(shape, np.int16), affine)
    reference, reference_affine = imaging._image(ct)
    meters = affine.copy()
    meters[:3] /= 1000
    path = save(tmp_path / "mask.nii.gz", np.ones(shape, np.uint8), meters, units="meter")
    assert imaging._binary_nonempty(path, reference, reference_affine) is True


@pytest.mark.parametrize("shape", [(5, 6), (5, 6, 7, 2), (5, 0, 7)])
def test_presence_rejects_non_scalar_or_empty_images(tmp_path, shape):
    path = save(tmp_path / "mask.nii.gz", np.zeros(shape, np.uint8))
    for read in (imaging._binary_nonempty, imaging._binary):
        with pytest.raises(ValueError, match="scalar three-dimensional"):
            read(path)


def test_presence_rejects_singular_patient_transform(tmp_path):
    affine = oblique_reflected_affine()
    affine[:3, 0] = 0
    path = save(tmp_path / "mask.nii.gz", np.ones((5, 6, 7), np.uint8), affine)
    for read in (imaging._binary_nonempty, imaging._binary):
        with pytest.raises(ValueError, match="invalid voxel-to-patient transform"):
            read(path)
