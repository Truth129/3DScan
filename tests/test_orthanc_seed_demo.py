"""The development archive uses deterministic real DICOM, never case-library files."""

from io import BytesIO
from pathlib import Path
import shutil

import nibabel as nib
import pytest

import numpy as np
from pydicom import dcmread
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian

from scripts.orthanc_seed_demo import COMPLETE_SERIES_UID, FRAME_UID, STUDY_UID, UID_ROOT, dicom_bytes


def test_seed_is_real_signed_ct_with_full_source_identity_and_declared_count():
    frames = [dcmread(BytesIO(dicom_bytes(3, index))) for index in range(12)]
    assert {frame.StudyInstanceUID for frame in frames} == {STUDY_UID}
    assert {frame.FrameOfReferenceUID for frame in frames} == {FRAME_UID}
    assert {frame.SeriesInstanceUID for frame in frames} == {COMPLETE_SERIES_UID}
    assert COMPLETE_SERIES_UID != f"{UID_ROOT}.1.3"
    assert frames[0].SOPInstanceUID != f"{UID_ROOT}.4.1"
    assert {frame.StudyTime for frame in frames} == {"090000"}
    assert {frame.SeriesTime for frame in frames} == {"090000"}
    assert len({frame.SOPInstanceUID for frame in frames}) == 12
    for index, frame in enumerate(frames):
        assert frame.Modality == "CT" and frame.SOPClassUID == CTImageStorage
        assert frame.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
        assert frame.PatientID == "SCAN3D-ORTHANC-DEMO" and str(frame.PatientName) == "SYNTHETIC^Orthanc"
        assert frame.ImagesInAcquisition == 12 and frame.NumberOfTemporalPositions == 1
        assert list(frame.ImagePositionPatient) == [-32, -32, index * 2]
        assert list(frame.ImageOrientationPatient) == [1, 0, 0, 0, 1, 0]
        assert list(frame.PixelSpacing) == [1, 1] and frame.SliceThickness == 2
        pixels = frame.pixel_array
        assert pixels.shape == (64, 64) and pixels.dtype == np.dtype("int16")
        assert pixels.min() == -1000 and pixels.max() == 650
    assert dicom_bytes(3, 0) == dicom_bytes(3, 0)


def test_incomplete_fixture_keeps_declared_total_but_a_distinct_series_and_sops():
    complete = dcmread(BytesIO(dicom_bytes(3, 0)))
    incomplete = [dcmread(BytesIO(dicom_bytes(4, index, complete=False))) for index in range(3)]
    assert all(frame.StudyInstanceUID == complete.StudyInstanceUID for frame in incomplete)
    assert all(frame.SeriesInstanceUID != complete.SeriesInstanceUID for frame in incomplete)
    assert all(frame.SOPInstanceUID != complete.SOPInstanceUID for frame in incomplete)
    assert all(frame.ImagesInAcquisition == 12 and frame.NumberOfTemporalPositions == 1 for frame in incomplete)


def test_seed_converts_to_one_native_3d_ct_without_force_merging(tmp_path, monkeypatch):
    from scan3d.dicom import convert_series

    bundled = Path(__file__).resolve().parents[1] / ".tools/bin/dcm2niix"
    executable = str(bundled) if bundled.is_file() else shutil.which("dcm2niix")
    if not executable:
        pytest.skip("dcm2niix is not installed")
    monkeypatch.setenv("SCAN3D_DCM2NIIX", executable)
    frames = []
    originals = []
    for index in range(12):
        data = dicom_bytes(3, index)
        frame = tmp_path / f"slice-{index:02d}.dcm"
        frame.write_bytes(data)
        frames.append(frame)
        originals.append(dcmread(BytesIO(data)).pixel_array)
    image = nib.load(convert_series(frames, tmp_path / "converted"))
    assert image.shape == (64, 64, 12)
    assert abs(np.linalg.det(image.affine[:3, :3])) == pytest.approx(2)
    np.testing.assert_array_equal(np.sort(np.asanyarray(image.dataobj).reshape(-1)), np.sort(np.stack(originals).reshape(-1)))
