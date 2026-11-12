"""TotalSpineSeg's grids and numbering, on toy volumes whose expected labels are upstream's own outputs."""

import numpy as np
import nibabel as nib
import pytest

from scan3d import totalspineseg as tss


def canal_line(shape, value=8):
    seg = np.zeros(shape, np.uint8)
    seg[4, 10:13, :] = value
    return seg


def test_sort_gives_only_label_one_the_z_of_its_anterior_part():
    seg = canal_line((9, 40, 60))
    line, front = tss._centerline(seg, [8, 9]), tss._anterior(seg, [8, 9])

    def components(labels=(1, 2, 3), body_of_one=True):
        labeled = np.zeros(seg.shape, np.uint8)
        for label, body, process in ((1, 50, 40), (2, 30, 20), (3, 12, 2)):
            if label in labels:
                if label != 1 or body_of_one:
                    labeled[3:6, 14:21, body : body + 4] = label
                labeled[3:6, 0:9, process : process + 4] = label
        return labeled

    assert tss._sorted(components(), range(1, 4), line, front) == ([1, 2, 3], [51, 26, 8])
    # Label 1 with no voxel in front of the canal takes the centreline's first z.
    assert tss._sorted(components(body_of_one=False), range(1, 4), line, front) == ([2, 3, 1], [26, 8, 0])
    assert tss._sorted(components(), [3, 2], line, front) == ([2, 3], [26, 8])


@pytest.mark.parametrize(("gap", "kept"), [(10, True), (11, False)])
def test_largest_component_keeps_pieces_within_ten_empty_voxels(gap, kept):
    seg = np.zeros((30, 30, 80), np.uint8)
    seg[10:20, 10:20, 5:25] = 6
    seg[12:15, 12:15, 25 + gap : 29 + gap] = 1
    assert bool((tss.largest_component(seg) == 1).any()) is kept
    assert np.array_equal(tss.largest_component(seg) == 6, seg == 6)


def column(values, canal=8):
    """A canal line and one disc block of 300 voxels per value, the top block first."""
    seg = np.zeros((12, 40, 20 * len(values) + 10), np.uint8)
    seg[5:7, 10:13, :] = canal
    for n, value in enumerate(values[::-1]):
        seg[3:9, 16:26, 10 + 20 * n : 15 + 20 * n] = value
    return seg


def discs(out):
    """Disc values from the top down."""
    return [int(out[5, 20, z]) for z in range(out.shape[2] - 1, -1, -1) if out[5, 20, z] >= 63][::5]


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ([1, 1, 1, 1, 5], [92, 93, 94, 95, 100]),
        ([4, 1, 1, 1, 1], [91, 92, 93, 94, 95]),
        # A sixth lumbar disc below T12-L1 is 96 when no L5-S is found.
        ([4, 1, 1, 1, 1, 1], [91, 92, 93, 94, 95, 96]),
    ],
)
def test_discs_are_numbered_from_the_landmark(values, expected):
    assert discs(tss.iterative_label(column(values), **tss.STEP1)) == expected


@pytest.mark.parametrize(("gap", "pieces"), [(2, 1), (3, 2)])
def test_disc_pieces_join_across_a_gap_of_two_voxels(gap, pieces):
    seg = np.zeros((12, 40, 60), np.uint8)
    seg[5:7, 10:13, :] = 8
    seg[3:9, 16:26, 20:24] = 1
    seg[3:9, 16:26, 24 + gap : 28 + gap] = 1
    line, front = tss._centerline(seg, [8, 9]), tss._anterior(seg, [8, 9])
    _, order, _ = tss._components(seg, [[1, 2, 3, 4, 5]], line, front, tss.NO_DISC)
    assert len(order) == pieces


def test_untrusted_landmarks_leave_the_spine_unnumbered():
    small = column([1, 1, 1, 1, 1])
    small[4:9, 16:24, 10:15] = 5  # Exactly 200 voxels of L5-S in the bottom disc.
    for seg in (column([1, 1, 1, 1, 1]), small, column([1, 1, 5, 1, 1])):
        with pytest.raises(tss.Unusable, match="^TotalSpineSeg could not number this spine"):
            tss.iterative_label(seg, **tss.STEP1)


def spine(split_top=False):
    """Second-pass values: sacrum, then alternating odd and even vertebrae with a disc above each, L5-S at the bottom."""
    seg = np.zeros((12, 40, 110), np.uint8)
    seg[5:7, 10:13, :] = 10
    seg[3:9, 16:26, 0:10] = 9
    for z, value in ((12, 5), (33, 1), (54, 1), (75, 1)):
        seg[3:9, 16:26, z : z + 5] = value
    for z, value in ((19, 8), (40, 7), (61, 8), (82, 7)):
        seg[3:9, 16:26, z : z + 12] = value
    if split_top:
        seg[3:9, 16:26, 88:91] = 0
    return seg


def test_vertebrae_are_numbered_from_the_discs_and_pieces_of_one_class_merge():
    out = tss.iterative_label(spine(), **tss.STEP2)
    assert [int(out[5, 20, z]) for z in (85, 70, 45, 25, 5)] == [42, 43, 44, 45, 50]
    assert discs(out) == [93, 94, 95, 100]
    # Two pieces of one class with no disc between them are one vertebra.
    out = tss.iterative_label(spine(split_top=True), **tss.STEP2)
    assert int(out[5, 20, 85]) == int(out[5, 20, 92]) == 42
    # Vertebra voxels of no class join the vertebra they touch, and are dropped elsewhere.
    seg = spine()
    seg[3:9, 26, 44:48] = seg[3:9, 34, 44:48] = 6
    out = tss.iterative_label(seg, **tss.STEP2)
    assert (out[3:9, 26, 44:48] == 44).all() and not out[3:9, 34, 44:48].any()


def test_fill_canal_keeps_one_cord_and_one_canal_and_fills_each_slice():
    seg = np.zeros((10, 20, 40), np.uint8)
    seg[2:8, 5:15, 0:30] = 2
    seg[4:6, 8:12, 0:20] = 1
    seg[8, 8:12, 23:25] = 1  # A detached cord piece, touching the canal but outside it.
    seg[3, 6, 5] = 0
    seg[3, 7, 6] = 65
    seg[5, :, 28] = 0  # Inside every x span of its slice, outside any y span.
    seg[2:4, 0:2, 35:38] = 2  # A detached canal piece.
    out = tss.fill_canal(seg)
    assert (out[8, 8:12, 23:25] == 2).all() and out[3, 6, 5] == out[3, 7, 6] == 2
    assert not out[5, :, 28].any() and out[4, 10, 28] == out[6, 10, 28] == 2
    assert not out[2:4, 0:2, 35:38].any() and (out[4:6, 8:12, 0:20] == 1).all()


def test_every_second_disc_by_value_is_the_second_channel():
    seg = np.zeros((4, 4, 4), np.uint8)
    seg[0, 0, :] = [92, 93, 95, 100]
    assert tss.alternate_discs(seg)[0, 0].tolist() == [1, 0, 1, 0]


def test_failures_follow_upstreams_order():
    with pytest.raises(tss.Unusable, match=f"^{tss.NO_CANAL}$"):
        tss.iterative_label(np.ones((5, 5, 5), np.uint8), **tss.STEP1)
    # Two canal columns: no slice has a canal voxel at the centre of the canal's box.
    seg = np.zeros((12, 30, 40), np.uint8)
    seg[2, 10:13, :] = seg[8, 10:13, :] = 8
    with pytest.raises(tss.Unusable, match=f"^{tss.NO_DISC}$"):
        tss.iterative_label(seg, **tss.STEP1)
    seg[3:9, 16:24, 10:16] = 5
    with pytest.raises(tss.Unusable, match=f"^{tss.NO_CENTRELINE}$"):
        tss.iterative_label(seg, **tss.STEP1)


def stored(data, affine, slope=None):
    """The image a NIfTI file of these voxels would load as, with the scale factor written into its header."""
    image = nib.Nifti1Image(data, affine)
    if slope is None:
        return nib.Nifti1Image.from_bytes(image.to_bytes())
    header = image.header.copy()
    header["scl_slope"], header["scl_inter"], header["vox_offset"] = slope, 0.0, 352
    return nib.Nifti1Image.from_bytes(header.binaryblock + b"\0" * 4 + np.asfortranarray(data).tobytes(order="F"))


def test_the_one_mm_grid_is_upstreams():
    planes = np.zeros((2, 3, 3), np.int16)
    planes[0], planes[1] = 3, 4
    iso = tss.canonical_iso(stored(planes, np.diag([2.0, 1.0, 1.0, 1.0])))
    assert iso.shape == (4, 3, 3) and iso.affine[0, 3] == -0.5
    # Cast back to the file's type, which truncates.
    assert np.asanyarray(iso.dataobj)[:, 1, 1].tolist() == [3, 3, 3, 4]
    iso = tss.canonical_iso(stored(planes.astype(np.float32), np.diag([2.0, 1.0, 1.0, 1.0])))
    assert np.asanyarray(iso.dataobj)[:, 1, 1].tolist() == [3, 3.25, 3.75, 4]
    # A scaled file reads as float64, so it is not truncated.
    iso = tss.canonical_iso(stored(planes, np.diag([2.0, 1.0, 1.0, 1.0]), slope=0.5))
    assert np.asanyarray(iso.dataobj).dtype == np.float64 and np.asanyarray(iso.dataobj)[:, 1, 1].tolist() == [1.5, 1.625, 1.875, 2.0]
    # Oblique: the size is floored from the float32 header's zooms, 60 x 0.79999998 -> 47.
    x, z = np.radians(7.3), np.radians(-3.1)
    about_x = np.array([[1, 0, 0], [0, np.cos(x), -np.sin(x)], [0, np.sin(x), np.cos(x)]])
    about_z = np.array([[np.cos(z), -np.sin(z), 0], [np.sin(z), np.cos(z), 0], [0, 0, 1]])
    affine = np.eye(4)
    affine[:3, :3], affine[:3, 3] = about_x @ about_z * np.array([0.8, 0.8, 3.3]), [-31.37, 12.91, -77.03]
    assert tss.canonical_iso(stored(np.zeros((60, 64, 15), np.int16), affine)).shape == (47, 51, 49)


@pytest.mark.parametrize("flip", [False, True])
def test_labels_return_to_the_source_grid_exactly(tmp_path, flip):
    """A 1 mm RAS source, and the same voxels stored as LAS, a reflected grid."""
    shape = (20, 22, 24)
    affine = np.eye(4)
    affine[:3, 3] = [-10.0, 5.0, 30.0]
    block = np.zeros(shape, np.uint8)
    block[12:18, 3:9, 15:20] = 21
    block[4:7, 14:19, 2:6] = 63
    if flip:
        affine = affine @ np.array([[-1, 0, 0, shape[0] - 1], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]])
    source = stored(np.arange(np.prod(shape), dtype=np.int16).reshape(shape), affine)
    iso = tss.canonical_iso(source)
    on_iso = block[::-1] if flip else block
    crop, box, _ = tss.step2_crop(iso, on_iso)
    labels2 = tss.raw_image(crop, on_iso[box].transpose(2, 1, 0), tmp_path)
    assert np.array_equal(tss.on_source(source, iso, labels2), block)


def test_sheared_scans_and_other_units_are_refused():
    sheared = np.eye(4)
    sheared[0, 1] = 0.3
    with pytest.raises(tss.Unusable, match=f"^{tss.SHEARED}$"):
        tss._check(stored(np.zeros((4, 4, 4), np.int16), sheared))
    metres = stored(np.zeros((4, 4, 4), np.int16), np.eye(4))
    metres.header.set_xyzt_units("meter")
    with pytest.raises(tss.Unusable, match=f"^{tss.NOT_MILLIMETRES}$"):
        tss._check(metres)
    tss._check(stored(np.zeros((4, 4, 4), np.int16), np.eye(4)))


def test_scans_under_one_millimetre_thick_are_refused():
    thin = stored(np.ones((8, 8, 2), np.int16), np.diag([1.0, 1.0, 0.4, 1.0]))
    with pytest.raises(tss.Unusable, match=f"^{tss.TOO_THIN}$"):
        tss.segment(thin, None, None, None)
