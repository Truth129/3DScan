# SPDX-License-Identifier: LGPL-3.0-only
#
# Adapted from TotalSpineSeg (https://github.com/neuropoly/totalspineseg), release r20260730, commit b062b10,
# by Yehuda Warszawer, Nathan Molinier, Anat Achiron and Julien Cohen-Adad, which is licensed under the GNU
# Lesser General Public License version 3. This file is a modified version of parts of that work, made for
# 3DScan in November 2026, and stays under the same licence; the licence texts are in scan3d/licenses/. The
# SimpleITK geometry conversions and the 1 mm reference grid are adapted from TorchIO 1.2.1
# (https://github.com/TorchIO-project/torchio, Apache-2.0, TorchIO contributors). The rest of 3DScan is
# Apache-2.0.
"""TotalSpineSeg's pipeline around its two nnU-Nets, in one process.

Reproduces the default path of TotalSpineSeg r20260730 (inference.py without --loc, --iso or --step1): the
scan is turned to RAS and resampled to 1 mm, the first network finds the discs, canal and cord, its discs
are numbered from the landmark discs, the 1 mm image is cropped to its labels and every second disc becomes
the second network's second channel, the second network's vertebrae and discs are numbered, and the labels
return to the scan's grid. Left out: the previews, the soft cord and canal maps and the levels file, which
never feed the labels. Every save and load upstream does between steps is repeated in memory, because a
NIfTI round trip changes affines and the 1 mm grid's size.

Upstream's defects are kept on purpose: its superior-to-inferior sort reads a boolean array as labels
(_sorted), and every quirk of its numbering is copied, so the labels are upstream's voxel for voxel.
The networks arrive as callables, so this file needs no PyTorch.
"""

from __future__ import annotations

import nibabel as nib
import numpy as np
from scipy import ndimage as ndi
import SimpleITK as sitk


class Unusable(Exception):
    """The scan cannot be labelled; the message is a sentence for the reader."""


NO_CANAL = "TotalSpineSeg found no spinal canal in this scan."
NO_CENTRELINE = "TotalSpineSeg could not follow the spinal canal in this scan."
NO_DISC = "TotalSpineSeg found no disc in this scan."
NO_VERTEBRA = "TotalSpineSeg found no vertebra in this scan."
NO_LANDMARK = (
    "TotalSpineSeg could not number this spine: it counts from the C2-C3, C7-T1, T12-L1 or L5-S disc and found none "
    "of them. Use a series that shows the top of the neck, the base of the neck, the thoracolumbar junction or the sacrum."
)
NO_NUMBERING = "TotalSpineSeg could not number the vertebrae in this scan."
NOT_MILLIMETRES = "TotalSpineSeg needs a scan measured in millimetres."
SHEARED = "TotalSpineSeg cannot run on a scan with sheared slices."
TOO_THIN = "TotalSpineSeg needs a scan at least 1 mm thick in every direction."

CROSS = ndi.generate_binary_structure(3, 1)
CUBE = np.ones((3, 3, 3))
# Upstream's output values. A disc names the vertebra below it; C1 and C2 come before the first disc.
DISC_LANDMARKS = {2: 63, 3: 71, 4: 91, 5: 100}
DISCS_MAX = [*range(63, 68), *range(71, 83), *range(91, 97), 100]
DISCS_DEFAULT = [*range(63, 68), *range(71, 83), *range(91, 96), 100]
VERTEBRAE_MAX = [11, 12, *range(13, 18), *range(21, 33), *range(41, 47), 50]
VERTEBRAE_DEFAULT = [11, 12, *range(13, 18), *range(21, 33), *range(41, 46), 50]
BELOW = dict(zip(DISCS_MAX, VERTEBRAE_MAX[2:]))
# The networks' raw values. Step 1: discs 1-5 (2-5 the landmark discs), 6 vertebra or sacrum, 7 C1, 8 canal,
# 9 cord. Step 2: discs as step 1, 6 vertebra not yet odd or even, 7 odd, 8 even, 9 sacrum, 10 canal, 11 cord.
STEP1 = dict(discs=(1, 2, 3, 4, 5), vertebrae=(), extra=(), canal=8, cord=9, sacrum=6, also={7: 11})
STEP2 = dict(discs=(1, 2, 3, 4, 5), vertebrae=(7, 8, 9), extra=(6,), canal=10, cord=11, sacrum=9, also={})
_FLIP = np.diag([-1, -1, 1])


def segment(image, first, second, scratch, step=lambda _: None):
    """Final labels, in upstream's values, as an array in the image's own voxel order.

    image is the nibabel image as loaded. first and second take an (c, z, y, x) float32 array and its spacing
    and return the network's uint8 (z, y, x) values. scratch is an empty folder for two small files.
    """
    _check(image)
    step("resampling to 1 mm")
    iso = canonical_iso(image)
    # Under 1 mm along an axis, upstream's 1 mm grid has no voxel there: it cannot read its own file, or runs
    # its first network on a slice of zeros.
    if 0 in iso.shape:
        raise Unusable(TOO_THIN)
    raw1 = first(network_input(iso), [1.0, 1.0, 1.0]).transpose(2, 1, 0)
    step("numbering the discs")
    labels1 = step1_labels(raw1)
    crop, _, channel = step2_crop(iso, labels1)
    both = np.vstack([network_input(crop), channel.transpose(2, 1, 0)[np.newaxis].astype(np.float32)])
    raw2 = raw_image(crop, second(both, [1.0, 1.0, 1.0]), scratch)
    step("numbering the vertebrae")
    labels2 = step2_labels(raw2)
    step("returning to the scan's grid")
    return on_source(image, iso, labels2)


def _check(image):
    # Upstream reads the affine as millimetres whatever the header says.
    if image.header.get_xyzt_units()[0] not in ("mm", "unknown"):
        raise Unusable(NOT_MILLIMETRES)
    # Its 1 mm grid keeps the scan's directions, which a sheared grid does not have.
    rotation_zoom = np.asarray(image.affine, dtype=np.float64)[:3, :3]
    direction = rotation_zoom / np.sqrt((rotation_zoom * rotation_zoom).sum(axis=0))
    if np.abs(direction.T @ direction - np.eye(3)).max() > 1e-4:
        raise Unusable(SHEARED)


# Geometry. Upstream saves and loads a NIfTI file between steps, which stores the sform as float32 and lets
# nibabel keep a header's old affine when a new one is close to it, so its images are carried and its nibabel
# calls repeated here; no affine is rebuilt by hand.


def _resaved(data, affine, header, dtype):
    """What the next step would load from the NIfTI file upstream writes here."""
    image = nib.Nifti1Image(np.asanyarray(data).astype(dtype), affine, header)
    image.set_data_dtype(dtype)
    image.set_qform(image.affine)
    image.set_sform(image.affine)
    return nib.Nifti1Image.from_bytes(image.to_bytes())


def _sitk_geometry(affine):
    # As torchio 1.2.1, which upstream resamples with.
    rotation_zoom = affine[:3, :3]
    spacing = np.sqrt(np.sum(rotation_zoom * rotation_zoom, axis=0))
    direction = np.dot(_FLIP, rotation_zoom / spacing)
    origin = np.dot(_FLIP, affine[:3, 3])
    return tuple(origin), tuple(spacing), tuple(direction.flatten())


def _to_sitk(array, affine):
    image = sitk.GetImageFromArray(array.transpose())
    origin, spacing, direction = _sitk_geometry(np.asarray(affine).astype(np.float64))
    image.SetOrigin(origin)
    image.SetSpacing(spacing)
    image.SetDirection(direction)
    return image


def _from_sitk(image):
    data = sitk.GetArrayFromImage(image).transpose()
    spacing = np.array(image.GetSpacing(), dtype=np.float64)
    rotation = np.dot(_FLIP, np.array(image.GetDirection(), dtype=np.float64).reshape(3, 3))
    affine = np.eye(4)
    affine[:3, :3] = rotation * spacing
    affine[:3, 3] = np.dot(_FLIP, np.array(image.GetOrigin(), dtype=np.float64))
    return data, affine


def _data_dtype(image):
    # Upstream keeps the type of the loaded data: float64 when the file has a scale factor.
    return np.dtype(np.asanyarray(image.dataobj).dtype.name)


def canonical_iso(image):
    """Upstream's average4d, reorient_canonical and resample: the scan in RAS on a 1 mm grid along its own axes."""
    dtype = _data_dtype(image)
    # The app stores only 3D series, so the 4D mean never runs; the step is still its cast and its file.
    image = nib.Nifti1Image(np.asanyarray(image.dataobj).astype(np.float64), image.affine, image.header)
    image = _resaved(image.dataobj, image.affine, image.header, dtype)
    image = nib.as_closest_canonical(image)
    image = _resaved(image.dataobj, image.affine, image.header, dtype)
    # Linear, centre kept, size floored, zero outside, as torchio's Resample.
    moving = _to_sitk(np.asanyarray(image.dataobj).astype(np.float64), image.affine)
    old_spacing = np.array(moving.GetSpacing(), dtype=float)
    old_size = np.array(moving.GetSize())
    last = np.array(moving.TransformIndexToPhysicalPoint((old_size - 1).tolist()), dtype=float)
    center = (last + np.array(moving.GetOrigin(), dtype=float)) / 2
    new_spacing = np.array((1.0, 1.0, 1.0), dtype=float)
    new_size = np.floor(old_size * old_spacing / new_spacing)
    new_size[old_size == 1] = 1
    direction = np.asarray(moving.GetDirection(), dtype=float).reshape(3, 3)
    origin = (center - direction @ ((new_size - 1) / 2 * new_spacing)).tolist()
    reference = sitk.Image(new_size.astype(int).tolist(), moving.GetPixelID(), moving.GetNumberOfComponentsPerPixel())
    reference.SetDirection(moving.GetDirection())
    reference.SetSpacing(new_spacing.tolist())
    reference.SetOrigin(origin)
    resampler = sitk.ResampleImageFilter()
    resampler.SetInterpolator(sitk.sitkLinear)
    resampler.SetReferenceImage(reference)
    data, affine = _from_sitk(resampler.Execute(moving))
    out = nib.Nifti1Image(data.astype(np.float64), affine, image.header)
    out.set_qform(out.affine)
    out.set_qform(out.affine)  # Upstream calls it twice.
    # Cast back to the file's type, which truncates an integer image as upstream does.
    return _resaved(out.dataobj, out.affine, out.header, dtype)


def network_input(image):
    """What nnU-Net's SimpleITK reader gives for the file nibabel would write, cast as nnU-Net casts it."""
    return np.asanyarray(image.dataobj).transpose(2, 1, 0)[np.newaxis].astype(np.float32)


def step2_crop(iso, labels1):
    """Upstream's crop_image2seg (margin 10), transform_seg2image and extract_alternate."""
    x, y, z = np.nonzero(labels1)
    low = [max(int(v.min()) - 10, 0) for v in (x, y, z)]
    high = [min(int(v.max()) + 10, n - 1) for v, n in zip((x, y, z), labels1.shape)]
    box = tuple(slice(a, b + 1) for a, b in zip(low, high))
    crop = iso.slicer[box]
    crop = _resaved(crop.dataobj, crop.affine, crop.header, _data_dtype(iso))
    # Upstream resamples the labels onto the crop, nearest neighbour, which moves no voxel: a slice is exact.
    return crop, box, alternate_discs(labels1[box])


def raw_image(crop, raw, scratch):
    """The second network's labels as upstream writes them: through SimpleITK, with the geometry it read from the crop.

    That header is not nibabel's, and it is the affine the return to the 1 mm grid uses when the crop starts at
    the grid's corner, so the two files are written as upstream writes them.
    """
    image_path, label_path = scratch / "crop_0000.nii", scratch / "crop.nii.gz"
    nib.save(crop, str(image_path))
    geometry = sitk.ReadImage(str(image_path))
    labels = sitk.GetImageFromArray(raw.astype(np.uint8))
    labels.SetSpacing(geometry.GetSpacing())
    labels.SetOrigin(geometry.GetOrigin())
    labels.SetDirection(geometry.GetDirection())
    sitk.WriteImage(labels, str(label_path), True)
    # In memory before the folder goes.
    return nib.Nifti1Image.from_bytes(nib.load(str(label_path)).to_bytes())


def _nearest(seg, shape, affine):
    """Upstream's transform_seg2image(image, seg, 'nearest'), as an array on the image's grid."""
    data = np.asanyarray(seg.dataobj).round().astype(np.uint8)
    resampler = sitk.ResampleImageFilter()
    resampler.SetInterpolator(sitk.sitkNearestNeighbor)
    origin, spacing, direction = _sitk_geometry(np.asarray(affine).astype(np.float64))
    resampler.SetOutputDirection(direction)
    resampler.SetOutputOrigin(origin)
    resampler.SetOutputSpacing(spacing)
    resampler.SetSize([int(n) for n in shape])
    moved = resampler.Execute(_to_sitk(data, seg.affine.copy()))
    return sitk.GetArrayFromImage(moved).transpose().round().astype(np.uint8)


def on_iso(iso, labels2):
    """The crop's labels on the 1 mm grid, with the header upstream's file gets.

    nibabel decides which affine it carries: the 1 mm image's when the crop moved the origin, the
    SimpleITK-written one when the crop starts at the corner. The calls are upstream's, so that follows.
    """
    inner = nib.Nifti1Image(_nearest(labels2, iso.shape, iso.affine.copy()), iso.affine, labels2.header)
    return _resaved(np.asanyarray(inner.dataobj).round(), inner.affine, inner.header, np.uint8)


def on_source(image, iso, labels2):
    """The labels on the source grid, nearest neighbour between the two full affines."""
    return _nearest(on_iso(iso, labels2), image.shape, image.affine.copy())


def step1_labels(raw):
    """Step 1's labels from the first network's values, both uint8 (x, y, z) on the 1 mm grid."""
    return fill_canal(iterative_label(largest_component(raw), **STEP1))


def step2_labels(raw):
    """Step 2's labels from the second network's label image, re-saved after each change as upstream does."""
    for change in (largest_component, lambda data: iterative_label(data, **STEP2), fill_canal):
        data = change(np.asanyarray(raw.dataobj).round().astype(np.uint8))
        inner = nib.Nifti1Image(data, raw.affine, raw.header)
        raw = _resaved(np.asanyarray(inner.dataobj).round(), inner.affine, inner.header, np.uint8)
    return raw


# Labelling. Transliterated from upstream's utils; arrays are uint8 (x, y, z) on the 1 mm grid, read as left to
# right, posterior to anterior and inferior to superior, with a voxel a millimetre.


def largest_component(seg):
    """Upstream's largest_component with binarize and a dilation of 5: every piece more than 10 voxels from the largest goes."""
    mask = seg != 0
    if not mask.any():
        return np.zeros_like(seg)
    pieces, _ = ndi.label(ndi.binary_dilation(mask, ndi.iterate_structure(CROSS, 5)), CUBE)
    pieces *= mask
    keep = np.bincount(pieces.ravel())[1:].argmax() + 1
    return (pieces == keep).astype(np.uint8) * seg


def _centerline(seg, canal_values):
    if not np.any(np.isin(canal_values, seg)):
        raise Unusable(NO_CANAL)
    indices = np.indices(seg.shape)
    canal = np.isin(seg, canal_values)
    # Per axial slice, the middle of the canal's box; a slice without canal gives -1, which no index equals.
    min_x = np.min(indices[0], where=canal, initial=np.iinfo(indices.dtype).max, keepdims=True, axis=(0, 1))
    max_x = np.max(indices[0], where=canal, initial=np.iinfo(indices.dtype).min, keepdims=True, axis=(0, 1))
    mid_x = indices[0] == ((min_x + max_x) // 2)
    min_y = np.min(indices[1], where=canal, initial=np.iinfo(indices.dtype).max, keepdims=True, axis=(0, 1))
    max_y = np.max(indices[1], where=canal, initial=np.iinfo(indices.dtype).min, keepdims=True, axis=(0, 1))
    mid_y = indices[1] == ((min_y + max_y) // 2)
    return np.array(np.nonzero(canal * mid_x * mid_y)).T


def _anterior(seg, canal_values):
    indices = np.indices(seg.shape)
    canal = np.isin(seg, canal_values)
    min_y = np.min(indices[1], where=canal, initial=np.iinfo(indices.dtype).max, keepdims=True, axis=(0, 1))
    max_y = np.max(indices[1], where=canal, initial=np.iinfo(indices.dtype).min, keepdims=True, axis=(0, 1))
    return indices[1] > (min_y + max_y) // 2


def _sorted(labeled, labels, centerline, anterior):
    """Upstream's _sort_labels_si: labels from superior to inferior by the nearest centreline voxel's z.

    Kept with its defect: the labels with voxels anterior to the canal are taken as a boolean array, so only
    label 1 gets the z of its anterior part (or the centreline's first z, when it has none there).
    """
    if not len(centerline):
        # Upstream fails here with numpy's argmin of an empty sequence.
        raise Unusable(NO_CENTRELINE)
    indices = np.array(ndi.center_of_mass(np.isin(labeled, labels), labeled, labels))
    distances = np.linalg.norm(indices[:, None, :] - centerline[None, ...], axis=2)
    z = centerline[np.argmin(distances, axis=1), -1]
    by_label = dict(zip(labels, z))
    front = anterior * labeled
    flags = np.isin(labels, front)
    with np.errstate(invalid="ignore"):
        front_indices = np.array(ndi.center_of_mass(np.isin(front, flags), front, flags))
    front_distances = np.linalg.norm(front_indices[:, None, :] - centerline[None, :], axis=2)
    front_z = centerline[np.argmin(front_distances, axis=1), -1]
    for flag, value in zip(flags, front_z):
        by_label[flag] = value
    z = [by_label[label] for label in labels]
    z, labels = zip(*sorted(zip(z, labels))[::-1])
    return list(labels), list(z)


def _smaller(labeled):
    if labeled.max() < np.iinfo(np.uint8).max:
        return labeled.astype(np.uint8)
    if labeled.max() < np.iinfo(np.uint16).max:
        return labeled.astype(np.uint16)
    return labeled


def _components(seg, groups, centerline, anterior, missing):
    """Upstream's _get_si_sorted_components: pieces of 10 voxels or more, numbered, then sorted."""
    labeled, count = np.zeros(seg.shape, dtype=np.uint32), 0
    for group in groups:
        mask = np.isin(seg, group)
        # Each side of a gap grows by one voxel and the labelling joins corners, so a gap of two voxels joins.
        pieces, found = ndi.label(ndi.binary_dilation(mask, CROSS).astype(np.uint32), CUBE)
        pieces *= mask
        sizes = np.bincount(pieces.ravel(), minlength=found + 1)
        kept = 0
        for piece in range(1, found + 1):
            if sizes[piece] >= 10:
                kept += 1
                labeled[pieces == piece] = count + kept
        count += kept
    if count == 0:
        raise Unusable(missing)
    labeled = _smaller(labeled)
    order, z = _sorted(labeled, range(1, count + 1), centerline, anterior)
    return labeled, order, z


def _fill(mask):
    """A voxel is in when it lies between the first and last mask voxel of its x line, or its y line, or its z line."""
    indices = np.indices(mask.shape)
    min_x = np.min(np.where(mask, indices[0], np.inf), axis=0)[np.newaxis, ...]
    max_x = np.max(np.where(mask, indices[0], -np.inf), axis=0)[np.newaxis, ...]
    min_y = np.min(np.where(mask, indices[1], np.inf), axis=1)[:, np.newaxis, :]
    max_y = np.max(np.where(mask, indices[1], -np.inf), axis=1)[:, np.newaxis, :]
    min_z = np.min(np.where(mask, indices[2], np.inf), axis=2)[:, :, np.newaxis]
    max_z = np.max(np.where(mask, indices[2], -np.inf), axis=2)[:, :, np.newaxis]
    return ((min_x <= indices[0]) & (indices[0] <= max_x)) | ((min_y <= indices[1]) & (indices[1] <= max_y)) | ((min_z <= indices[2]) & (indices[2] <= max_z))


def _landmarks(seg, disc_labeled, disc_order):
    """Upstream's choice of trusted landmark discs: {disc piece: output value}."""
    # A fresh list on every call; upstream mutates a default argument.
    selected = [2, 5, 3, 4]
    if not np.any((disc_labeled == disc_order[0]) & (seg == 2)):
        selected.remove(2)  # C2-C3 counts only in the top disc.
    if not np.any((disc_labeled == disc_order[-1]) & (seg == 5)):
        selected.remove(5)  # L5-S counts only in the bottom disc.
    found, is_landmark = {}, np.isin(seg, list(DISC_LANDMARKS))
    for value in selected:
        # Upstream trusts only the first two listed once one is found.
        if found and selected.index(value) > 1:
            break
        here = seg == value
        if np.sum(here) > 200:
            component = np.argmax(np.bincount(disc_labeled[here].flat))
            # Only when the landmark covers most of that piece's landmark voxels.
            if np.argmax(np.bincount(seg[is_landmark & (disc_labeled == component)].flat)) == value:
                found[component] = DISC_LANDMARKS[value]
    if not found:
        raise Unusable(NO_LANDMARK)
    return found


def iterative_label(seg, discs, vertebrae, extra, canal, cord, sacrum, also):
    """Upstream's iterative_label on the default path: number the discs from the landmarks, then the vertebrae."""
    out = np.zeros_like(seg)
    centerline = _centerline(seg, [canal, cord])
    anterior = _anterior(seg, [canal, cord])
    disc_labeled, disc_order, disc_z = _components(seg, [list(discs)], centerline, anterior, NO_DISC)
    vert_labeled, vert_order, vert_z = None, [], []
    if vertebrae:
        vert_labeled, vert_order, vert_z = _components(seg, [[value] for value in vertebrae], centerline, anterior, NO_VERTEBRA)
        if len(vertebrae) > 1:
            # Pieces of the same class in a row are one vertebra.
            kept, previous, previous_class = [], 0, 0
            for label in vert_order:
                cls = seg[vert_labeled == label].flat[0]
                if cls == previous_class:
                    vert_labeled[vert_labeled == label] = previous
                else:
                    kept.append(label)
                    previous, previous_class = label, cls
            vert_labeled = _smaller(vert_labeled)
            vert_order, vert_z = _sorted(vert_labeled, kept, centerline, anterior)
        if extra:
            # Vertebra voxels of no class join the vertebra they touch, from the inferior one up.
            is_extra = np.isin(seg, extra)
            for label in reversed(vert_order):
                near = ndi.binary_dilation(_fill(vert_labeled == label), CROSS)
                vert_labeled[is_extra * near] = label
            vert_order, vert_z = _sorted(vert_labeled, vert_order, centerline, anterior)
    found = _landmarks(seg, disc_labeled, disc_order)

    # Discs: from the top of the image down to the first landmark by the default sizes, then from each landmark down.
    numbers = {}
    for label in [label for label in disc_order if label in found]:
        if not numbers:
            a, b = disc_order.index(label), DISCS_DEFAULT.index(found[label])
            a, b = max(0, a - b), max(0, b - a)
            numbers.update(zip(disc_order[a:], DISCS_DEFAULT[b:]))
        a, b = disc_order.index(label), DISCS_MAX.index(found[label])
        numbers.update(zip(disc_order[a:], DISCS_MAX[b:]))
    for label, value in numbers.items():
        out[disc_labeled == label] = value

    if vert_order:
        items = sorted(zip(vert_z + disc_z, vert_order + disc_order, [True] * len(vert_order) + [False] * len(disc_order)))[::-1]
        z_list, labels, is_vert = (list(column) for column in zip(*items))

        def swap(i, j):
            for column in (z_list, labels, is_vert):
                column[i], column[j] = column[j], column[i]

        # Two discs with no vertebra between them: move a vertebra between them from a pair below, else above.
        for i in range(len(labels) - 1):
            if not is_vert[i] and not is_vert[i + 1]:
                if i < len(labels) - 3 and is_vert[i + 2] and is_vert[i + 3]:
                    swap(i + 1, i + 2)
                elif i > 1 and is_vert[i - 1] and is_vert[i - 2]:
                    swap(i, i - 1)
        if len(labels) > 2 and not is_vert[0] and is_vert[1] and is_vert[2]:
            swap(0, 1)
        if len(labels) > 2 and not is_vert[-1] and is_vert[-2] and is_vert[-3]:
            swap(-1, -2)

        numbered, current = {}, 0
        for i, label, vertebra in zip(range(len(labels)), labels, is_vert):
            if not vertebra:
                if label not in numbers:
                    continue
                current = BELOW[numbers[label]]
                if i > 0 and not numbered:
                    # The vertebrae above the first numbered disc, nearest first, by the default sizes.
                    try:
                        at = VERTEBRAE_DEFAULT.index(current)
                    except ValueError as error:
                        raise Unusable(NO_NUMBERING) from error
                    above = [vert for vert, is_v in zip(labels[i - 1 :: -1], is_vert[i - 1 :: -1]) if is_v]
                    numbered.update(zip(above, VERTEBRAE_DEFAULT[at - 1 :: -1]))
            elif current > 0:
                # A last vertebra with no disc above it is the next level down.
                if i == len(labels) - 1 and i > 0 and is_vert[i - 1] and current != VERTEBRAE_MAX[-1]:
                    numbered[label] = VERTEBRAE_MAX[VERTEBRAE_MAX.index(current) + 1]
                else:
                    numbered[label] = current
        for label, value in numbered.items():
            out[vert_labeled == label] = value

    out[seg == canal] = 2
    out[seg == cord] = 1
    out[seg == sacrum] = 50
    for value, new in also.items():
        out[seg == value] = new
    return out


def _largest(mask):
    if mask.sum() == 0:
        return mask
    pieces, _ = ndi.label(mask, CUBE)
    return pieces == np.bincount(pieces.ravel())[1:].argmax() + 1


def fill_canal(seg):
    """Upstream's fill_canal with canal 2, cord 1 and both largest pieces kept: the canal fills each slice's span."""
    out = seg.copy()
    if 1 in out:
        cord = out == 1
        out[cord & ~_largest(cord)] = 2
    if 2 in out:
        canal = np.isin(out, [1, 2])
        out[canal & ~_largest(canal)] = 0
        x = np.broadcast_to(np.arange(out.shape[0])[..., np.newaxis, np.newaxis], out.shape)
        y = np.broadcast_to(np.arange(out.shape[1])[..., np.newaxis], out.shape)
        canal = np.isin(out, [1, 2])
        min_x = np.min(np.where(canal, x, np.inf), axis=0)[np.newaxis, ...]
        max_x = np.max(np.where(canal, x, -np.inf), axis=0)[np.newaxis, ...]
        min_y = np.min(np.where(canal, y, np.inf), axis=1)[:, np.newaxis, :]
        max_y = np.max(np.where(canal, y, -np.inf), axis=1)[:, np.newaxis, :]
        fill = (min_x <= x) & (x <= max_x) & (min_y <= y) & (y <= max_y)
        out[fill & (out != 1)] = 2
    return out


def alternate_discs(seg):
    """Upstream's extract_alternate: the most superior disc present and every second one after it, by value."""
    values = np.array(range(63, 101))[np.isin(range(63, 101), seg)]
    return np.isin(seg, values[::2]).astype(np.uint8)
