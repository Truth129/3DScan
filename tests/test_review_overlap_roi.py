"""ROI overlap processing must match complete-volume binary set operations."""

from pathlib import Path
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest

from scan3d import imaging, review
from scan3d.relationships import INDEPENDENT


@pytest.fixture
def fixture(tmp_path):
    angle = .37
    affine = np.eye(4)
    affine[:3, :3] = np.array([[np.cos(angle), -np.sin(angle), 0],
                              [np.sin(angle), np.cos(angle), 0], [0, 0, 1]]) @ np.diag([-1.3, 2.1, 3.7])
    affine[:3, 3] = [45, -60, 120]
    shape = (11, 13, 17)
    image = nib.Nifti1Image(np.zeros(shape, np.int16), affine)
    image.header.set_xyzt_units("mm")
    nib.save(image, tmp_path / "ct.nii.gz")
    image, affine = imaging._image(tmp_path / "ct.nii.gz")
    folder = tmp_path / "candidate"
    folder.mkdir()
    checks = []
    service = SimpleNamespace(store=None, stage=lambda *args: None, check_cancel=lambda job: checks.append(job))
    manager = review.ReviewManager(service)

    def save(name, values, transform=None):
        path = tmp_path / (name + ".nii.gz")
        saved = nib.Nifti1Image(values, image.affine if transform is None else transform)
        saved.header.set_xyzt_units("mm")
        nib.save(saved, path)
        return path

    def run(after, peer_values, *, exclusive=False, peer_affine=None):
        target = save("target", after.astype(np.uint8))
        peer = save("peer", peer_values, peer_affine)
        selected = {"id": "target", "name": "Target", "mask_file": str(target)}
        entry = {"id": "peer", "name": "Peer", "mask_file": str(peer)}
        relationship = {"kind": "subdivision", "exclusive_with": ["peer"]} if exclusive else INDEPENDENT
        summary = review._summary(int(after.sum()), int(after.sum()), 0, 0, abs(np.linalg.det(affine[:3, :3])) / 1000)
        job = {"payload": {"relationship": relationship}}
        result = manager._overlaps(job, {"structures": [selected, entry]}, selected, after, image, affine,
                                   folder, target, summary)
        return result, peer, target

    return SimpleNamespace(shape=shape, image=image, affine=affine, manager=manager, service=service,
                           folder=folder, save=save, run=run, checks=checks)


@pytest.mark.parametrize("storage", ["C", "F", "strided"])
@pytest.mark.parametrize("dtype", [np.uint8, np.float32])
@pytest.mark.parametrize("exclusive", [False, True])
def test_complete_volume_oracle_and_readonly_native_layouts(fixture, monkeypatch, storage, dtype, exclusive):
    rng = np.random.default_rng(53)
    target = np.zeros(fixture.shape, bool)
    target[3:8, 2:7, 4:10] = rng.random((5, 5, 6)) > .5
    target[3, 2, 4] = target[7, 6, 9] = True
    expected_peer = (rng.random(fixture.shape) > .75).astype(dtype)
    expected_peer[3, 2, 4] = 1
    if storage == "strided":
        array = np.zeros((fixture.shape[0] * 2, *fixture.shape[1:]), dtype=dtype)
        values = array[::2]
        values[:] = expected_peer
        assert not values.flags.c_contiguous and not values.flags.f_contiguous
    else:
        values = np.array(expected_peer, order=storage)
    values.flags.writeable = False
    initial = values.copy()
    original = imaging._binary_values

    def native(path, *args, **kwargs):
        actual, image, affine, present = original(path, *args, **kwargs)
        if Path(path).name == "peer.nii.gz":
            np.testing.assert_array_equal(actual, values)
            return values, image, affine, present
        return actual, image, affine, present

    monkeypatch.setattr(imaging, "_binary_values", native)
    # Any accidental reintroduction of the full C-order conversion must fail.
    monkeypatch.setattr(imaging, "_binary", lambda *args, **kwargs: pytest.fail("Full peer conversion requested"))
    monkeypatch.setattr(review, "_OVERLAP_CHUNK_VOXELS", 19)
    result, source, target_path = fixture.run(target, expected_peer, exclusive=exclusive)
    overlap = int(np.count_nonzero(target & (expected_peer != 0)))
    assert result["overlaps"][0]["voxel_count"] == overlap
    assert result["overlaps"][0]["policy"] == ("removed" if exclusive else "preserved")
    np.testing.assert_array_equal(values, initial)
    np.testing.assert_array_equal(np.asanyarray(nib.load(source).dataobj), expected_peer)
    if exclusive:
        affected = result["affected_structures"][1]
        assert affected["summary"]["before_voxels"] == int(np.count_nonzero(expected_peer))
        assert affected["summary"]["removed_voxels"] == overlap
        assert affected["summary"]["after_voxels"] == int(np.count_nonzero(expected_peer)) - overlap
        output = np.asanyarray(nib.load(affected["mask_file"]).dataobj).astype(bool)
        np.testing.assert_array_equal(output, (expected_peer != 0) & ~target)
        np.testing.assert_array_equal(output[~target], expected_peer[~target])
    else:
        assert len(result["affected_structures"]) == 1
        assert not list(fixture.folder.glob("peer-*.nii.gz"))
    assert result["affected_structures"][0]["metadata_only"] is True
    assert result["affected_structures"][0]["mask_file"] == str(target_path)


def test_chunked_count_and_overlap_do_not_exceed_limit(monkeypatch):
    limit = 11
    monkeypatch.setattr(review, "_OVERLAP_CHUNK_VOXELS", limit)
    target = np.zeros((7, 9, 13), bool)
    target[1:6, 2:8, 3:12] = True
    peer = np.ones(target.shape, dtype=np.float32, order="F")
    selected = target[::2, 1::2, ::2]
    other = peer[::2, 1::2, ::2]
    expected = int(np.count_nonzero(selected))
    sizes, checks = [], []
    logical_and = np.logical_and

    def bounded(left, right):
        sizes.append(left.size)
        assert left.size <= limit and right.size <= limit
        return logical_and(left, right)

    monkeypatch.setattr(review.np, "logical_and", bounded)
    assert review._count_overlap(selected, other, lambda: checks.append(1)) == expected
    assert len(sizes) > 1 and len(checks) == len(sizes)
    assert review._count_foreground(other, lambda: checks.append(1)) == other.size
    assert review._count_foreground(other[:, :, :0], lambda: None) == 0


def test_relationship_only_erases_old_overlap_and_preserves_outside_region(fixture):
    target = np.zeros(fixture.shape, bool)
    target[1, 1, 1] = target[9, 11, 15] = True
    peer = np.zeros(fixture.shape, np.uint8)
    peer[9, 11, 15] = peer[0, 0, 0] = 1
    result, _, _ = fixture.run(target, peer, exclusive=True)
    affected = result["affected_structures"][1]
    output = np.asanyarray(nib.load(affected["mask_file"]).dataobj)
    assert result["affected_structures"][0]["summary"]["changed_voxels"] == 0
    assert result["total_changed_voxels"] == 1
    assert output[9, 11, 15] == 0 and output[0, 0, 0] == 1
    assert affected["summary"]["before_voxels"] == 2
    assert affected["summary"]["after_voxels"] == 1


def test_exclusive_peer_empty_warning_uses_complete_count(fixture):
    target = np.zeros(fixture.shape, bool)
    target[3:5, 3:5, 3:5] = True
    result, _, _ = fixture.run(target, target.astype(np.float32), exclusive=True)
    assert result["affected_structures"][1]["summary"]["after_voxels"] == 0
    assert result["warnings"] == ["Replacing overlap leaves Peer empty; inspect this subdivision"]


@pytest.mark.parametrize("invalid", [2., .5, np.nan])
def test_binary_validation_checks_values_outside_target_roi(fixture, invalid):
    target = np.zeros(fixture.shape, bool)
    target[3, 4, 5] = True
    peer = np.zeros(fixture.shape, np.float32)
    peer[0, 0, 0] = invalid
    with pytest.raises(ValueError, match="binary"):
        fixture.run(target, peer)


def test_reference_grid_validation_precedes_roi_overlap(fixture):
    target = np.zeros(fixture.shape, bool)
    target[3, 4, 5] = True
    peer = target.astype(np.uint8)
    shifted = fixture.affine.copy()
    shifted[0, 3] += 1
    with pytest.raises(ValueError, match="geometry"):
        fixture.run(target, peer, peer_affine=shifted)


def test_source_change_during_overlap_is_rejected(fixture, monkeypatch):
    target = np.zeros(fixture.shape, bool)
    target[3, 4, 5] = True
    original = imaging._binary_values

    def changed(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        fixture.save("peer", np.zeros(fixture.shape, np.uint8))
        return result

    monkeypatch.setattr(imaging, "_binary_values", changed)
    with pytest.raises(ValueError, match="source mask changed"):
        fixture.run(target, target.astype(np.uint8))


def test_cancellation_interrupts_native_chunk_processing(fixture, monkeypatch):
    monkeypatch.setattr(review, "_OVERLAP_CHUNK_VOXELS", 7)
    target = np.ones(fixture.shape, bool)
    count = 0

    def cancelled(job):
        nonlocal count
        count += 1
        if count == 3:
            raise InterruptedError("Correction cancelled")

    fixture.service.check_cancel = cancelled
    with pytest.raises(InterruptedError, match="cancelled"):
        fixture.run(target, np.ones(fixture.shape, np.uint8), exclusive=True)
    assert count == 3
    assert not list(fixture.folder.glob("peer-*.nii.gz"))
