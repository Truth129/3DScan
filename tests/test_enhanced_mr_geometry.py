"""Independent source-pixel/physical-grid invariants at the converter boundary.

These fixtures construct NIfTI values directly from DICOM functional groups;
they do not use the application's enhanced-frame parser to define the oracle.
"""
from pathlib import Path
import subprocess

import nibabel as nib
import numpy as np
import pydicom
import pytest

from scan3d import dicom
from test_enhanced_mr import write_enhanced_mr


def native_volume(path):
    source = pydicom.dcmread(path)
    shared = source.SharedFunctionalGroupsSequence[0]

    def group(frame, keyword):
        return (frame.get(keyword) or shared[keyword].value)[0]

    frames = source.PerFrameFunctionalGroupsSequence
    orientation = np.asarray(group(frames[0], "PlaneOrientationSequence").ImageOrientationPatient, dtype=float)
    spacing = np.asarray(group(frames[0], "PixelMeasuresSequence").PixelSpacing, dtype=float)
    positions = np.asarray([group(frame, "PlanePositionSequence").ImagePositionPatient for frame in frames], dtype=float)
    order = np.argsort(positions @ np.cross(orientation[:3], orientation[3:]))
    # DICOM [frame,row,column] becomes a common NIfTI [column,reversed-row,slice].
    values = source.pixel_array[order].transpose(2, 1, 0)[:, ::-1, :].copy()
    lps_to_ras = np.diag([-1., -1., 1.])
    affine = np.eye(4)
    affine[:3, 0] = lps_to_ras @ (orientation[:3] * spacing[1])
    affine[:3, 1] = -lps_to_ras @ (orientation[3:] * spacing[0])
    affine[:3, 2] = lps_to_ras @ ((positions[order[-1]] - positions[order[0]]) / (len(frames) - 1))
    affine[:3, 3] = lps_to_ras @ (positions[order[0]] + orientation[3:] * spacing[0] * (int(source.Rows) - 1))
    scaling = group(frames[0], "PixelValueTransformationSequence")
    return values, affine, float(scaling.RescaleSlope), float(scaling.RescaleIntercept)


def converted_with(monkeypatch, values, affine, slope, intercept):
    def converter(executable, source, target, output_dir, **kwargs):
        image = nib.Nifti1Image(values, affine)
        image.header.set_xyzt_units("mm")
        image.header.set_slope_inter(slope, intercept)
        nib.save(image, Path(target) / "reference.nii.gz")
        return subprocess.CompletedProcess([str(executable)], 0, b"dcm2niix version v1.0.20250505", b""), "Synthetic converter"

    monkeypatch.setenv("SCAN3D_DCM2NIIX", "/synthetic-dcm2niix")
    monkeypatch.setattr(dicom, "_run_converter", converter)


@pytest.mark.parametrize("shared_geometry,reflected", [(False, False), (True, True)])
@pytest.mark.parametrize("mapping", ["native", "flip_i", "swap_i_k"])
def test_source_preserving_signed_axis_permutations_are_accepted(tmp_path, monkeypatch, shared_geometry, reflected, mapping):
    source = write_enhanced_mr(tmp_path / "enhanced.dcm", shared_geometry=shared_geometry, reflected=reflected)
    values, affine, slope, intercept = native_volume(source)
    if mapping == "flip_i":
        transform = np.eye(4)
        transform[0, 0] = -1
        transform[0, 3] = values.shape[0] - 1
        values = values[::-1].copy()
        affine = affine @ transform
    elif mapping == "swap_i_k":
        transform = np.array([[0, 0, 1, 0], [0, 1, 0, 0], [1, 0, 0, 0], [0, 0, 0, 1]])
        values = values.transpose(2, 1, 0).copy()
        affine = affine @ transform
    converted_with(monkeypatch, values, affine, slope, intercept)
    result = dicom.convert_series([source], tmp_path / "converted")
    np.testing.assert_array_equal(nib.load(result).dataobj.get_unscaled(), values)
    np.testing.assert_allclose(nib.load(result).affine, affine, atol=1e-5)


def test_same_center_and_voxel_determinant_do_not_validate_a_sheared_grid(tmp_path, monkeypatch):
    source = write_enhanced_mr(tmp_path / "enhanced.dcm")
    values, affine, slope, intercept = native_volume(source)
    center_index = (np.asarray(values.shape) - 1) / 2
    center_ras = affine @ np.r_[center_index, 1]
    shear = np.eye(3)
    shear[0, 1] = .25
    bad = affine.copy()
    bad[:3, :3] = affine[:3, :3] @ shear
    bad[:3, 3] = center_ras[:3] - bad[:3, :3] @ center_index
    assert np.isclose(np.linalg.det(bad), np.linalg.det(affine))
    np.testing.assert_allclose(bad @ np.r_[center_index, 1], center_ras)
    converted_with(monkeypatch, values, bad, slope, intercept)
    with pytest.raises(ValueError):
        dicom.convert_series([source], tmp_path / "converted")
    assert not (tmp_path / "converted/reference.nii.gz").exists()


def test_reflected_affine_without_reflecting_pixels_is_rejected(tmp_path, monkeypatch):
    source = write_enhanced_mr(tmp_path / "enhanced.dcm")
    values, affine, slope, intercept = native_volume(source)
    assert not np.array_equal(values, values[::-1]), "The independent fixture must expose left/right data reversal"
    transform = np.eye(4)
    transform[0, 0] = -1
    transform[0, 3] = values.shape[0] - 1
    converted_with(monkeypatch, values, affine @ transform, slope, intercept)
    with pytest.raises(ValueError):
        dicom.convert_series([source], tmp_path / "converted")
    assert not (tmp_path / "converted/reference.nii.gz").exists()


@pytest.mark.parametrize("corruption", ["samples", "slope", "intercept"])
def test_original_signal_samples_and_standard_rescale_are_checked(tmp_path, monkeypatch, corruption):
    source = write_enhanced_mr(tmp_path / "enhanced.dcm")
    values, affine, slope, intercept = native_volume(source)
    if corruption == "samples":
        values = values + 7
    elif corruption == "slope":
        slope *= 2
    else:
        intercept += 23
    converted_with(monkeypatch, values, affine, slope, intercept)
    with pytest.raises(ValueError):
        dicom.convert_series([source], tmp_path / "converted")
    assert not (tmp_path / "converted/reference.nii.gz").exists()
