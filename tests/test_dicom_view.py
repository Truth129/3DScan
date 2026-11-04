"""Real Secondary Capture encoding: exact pixels, copied identity, refused captures."""

from contextlib import contextmanager
from io import BytesIO
import os
from pathlib import Path
import stat
import struct
import threading
import warnings
import zlib

import highdicom
import numpy as np
from PIL import Image
import pydicom
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid
import pytest

from scan3d import dicom_view
from scan3d.dicom_view import DicomViewError, export_view, read_view_png
from synthetic_dicom import dicom_bytes

REVISION = "abcdef0123456789abcdef0123456789"


def noise(rows=24, columns=32, channels=3, seed=1):
    return np.random.default_rng(seed).integers(0, 256, (rows, columns, channels), dtype=np.uint8)


def png_bytes(pixels, **kwargs):
    stream = BytesIO()
    Image.fromarray(pixels).save(stream, "PNG", **kwargs)
    return stream.getvalue()


def chunk(kind, body):
    return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))


def hand_png(width, height, depth=8, colour=6, data=b"\0" * 64, *, before_data=b""):
    header = struct.pack(">IIBBBBB", width, height, depth, colour, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + before_data + chunk(b"IDAT", zlib.compress(data)) + chunk(b"IEND", b"")


def chunk_offset(data, kind):
    return data.index(kind) - 4


def flipped(data, offset):
    damaged = bytearray(data)
    damaged[offset] ^= 0xFF
    return bytes(damaged)


GOOD = png_bytes(noise(channels=4))
IDAT = chunk_offset(GOOD, b"IDAT")
IDAT_LENGTH = struct.unpack(">I", GOOD[IDAT : IDAT + 4])[0]


def apng():
    stream = BytesIO()
    first, second = (Image.fromarray(noise(seed=seed)) for seed in (1, 2))
    first.save(stream, "PNG", save_all=True, append_images=[second])
    return stream.getvalue()


REFUSED = {
    "empty": b"",
    "truncated in IDAT": GOOD[: IDAT + 30],
    "bytes after IEND": GOOD + b"\0",
    "IDAT checksum only": flipped(GOOD, IDAT + 8 + IDAT_LENGTH),
    "IDAT too short": hand_png(16, 16, data=b"\0" * 8),
    "first chunk is not IHDR": b"\x89PNG\r\n\x1a\n" + chunk(b"tEXt", b"a\0b") + GOOD[8:],
    "chunk longer than the file": GOOD[:IDAT] + struct.pack(">I", 2**31) + GOOD[IDAT + 4 :],
    "16 bit": png_bytes(noise(channels=1)[..., 0].astype(np.uint16) * 257),
    "palette": hand_png(4, 4, colour=3),
    "animated": apng(),
    "colour key": hand_png(2, 1, colour=2, data=b"\0\x01\x02\x03\x04\x05\x06", before_data=chunk(b"tRNS", b"\0\x01\0\x02\0\x03")),
    "wider than an axis allows": hand_png(16385, 1),
    "just over the pixel limit": hand_png(6145, 6145),
    "blank after alpha": png_bytes(np.dstack([noise(8, 8), np.zeros((8, 8), dtype=np.uint8)])),
}


@pytest.mark.parametrize("name", REFUSED)
def test_every_bad_capture_is_a_fixed_public_refusal(tmp_path, name):
    path = tmp_path / "private-capture.png"
    path.write_bytes(REFUSED[name])
    with pytest.raises(DicomViewError) as caught:
        read_view_png(path)
    assert type(caught.value) is DicomViewError
    assert str(tmp_path) not in str(caught.value) and "capture" in str(caught.value).lower()


@pytest.mark.parametrize("error", [Image.DecompressionBombError("synthetic"), zlib.error("synthetic")])
def test_decoder_failures_never_escape_as_pillow_errors(tmp_path, monkeypatch, error):
    path = tmp_path / "capture.png"
    path.write_bytes(GOOD)

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr(dicom_view.Image, "open", fail)
    with pytest.raises(DicomViewError, match="not a complete PNG"):
        read_view_png(path)


def test_decoding_is_serialized_across_threads(tmp_path, monkeypatch):
    path = tmp_path / "capture.png"
    path.write_bytes(GOOD)
    flatten, active, peak, guard = dicom_view._flatten, [0], [0], threading.Lock()
    barrier = threading.Barrier(4)

    def tracked(pixels):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        threading.Event().wait(0.02)
        with guard:
            active[0] -= 1
        return flatten(pixels)

    monkeypatch.setattr(dicom_view, "_flatten", tracked)

    def decode():
        barrier.wait()
        read_view_png(path)

    threads = [threading.Thread(target=decode) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak[0] == 1


@contextmanager
def lenient_writing():
    """Source files hold values the writer refuses; the exporter's writer stays strict."""
    settings = pydicom.config.settings
    previous = settings.writing_validation_mode
    settings.writing_validation_mode = pydicom.config.IGNORE
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            yield
    finally:
        settings.writing_validation_mode = previous


def write_sources(root, count=3, *, edit=None, **tags):
    """Synthetic CT files with ``tags`` on every image; None removes a tag."""
    study, series = generate_uid(), generate_uid()
    root.mkdir(exist_ok=True)
    paths = []
    with lenient_writing():
        for index in range(count):
            image = pydicom.dcmread(BytesIO(dicom_bytes(3, index)))
            image.StudyInstanceUID, image.SeriesInstanceUID = study, series
            image.SOPInstanceUID = image.file_meta.MediaStorageSOPInstanceUID = generate_uid()
            image.PatientName, image.PatientID = "SYNTHETIC^VIEW", "SCAN3D-VIEW"
            image.PatientBirthDate, image.PatientSex = "19700101", "O"
            image.ReferringPhysicianName = "SYNTHETIC^REFERRER"
            for key, value in tags.items():
                if value is None:
                    if key in image:
                        delattr(image, key)
                else:
                    setattr(image, key, value)
            if edit:
                edit(index, image)
            path = root / f"source-{index:03d}.dcm"
            image.save_as(path, enforce_file_format=True)
            paths.append(path)
    return paths


def written(tmp_path, pixels):
    path = tmp_path / "written.png"
    path.write_bytes(png_bytes(pixels))
    return path


def export(tmp_path, pixels=None, *, sources=None, **tags):
    root = tmp_path / "run"
    root.mkdir(exist_ok=True)
    capture = root / "capture.png"
    capture.write_bytes(png_bytes(noise() if pixels is None else pixels))
    paths = sources or write_sources(root / "sources", **tags)
    result = export_view(paths, capture, root / "export", revision_id=REVISION)
    return result, pydicom.dcmread(result.path), pydicom.dcmread(paths[0], stop_before_pixels=True)


@pytest.mark.parametrize("channels", [3, 4])
def test_opaque_captures_keep_exact_pixels(tmp_path, channels):
    pixels = noise(channels=channels)
    if channels == 4:
        pixels[..., 3] = 255
    decoded = read_view_png(written(tmp_path, pixels))
    assert decoded.dtype == np.uint8 and decoded.shape == (24, 32, 3)
    np.testing.assert_array_equal(decoded, pixels[..., :3])
    result, ds, _ = export(tmp_path, pixels)
    np.testing.assert_array_equal(ds.pixel_array, pixels[..., :3])
    assert (result.rows, result.columns) == (24, 32)


def test_transparency_blends_over_black_with_integer_rounding(tmp_path):
    pixels = noise(8, 8, 4)
    pixels[..., 3] = np.arange(64, dtype=np.uint8).reshape(8, 8) * 4
    pixels[0, 0] = (200, 100, 50, 0)
    pixels[0, 1] = (255, 255, 255, 255)
    pixels[0, 2] = (255, 127, 1, 128)
    decoded = read_view_png(written(tmp_path, pixels))
    expected = (pixels[..., :3].astype(np.uint32) * pixels[..., 3:4].astype(np.uint32) + 127) // 255
    np.testing.assert_array_equal(decoded, expected)
    assert decoded[0, 0].tolist() == [0, 0, 0] and decoded[0, 1].tolist() == [255, 255, 255]
    assert decoded[0, 2].tolist() == [128, 64, 1]


@pytest.mark.parametrize("rows,columns", [(1, 2), (3, 5), (5, 16384)])
def test_any_size_round_trips_with_even_length_pixel_data(tmp_path, rows, columns):
    pixels = noise(rows, columns)
    result, ds, _ = export(tmp_path, pixels)
    expected = rows * columns * 3
    assert len(ds.PixelData) == expected + expected % 2
    if expected % 2:
        assert ds.PixelData[-1] == 0
    assert ds["PixelData"].VR == "OB"
    np.testing.assert_array_equal(ds.pixel_array.reshape(rows, columns, 3), pixels)
    assert result.size == result.path.stat().st_size and result.size % 2 == 0


def test_every_designed_attribute(tmp_path):
    result, ds, source = export(tmp_path, StudyDescription="Synthetic view study")
    assert ds.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
    assert ds.SOPClassUID == ds.file_meta.MediaStorageSOPClassUID == SecondaryCaptureImageStorage
    assert ds.SOPInstanceUID == ds.file_meta.MediaStorageSOPInstanceUID == result.sop_instance_uid
    assert ds.StudyInstanceUID == source.StudyInstanceUID == result.study_instance_uid
    assert ds.SeriesInstanceUID == result.series_instance_uid != source.SeriesInstanceUID
    assert ds.Modality == "OT" and ds.ConversionType == "WSD"
    assert list(ds.ImageType) == ["DERIVED", "SECONDARY", "OTHER"]
    assert ds.SeriesNumber == 501 and ds.InstanceNumber == 1
    assert ds.SeriesDescription == "3DScan view abcdef01"
    assert ds.SpecificCharacterSet == "ISO_IR 192"
    assert ds.Manufacturer == "3DScan" and ds.SeriesDate and ds.SeriesTime
    assert (ds.PhotometricInterpretation, ds.SamplesPerPixel, ds.PlanarConfiguration) == ("RGB", 3, 0)
    assert (ds.BitsAllocated, ds.BitsStored, ds.HighBit, ds.PixelRepresentation) == (8, 8, 7, 0)
    assert ds.BurnedInAnnotation == "NO"
    assert ds["PatientOrientation"].VM == 0 and ds["PatientOrientation"].VR == "CS"
    for keyword in (
        "RecognizableVisualFeatures",
        "PixelSpacing",
        "ImagerPixelSpacing",
        "ImagePositionPatient",
        "ImageOrientationPatient",
        "FrameOfReferenceUID",
        "SourceImageSequence",
        "ReferencedSeriesSequence",
        "ReferencedInstanceSequence",
        "SliceThickness",
        "Laterality",
        "IssuerOfPatientID",
    ):
        assert keyword not in ds, keyword
    comments = ds.ImageComments.lower()
    assert "3dscan" in comments and "do not measure" in comments and "orientation" in comments and "research use only" in comments
    assert len(ds.ImageComments) <= 160
    assert REVISION in ds.DerivationDescription and str(source.SeriesInstanceUID) in ds.DerivationDescription
    assert len(ds.DerivationDescription) <= 1024
    assert "SYNTHETIC" not in ds.ImageComments + ds.DerivationDescription


def test_identity_elements_are_exact_copies_of_the_source_even_when_odd(tmp_path):
    tags = dict(PatientSex="U", StudyTime="101010.1234567", PatientBirthDate="19700000", StudyDate="2026-11-01", Laterality="L")
    _, ds, source = export(tmp_path, StudyDescription="Synthetic view study", IssuerOfPatientID="SYNTHETIC-ISSUER", **tags)
    for keyword in dicom_view._COPIED:
        assert ds[keyword].value == source[keyword].value, keyword
        assert ds[keyword].VR == source[keyword].VR, keyword
    assert {keyword: str(ds[keyword].value) for keyword in tags} == tags


def test_missing_type_two_elements_stay_empty_and_are_not_invented(tmp_path):
    _, ds, _ = export(tmp_path, StudyDate=None, StudyTime=None, AccessionNumber=None, StudyID=None, PatientSex=None, PatientBirthDate=None)
    for keyword in ("StudyDate", "StudyTime", "AccessionNumber", "StudyID", "PatientSex", "PatientBirthDate"):
        assert keyword in ds and not ds[keyword].value, keyword


CHARSETS = [
    ("ISO_IR 100", "Müller^José", "MÜ12", "Thorax ärztlich"),
    ("GB18030", "王^小东", "编号-1", "胸部平扫"),
    (["", "ISO 2022 IR 87"], "Yamada^Tarou=山田^太郎=やまだ^たろう", "ID-山田", "胸部単純"),
]


@pytest.mark.parametrize("charset,name,patient_id,description", CHARSETS)
def test_every_source_character_set_is_rewritten_as_utf8(tmp_path, charset, name, patient_id, description):
    _, ds, source = export(tmp_path, SpecificCharacterSet=charset, PatientName=name, PatientID=patient_id, StudyDescription=description)
    assert str(source.PatientName) == name and source.PatientID == patient_id
    assert ds.SpecificCharacterSet == "ISO_IR 192"
    assert str(ds.PatientName) == name and ds.PatientID == patient_id and ds.StudyDescription == description
    raw = next((tmp_path / "run" / "export").glob("*.dcm")).read_bytes()
    header = raw[: raw.index(b"\xe0\x7f\x10\x00OB")]
    assert name.encode() in header and patient_id.encode() in header and description.encode() in header
    assert b"\x1b" not in header


@pytest.mark.parametrize("declared", [b"ISO_IR 6  ", b"          "])
def test_ascii_declaring_sources_with_legacy_bytes_keep_pydicoms_reading(tmp_path, declared):
    paths = write_sources(tmp_path / "sources", SpecificCharacterSet="ISO_IR 100", PatientName="Müller^José", PatientID="MÜ12")
    for path in paths:
        path.write_bytes(path.read_bytes().replace(b"ISO_IR 100", declared))
    _, ds, source = export(tmp_path, sources=paths)
    assert ds.SpecificCharacterSet == "ISO_IR 192"
    assert str(ds.PatientName) == str(source.PatientName) and ds.PatientID == source.PatientID


@pytest.mark.parametrize(
    "values,expected",
    [
        ((None, None, None), "NO"),
        (("NO", "NO", "YES"), "YES"),
        (("yes", None, None), "YES"),
    ],
)
def test_burned_in_annotation_follows_every_source_header(tmp_path, values, expected):
    def edit(index, image):
        if values[index] is not None:
            image.BurnedInAnnotation = values[index]

    _, ds, _ = export(tmp_path, sources=write_sources(tmp_path / "sources", edit=edit))
    assert ds.BurnedInAnnotation == expected


def test_contributing_equipment_keeps_source_device_without_patient_identity(tmp_path):
    _, ds, _ = export(tmp_path, Manufacturer="SyntheticVendor", ManufacturerModelName="Model", DeviceSerialNumber="1", SoftwareVersions="1")
    items = ds.ContributingEquipmentSequence
    assert items[0].Manufacturer == "SyntheticVendor"
    assert items[0].PurposeOfReferenceCodeSequence[0].CodeValue == "109101"
    assert all("PatientName" not in item and "PatientID" not in item for item in items)


@pytest.mark.parametrize(
    "keyword,value",
    [
        ("PatientID", "OTHER"),
        ("PatientBirthDate", "19800101"),
        ("StudyInstanceUID", "1.2.3"),
        ("SeriesInstanceUID", "1.2.3"),
    ],
)
def test_mixed_sources_are_refused_whichever_file_differs(tmp_path, keyword, value):
    def edit(index, image):
        if index == 2:
            setattr(image, keyword, value)

    paths = write_sources(tmp_path / "sources", edit=edit)
    capture = written(tmp_path, noise())
    with pytest.raises(DicomViewError, match="one patient, study and series"):
        export_view(paths, capture, tmp_path / "export", revision_id=REVISION)
    assert not (tmp_path / "export").exists()


@pytest.mark.parametrize("keyword,value", [("StudyInstanceUID", ""), ("SeriesInstanceUID", "not.a.uid"), ("SOPInstanceUID", "1." + "2" * 80)])
def test_invalid_source_uids_are_refused(tmp_path, keyword, value):
    def edit(index, image):
        setattr(image, keyword, value)
        if keyword == "SOPInstanceUID":
            image.file_meta.MediaStorageSOPInstanceUID = "1.2.3"

    paths = write_sources(tmp_path / "sources", 1, edit=edit)
    with pytest.raises(DicomViewError, match="invalid DICOM identity"):
        export_view(paths, written(tmp_path, noise()), tmp_path / "export", revision_id=REVISION)


def test_file_and_dataset_identity_must_agree(tmp_path):
    paths = write_sources(tmp_path / "sources", 1)
    uid = str(pydicom.dcmread(paths[0], stop_before_pixels=True).SOPInstanceUID)
    # The writer would repair this, so change the file meta's copy, which comes first.
    paths[0].write_bytes(paths[0].read_bytes().replace(uid.encode(), (uid[:-1] + ("1" if uid[-1] != "1" else "2")).encode(), 1))
    with pytest.raises(DicomViewError, match="identities disagree"):
        export_view(paths, written(tmp_path, noise()), tmp_path / "export", revision_id=REVISION)


def test_unusable_source_lists_are_refused(tmp_path):
    capture = written(tmp_path, noise())
    paths = write_sources(tmp_path / "sources", 2)
    junk = tmp_path / "junk.dcm"
    junk.write_bytes(b"not dicom")
    for sources, match in (
        ([], "complete original"),
        ([paths[0], paths[0]], "complete original"),
        ([junk], "header cannot be read"),
        ([tmp_path / "missing.dcm"], "unavailable"),
    ):
        with pytest.raises(DicomViewError, match=match):
            export_view(sources, capture, tmp_path / "export", revision_id=REVISION)


def test_export_requires_a_revision_and_a_real_capture(tmp_path):
    paths = write_sources(tmp_path / "sources", 1)
    capture = written(tmp_path, noise())
    with pytest.raises(DicomViewError, match="saved revision"):
        export_view(paths, capture, tmp_path / "export", revision_id="")
    with pytest.raises(DicomViewError, match="unavailable"):
        export_view(paths, tmp_path / "missing.png", tmp_path / "export", revision_id=REVISION)
    capture.write_bytes(b"not a png")
    with pytest.raises(DicomViewError, match="not a complete PNG"):
        export_view(paths, capture, tmp_path / "export", revision_id=REVISION)
    assert not (tmp_path / "export").exists()


def test_output_is_private_and_a_directory_is_never_reused(tmp_path):
    result, _, _ = export(tmp_path)
    output = result.path.parent
    assert stat.S_IMODE(result.path.stat().st_mode) == 0o600 and stat.S_IMODE(output.stat().st_mode) == 0o700
    before = result.path.read_bytes()
    with pytest.raises(DicomViewError, match="new DICOM view export directory"):
        export_view(sorted((tmp_path / "run" / "sources").glob("*.dcm")), tmp_path / "run" / "capture.png", output, revision_id=REVISION)
    assert result.path.read_bytes() == before
    assert [path.name for path in output.iterdir()] == ["view-0001.dcm"]


def test_a_failed_write_or_cancellation_publishes_nothing_and_leaves_no_staging(tmp_path, monkeypatch):
    def fail(self, path, **kwargs):
        Path(path).write_bytes(b"partial")
        raise OSError("private/patient/path")

    monkeypatch.setattr(highdicom.sc.SCImage, "save_as", fail)
    root = tmp_path / "exports"
    sources, capture = write_sources(tmp_path / "sources", 1), written(tmp_path, noise())
    with pytest.raises(DicomViewError) as caught:
        export_view(sources, capture, root / "export", revision_id=REVISION)
    assert "private" not in str(caught.value)
    assert not list(root.iterdir())
    with pytest.raises(InterruptedError):
        export_view(sources, capture, root / "export", revision_id=REVISION, cancelled=lambda: True)
    assert not list(root.iterdir())


@pytest.mark.parametrize("target", ["capture", "source"])
def test_an_input_changed_during_export_is_refused(tmp_path, target):
    sources = write_sources(tmp_path / "sources", 2)
    capture = written(tmp_path, noise())
    path = capture if target == "capture" else sources[-1]
    calls = iter(range(1, 1000))

    def touch():
        # Valid bytes, so only the stamp comparison can notice.
        stamp = path.stat()
        os.utime(path, ns=(stamp.st_atime_ns, stamp.st_mtime_ns + next(calls)))
        return False

    with pytest.raises(DicomViewError, match="changed during"):
        export_view(sources, capture, tmp_path / "exports" / "export", revision_id=REVISION, cancelled=touch)
    assert not list((tmp_path / "exports").iterdir())


def test_an_unqualified_encoder_version_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(highdicom, "__version__", "99.0.0")
    with pytest.raises(DicomViewError, match="qualification"):
        export_view(write_sources(tmp_path / "sources", 1), written(tmp_path, noise()), tmp_path / "export", revision_id=REVISION)
