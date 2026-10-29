"""Regression gates for private export publication and bounded mask lifetimes."""

from dataclasses import replace
from pathlib import Path
import weakref

import nibabel as nib
import numpy as np
import pytest

from scan3d import osirix_roi
from test_dicom_seg import fixture


_GENERIC_ERROR = "Original CT or saved masks cannot be encoded as native ROIs"


def _export(fixture, **changes):
    arguments = {
        "source_paths": fixture["paths"],
        "reference_path": fixture["reference"],
        "segments": fixture["segments"],
        "output_dir": fixture["output"],
        "revision_id": "synthetic-saved-revision",
    }
    arguments.update(changes)
    return osirix_roi.export_native_rois(**arguments)


def _assert_no_staging(fixture):
    assert not [path for path in fixture["output"].parent.iterdir() if path.name.startswith(".osirix-roi-")]


def test_exclusive_publish_preserves_raced_empty_directory(fixture, monkeypatch):
    publish = osirix_roi._publish_new
    raced = []

    def race(staging, output):
        output = Path(output)
        output.mkdir()
        raced.append((output.stat().st_dev, output.stat().st_ino))
        return publish(staging, output)

    monkeypatch.setattr(osirix_roi, "_publish_new", race)
    with pytest.raises(osirix_roi.NativeROIError):
        _export(fixture)
    output = fixture["output"]
    assert len(raced) == 1
    assert (output.stat().st_dev, output.stat().st_ino) == raced[0]
    assert list(output.iterdir()) == []
    _assert_no_staging(fixture)


def test_previous_dense_mask_is_released_before_loading_next_structure(fixture, monkeypatch):
    original = osirix_roi.imaging._binary
    references = []

    def binary(*args, **kwargs):
        assert all(reference() is None for reference in references), "The previous full mask is retained by a source-plane view"
        result = original(*args, **kwargs)
        references.append(weakref.ref(result[0]))
        return result

    monkeypatch.setattr(osirix_roi.imaging, "_binary", binary)
    _export(fixture)
    assert len(references) == len(fixture["segments"]) >= 2
    assert all(reference() is None for reference in references)
    _assert_no_staging(fixture)


@pytest.mark.parametrize("target", ["reference", "mask"])
def test_corrupt_nifti_is_reported_without_source_text_or_paths(fixture, target):
    path = fixture["reference"] if target == "reference" else fixture["segments"][0].mask_path
    marker = b"SYNTHETIC-INVALID-NIFTI-CONTENT"
    path.write_bytes(marker)
    with pytest.raises(osirix_roi.NativeROIError) as error:
        _export(fixture)
    assert str(error.value) == _GENERIC_ERROR
    assert str(path) not in str(error.value) and marker.decode() not in str(error.value)
    assert not fixture["output"].exists()
    _assert_no_staging(fixture)


@pytest.mark.parametrize(
    "changes",
    [
        {"source_paths": None},
        {"source_paths": [None]},
        {"source_paths": [object()]},
        {"reference_path": None},
        {"reference_path": object()},
        {"output_dir": None},
        {"output_dir": object()},
        {"segments": None},
    ],
)
def test_malformed_input_containers_and_paths_have_a_constant_public_error(fixture, changes):
    with pytest.raises(osirix_roi.NativeROIError) as error:
        _export(fixture, **changes)
    assert str(error.value) == _GENERIC_ERROR
    assert not fixture["output"].exists()
    _assert_no_staging(fixture)


@pytest.mark.parametrize("segments", [[], [object()], ["SYNTHETIC-INVALID-SELECTION"], {"SYNTHETIC-INVALID-SELECTION": "not a structure"}])
def test_malformed_selection_has_a_constant_public_error(fixture, segments):
    with pytest.raises(osirix_roi.NativeROIError) as error:
        _export(fixture, segments=segments)
    assert str(error.value) == "Select between one and 1000 saved structures"
    assert not fixture["output"].exists()
    _assert_no_staging(fixture)


def test_malformed_mask_path_has_a_constant_public_error(fixture):
    segment = replace(fixture["segments"][0], mask_path=None)
    with pytest.raises(osirix_roi.NativeROIError) as error:
        _export(fixture, segments=[segment])
    assert str(error.value) == _GENERIC_ERROR
    assert not fixture["output"].exists()
    _assert_no_staging(fixture)


@pytest.mark.parametrize("corruption", ["truncated", "invalid-deflate"])
def test_corrupt_gzip_payload_has_a_safe_error(fixture, corruption):
    path = fixture["segments"][0].mask_path
    image = nib.load(path)
    # The header remains readable, while later voxel decompression reaches EOF.
    values = np.random.default_rng(4921).integers(0, 2, image.shape, dtype=np.uint8)
    nib.save(nib.Nifti1Image(values, image.affine), path)
    data = bytearray(path.read_bytes())
    if corruption == "truncated":
        path.write_bytes(data[: len(data) // 2])
        osirix_roi.imaging._image(path)
    else:
        data[len(data) // 2] = 255
        path.write_bytes(data)
    with pytest.raises(osirix_roi.NativeROIError) as error:
        _export(fixture)
    assert str(error.value) == _GENERIC_ERROR
    assert not fixture["output"].exists()
    _assert_no_staging(fixture)
