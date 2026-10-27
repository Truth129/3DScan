"""Static MR ingestion preserves signal and physical coordinates without merging dimensions."""
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, MRImageStorage, generate_uid
import pytest

from scan3d import dicom
from scan3d.domain import PhotometricInterpretation
from scan3d.modalities import get_modality_adapter


def write_mr_series(directory, *, reflected=False, signed=False, photometric="MONOCHROME2", count=5):
    directory.mkdir(parents=True)
    series, study, frame = generate_uid(), generate_uid(), generate_uid()
    angle, tilt = np.deg2rad([23, 17])
    row = np.array([np.cos(angle) * np.cos(tilt), np.sin(angle) * np.cos(tilt), -np.sin(tilt)])
    column = np.array([-np.sin(angle), np.cos(angle), 0.])
    if reflected:
        row *= -1
    normal = np.cross(row, column)
    for index in range(count):
        # Deliberately reverse lexical ordering: geometry determines slice order.
        path = directory / f"{count - index:03d}.dcm"
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = MRImageStorage
        meta.MediaStorageSOPInstanceUID = generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = FileDataset(str(path), {}, file_meta=meta, preamble=b"\0" * 128)
        ds.SOPClassUID = MRImageStorage
        ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
        ds.SeriesInstanceUID, ds.StudyInstanceUID, ds.FrameOfReferenceUID = series, study, frame
        ds.Modality = "MR"
        ds.SeriesDescription = "Synthetic anatomical MR"
        ds.Manufacturer = "Synthetic"
        ds.SeriesNumber, ds.InstanceNumber, ds.AcquisitionNumber = 1, index + 1, 1
        ds.ImageType = ["ORIGINAL", "PRIMARY", "M"]
        ds.Rows, ds.Columns = 10, 12
        ds.ImageOrientationPatient = [*row, *column]
        ds.ImagePositionPatient = (np.array([31., -21., 19.]) + normal * index * 1.6).tolist()
        ds.PixelSpacing = [.8, .7]
        ds.SliceThickness = ds.SpacingBetweenSlices = 1.6
        ds.ScanningSequence, ds.SequenceVariant, ds.MRAcquisitionType = "GR", "SP", "3D"
        ds.SequenceName = "synthetic_t1"
        ds.EchoTime, ds.EchoNumbers, ds.RepetitionTime, ds.InversionTime = 3, 1, 8, 0
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, photometric
        ds.BitsAllocated = ds.BitsStored = 16
        ds.HighBit, ds.PixelRepresentation = 15, int(signed)
        ds.RescaleSlope, ds.RescaleIntercept = .125, -.03125
        ds.WindowCenter, ds.WindowWidth = 125.25, 249.5
        rows, columns = np.indices((10, 12))
        values = rows * 12 + columns + index * 500 - (300 if signed else 0)
        ds.PixelData = values.astype("<i2" if signed else "<u2").tobytes()
        ds["PixelData"].VR = "OW"
        ds.save_as(path, enforce_file_format=True)
    return directory


def change(path, attribute, value):
    ds = pydicom.dcmread(path)
    # Dataset's documented keyword API adds attributes that were absent too.
    ds.update({attribute: value})
    ds.save_as(path, enforce_file_format=True)


def test_static_mr_index_exposes_sequence_metadata_and_real_series_identity(tmp_path):
    source = write_mr_series(tmp_path / "source")
    record, = dicom.index_dicom(source)
    ds = pydicom.dcmread(record["files"][0])
    assert record["uid"] == ds.SeriesInstanceUID and record["modality"] == "MR"
    assert record["warnings"] == [] and record["slice_count"] == 5
    assert [Path(name).name for name in record["files"]] == ["005.dcm", "004.dcm", "003.dcm", "002.dcm", "001.dcm"]
    assert record["spacing"] == pytest.approx([.7, .8, 1.6])
    assert record["sequence_name"] == "synthetic_t1"
    assert record["scanning_sequence"] == "GR" and record["sequence_variant"] == "SP"
    assert record["acquisition_type"] == "3D" and record["echo_number"] == 1
    assert record["echo_time_ms"] == 3 and record["repetition_time_ms"] == 8 and record["inversion_time_ms"] == 0
    assert record["window_center"] == 125.25 and record["window_width"] == 249.5
    assert record["photometric_interpretation"] == "MONOCHROME2"


@pytest.mark.parametrize("attribute,value,diagnostic", [
    ("EchoTime", 4, "EchoTime"), ("EchoNumbers", 2, "EchoNumbers"),
    ("RepetitionTime", 9, "RepetitionTime"), ("InversionTime", 100, "InversionTime"),
    ("TemporalPositionIdentifier", 2, "temporal"), ("NumberOfTemporalPositions", 2, "Dynamic/cine"),
    ("CardiacNumberOfImages", 12, "Dynamic/cine"), ("AcquisitionNumber", 2, "AcquisitionNumber"),
    ("ComplexImageComponent", "PHASE", "magnitude"), ("DiffusionBValue", 1000, "Diffusion"),
    ("AcquisitionContrast", "DIFFUSION", "Diffusion"),
    ("PhotometricInterpretation", "MONOCHROME1", "PhotometricInterpretation"),
    ("SamplesPerPixel", 3, "scalar"), ("RescaleSlope", .25, "rescale"),
    ("RescaleIntercept", -1, "rescale"),
    ("ImageType", ["ORIGINAL", "PRIMARY", "MOSAIC"], "mosaic"),
    ("ImageType", ["ORIGINAL", "PRIMARY", "PROJECTION IMAGE"], "projection"),
    ("ImageType", ["ORIGINAL", "PRIMARY", "T2 MAP"], "parametric"),
    ("ImageType", ["ORIGINAL", "PRIMARY", "LOCALIZER"], "localizer"),
    ("SOPClassUID", "1.2.840.10008.5.1.4.1.1.4.1", "Do not combine"),
    ("SOPClassUID", "1.2.840.10008.5.1.4.1.1.4.4", "classic single-frame"),
    ("NumberOfFrames", 5, "multi-frame"),
])
def test_mr_rejects_mixed_or_unsupported_dimensions_before_converter(tmp_path, monkeypatch, attribute, value, diagnostic):
    source = write_mr_series(tmp_path / "source")
    change(source / "001.dcm", attribute, value)
    record, = dicom.index_dicom(source)
    assert any(diagnostic in message for message in record["warnings"])
    before = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in record["files"]}
    monkeypatch.setattr(dicom.subprocess, "run", lambda *args, **kwargs: pytest.fail("Rejected MRI reached converter"))
    with pytest.raises(ValueError, match=diagnostic):
        dicom.convert_series(record["files"], tmp_path / "output")
    assert not (tmp_path / "output").exists()
    assert {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in record["files"]} == before


@pytest.mark.parametrize("attribute,value", [("RescaleSlope", 0), ("RescaleSlope", float("nan")),
                                            ("RescaleIntercept", float("inf"))])
def test_mr_rejects_nonfinite_or_zero_rescale(tmp_path, attribute, value):
    source = write_mr_series(tmp_path / "source")
    change(source / "001.dcm", attribute, value)
    record, = dicom.index_dicom(source)
    assert any("must be finite" in message for message in record["warnings"])


def test_mr_sequences_with_distinct_uids_remain_separate_selections(tmp_path):
    write_mr_series(tmp_path / "t1")
    write_mr_series(tmp_path / "t2")
    records = dicom.index_dicom(tmp_path)
    assert len(records) == 2 and records[0]["uid"] != records[1]["uid"]
    assert all(not item["warnings"] for item in records)


def test_mr_nonuniform_slice_voi_uses_image_range_instead_of_one_arbitrary_slice(tmp_path):
    source = write_mr_series(tmp_path / "source")
    change(source / "001.dcm", "WindowCenter", 400)
    record, = dicom.index_dicom(source)
    assert record["warnings"] == [] and record["window_center"] is None and record["window_width"] is None


@pytest.mark.parametrize("attribute", ["EchoTime", "RepetitionTime", "InversionTime", "TriggerTime",
                                       "SpacingBetweenSlices", "NumberOfTemporalPositions",
                                       "TemporalPositionIdentifier", "CardiacNumberOfImages"])
def test_mr_legal_empty_optional_and_type2_values_do_not_invent_dimensions(tmp_path, attribute):
    source = write_mr_series(tmp_path / "source")
    for path in source.glob("*.dcm"):
        change(path, attribute, None)
    record, = dicom.index_dicom(source)
    assert record["supported"] and record["warnings"] == [] and record["unsupported_reason"] is None
    assert record["spacing"] == pytest.approx([.7, .8, 1.6])
    if attribute == "EchoTime":
        assert record["echo_time_ms"] is None


def test_mr_missing_and_empty_optional_values_have_the_same_unknown_meaning(tmp_path):
    source = write_mr_series(tmp_path / "source")
    for path in source.glob("*.dcm"):
        ds = pydicom.dcmread(path)
        del ds.InversionTime
        ds.save_as(path, enforce_file_format=True)
    change(source / "001.dcm", "InversionTime", None)
    # An unknown optional declared interval does not override patient positions.
    change(source / "002.dcm", "SpacingBetweenSlices", None)
    record, = dicom.index_dicom(source)
    assert record["supported"] and record["warnings"] == []
    assert record["inversion_time_ms"] is None


def test_mr_equivalent_decimal_timing_encodings_are_not_different_echoes(tmp_path):
    source = write_mr_series(tmp_path / "source")
    change(source / "001.dcm", "EchoTime", "3.0000")
    record, = dicom.index_dicom(source)
    assert record["supported"] and record["warnings"] == []


@pytest.mark.parametrize("attribute", ["ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing"])
def test_mr_empty_required_spatial_geometry_remains_unsupported(tmp_path, attribute):
    source = write_mr_series(tmp_path / "source")
    change(source / "001.dcm", attribute, None)
    record, = dicom.index_dicom(source)
    assert not record["supported"] and "Invalid image geometry" in record["unsupported_reason"]


def test_mr_empty_frame_count_is_reported_without_hiding_the_series(tmp_path):
    source = write_mr_series(tmp_path / "source")
    change(source / "001.dcm", "NumberOfFrames", None)
    record, = dicom.index_dicom(source)
    assert not record["supported"] and "multi-frame MR" in record["unsupported_reason"]
    assert record["slice_count"] == 5


@pytest.mark.skipif(not shutil.which("dcm2niix"), reason="Native dcm2niix is not installed")
@pytest.mark.parametrize("reflected,signed,photometric,empty_optional", [(False, False, "MONOCHROME2", False),
                                                                       (True, True, "MONOCHROME2", False),
                                                                       (False, False, "MONOCHROME1", False),
                                                                       (False, False, "MONOCHROME2", True)])
def test_real_mr_conversion_preserves_fractional_signal_patient_landmarks_and_source_bytes(
        tmp_path, monkeypatch, reflected, signed, photometric, empty_optional):
    source = write_mr_series(tmp_path / "source", reflected=reflected, signed=signed, photometric=photometric)
    if empty_optional:
        for path in source.glob("*.dcm"):
            ds = pydicom.dcmread(path)
            ds.update({name: None for name in ("EchoTime", "RepetitionTime", "InversionTime", "SpacingBetweenSlices",
                                               "TriggerTime", "NumberOfTemporalPositions", "TemporalPositionIdentifier")})
            ds.save_as(path, enforce_file_format=True)
    record, = dicom.index_dicom(source)
    assert record["warnings"] == []
    before = {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in record["files"]}
    native_run, commands = dicom.subprocess.run, []

    def run(command, **kwargs):
        commands.append(command)
        return native_run(command, **kwargs)

    monkeypatch.setattr(dicom.subprocess, "run", run)
    converted = dicom.convert_series(record["files"], tmp_path / "output")
    assert len(commands) == 1
    assert commands[0][commands[0].index("-m") + 1] == "n"
    assert commands[0][commands[0].index("-p") + 1] == "n"
    image = nib.load(converted)
    actual = image.get_fdata()
    for name in record["files"]:
        ds = pydicom.dcmread(name)
        expected = ds.pixel_array.astype(float) * float(ds.RescaleSlope) + float(ds.RescaleIntercept)
        rows, columns = np.indices(expected.shape)
        orientation = np.asarray(ds.ImageOrientationPatient, float)
        lps = (np.asarray(ds.ImagePositionPatient, float)[:, None, None]
               + orientation[:3, None, None] * columns * float(ds.PixelSpacing[1])
               + orientation[3:, None, None] * rows * float(ds.PixelSpacing[0]))
        ras = np.moveaxis(lps * np.array([-1, -1, 1])[:, None, None], 0, -1)
        locations = nib.affines.apply_affine(np.linalg.inv(image.affine), ras)
        np.testing.assert_allclose(locations, np.rint(locations), rtol=0, atol=1e-4)
        indices = tuple(np.moveaxis(np.rint(locations).astype(int), -1, 0))
        np.testing.assert_allclose(actual[indices], expected, rtol=0, atol=1e-7)
    assert {name: hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in record["files"]} == before
    manifest = json.loads((converted.parent / "conversion.json").read_text())
    assert manifest["modality"] == "MR" and manifest["intensity_scaling"] == "standard_dicom_rescale"
    assert manifest["rescale_slope"] == .125 and manifest["rescale_intercept"] == -.03125
    assert manifest["acquisition"]["photometric_interpretation"] == photometric
    assert manifest["normalizer"] is None
    assert not list(converted.parent.glob(".dicom-*"))


def test_mr_converter_does_not_accept_a_4d_result(tmp_path, monkeypatch):
    source = write_mr_series(tmp_path / "source")
    record, = dicom.index_dicom(source)

    def run(command, **kwargs):
        target = Path(command[command.index("-o") + 1])
        nib.save(nib.Nifti1Image(np.zeros((12, 10, 5, 1), np.int16), np.eye(4)), target / "reference.nii.gz")
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(dicom.subprocess, "run", run)
    monkeypatch.setenv("SCAN3D_DCM2NIIX", "synthetic-dcm2niix")
    with pytest.raises(ValueError, match="MR dimensions"):
        dicom.convert_series(record["files"], tmp_path / "output")
    assert not (tmp_path / "output" / "reference.nii.gz").exists()
    assert not (tmp_path / "output" / "conversion.json").exists()


def test_mr_presentation_honors_declared_voi_and_monochrome1_without_rewriting_samples(tmp_path):
    path = tmp_path / "signal.nii"
    values = np.linspace(.001, .009, 3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    nib.save(nib.Nifti1Image(values, np.eye(4)), path)
    before = path.read_bytes()
    policy = get_modality_adapter("MR").presentation_for_image(path, {
        "photometric_interpretation": "MONOCHROME1", "window_center": .004, "window_width": .002,
    })
    assert policy == {"units": "signal", "background_value": 0., "default_window": .002,
                      "default_level": .004, "photometric_interpretation": "MONOCHROME1",
                      "preset_group": "MR", "preview_quantization_step": 0.}
    assert path.read_bytes() == before
    np.testing.assert_array_equal(nib.load(path).dataobj, values)


@pytest.mark.parametrize("function", [None, "LINEAR", "LINEAR_EXACT", "SIGMOID", "UNSUPPORTED"])
def test_mr_source_voi_function_cannot_be_reinterpreted_as_a_linear_window(tmp_path, function):
    source = write_mr_series(tmp_path / "source", photometric="MONOCHROME1")
    for path in source.glob("*.dcm"):
        change(path, "VOILUTFunction", function)
    before = {path.name: path.read_bytes() for path in source.glob("*.dcm")}
    record, = dicom.index_dicom(source)
    assert record["supported"] and record["voi_lut_function"] == (function or "LINEAR")
    if function in {"SIGMOID", "UNSUPPORTED"}:
        assert record["window_center"] is None and record["window_width"] is None
    else:
        assert record["window_center"] == 125.25 and record["window_width"] == 249.5
    assert {path.name: path.read_bytes() for path in source.glob("*.dcm")} == before


@pytest.mark.parametrize("function", ["SIGMOID", "UNSUPPORTED"])
def test_mr_nonlinear_voi_metadata_uses_robust_signal_range_and_retains_inversion(tmp_path, function):
    path = tmp_path / "signal.nii"
    values = np.linspace(.001, .009, 3 * 4 * 5, dtype=np.float32).reshape(3, 4, 5)
    nib.save(nib.Nifti1Image(values, np.eye(4)), path)
    before = path.read_bytes()
    adapter = get_modality_adapter("MR")
    metadata = {"photometric_interpretation": "MONOCHROME1", "window_center": 125.25,
                "window_width": 249.5, "voi_lut_function": function}
    policy = adapter.presentation_for_image(path, metadata)
    expected = adapter.presentation_for_image(path, {"photometric_interpretation": "MONOCHROME1"})
    assert policy == expected
    assert policy["default_window"] < .01 and policy["default_level"] < .01
    assert policy["photometric_interpretation"] == "MONOCHROME1"
    assert metadata["voi_lut_function"] == function and metadata["window_width"] == 249.5
    assert path.read_bytes() == before
    np.testing.assert_array_equal(nib.load(path).dataobj, values)


@pytest.mark.parametrize("offset,scale", [(0, .000001), (40000, 10000), (-.005, .001)])
def test_mr_robust_window_uses_scaled_signal_and_preserves_fractional_or_large_ranges(tmp_path, offset, scale):
    path = tmp_path / "signal.nii"
    values = np.linspace(0, 1, 20 * 30 * 40, dtype=np.float32).reshape(20, 30, 40)
    image = nib.Nifti1Image(values, np.eye(4))
    image.header.set_slope_inter(scale, offset)
    nib.save(image, path)
    scaled = np.asarray(nib.load(path).dataobj)
    finite = scaled[scaled != 0]
    low, high = np.percentile(finite, [1, 99])
    policy = get_modality_adapter("MR").presentation_for_image(path, {"window_width": -1, "window_center": 4})
    assert policy["default_window"] == pytest.approx(high - low, rel=1e-6)
    assert policy["default_level"] == pytest.approx((high + low) / 2, rel=1e-6)
    assert policy["units"] == "signal" and policy["preview_quantization_step"] == 0


@pytest.mark.parametrize("value", [0., .00000001, -32000.])
def test_mr_constant_signal_has_a_finite_positive_window(tmp_path, value):
    path = tmp_path / "constant.nii"
    nib.save(nib.Nifti1Image(np.full((4, 5, 6), value, dtype=np.float32), np.eye(4)), path)
    policy = get_modality_adapter("MR").presentation_for_image(path)
    assert np.isfinite(policy["default_window"]) and policy["default_window"] > 0
    assert policy["default_level"] == pytest.approx(value)


def test_mr_presentation_rejects_nonfinite_or_4d_signal_and_unknown_photometric(tmp_path):
    path = tmp_path / "signal.nii"
    adapter = get_modality_adapter("MR")
    nib.save(nib.Nifti1Image(np.full((4, 5, 6), np.nan, dtype=np.float32), np.eye(4)), path)
    with pytest.raises(ValueError, match="finite signal"):
        adapter.presentation_for_image(path)
    with pytest.raises(ValueError, match="MONOCHROME"):
        adapter.presentation_for_image(path, {"photometric_interpretation": "RGB"})
    nib.save(nib.Nifti1Image(np.zeros((4, 5, 6, 2), dtype=np.float32), np.eye(4)), path)
    with pytest.raises(ValueError, match="scalar 3D"):
        adapter.presentation_for_image(path)


def test_ct_image_specific_presentation_preserves_original_default_policy(tmp_path):
    adapter = get_modality_adapter("CT")
    assert adapter.presentation_for_image(tmp_path / "unused.nii", {
        "window_center": 4, "window_width": 8, "photometric_interpretation": PhotometricInterpretation.MONOCHROME1,
    }) == adapter.presentation()

