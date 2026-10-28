"""Prepare and qualify a synthetic CT + binary DICOM SEG; never send anything.

Run in an isolated environment with highdicom 0.28.2 and the application's
imaging dependencies. Output must be a new /tmp/scan3d-osirix-seg-* directory.
The non-square oblique source and permuted/reflected NIfTI grid deliberately
make an axis-only transpose insufficient for segmentation export.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import tempfile
from uuid import uuid4

import nibabel as nib
import numpy as np
import pydicom
from pydicom.sr.coding import Code
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, SegmentationStorage, generate_uid
from pydicom.valuerep import format_number_as_ds

from scan3d import imaging
from scripts.orthanc_seed_demo import dicom_bytes


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


def source_positions_ras(image):
    row, column = np.indices((image.Rows, image.Columns))
    directions = np.asarray(image.ImageOrientationPatient, dtype=float).reshape(2, 3)
    lps = (
        np.asarray(image.ImagePositionPatient, dtype=float)
        + column[..., None] * float(image.PixelSpacing[1]) * directions[0]
        + row[..., None] * float(image.PixelSpacing[0]) * directions[1]
    )
    return lps * [-1, -1, 1]


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


def project_masks(images, masks, affine):
    """Gather exact mask voxels by physical position, without interpolation."""
    affine = np.asarray(affine, dtype=float)
    if masks.ndim != 4 or affine.shape != (4, 4) or not np.isfinite(affine).all() or abs(np.linalg.det(affine[:3, :3])) < 1e-9:
        raise ValueError("Invalid authoritative mask grid")
    inverse = np.linalg.inv(affine)
    result, errors = [], []
    for image in images:
        ras = source_positions_ras(image)
        ijk = ras @ inverse[:3, :3].T + inverse[:3, 3]
        integer = np.rint(ijk).astype(np.int64)
        if not np.allclose(ijk, integer, rtol=0, atol=1e-4):
            raise ValueError("Source pixels do not match integer authoritative mask voxels")
        if np.any(integer < 0) or np.any(integer >= np.array(masks.shape[:3])):
            raise ValueError("Source pixels fall outside the authoritative mask grid")
        result.append(masks[tuple(integer.transpose(2, 0, 1))])
        restored = integer @ affine[:3, :3].T + affine[:3, 3]
        errors.append(float(np.linalg.norm(restored - ras, axis=-1).max()))
    return np.stack(result), max(errors)


def encode_segmentation(images, masks):
    import highdicom as hd

    if hd.__version__ != "0.28.2":
        raise RuntimeError("Qualify with the pinned test-only highdicom 0.28.2")
    algorithm = hd.AlgorithmIdentificationSequence(
        name="3DScan synthetic phantom", version="1", family=Code("SYNTHETIC", "99SCAN3D", "Synthetic phantom generation")
    )
    descriptions = [
        hd.seg.SegmentDescription(
            segment_number=index,
            segment_label=label,
            segmented_property_category=Code("PHANTOM", "99SCAN3D", "Synthetic object"),
            segmented_property_type=Code(code, "99SCAN3D", label),
            algorithm_type=hd.seg.SegmentAlgorithmTypeValues.AUTOMATIC,
            algorithm_identification=algorithm,
            tracking_uid=generate_uid(),
            tracking_id=code,
            display_color=hd.color.CIELabColor.from_rgb(*color),
        )
        for index, (label, code, color) in enumerate([("Synthetic ring", "RING", (255, 64, 64)), ("Synthetic block", "BLOCK", (0, 220, 255))], start=1)
    ]
    result = hd.seg.Segmentation(
        source_images=images,
        pixel_array=masks.astype(np.uint8),
        segmentation_type=hd.seg.SegmentationTypeValues.BINARY,
        segment_descriptions=descriptions,
        series_instance_uid=generate_uid(),
        series_number=901,
        sop_instance_uid=generate_uid(),
        instance_number=1,
        manufacturer="3DScan",
        manufacturer_model_name="Synthetic return test",
        software_versions="1",
        device_serial_number="SYNTHETIC",
        series_description="3DScan synthetic binary SEG (2 masks)",
        transfer_syntax_uid=ExplicitVRLittleEndian,
        omit_empty_frames=False,
        content_label="SYNTHETIC_SEG",
        content_description="SYNTHETIC phantom; not patient data",
    )
    return result


def verify_segmentation(path, images, truth):
    """Decode packed bits and per-frame references independently of highdicom."""
    seg = pydicom.dcmread(path)
    validate_decimal_strings(seg)
    assert seg.SOPClassUID == SegmentationStorage and seg.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
    assert seg.SegmentationType == "BINARY" and seg.BitsAllocated == seg.BitsStored == 1
    assert seg.FrameOfReferenceUID == images[0].FrameOfReferenceUID and seg.StudyInstanceUID == images[0].StudyInstanceUID
    assert seg.PatientID == images[0].PatientID and seg.SeriesInstanceUID != images[0].SeriesInstanceUID
    assert len(seg.SegmentSequence) == 2 and int(seg.NumberOfFrames) == len(images) * 2
    assert [item.SegmentLabel for item in seg.SegmentSequence] == ["Synthetic ring", "Synthetic block"]
    sources = {str(image.SOPInstanceUID): (index, image) for index, image in enumerate(images)}
    referenced = seg.ReferencedSeriesSequence[0]
    assert referenced.SeriesInstanceUID == images[0].SeriesInstanceUID
    assert {str(item.ReferencedSOPInstanceUID) for item in referenced.ReferencedInstanceSequence} == set(sources)
    shared = seg.SharedFunctionalGroupsSequence[0]
    samples = int(seg.NumberOfFrames) * int(seg.Rows) * int(seg.Columns)
    frames = np.unpackbits(np.frombuffer(seg.PixelData, dtype=np.uint8), bitorder="little")[:samples]
    frames = frames.reshape(int(seg.NumberOfFrames), int(seg.Rows), int(seg.Columns))
    seen = set()
    for pixels, group in zip(frames, seg.PerFrameFunctionalGroupsSequence, strict=True):
        segment = int(group.SegmentIdentificationSequence[0].ReferencedSegmentNumber)
        source = group.DerivationImageSequence[0].SourceImageSequence[0]
        index, image = sources[str(source.ReferencedSOPInstanceUID)]
        assert source.ReferencedSOPClassUID == CTImageStorage and source.SpatialLocationsPreserved == "YES"
        assert (index, segment) not in seen
        seen.add((index, segment))
        assert np.array_equal(pixels, truth[index, :, :, segment - 1])
        assert (int(seg.Rows), int(seg.Columns)) == (int(image.Rows), int(image.Columns))
        for sequence, field, expected in [
            ("PlanePositionSequence", "ImagePositionPatient", image.ImagePositionPatient),
            ("PlaneOrientationSequence", "ImageOrientationPatient", image.ImageOrientationPatient),
            ("PixelMeasuresSequence", "PixelSpacing", image.PixelSpacing),
        ]:
            actual = getattr(group, sequence, getattr(shared, sequence, None))[0]
            assert np.allclose(getattr(actual, field), expected, rtol=0, atol=1e-6)
    assert seen == {(index, segment) for index in range(len(images)) for segment in (1, 2)}
    return {
        "independent_packed_bits": True,
        "all_frame_references_and_geometry": True,
        "decimal_strings_within_vr_limit": True,
        "binary_frames": len(seen),
    }


def prepare(output: Path):
    output = output.absolute()
    if output.parent.resolve() != Path(tempfile.gettempdir()).resolve() or not output.name.startswith("scan3d-osirix-seg-"):
        raise ValueError("Use a new /tmp/scan3d-osirix-seg-* directory")
    if any(parent.is_symlink() for parent in (output, *output.parents)):
        raise ValueError("Output must not traverse symbolic links")
    output.mkdir(mode=0o700)
    previous_umask = os.umask(0o077)
    try:
        images, pixels, truth = source_fixture()
        native_pixels, native_masks, affine = native_grid(images, pixels, truth)
        for name, array in [("reference.nii.gz", native_pixels), ("ring.nii.gz", native_masks[..., 0]), ("block.nii.gz", native_masks[..., 1])]:
            image = nib.Nifti1Image(array.astype(np.uint8) if array.dtype == bool else array, affine)
            image.header.set_xyzt_units("mm")
            nib.save(image, output / name)
        reference, saved_affine = imaging._image(output / "reference.nii.gz")
        projected_ct, _ = project_masks(images, np.asanyarray(reference.dataobj)[..., None], saved_affine)
        assert np.array_equal(projected_ct[..., 0], pixels)
        masks = np.stack([imaging._binary(output / name, reference, saved_affine)[0] for name in ("ring.nii.gz", "block.nii.gz")], axis=-1)
        projected, error = project_masks(images, masks, saved_affine)
        assert np.array_equal(projected, truth)
        ct = output / "ct"
        ct.mkdir(mode=0o700)
        for index, image in enumerate(images):
            image.save_as(ct / f"image-{index + 1:03d}.dcm", enforce_file_format=True)
        seg_path = output / "segmentation.dcm"
        encode_segmentation(images, projected).save_as(seg_path, enforce_file_format=True)
        checks = verify_segmentation(seg_path, images, truth)
        import highdicom as hd

        decoded = hd.seg.segread(seg_path).get_pixels_by_source_instance(
            source_sop_instance_uids=[str(image.SOPInstanceUID) for image in images], segment_numbers=[1, 2]
        )
        assert np.array_equal(decoded, truth)
        checks.update(highdicom_source_uid_roundtrip=True, exact_native_mask_projection=True, exact_native_ct_projection=True)
        report = {
            "synthetic_only": True,
            "network_operations": False,
            "patient_id": images[0].PatientID,
            "study_uid": str(images[0].StudyInstanceUID),
            "source_series_uid": str(images[0].SeriesInstanceUID),
            "seg_series_uid": str(pydicom.dcmread(seg_path, stop_before_pixels=True).SeriesInstanceUID),
            "ct_instances": len(images),
            "mask_shape_source": list(truth.shape),
            "mask_shape_native": list(masks.shape),
            "segment_voxels": truth.sum(axis=(0, 1, 2)).tolist(),
            "overlap_voxels": int(np.count_nonzero(truth[..., 0] & truth[..., 1])),
            "max_projection_error_mm": error,
            "highdicom_version": hd.__version__,
            "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "dicom_files": [
                {
                    "path": str(path.relative_to(output)),
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "size": path.stat().st_size,
                    "sop_instance_uid": str(pydicom.dcmread(path, stop_before_pixels=True).SOPInstanceUID),
                }
                for path in [*sorted(ct.glob("*.dcm")), seg_path]
            ],
            "checks": checks,
            "expected_view": "12 oblique CT slices; red Synthetic ring with a hole; cyan Synthetic block overlapping its right side",
        }
        (output / "qualification.json").write_text(json.dumps(report, indent=2) + "\n")
        return report
    finally:
        os.umask(previous_umask)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps({"output": str(args.output.absolute()), **prepare(args.output)}, indent=2))


if __name__ == "__main__":
    main()
