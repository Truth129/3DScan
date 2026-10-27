from io import BytesIO
from pathlib import Path
import json
import struct
import sys
import shutil
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import hashlib
from itertools import product
import zipfile

import nibabel as nib
import nrrd
import numpy as np
from PIL import Image
from scipy import ndimage as ndi
from scipy.spatial import cKDTree
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, JPEG2000Lossless, generate_uid
from pydicom.encaps import encapsulate
import pytest

from scan3d import dicom, imaging, review_volume, segmentation, slicer_io, surfaces


def save(path, values, affine, units="mm"):
    image = nib.Nifti1Image(np.asarray(values), affine)
    image.header.set_xyzt_units(units)
    nib.save(image, str(path))
    return path


@pytest.fixture
def fixture(tmp_path):
    angle = np.deg2rad(31)
    rotate = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    affine = np.eye(4)
    affine[:3, :3] = rotate @ np.diag([-0.8, 1.4, 2.2])
    affine[:3, 3] = [31, -52, 87]
    shape = (18, 20, 16)
    ii, jj, kk = np.indices(shape)
    ct = (ii * 10 + jj * 2 + kk * 3 - 900).astype(np.int16)
    mask = np.zeros(shape, np.uint8)
    mask[2:8, 3:10, 4:12] = 1
    mask[13:16, 14:17, 2:5] = 1
    ct_path = save(tmp_path / "ct.nii.gz", ct, affine)
    mask_path = save(tmp_path / "mask.nii.gz", mask, affine)
    return ct_path, mask_path, nib.load(ct_path).affine, ct, mask


def entry(mask_path, identifier="total:liver"):
    return {"id": identifier, "name": "Liver", "task": "total", "group": "Organs", "color": "#bb6655", "mask_file": str(mask_path)}


def test_oblique_reflected_geometry_and_volume(fixture):
    ct_path, mask_path, affine, ct, mask = fixture
    geom = imaging.geometry(ct_path)
    assert geom["shape"] == list(ct.shape)
    np.testing.assert_allclose(geom["spacing"], [0.8, 1.4, 2.2], atol=1e-6)
    expected_center = affine @ np.r_[(np.array(ct.shape) - 1) / 2, 1]
    np.testing.assert_allclose(geom["center_ras"], expected_center[:3])
    measured = imaging.analyze_mask(ct_path, mask_path)
    assert measured["voxel_count"] == int(mask.sum())
    assert measured["volume_ml"] == pytest.approx(mask.sum() * 0.8 * 1.4 * 2.2 / 1000, rel=1e-6)
    expected_centroid = affine @ np.r_[np.array(np.nonzero(mask)).mean(axis=1), 1]
    np.testing.assert_allclose(measured["centroid_ras"], expected_centroid[:3])
    assert "2 disconnected" in measured["qc"][0]


def test_meter_units_and_saved_mask_forms(tmp_path):
    affine = np.diag([0.001, -0.002, 0.003, 1.0])
    mask = np.ones((5, 5, 5), dtype=np.uint8)
    mask[0] = 0
    ct_path = save(tmp_path / "ct.nii.gz", mask.astype(np.int16), affine, "meter")
    mask_path = save(tmp_path / "mask.nii.gz", mask, affine, "meter")
    np.testing.assert_allclose(imaging.geometry(ct_path)["spacing"], [1, 2, 3], rtol=1e-6)
    output = tmp_path / "refined.nii.gz"
    result = imaging.refine_mask(ct_path, mask_path, output, {"type": "erase_region", "bounds_ras": [[0, -9, -1], [1.1, 1, 20]]})
    assert result["changed_voxels"] == 25
    corrected = nib.load(output)
    assert corrected.header.get_xyzt_units()[0] == "meter"
    np.testing.assert_allclose(corrected.affine, affine)
    np.testing.assert_allclose(corrected.get_sform(), nib.load(ct_path).get_sform())


def test_components_and_non_destructive_refinement(fixture, tmp_path):
    ct_path, mask_path, affine, _, original = fixture
    seed = (affine @ [14, 15, 3, 1])[:3]
    result = imaging.refine_mask(ct_path, mask_path, tmp_path / "keep.nii.gz", {"type": "keep_component", "seed_ras": seed.tolist()})
    assert result["changed_voxels"] == 6 * 7 * 8
    assert nib.load(tmp_path / "keep.nii.gz").get_fdata().sum() == 27
    np.testing.assert_array_equal(nib.load(mask_path).get_fdata(), original)
    with pytest.raises(ValueError, match="does not change"):
        imaging.refine_mask(ct_path, tmp_path / "keep.nii.gz", tmp_path / "again.nii.gz", {"type": "keep_component", "seed_ras": seed.tolist()})
    with pytest.raises(ValueError, match="entire"):
        imaging.refine_mask(ct_path, tmp_path / "keep.nii.gz", tmp_path / "empty.nii.gz", {"type": "remove_component", "seed_ras": seed.tolist()})
    with pytest.raises(ValueError, match="new mask"):
        imaging.refine_mask(ct_path, mask_path, mask_path, {"type": "keep_component", "seed_ras": seed.tolist()})


def test_region_operates_on_world_voxel_centers(fixture, tmp_path):
    ct_path, mask_path, affine, _, mask = fixture
    center = (affine @ [4, 6, 7, 1])[:3]
    bounds = np.stack((center - [2, 3, 5], center + [2, 3, 5]))
    indices = np.array(np.indices(mask.shape)).reshape(3, -1).T
    physical = indices @ affine[:3, :3].T + affine[:3, 3]
    region = ((physical >= bounds[0]) & (physical <= bounds[1])).all(axis=1).reshape(mask.shape)
    path = tmp_path / "region.nii.gz"
    result = imaging.refine_mask(ct_path, mask_path, path, {"type": "keep_region", "bounds_ras": bounds.tolist()})
    np.testing.assert_array_equal(nib.load(path).get_fdata(), mask.astype(bool) & region)
    assert result["changed_voxels"] == int((mask.astype(bool) & ~region).sum())


@pytest.mark.parametrize("plane,fixed", [("axial", 2), ("coronal", 1), ("sagittal", 0)])
def test_anatomical_slice_transform_and_probe(fixture, plane, fixed):
    ct_path, mask_path, affine, ct, _ = fixture
    center = np.array(imaging.geometry(ct_path)["center_ras"])
    frame = review_volume.review_frame(ct_path, mask_path, plane, center[fixed])["frame"]
    meta = frame["meta"]
    assert frame["pixels"] == meta["width"] * meta["height"]
    transform = np.array(meta["pixel_to_ras"])
    assert transform[fixed, 3] == pytest.approx(center[fixed])
    assert transform[fixed, 0] == transform[fixed, 1] == 0
    point = (affine @ [5, 6, 7, 1])[:3]
    assert imaging.probe(ct_path, point)["hu"] == ct[5, 6, 7]
    assert imaging.probe(ct_path, point)["voxel"] == [5, 6, 7]


@pytest.mark.parametrize("fixed", [None, 0, 1, 2])
def test_fast_reslice_matches_full_patient_coordinate_sampling(fixed):
    """Both the native-slab and fully oblique paths preserve the sampling rule."""
    rng = np.random.default_rng(92)
    values = rng.uniform(-1024, 2500, size=(13, 14, 15)).astype(np.float32)
    mask = rng.integers(0, 2, size=values.shape).astype(bool)
    mapping = np.eye(4)
    mapping[:3, [1, 0]] = [[0.73, -0.12], [0.07, 0.61], [-0.11, 0.19]]
    mapping[:3, 3] = [1.21, 1.37, 2.14]
    positions = [2.14]
    if fixed is not None:
        mapping[fixed, :2] = 0
        # Exercise fractional interpolation, exact voxel centres, and the
        # half-voxel border where mode='constant' must remain outside the CT.
        positions = [-0.1, 0, 2.37, 4.5, values.shape[fixed] - 1, values.shape[fixed] - 0.9]
    rows, columns = np.indices((11, 12), dtype=np.float64)
    for position in positions:
        if fixed is not None:
            mapping[fixed, 3] = position
        coordinates = mapping[:3, 0, None, None] * columns + mapping[:3, 1, None, None] * rows + mapping[:3, 3, None, None]
        expected = ndi.map_coordinates(values, coordinates, order=1, mode="constant", cval=-1024, prefilter=False)
        actual = imaging._reslice(values, mapping, rows.shape, order=1, cval=-1024)
        np.testing.assert_allclose(actual, expected, rtol=0, atol=0.0005)
        expected_mask = ndi.map_coordinates(mask, coordinates, order=0, mode="constant", cval=0, prefilter=False)
        actual_mask = imaging._reslice(mask, mapping, rows.shape, order=0, cval=0)
        # affine_transform evaluates the affine in C; the reference grid is
        # evaluated by NumPy first. Fused arithmetic can put an exact physical
        # half-voxel on opposite sides of the tie (SciPy 1.16 builds do this).
        # Require exact agreement everywhere else. At a few-ULP tie, accept
        # only the value of an equally nearest physical voxel.
        for row, column in np.argwhere(actual_mask != expected_mask):
            point = coordinates[:, row, column]
            assert np.all((point >= 0) & (point <= np.asarray(mask.shape) - 1)), point
            lower = np.floor(point).astype(int)
            tolerance = 4 * np.spacing(np.maximum(1, np.abs(point)))
            ties = np.abs(point - (lower + 0.5)) <= tolerance
            assert np.any(ties), {"pixel": [row, column], "voxel": point}
            neighbours = [(int(index), int(index + 1)) if tie else (int(np.floor(value + 0.5)),) for index, value, tie in zip(lower, point, ties)]
            allowed = {bool(mask[index]) for index in product(*neighbours)}
            assert bool(actual_mask[row, column]) in allowed


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_native_slab_half_voxel_selects_upper_slice(axis):
    """The explicitly fixed native axis retains its deterministic tie rule."""
    mask = np.zeros((8, 8, 8), dtype=bool)
    indices = [slice(None)] * 3
    indices[axis] = slice(5, None)
    mask[tuple(indices)] = True
    mapping = np.eye(4)
    mapping[:3, :2] = 0
    keep = [index for index in range(3) if index != axis]
    mapping[keep[0], 0] = 1
    mapping[keep[1], 1] = 1
    mapping[axis, 3] = 4.5
    np.testing.assert_array_equal(imaging._reslice(mask, mapping, (3, 4), order=0, cval=0), np.ones((3, 4), dtype=bool))


def test_misaligned_or_nonbinary_masks_rejected(fixture, tmp_path):
    ct, mask_path, affine, _, mask = fixture
    shifted = affine.copy()
    shifted[0, 3] += 1
    bad = save(tmp_path / "bad.nii.gz", mask, shifted)
    with pytest.raises(ValueError, match="geometry"):
        imaging.analyze_mask(ct, bad)
    bad = save(tmp_path / "bad_binary.nii.gz", mask * 2, affine)
    with pytest.raises(ValueError, match="binary"):
        imaging.analyze_mask(ct, bad)


def test_display_mask_cache_reuse_is_immutable_and_validates_each_reference_grid(fixture, monkeypatch):
    ct_path, mask_path, affine, _, _ = fixture
    ct, _ = imaging._image(ct_path)
    imaging._SLICE_MASK_CACHE.clear()
    original_binary = imaging._binary
    calls = []

    def load(*args):
        calls.append(args)
        return original_binary(*args)

    monkeypatch.setattr(imaging, "_binary", load)
    with ThreadPoolExecutor(max_workers=4) as executor:
        masks = list(executor.map(lambda _: imaging._slice_mask(mask_path, ct, affine), range(4)))
    assert len(calls) == 1
    assert all(mask is masks[0] for mask in masks)
    assert masks[0].flags.writeable is False
    with pytest.raises(ValueError, match="read-only"):
        masks[0][0, 0, 0] = True
    shifted = affine.copy()
    shifted[0, 3] += 2
    with pytest.raises(ValueError, match="geometry"):
        imaging._slice_mask(mask_path, ct, shifted)
    assert len(calls) == 2


def test_display_mask_cache_invalidates_replaced_file_and_respects_byte_bound(fixture, tmp_path, monkeypatch):
    ct_path, mask_path, affine, _, mask = fixture
    ct, _ = imaging._image(ct_path)
    imaging._SLICE_MASK_CACHE.clear()
    first = imaging._slice_mask(mask_path, ct, affine)
    replacement = np.zeros_like(mask)
    replacement[3:6, 4:8, 5:9] = 1
    save(tmp_path / "replacement.nii.gz", replacement, affine).replace(mask_path)
    second = imaging._slice_mask(mask_path, ct, affine)
    assert second is not first
    np.testing.assert_array_equal(second, replacement)
    assert len(imaging._SLICE_MASK_CACHE) == 1
    monkeypatch.setattr(imaging, "_SLICE_MASK_CACHE_BYTES", second.nbytes + 1)
    other = save(tmp_path / "other.nii.gz", mask, affine)
    imaging._slice_mask(other, ct, affine)
    assert len(imaging._SLICE_MASK_CACHE) == 1
    assert sum(value.nbytes for value in imaging._SLICE_MASK_CACHE.values()) <= imaging._SLICE_MASK_CACHE_BYTES
    monkeypatch.setattr(imaging, "_SLICE_MASK_CACHE_BYTES", 1)
    imaging._SLICE_MASK_CACHE.clear()
    imaging._slice_mask(other, ct, affine)
    assert not imaging._SLICE_MASK_CACHE


def read_glb(path):
    data = path.read_bytes()
    magic, version, total = struct.unpack_from("<III", data)
    assert magic == 0x46546C67 and version == 2 and total == len(data)
    json_length, kind = struct.unpack_from("<II", data, 12)
    assert kind == 0x4E4F534A
    doc = json.loads(data[20 : 20 + json_length])
    bin_length, bin_kind = struct.unpack_from("<II", data, 20 + json_length)
    assert bin_kind == 0x004E4942
    binary = data[28 + json_length :]
    assert bin_length == len(binary)
    return doc, binary


def test_glb_real_geometry_scale_identity_and_winding(fixture, tmp_path):
    ct_path, mask_path, _, _, _ = fixture
    path = tmp_path / "model.glb"
    report = surfaces.build_model([entry(mask_path)], path, detailed=True)
    doc, binary = read_glb(path)
    assert doc["nodes"][0]["extras"]["structure_id"] == "total:liver"
    assert doc["nodes"][0]["name"] == "total:liver"
    primitive = doc["meshes"][0]["primitives"][0]

    def array(index, dtype, columns):
        acc = doc["accessors"][index]
        view = doc["bufferViews"][acc["bufferView"]]
        return np.frombuffer(binary, dtype=dtype, count=acc["count"] * columns, offset=view.get("byteOffset", 0)).reshape(-1, columns)

    positions = array(primitive["attributes"]["POSITION"], "<f4", 3)
    faces = array(primitive["indices"], "<u4", 1).reshape(-1, 3)
    assert len(faces) == report["triangles"]
    assert report["structures"] == 1
    ras = positions[:, [0, 2, 1]] * np.array([-1000, 1000, 1000])
    bounds = np.array(imaging.analyze_mask(ct_path, mask_path)["bounds_ras"])
    # Cell-based bounds can slightly exceed marching-contour diagonal extrema.
    assert np.all(ras.min(axis=0) >= bounds[0] - 1e-3)
    assert np.all(ras.max(axis=0) <= bounds[1] + 1e-3)
    assert positions.max() < 1  # metres, not millimetres
    centered = positions.astype(float) - positions.mean(axis=0)
    a, b, c = (centered[faces[:, i]] for i in range(3))
    assert np.einsum("ij,ij->i", a, np.cross(b, c)).sum() / 6 > 0
    original = path.read_bytes()

    def cancelled(_):
        raise InterruptedError("Cancelled before mesh extraction")

    with pytest.raises(InterruptedError):
        surfaces.build_model([entry(mask_path)], path, detailed=True, progress=cancelled)
    assert path.read_bytes() == original


def test_display_simplification_preserves_thin_branch_tips_and_closed_topology(tmp_path, monkeypatch):
    shape = (64, 64, 64)
    skeleton = np.zeros(shape, bool)
    center = np.array([32, 32, 30])
    tips = [np.array([32, 32, 5]), np.array([8, 32, 55]), np.array([56, 32, 55]), np.array([32, 8, 40])]
    for tip in tips:
        points = np.rint(np.linspace(center, tip, 100)).astype(int)
        skeleton[tuple(points.T)] = True
    mask = ndi.distance_transform_edt(~skeleton, sampling=[0.5, 0.5, 0.5]) <= 0.8
    path = save(tmp_path / "branches.nii.gz", mask.astype(np.uint8), np.diag([0.5, 0.5, 0.5, 1]))
    monkeypatch.setattr(surfaces, "_DISPLAY_TREE_TRIANGLES", 200)
    positions, _, faces = surfaces._mesh(path, detailed=False, delicate=True)
    _, _, detailed_faces = surfaces._mesh(path, detailed=True, delicate=True)
    assert len(detailed_faces) > len(faces) > 200  # physical error/topology take precedence over target
    # Every terminal branch still reaches its original anatomical tip.
    expected_tips = np.array(tips) * 0.5
    scene_tips = expected_tips[:, [0, 2, 1]] * np.array([-0.001, 0.001, 0.001])
    distances, _ = cKDTree(positions).query(scene_tips)
    assert distances.max() * 1000 < 1.1
    edges = np.sort(np.concatenate((faces[:, [0, 1]], faces[:, [1, 2]], faces[:, [2, 0]])), axis=1)
    unique_edges, counts = np.unique(edges, axis=0, return_counts=True)
    assert np.all(counts == 2)  # closed, manifold surface
    assert len(np.unique(faces)) - len(unique_edges) + len(faces) == 2  # one genus-zero tree surface


def test_slicer_overlap_roundtrip(fixture, tmp_path):
    ct_path, first_path, affine, _, mask = fixture
    second_mask = np.zeros(mask.shape, np.uint8)
    second_mask[4:10, 5:12, 6:13] = 1
    second_path = save(tmp_path / "second.nii.gz", second_mask, affine)
    structures = [entry(first_path), entry(second_path, "lung_vessels:lung_arteries")]
    bundle = slicer_io.export_bundle(ct_path, structures, tmp_path / "slicer.zip")
    unpacked = tmp_path / "unpacked"
    with zipfile.ZipFile(bundle) as archive:
        assert sorted(archive.namelist()) == ["mapping.json", "reference.nii.gz", "segmentation.seg.nrrd"]
        archive.extractall(unpacked)
    data, header = nrrd.read(str(unpacked / "segmentation.seg.nrrd"))
    assert data.shape[0] == 2
    assert header["Segment0_ID"] == structures[0]["id"]
    imported = slicer_io.import_segmentation(ct_path, unpacked / "segmentation.seg.nrrd", structures, tmp_path / "corrected", require_source_hash=True)
    for item, expected in zip(imported, [mask, second_mask]):
        np.testing.assert_array_equal(nib.load(item["mask_file"]).get_fdata(), expected)
    mapping = json.loads((unpacked / "mapping.json").read_text())
    assert "mask_file" not in mapping["structures"][0]


def test_slicer_cropped_labelmap_origin_not_double_offset(fixture, tmp_path):
    ct_path, mask_path, affine, _, mask = fixture
    low = np.array([2, 3, 4])
    high = np.array([8, 10, 12])
    cropped = mask[tuple(slice(a, b) for a, b in zip(low, high))]
    cropped_affine = affine.copy()
    cropped_affine[:3, 3] = (affine @ np.r_[low, 1])[:3]
    flip = np.diag([-1, -1, 1])
    header = {
        "space": "left-posterior-superior",
        "space directions": (flip @ cropped_affine[:3, :3]).T,
        "space origin": flip @ cropped_affine[:3, 3],
        "encoding": "gzip",
        "Segmentation_ReferenceImageExtentOffset": "2 3 4",
        "Segment0_ID": "total:liver",
        "Segment0_Name": "Liver",
        "Segment0_Layer": "0",
        "Segment0_LabelValue": "1",
    }
    seg_path = tmp_path / "crop.seg.nrrd"
    nrrd.write(str(seg_path), cropped, header)
    imported = slicer_io.import_segmentation(ct_path, seg_path, [entry(mask_path)], tmp_path / "out")
    actual = nib.load(imported[0]["mask_file"]).get_fdata()
    expected = np.zeros(mask.shape)
    expected[tuple(slice(a, b) for a, b in zip(low, high))] = cropped
    np.testing.assert_array_equal(actual, expected)
    header["Segment0_ID"] = "unknown:label"
    nrrd.write(str(seg_path), cropped, header)
    with pytest.raises(ValueError, match="unknown structure"):
        slicer_io.import_segmentation(ct_path, seg_path, [entry(mask_path)], tmp_path / "bad")


def write_dicom(path, series, frame, z):
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = CTImageStorage
    file_meta.MediaStorageSOPInstanceUID = generate_uid()
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset(str(path), {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
    ds.SeriesInstanceUID = series
    ds.FrameOfReferenceUID = frame
    ds.Modality = "CT"
    ds.SeriesDescription = "Synthetic CT"
    ds.Rows = 10
    ds.Columns = 12
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.ImagePositionPatient = [0, 0, z]
    ds.PixelSpacing = [0.8, 0.7]
    ds.SliceThickness = 1
    ds.save_as(str(path), enforce_file_format=True)


def test_dicom_index_sorts_physical_position_and_flags_gaps(tmp_path):
    series, frame = generate_uid(), generate_uid()
    for filename, z in [("c.dcm", 0), ("a.dcm", 2), ("b.dcm", 1)]:
        write_dicom(tmp_path / filename, series, frame, z)
    records = dicom.index_dicom(tmp_path)
    assert len(records) == 1
    assert records[0]["warnings"] == []
    assert [Path(p).name for p in records[0]["files"]] == ["c.dcm", "b.dcm", "a.dcm"]
    assert records[0]["spacing"] == [0.7, 0.8, 1]
    write_dicom(tmp_path / "gap.dcm", series, frame, 4)
    assert any("missing slices" in message for message in dicom.index_dicom(tmp_path)[0]["warnings"])


def test_dicom_duplicate_positions_are_not_silently_merged(tmp_path):
    series, frame = generate_uid(), generate_uid()
    write_dicom(tmp_path / "a.dcm", series, frame, 0)
    write_dicom(tmp_path / "b.dcm", series, frame, 0)
    record = dicom.index_dicom(tmp_path)[0]
    assert any("Repeated slice positions" in message for message in record["warnings"])
    with pytest.raises(ValueError, match="Repeated slice positions"):
        dicom.convert_series(record["files"], tmp_path / "converted")


@pytest.mark.parametrize("missing_tag", ["ImagePositionPatient", "ImageOrientationPatient", "PixelSpacing", "Rows", "Columns"])
def test_dicom_missing_required_geometry_is_reported_before_conversion(tmp_path, missing_tag):
    series, frame = generate_uid(), generate_uid()
    for index in range(2):
        path = tmp_path / f"{index}.dcm"
        write_dicom(path, series, frame, index)
    path = tmp_path / "1.dcm"
    incomplete = pydicom.dcmread(path)
    del incomplete[missing_tag]
    incomplete.save_as(path, enforce_file_format=True)

    record = dicom.index_dicom(tmp_path)[0]
    assert "Missing patient position, orientation or pixel geometry" in record["warnings"]
    with pytest.raises(ValueError, match="Missing patient position, orientation or pixel geometry"):
        dicom.convert_series(record["files"], tmp_path / "converted")


def test_dicom_missing_coordinate_frame_has_an_explicit_diagnostic(tmp_path):
    series, frame = generate_uid(), generate_uid()
    for index in range(2):
        path = tmp_path / f"{index}.dcm"
        write_dicom(path, series, frame, index)
        incomplete = pydicom.dcmread(path)
        del incomplete["FrameOfReferenceUID"]
        incomplete.save_as(path, enforce_file_format=True)

    record = dicom.index_dicom(tmp_path)[0]
    assert record["slice_count"] == 2  # Missing NumberOfFrames defaults to one.
    assert record["study_uid"] is None and record["frame_of_reference_uid"] is None
    assert "Series has missing or multiple patient coordinate frames" in record["warnings"]
    with pytest.raises(ValueError, match="missing or multiple patient coordinate frames"):
        dicom.convert_series(record["files"], tmp_path / "converted")


def conversion_sources(tmp_path, syntax=None):
    source = tmp_path / "dicom"
    source.mkdir()
    series, frame = generate_uid(), generate_uid()
    for index in range(2):
        path = source / f"{index}.dcm"
        write_dicom(path, series, frame, index)
        if syntax:
            ds = pydicom.dcmread(path)
            ds.file_meta.TransferSyntaxUID = syntax
            ds.save_as(path, enforce_file_format=True)
    return dicom.index_dicom(source)[0]["files"]


@pytest.mark.parametrize("syntax", ["1.2.840.10008.1.2.4.90", "1.2.840.10008.1.2.4.91"])
def test_dicom_unsupported_jpeg2000_explains_installed_decoder_and_keeps_private_log(tmp_path, monkeypatch, syntax):
    files = conversion_sources(tmp_path, syntax)
    output = tmp_path / "converted"
    output.mkdir()
    log = output / "dcm2niix.log"
    log.write_text("old log")
    log.chmod(0o644)
    patient_details = b"Patient: PRIVATE_NAME, source /private/patient/source.dcm"
    monkeypatch.setenv("SCAN3D_DCM2NIIX", "synthetic-dcm2niix")
    monkeypatch.setattr(
        dicom.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=2,
            stdout=b"Warning: Unsupported transfer syntax: " + syntax.encode() + b"\n" + patient_details,
            stderr=b"No valid DICOM images were found",
        ),
    )
    with pytest.raises(RuntimeError, match="JPEG 2000.*OpenJPEG") as error:
        dicom.convert_series(files, output)
    assert "verify the selected" not in str(error.value)
    assert "PRIVATE_NAME" not in str(error.value) and "/private/" not in str(error.value)
    assert "dcm2niix.log" in str(error.value)
    assert patient_details.decode() in log.read_text()
    assert "exit code: 2" in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600
    assert not (output / "reference.nii.gz").exists()
    assert not list(output.glob(".dicom-*"))


def test_dicom_unknown_unsupported_compression_never_exposes_converter_metadata(tmp_path, monkeypatch):
    files = conversion_sources(tmp_path)
    monkeypatch.setenv("SCAN3D_DCM2NIIX", "synthetic-dcm2niix")
    monkeypatch.setattr(
        dicom.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=2, stdout="Unsupported transfer syntax: PRIVATE_UNKNOWN_SYNTAX /private/patient", stderr=""),
    )
    with pytest.raises(RuntimeError, match="installed DICOM converter.*required decoder") as error:
        dicom.convert_series(files, tmp_path / "converted")
    assert "PRIVATE_UNKNOWN_SYNTAX" not in str(error.value) and "/private" not in str(error.value)


@pytest.mark.parametrize("returncode,output_count", [(0, 0), (0, 2), (2, 0), (8, 1)])
def test_dicom_missing_multiple_or_partially_failed_converter_outputs_are_never_accepted(tmp_path, monkeypatch, returncode, output_count):
    files = conversion_sources(tmp_path)
    output = tmp_path / "converted"
    monkeypatch.setenv("SCAN3D_DCM2NIIX", "synthetic-dcm2niix")

    def convert(command, **kwargs):
        target = Path(command[command.index("-o") + 1])
        for index in range(output_count):
            # A nonzero converter result must fail before even trying to load a
            # partial output, regardless of whether the file looks like NIfTI.
            (target / f"reference{index}.nii.gz").write_bytes(b"partial output")
        return SimpleNamespace(returncode=returncode, stdout=b"source /private/patient", stderr=b"conversion details")

    monkeypatch.setattr(dicom.subprocess, "run", convert)
    message = "exactly one" if not returncode else "no readable CT images" if returncode == 2 else "exit 8"
    with pytest.raises((ValueError, RuntimeError), match=message) as error:
        dicom.convert_series(files, output)
    assert "/private" not in str(error.value)
    assert "dcm2niix.log" in str(error.value)
    assert (output / "dcm2niix.log").exists()
    assert not (output / "reference.nii.gz").exists()


def test_dicom_timeout_keeps_partial_private_diagnostics(tmp_path, monkeypatch):
    files = conversion_sources(tmp_path)
    output = tmp_path / "converted"
    monkeypatch.setenv("SCAN3D_DCM2NIIX", "synthetic-dcm2niix")

    def timeout(command, **kwargs):
        raise dicom.subprocess.TimeoutExpired(command, 600, output=b"/private/patient", stderr=b"partial decoder output")

    monkeypatch.setattr(dicom.subprocess, "run", timeout)
    with pytest.raises(RuntimeError, match="timed out after 10 minutes.*dcm2niix.log") as error:
        dicom.convert_series(files, output)
    assert "/private" not in str(error.value)
    assert "partial decoder output" in (output / "dcm2niix.log").read_text()
    assert (output / "dcm2niix.log").stat().st_mode & 0o777 == 0o600


def compressed_conversion_sources(tmp_path):
    from PIL import features
    from pydicom.pixels.decoders import JPEG2000LosslessDecoder

    if not features.check_codec("jpg_2000") or "gdcm" not in JPEG2000LosslessDecoder.available_plugins:
        pytest.skip("Independent JPEG 2000 fixture encoding/GDCM decoding is unavailable")
    files = conversion_sources(tmp_path, str(JPEG2000Lossless))
    angle = np.deg2rad(23)
    for index, name in enumerate(files):
        ds = pydicom.dcmread(name)
        ds.Rows = ds.Columns = 64
        ds.InstanceNumber = index + 1
        ds.ImageOrientationPatient = [np.cos(angle), np.sin(angle), 0, -np.sin(angle), np.cos(angle), 0]
        ds.ImagePositionPatient = [10, 20, 30 + index * 1.5]
        ds.SliceThickness = 1.5
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleSlope = 2
        ds.RescaleIntercept = -1024
        ds.RescaleType = "HU"
        ds.LossyImageCompression = "00"
        values = (np.arange(64 * 64).reshape(64, 64) + index * 100).astype(np.uint16)
        encoded = BytesIO()
        Image.fromarray(values).save(encoded, format="JPEG2000", no_jp2=True, irreversible=False)
        ds.PixelData = encapsulate([encoded.getvalue()])
        ds["PixelData"].VR = "OB"
        ds["PixelData"].is_undefined_length = True
        ds.save_as(name, enforce_file_format=True)
    return files


@pytest.mark.skipif(not shutil.which("dcm2niix"), reason="Native dcm2niix is not installed")
def test_dicom_codec_fallback_preserves_original_bytes_hu_and_oblique_geometry(tmp_path, monkeypatch):
    files = compressed_conversion_sources(tmp_path)
    original_hashes = [hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in files]
    native_run = dicom.subprocess.run
    calls = []

    def convert(command, **kwargs):
        source = Path(command[-1])
        target = Path(command[command.index("-o") + 1])
        calls.append((source, target))
        if len(calls) == 1:
            assert all((source / f"{index:06d}.dcm").stat().st_ino == Path(name).stat().st_ino for index, name in enumerate(files))
            (target / "native-partial.nii.gz").write_bytes(b"must never be accepted")
            return SimpleNamespace(returncode=1, stdout=b"OpenJPEG failed to read the header /private/patient", stderr=b"")
        assert source != calls[0][0] and target != calls[0][1]
        assert not list(target.iterdir())
        for index, name in enumerate(files):
            normalized = source / f"{index:06d}.dcm"
            assert normalized.stat().st_ino != Path(name).stat().st_ino
            before, after = pydicom.dcmread(name), pydicom.dcmread(normalized)
            assert after.file_meta.TransferSyntaxUID == ExplicitVRLittleEndian
            for key in (
                "SOPInstanceUID",
                "FrameOfReferenceUID",
                "ImagePositionPatient",
                "ImageOrientationPatient",
                "PixelSpacing",
                "RescaleSlope",
                "RescaleIntercept",
                "LossyImageCompression",
            ):
                assert getattr(after, key) == getattr(before, key)
            before.pixel_array_options(decoding_plugin="gdcm")
            np.testing.assert_array_equal(after.pixel_array, before.pixel_array)
        return native_run(command, **kwargs)

    monkeypatch.setattr(dicom.subprocess, "run", convert)
    output = tmp_path / "converted"
    converted = dicom.convert_series(files, output)
    assert len(calls) == 2
    assert [hashlib.sha256(Path(name).read_bytes()).hexdigest() for name in files] == original_hashes
    result = nib.load(converted)
    values = result.get_fdata()
    for name in files:
        ds = pydicom.dcmread(name)
        ds.pixel_array_options(decoding_plugin="gdcm")
        expected = ds.pixel_array.astype(float) * float(ds.RescaleSlope) + float(ds.RescaleIntercept)
        rows, columns = np.indices(expected.shape)
        orientation = np.asarray(ds.ImageOrientationPatient, float)
        lps = (
            np.asarray(ds.ImagePositionPatient, float)[:, None, None]
            + orientation[:3, None, None] * columns * float(ds.PixelSpacing[1])
            + orientation[3:, None, None] * rows * float(ds.PixelSpacing[0])
        )
        locations = nib.affines.apply_affine(np.linalg.inv(result.affine), np.moveaxis(lps * np.array([-1, -1, 1])[:, None, None], 0, -1))
        np.testing.assert_allclose(locations, np.rint(locations), rtol=0, atol=1e-4)
        np.testing.assert_array_equal(values[tuple(np.moveaxis(np.rint(locations).astype(int), -1, 0))], expected)
    manifest = json.loads((output / "conversion.json").read_text())
    assert manifest["primary_exit_code"] == 1 and manifest["final_exit_code"] == 0
    assert manifest["normalizer"]["name"] == "pydicom-gdcm"
    assert manifest["normalizer"]["decoded_slice_count"] == 2
    assert manifest["normalizer"]["resampled"] is manifest["normalizer"]["recompressed"] is False
    assert "/private" not in json.dumps(manifest)
    assert "OpenJPEG failed" in (output / "dcm2niix.log").read_text()
    assert "Normalized retry" in (output / "dcm2niix.log").read_text()


@pytest.mark.parametrize("native_message,expected_calls", [(b"generic conversion error", 1), (b"Failed to decode frame", 2)])
@pytest.mark.parametrize("retry_exit,retry_outputs", [(0, 0), (1, 1)])
def test_dicom_fallback_is_targeted_and_failed_or_empty_retry_never_uses_primary_partial_output(
    tmp_path, monkeypatch, native_message, expected_calls, retry_exit, retry_outputs
):
    files = compressed_conversion_sources(tmp_path)
    calls = []
    monkeypatch.setenv("SCAN3D_DCM2NIIX", "synthetic-dcm2niix")

    def convert(command, **kwargs):
        target = Path(command[command.index("-o") + 1])
        calls.append(target)
        if len(calls) == 1:
            (target / "native-partial.nii.gz").write_bytes(b"must not be accepted")
            return SimpleNamespace(returncode=1, stdout=native_message, stderr=b"")
        assert target != calls[0] and not list(target.iterdir())
        if retry_outputs:
            (target / "retry-partial.nii.gz").write_bytes(b"must not be accepted")
        return SimpleNamespace(returncode=retry_exit, stdout=b"retry diagnostic", stderr=b"")

    monkeypatch.setattr(dicom.subprocess, "run", convert)
    output = tmp_path / "converted"
    with pytest.raises((ValueError, RuntimeError)):
        dicom.convert_series(files, output)
    assert len(calls) == expected_calls
    assert not (output / "reference.nii.gz").exists()
    assert not (output / "conversion.json").exists()
    assert not list(output.glob(".dicom-*"))


def test_gpu_selection_uses_discrete_r9700_and_never_silently_falls_back(monkeypatch):
    available = True
    names = ["AMD Radeon 610M", "AMD Radeon AI PRO R9700"]
    fake = SimpleNamespace(
        __version__="test",
        version=SimpleNamespace(hip="7.2.4", cuda=None),
        cuda=SimpleNamespace(is_available=lambda: available, device_count=lambda: len(names), get_device_name=lambda i: names[i]),
    )
    monkeypatch.setitem(sys.modules, "torch", fake)
    assert segmentation._device_info("gpu")["selected_device"] == "gpu:1"
    assert segmentation._device_info("gpu:0")["selected_device"] == "gpu:0"
    available = False
    with pytest.raises(RuntimeError, match="cannot access"):
        segmentation._device_info("gpu")
    assert segmentation._device_info("cpu")["explicit_cpu"] is True


@pytest.mark.parametrize("profile", ["abdomen", "chest", "chest_abdomen"])
def test_full_resolution_adapter_retains_all_nonempty_outputs_and_retries(fixture, tmp_path, monkeypatch, profile):
    ct_path, _, affine, ct, _ = fixture
    commands = []
    monkeypatch.setattr(segmentation, "_device_info", lambda _: {"selected_device": "gpu:1"})
    monkeypatch.setattr(segmentation, "model_fingerprint", lambda _: {"digest": "test", "weights_complete": True})
    monkeypatch.setattr(segmentation.metadata, "version", lambda _: "test-pinned")
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu")

    class Worker:
        pid = None  # Completed in-process fixture; no native worker to sample.

        def __init__(self, command, **_):
            commands.append(command)
            self.returncode = 0
            destination = Path(command[command.index("-o") + 1])
            task = command[command.index("--task") + 1]
            names = (
                ["liver", "spleen", "brain", "skull", "vertebrae_C1", "humerus_left", "femur_left", "gluteus_maximus_left", "atrial_appendage_left"]
                if task == "total"
                else sorted(segmentation._LUNG)
            )
            for name in names:
                mask = np.zeros(ct.shape, np.uint8)
                if name != "spleen":
                    mask[2:5, 3:6, 4:7] = 1
                save(destination / f"{name}.nii.gz", mask, affine)
            Path(command[command.index("--report") + 1]).write_text(
                json.dumps(
                    {
                        "device": "gpu",
                        "fast": False,
                        "fastest": False,
                        "save_lowres": False,
                        "classes": {str(i): name for i, name in enumerate(names)},
                        "nnunetv2_version": "test",
                        "runtime_seconds": 1,
                    }
                )
            )

        def poll(self):
            return 0

    monkeypatch.setattr(segmentation.subprocess, "Popen", Worker)
    destination = tmp_path / "runs"
    first = segmentation.segment(ct_path, destination, profile, lambda _: None, lambda: False)
    second = segmentation.segment(ct_path, destination, profile, lambda _: None, lambda: False)
    retained = {entry["id"]: entry for entry in first}
    assert set(retained) == {
        "total:liver",
        "total:brain",
        "total:skull",
        "total:vertebrae_C1",
        "total:humerus_left",
        "total:femur_left",
        "total:gluteus_maximus_left",
        "total:atrial_appendage_left",
    } | ({f"lung_vessels:{name}" for name in segmentation._LUNG} if profile != "abdomen" else set())
    assert retained["total:skull"]["group"] == "Bones"
    assert retained["total:vertebrae_C1"]["group"] == "Bones"
    assert retained["total:humerus_left"]["group"] == "Bones"
    assert retained["total:femur_left"]["group"] == "Bones"
    assert retained["total:brain"]["group"] == "Head organs"
    assert retained["total:gluteus_maximus_left"]["group"] == "Muscles"
    assert retained["total:atrial_appendage_left"]["group"] == "Chest organs"
    assert first[0]["mask_file"] != second[0]["mask_file"]
    assert [command[command.index("--task") + 1] for command in commands] == (["total", "total"] if profile == "abdomen" else ["total", "lung_vessels"] * 2)
    assert "--fast" not in commands[0]
    assert commands[0][commands[0].index("--device") + 1] == "gpu:1"
    manifest = json.loads((destination / "inference.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["output_selection"] == "all_nonempty_task_outputs"
    assert manifest["structure_count"] == len(retained)
    assert manifest["empty_labels"] == ["total:spleen"]
    assert manifest["qc"]


def test_checkpoint_fingerprint_changes_when_weight_bytes_change(tmp_path, monkeypatch):
    # The installed task registry is pure metadata; tiny synthetic checkpoint
    # files test our identity contract without loading/training a model.
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(tmp_path / "weights"))
    incomplete = segmentation.model_fingerprint("abdomen")
    assert incomplete["weights_complete"] is False
    for item in incomplete["models"]:
        folder = tmp_path / "weights" / f"Dataset{item['dataset_id']:03d}_fixture" / f"{item['trainer']}__{item['plans']}__{item['configuration']}"
        folder.mkdir(parents=True)
        (folder / "plans.json").write_text("{}")
        (folder / "dataset.json").write_text("{}")
        (folder / "fold_0").mkdir()
        (folder / "fold_0" / "checkpoint_final.pth").write_bytes(b"synthetic weights v1")
    first = segmentation.model_fingerprint("abdomen")
    assert first["weights_complete"] is True
    checkpoint = next((tmp_path / "weights").rglob("checkpoint_final.pth"))
    checkpoint.write_bytes(b"synthetic weights v2")
    second = segmentation.model_fingerprint("abdomen")
    assert second["digest"] != first["digest"]
    assert str(tmp_path) not in json.dumps(second)


def test_slicer_rejects_different_lattice(fixture, tmp_path):
    ct_path, mask_path, affine, _, mask = fixture
    header = {
        "space": "right-anterior-superior",
        "space directions": affine[:3, :3].T * 0.5,
        "space origin": affine[:3, 3],
        "Segment0_ID": "total:liver",
        "Segment0_Layer": "0",
        "Segment0_LabelValue": "1",
    }
    path = tmp_path / "wrong_grid.seg.nrrd"
    nrrd.write(str(path), mask, header)
    with pytest.raises(ValueError, match="different voxel grid"):
        slicer_io.import_segmentation(ct_path, path, [entry(mask_path)], tmp_path / "wrong")


def test_slicer_strict_case_hash_prevents_cross_case_and_missing_identity(fixture, tmp_path):
    ct_path, mask_path, affine, ct, mask = fixture
    header = {
        "space": "right-anterior-superior",
        "space directions": affine[:3, :3].T,
        "space origin": affine[:3, 3],
        "Segment0_ID": "total:liver",
        "Segment0_Layer": "0",
        "Segment0_LabelValue": "1",
    }
    path = tmp_path / "no_identity.seg.nrrd"
    nrrd.write(str(path), mask, header)
    with pytest.raises(ValueError, match="lacks the source CT identity"):
        slicer_io.import_segmentation(ct_path, path, [entry(mask_path)], tmp_path / "missing", require_source_hash=True)
    different = save(tmp_path / "different_ct.nii.gz", ct + 1, affine)
    header["Segment0_Tags"] = f"Scan3D.ReferenceSHA256:{slicer_io._reference_digest(different)}|"
    nrrd.write(str(path), mask, header)
    with pytest.raises(ValueError, match="different reference CT"):
        slicer_io.import_segmentation(ct_path, path, [entry(mask_path)], tmp_path / "different", require_source_hash=True)
    # A valid segment tag alone suffices when Slicer discards custom globals.
    header["Segment0_Tags"] = f"Scan3D.ReferenceSHA256:{slicer_io._reference_digest(ct_path)}|"
    nrrd.write(str(path), mask, header)
    corrected = slicer_io.import_segmentation(ct_path, path, [entry(mask_path)], tmp_path / "valid", require_source_hash=True)
    np.testing.assert_array_equal(nib.load(corrected[0]["mask_file"]).get_fdata(), mask)


def test_slicer_rejects_detached_local_file_references_before_reading(fixture, tmp_path):
    ct_path, mask_path, _, _, _ = fixture
    path = tmp_path / "detached.seg.nrrd"
    path.write_text("NRRD0005\ntype: uint8\ndimension: 3\nsizes: 2 2 2\nencoding: raw\ndata file: /does/not/exist\n\n")
    with pytest.raises(ValueError, match="Detached"):
        slicer_io.import_segmentation(ct_path, path, [entry(mask_path)], tmp_path / "detached-out")


@pytest.mark.skipif(not shutil.which("dcm2niix"), reason="Native dcm2niix is not installed")
def test_real_dcm2niix_preserves_patient_coordinates_and_rescaled_hu(tmp_path):
    source = tmp_path / "dicom"
    source.mkdir()
    series, frame, study = generate_uid(), generate_uid(), generate_uid()
    for index in range(5):
        path = source / f"{index}.dcm"
        write_dicom(path, series, frame, 10 + 1.5 * index)
        ds = pydicom.dcmread(path)
        ds.StudyInstanceUID = study
        ds.SeriesNumber = 1
        ds.InstanceNumber = index + 1
        ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.HighBit = 15
        ds.PixelRepresentation = 0
        ds.RescaleSlope = 2
        ds.RescaleIntercept = -1024
        ds.RescaleType = "HU"
        ds.SliceThickness = 1.5
        ds.AcquisitionNumber = 1
        ds.ConvolutionKernel = "STANDARD"
        ds.Exposure = [6, 5, 4, 6, 4][index]
        rows, columns = np.indices((int(ds.Rows), int(ds.Columns)))
        pixels = (rows * 10 + columns + index * 100).astype("<u2")
        ds.PixelData = pixels.tobytes()
        ds["PixelData"].VR = "OW"
        ds.save_as(path, enforce_file_format=True)
    record = dicom.index_dicom(source)[0]
    converted = dicom.convert_series(record["files"], tmp_path / "converted")
    assert (converted.parent / "dcm2niix.log").stat().st_mode & 0o777 == 0o600
    # DICOM source pixel column=3,row=4,slice=2 is LPS=(2.1,3.2,13).
    result = imaging.probe(converted, [-2.1, -3.2, 13])
    assert result["hu"] == (4 * 10 + 3 + 2 * 100) * 2 - 1024
    assert imaging.geometry(converted)["spacing"] == pytest.approx([0.7, 0.8, 1.5], rel=1e-6)


@pytest.mark.parametrize("attribute,value", [("AcquisitionNumber", 2), ("ConvolutionKernel", "LUNG"), ("TemporalPositionIdentifier", 2)])
def test_dicom_does_not_merge_distinct_acquisitions_or_kernels(tmp_path, attribute, value):
    series, frame = generate_uid(), generate_uid()
    write_dicom(tmp_path / "a.dcm", series, frame, 0)
    write_dicom(tmp_path / "b.dcm", series, frame, 1)
    item = pydicom.dcmread(tmp_path / "b.dcm")
    setattr(item, attribute, value)
    item.save_as(tmp_path / "b.dcm", enforce_file_format=True)
    record = dicom.index_dicom(tmp_path)[0]
    assert any(attribute in message for message in record["warnings"])
    with pytest.raises(ValueError, match=attribute):
        dicom.convert_series(record["files"], tmp_path / "output")
