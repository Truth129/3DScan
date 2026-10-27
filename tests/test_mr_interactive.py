"""MRI signal reaches the correction engine unchanged on the native grid."""
import nibabel as nib
import numpy as np
import pytest

from scan3d import interactive
from test_interactive_runtime import runtime_files


@pytest.mark.parametrize("slope,intercept", [(.000001, .00001), (10., 40000.)])
def test_interactive_accepts_scaled_mr_signal_without_hu_windowing_or_quantization(
        runtime_files, tmp_path, slope, intercept):
    fixture = runtime_files
    path = tmp_path / "mr-signal.nii.gz"
    stored = np.arange(np.prod(fixture.values.shape), dtype=np.uint16).reshape(fixture.values.shape)
    image = nib.Nifti1Image(stored, fixture.affine)
    image.header.set_slope_inter(slope, intercept)
    nib.save(image, path)
    expected = np.asarray(nib.load(path).dataobj)
    original = path.read_bytes()
    baseline = fixture.mask.read_bytes()
    runtime = interactive.InteractiveRuntime()
    output = tmp_path / "mr-candidate.nii.gz"
    prompt = fixture.point("mr-add", [7, 8, 9])
    result = runtime.predict(path, fixture.mask, [prompt], output, lambda _: None, lambda: False, "mr-session")
    session, = fixture.sessions
    np.testing.assert_array_equal(session.images[0][0], expected)
    assert session.images[0].shape == (1, *stored.shape)
    assert result["changed_voxels"] == 1 and result["prompts"][0]["voxel"] == [7, 8, 9]
    candidate = np.asarray(nib.load(output).dataobj)
    expected_mask = fixture.baseline.copy()
    expected_mask[7, 8, 9] = 1
    np.testing.assert_array_equal(candidate, expected_mask)
    assert path.read_bytes() == original and fixture.mask.read_bytes() == baseline
    # Switching to another source loads its actual samples despite equal grids.
    runtime.predict(fixture.ct, fixture.mask, [], tmp_path / "ct-candidate.nii.gz",
                    lambda _: None, lambda: False, "ct-session")
    assert len(session.images) == 2
    np.testing.assert_array_equal(session.images[1][0], fixture.values)
    runtime.release()

