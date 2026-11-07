"""Synthetic oblique CT series and masks shared by the DICOM SEG and OsiriX ROI tests.

The non-square oblique source and permuted/reflected NIfTI grid deliberately make
an axis-only transpose insufficient for segmentation export.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
from uuid import uuid4

import numpy as np
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid
from pydicom.valuerep import format_number_as_ds


UID_ROOT = "1.2.826.0.1.3680043.10.543.20261028"
STUDY_UID = UID_ROOT + ".1"
FRAME_UID = UID_ROOT + ".9"
FIXTURE_GENERATION = 3


def dicom_bytes(series_number: int, index: int, *, complete: bool = True) -> bytes:
    """One classic CT image with real signed pixels and physical source geometry."""
    from io import BytesIO

    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = f"{UID_ROOT}.{FIXTURE_GENERATION}.{series_number + 1}.{index + 1}"
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    file_meta.ImplementationClassUID = "1.2.826.0.1.3680043.10.543.1"
    ds = FileDataset(None, {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SpecificCharacterSet = "ISO_IR 100"
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID = STUDY_UID
    ds.SeriesInstanceUID = f"{UID_ROOT}.{FIXTURE_GENERATION}.1.{series_number}"
    ds.FrameOfReferenceUID = FRAME_UID
    ds.PatientName = "SYNTHETIC^DICOM"
    ds.PatientID = "SCAN3D-SYNTHETIC"
    ds.StudyID = "ORTHANC-DEMO"
    ds.AccessionNumber = "SYNTHETIC"
    ds.StudyDescription = "Synthetic Orthanc integration"
    ds.SeriesDescription = "Synthetic CT complete v3 (12 of12)" if complete else "Synthetic CT incomplete v3 (3 of12)"
    ds.Modality = "CT"
    ds.SeriesNumber = series_number
    ds.AcquisitionNumber = 1
    ds.InstanceNumber = index + 1
    ds.ImagesInAcquisition = 12
    ds.NumberOfTemporalPositions = 1
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.PatientPosition = "HFS"
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.ImagePositionPatient = [-32, -32, index * 2]
    ds.PixelSpacing = [1, 1]
    ds.SliceThickness = 2
    ds.SpacingBetweenSlices = 2
    ds.Rows = ds.Columns = 64
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 1
    ds.RescaleSlope = 1
    ds.RescaleIntercept = 0
    ds.RescaleType = "HU"
    ds.WindowCenter = 40
    ds.WindowWidth = 400
    for date in ["StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate"]:
        setattr(ds, date, "20261028")
    # Study/series times describe the shared acquisition, not each slice. Varying
    # them makes dcm2niix correctly split the source into separate volumes.
    ds.StudyTime = ds.SeriesTime = "090000"
    ds.AcquisitionTime = ds.ContentTime = f"0900{index:02d}"
    y, x = np.mgrid[:64, :64]
    pixels = np.full((64, 64), -1000, dtype="<i2")
    body = ((x - 32) / 26) ** 2 + ((y - 32) / 25) ** 2 < 1
    pixels[body] = 35 + index
    for center in [22, 42]:
        lung = ((x - center) / 8) ** 2 + ((y - 29) / 15) ** 2 < 1
        pixels[lung & body] = -720
    pixels[(x - 32) ** 2 + (y - 46) ** 2 < 16] = 650
    pixels[(x - 31) ** 2 + (y - 35) ** 2 < 9] = 350
    ds.PixelData = pixels.tobytes()
    stream = BytesIO()
    ds.save_as(stream, enforce_file_format=True)
    return stream.getvalue()


def validate_decimal_strings(dataset):
    for element in dataset.iterall():
        if element.VR == "DS":
            values = element.value if element.VM > 1 else [element.value]
            if any(len(str(value)) > 16 for value in values):
                raise ValueError("A generated DICOM decimal string exceeds its 16-character VR limit")


def source_fixture():
    """Generate fresh synthetic source identities and visibly asymmetric masks."""
    z, row, column = np.indices((12, 48, 64))
    ring = ((column - 23) / 11) ** 2 + ((row - 21) / 13) ** 2 + ((z - 5) / 4) ** 2 <= 1
    ring &= ~(((column - 23) ** 2 + (row - 21) ** 2 < 9) & (z >= 2) & (z <= 8))
    block = (z >= 4) & (z <= 9) & (row >= 17) & (row <= 34) & (column >= 25) & (column <= 45)
    block &= ~((row >= 29) & (column >= 39))
    masks = np.stack([ring, block], axis=-1)
    pixels = np.full(z.shape, -1000, dtype=np.int16)
    body = ((column - 32) / 29) ** 2 + ((row - 24) / 22) ** 2 < 1
    pixels[body] = (25 + z)[body]
    pixels[ring], pixels[block], pixels[ring & block] = 150, 350, 550
    angle = np.deg2rad(23)
    orientation = np.array([np.cos(angle), 0, -np.sin(angle), 0, 1, 0])
    normal = np.cross(orientation[:3], orientation[3:])
    study, series, frame = [generate_uid() for _ in range(3)]
    patient = "SCAN3D-SEG-" + uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    images = []
    for index in range(len(pixels)):
        image = pydicom.dcmread(BytesIO(dicom_bytes(3, index)))
        del image.NumberOfTemporalPositions
        image.StudyInstanceUID, image.SeriesInstanceUID, image.FrameOfReferenceUID = study, series, frame
        image.SOPInstanceUID = image.file_meta.MediaStorageSOPInstanceUID = generate_uid()
        image.PatientName, image.PatientID = "SYNTHETIC^SEGRETURN", patient
        image.PatientBirthDate = image.PatientSex = image.ReferringPhysicianName = ""
        image.StudyID, image.AccessionNumber = "SEGRETURN", "SYNTHETIC"
        image.StudyDescription = "3DScan segmentation return SYNTHETIC"
        image.SeriesDescription = "3DScan synthetic oblique CT source"
        image.Manufacturer, image.KVP = "3DScan", ""
        image.SeriesNumber, image.PositionReferenceIndicator = 900, ""
        image.Rows, image.Columns = pixels.shape[1:]
        image.ImageOrientationPatient = [format_number_as_ds(float(value)) for value in orientation]
        position = np.array([44, -70, -20]) + index * 2.4 * normal
        image.ImagePositionPatient = [format_number_as_ds(float(value)) for value in position]
        image.PixelSpacing = [format_number_as_ds(value) for value in (1.1, 0.8)]
        image.SliceThickness = image.SpacingBetweenSlices = format_number_as_ds(2.4)
        for field in ("StudyDate", "SeriesDate", "AcquisitionDate", "ContentDate"):
            setattr(image, field, now.strftime("%Y%m%d"))
        for field in ("StudyTime", "SeriesTime", "AcquisitionTime", "ContentTime"):
            setattr(image, field, now.strftime("%H%M%S"))
        image.PixelData = pixels[index].astype("<i2").tobytes()
        validate_decimal_strings(image)
        images.append(image)
    return images, pixels, masks


def as_classic_mr(image):
    """The same synthetic plane stored as single-frame classic MR, without CT-only attributes."""
    image.SOPClassUID = image.file_meta.MediaStorageSOPClassUID = pydicom.uid.MRImageStorage
    image.Modality, image.ImageType = "MR", ["ORIGINAL", "PRIMARY", "M"]
    for keyword in ("KVP", "RescaleType"):
        if keyword in image:
            delattr(image, keyword)
    return image


def native_grid(images, pixels, masks):
    """An exact alternative storage grid, with columns reversed and axes permuted."""
    first = images[0]
    directions = np.asarray(first.ImageOrientationPatient, dtype=float).reshape(2, 3)
    column_step = float(first.PixelSpacing[1]) * directions[0]
    row_step = float(first.PixelSpacing[0]) * directions[1]
    slice_step = np.asarray(images[1].ImagePositionPatient) - np.asarray(first.ImagePositionPatient)
    affine = np.eye(4)
    affine[:3, :3] = np.column_stack([-column_step, slice_step, row_step])
    affine[:3, 3] = np.asarray(first.ImagePositionPatient) + (int(first.Columns) - 1) * column_step
    affine[:3] *= np.array([-1, -1, 1])[:, None]
    return pixels.transpose(2, 0, 1)[::-1], masks.transpose(2, 0, 1, 3)[::-1], affine
