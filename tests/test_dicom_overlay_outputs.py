"""Native DICOM overlays are annotations, while ambiguous scan outputs still fail."""
import hashlib
import json
from pathlib import Path
import shutil
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
import pytest

from scan3d import dicom


SHAPE = (12, 10, 3)
AFFINE = np.diag([-.7, -.8, 1., 1.])


def sources(directory, *, groups=(0x6000,)):
    directory.mkdir()
    series, frame, study = generate_uid(), generate_uid(), generate_uid()
    for z in range(SHAPE[2]):
        path = directory / f'{z}.dcm'
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID, meta.MediaStorageSOPInstanceUID = CTImageStorage, generate_uid()
        meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = FileDataset(str(path), {}, file_meta=meta, preamble=b'\0' * 128)
        ds.SOPClassUID, ds.SOPInstanceUID = meta.MediaStorageSOPClassUID, meta.MediaStorageSOPInstanceUID
        ds.SeriesInstanceUID, ds.StudyInstanceUID, ds.FrameOfReferenceUID = series, study, frame
        ds.Modality, ds.SeriesDescription, ds.PatientName, ds.PatientID = 'CT', 'Synthetic overlay CT', 'Synthetic^Overlay', 'TEST'
        ds.SeriesNumber, ds.InstanceNumber = 1, z + 1
        ds.ImageType = ['ORIGINAL', 'PRIMARY', 'AXIAL']
        ds.ImageOrientationPatient, ds.ImagePositionPatient = [1, 0, 0, 0, 1, 0], [0, 0, z]
        ds.Rows, ds.Columns, ds.PixelSpacing, ds.SliceThickness = 10, 12, [.8, .7], 1
        ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, 'MONOCHROME2'
        ds.BitsAllocated = ds.BitsStored = 16
        ds.HighBit, ds.PixelRepresentation = 15, 0
        ds.RescaleSlope, ds.RescaleIntercept, ds.RescaleType = 1, -1024, 'HU'
        ds.PixelData = (np.arange(120).reshape(10, 12) + 1000 + z * 200).astype('<u2').tobytes()
        ds['PixelData'].VR = 'OW'
        if z == 1:  # A real annotation need not exist on every scan slice.
            mask = np.zeros((10, 12), np.uint8); mask[3:7, 4:8] = 1
            packed = np.packbits(mask.reshape(-1), bitorder='little').tobytes()
            packed += b'\0' * (len(packed) % 2)
            for group in groups:
                for element, vr, value in [(0x0010, 'US', 10), (0x0011, 'US', 12), (0x0040, 'CS', 'G'),
                                           (0x0050, 'SS', [1, 1]), (0x0100, 'US', 1), (0x0102, 'US', 0),
                                           (0x3000, 'OW', packed)]:
                    ds.add_new((group, element), vr, value)
        ds.save_as(path, enforce_file_format=True)
    return dicom.index_dicom(directory)[0]['files']


def save_image(path, values=None, affine=AFFINE, *, slope=None):
    values = np.full(SHAPE, 75, np.int16) if values is None else values
    image = nib.Nifti1Image(values, affine)
    image.header.set_xyzt_units('mm')
    if slope is not None:
        image.header.set_slope_inter(slope, 0)
    nib.save(image, path)


def fake_converter(monkeypatch, write):
    monkeypatch.setenv('SCAN3D_DCM2NIIX', 'synthetic-dcm2niix')
    def run(command, **kwargs):
        output = Path(command[command.index('-o') + 1]); write(output)
        return SimpleNamespace(returncode=0, stdout=b'dcm2niix version v1.0.20250506', stderr=b'')
    monkeypatch.setattr(dicom.subprocess, 'run', run)


def test_proven_multiple_overlay_groups_are_recorded_without_altering_reference(tmp_path, monkeypatch):
    files = sources(tmp_path / 'source', groups=(0x6000, 0x6006))
    before = {p: Path(p).read_bytes() for p in files}
    def write(output):
        save_image(output / 'reference.nii.gz')
        for number in (1, 4):
            mask = np.zeros(SHAPE, np.uint8); mask[3:7, 3:7, 1] = 1
            save_image(output / f'reference_ROI{number}.nii.gz', mask)
    fake_converter(monkeypatch, write)
    reference = dicom.convert_series(files, tmp_path / 'converted')
    np.testing.assert_array_equal(nib.load(reference).dataobj, np.full(SHAPE, 75, np.int16))
    manifest = json.loads((reference.parent / 'conversion.json').read_text())
    assert manifest['ignored_outputs'] == [
        {'file': 'reference_ROI1.nii.gz', 'kind': 'dicom_overlay', 'overlay_group': '6000', 'binary_grid_verified': True},
        {'file': 'reference_ROI4.nii.gz', 'kind': 'dicom_overlay', 'overlay_group': '6006', 'binary_grid_verified': True}]
    assert all(Path(p).read_bytes() == content for p, content in before.items())
    assert not list(reference.parent.glob('*ROI*'))  # Source DICOM retains the original annotation.


@pytest.mark.parametrize('problem', ['no_source_overlay', 'wrong_group', 'extra_ct', 'extra_ct_and_overlay',
    'bad_shape', 'bad_affine', 'nonbinary', 'scaled', 'wrong_dtype', 'broken', 'overlay_only', 'invalid_overlay_only'])
def test_overlay_names_cannot_hide_unknown_or_malformed_scan_outputs(tmp_path, monkeypatch, problem):
    files = sources(tmp_path / 'source', groups=() if problem == 'no_source_overlay' else (0x6000,))
    def write(output):
        if problem not in ('overlay_only', 'invalid_overlay_only'):
            save_image(output / 'reference.nii.gz')
        if problem in ('extra_ct', 'extra_ct_and_overlay'):
            save_image(output / 'reference_second.nii.gz')
        if problem == 'extra_ct':
            return
        name = 'reference_ROI2.nii.gz' if problem == 'wrong_group' else 'reference_ROI17.nii.gz' if problem == 'invalid_overlay_only' else 'reference_ROI1.nii.gz'
        values = np.zeros((12, 10, 2) if problem == 'bad_shape' else SHAPE, np.int16 if problem == 'wrong_dtype' else np.uint8)
        if problem == 'nonbinary': values[-1, -1, -1] = 2
        affine = AFFINE.copy()
        if problem == 'bad_affine': affine[0, 3] += .2
        if problem == 'broken': (output / name).write_bytes(b'not a NIfTI image')
        else: save_image(output / name, values, affine, slope=2 if problem == 'scaled' else None)
    fake_converter(monkeypatch, write)
    output = tmp_path / 'converted'
    with pytest.raises(ValueError, match='exactly one'):
        dicom.convert_series(files, output)
    assert not (output / 'reference.nii.gz').exists()
    assert not (output / 'conversion.json').exists()


def test_binary_overlay_validation_streams_to_the_last_block_without_materializing_proxy(tmp_path, monkeypatch):
    shape = (257, 257, 65)  # Cross the bounded 4 MiB read boundary.
    reference = nib.Nifti1Image(np.zeros(shape, np.int16), np.eye(4))
    mask = np.zeros(shape, np.uint8); path = tmp_path / 'roi.nii.gz'
    nib.save(nib.Nifti1Image(mask, np.eye(4)), path)
    def materialization_is_forbidden(*args, **kwargs):
        pytest.fail('Overlay validation must stream, not materialize its array proxy')
    monkeypatch.setattr(nib.arrayproxy.ArrayProxy, '__array__', materialization_is_forbidden)
    assert dicom._is_native_overlay(path, reference)
    mask[-1, -1, -1] = 2
    nib.save(nib.Nifti1Image(mask, np.eye(4)), path)
    assert not dicom._is_native_overlay(path, reference)


def test_native_dcm2niix_overlay_is_not_mistaken_for_a_second_ct(tmp_path, monkeypatch):
    pinned = Path(__file__).resolve().parents[1] / '.tools/bin/dcm2niix'
    executable = str(pinned) if pinned.is_file() else shutil.which('dcm2niix')
    if not executable: pytest.skip('Native dcm2niix not installed')
    monkeypatch.setenv('SCAN3D_DCM2NIIX', executable)
    files = sources(tmp_path / 'source')
    before = {p: hashlib.sha256(Path(p).read_bytes()).hexdigest() for p in files}
    converted = dicom.convert_series(files, tmp_path / 'converted')
    image = nib.load(converted); values = np.asanyarray(image.dataobj); inverse = np.linalg.inv(image.affine)
    for filename in files:
        source = pydicom.dcmread(filename)
        rows, columns = np.indices((source.Rows, source.Columns)); rows, columns = rows.reshape(-1), columns.reshape(-1)
        lps = np.asarray(source.ImagePositionPatient) + np.column_stack((columns * .7, rows * .8, np.zeros(len(rows))))
        ijk = np.rint(nib.affines.apply_affine(inverse, lps * [-1, -1, 1])).astype(int)
        expected = source.pixel_array.reshape(-1).astype(float) - 1024
        np.testing.assert_array_equal(values[tuple(ijk.T)], expected)
    manifest = json.loads((converted.parent / 'conversion.json').read_text())
    assert manifest['ignored_outputs'][0]['file'] == 'reference_ROI1.nii.gz'
    assert all(hashlib.sha256(Path(p).read_bytes()).hexdigest() == digest for p, digest in before.items())


def test_codec_retry_keeps_overlay_derivatives_separate_from_primary_ct(tmp_path, monkeypatch):
    from test_imaging import compressed_conversion_sources
    files = compressed_conversion_sources(tmp_path)
    for filename in files:
        source = pydicom.dcmread(filename)
        mask = np.zeros((64, 64), np.uint8); mask[20:40, 20:40] = 1
        for element, vr, value in [(0x0010, 'US', 64), (0x0011, 'US', 64), (0x0040, 'CS', 'G'),
                                   (0x0050, 'SS', [1, 1]), (0x0100, 'US', 1), (0x0102, 'US', 0),
                                   (0x3000, 'OW', np.packbits(mask.reshape(-1), bitorder='little').tobytes())]:
            source.add_new((0x6000, element), vr, value)
        source.save_as(filename, enforce_file_format=True)
    before = {p: Path(p).read_bytes() for p in files}
    pinned = Path(__file__).resolve().parents[1] / '.tools/bin/dcm2niix'
    executable = str(pinned) if pinned.is_file() else shutil.which('dcm2niix')
    if not executable: pytest.skip('Native dcm2niix not installed')
    monkeypatch.setenv('SCAN3D_DCM2NIIX', executable)
    native_run, calls = dicom.subprocess.run, []
    def run(command, **kwargs):
        target = Path(command[command.index('-o') + 1]); calls.append(target)
        if len(calls) == 1:
            (target / 'reference.nii.gz').write_bytes(b'never publish a partial failed conversion')
            (target / 'reference_ROI1.nii.gz').write_bytes(b'never reuse a partial overlay')
            return SimpleNamespace(returncode=1, stdout=b'OpenJPEG failed to read the header', stderr=b'')
        assert target != calls[0] and not list(target.iterdir())
        return native_run(command, **kwargs)
    monkeypatch.setattr(dicom.subprocess, 'run', run)
    converted = dicom.convert_series(files, tmp_path / 'converted')
    manifest = json.loads((converted.parent / 'conversion.json').read_text())
    assert len(calls) == 2
    assert manifest['normalizer']['name'] == 'pydicom-gdcm'
    assert manifest['ignored_outputs'][0]['overlay_group'] == '6000'
    assert all(Path(p).read_bytes() == original for p, original in before.items())
    # Actual native retry still preserves all scaled CT samples, independently of overlay pixels.
    image = nib.load(converted); values = np.asanyarray(image.dataobj)
    for filename in files:
        source = pydicom.dcmread(filename); source.pixel_array_options(decoding_plugin='gdcm')
        rows, columns = np.indices((64, 64)); positions = np.asarray(source.ImagePositionPatient)[:, None, None]
        orientation = np.asarray(source.ImageOrientationPatient)
        lps = positions + orientation[:3, None, None] * columns * float(source.PixelSpacing[1]) + orientation[3:, None, None] * rows * float(source.PixelSpacing[0])
        ijk = np.rint(nib.affines.apply_affine(np.linalg.inv(image.affine), np.moveaxis(lps * np.array([-1, -1, 1])[:, None, None], 0, -1))).astype(int)
        np.testing.assert_array_equal(values[tuple(np.moveaxis(ijk, -1, 0))], source.pixel_array.astype(float) * 2 - 1024)
