"""Image operations in patient RAS millimetres, independent of application state."""

from __future__ import annotations

from io import BytesIO
from itertools import product
from pathlib import Path
from collections import OrderedDict
import math
import threading

import nibabel as nib
import numpy as np
from PIL import Image
from scipy import ndimage as ndi


_CT_CACHE = OrderedDict()
_CT_CACHE_LOCK = threading.RLock()
_CT_CACHE_BYTES = 1024 ** 3
_SLICE_MASK_CACHE = OrderedDict()
_SLICE_MASK_CACHE_LOCK = threading.RLock()
_SLICE_MASK_CACHE_BYTES = 512 * 1024 ** 2


def _slice_mask(path, ct_image, ct_affine):
    """Cache only immutable display masks, independently of numerical edits.

    Geometry remains part of the key, so reuse cannot bypass reference-grid
    validation. A lock spans loading/validation to avoid concurrent full-volume
    decompression for the same mask. Numerical callers continue using _binary.
    """
    path = Path(path).resolve()
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns,
           tuple(ct_image.shape), np.asarray(ct_affine, dtype=np.float64).tobytes())
    with _SLICE_MASK_CACHE_LOCK:
        if key in _SLICE_MASK_CACHE:
            _SLICE_MASK_CACHE.move_to_end(key)
            return _SLICE_MASK_CACHE[key]
        mask, _, _ = _binary(path, ct_image, ct_affine)
        mask.flags.writeable = False
        # Replace earlier versions of this path rather than retaining stale
        # arrays, while allowing distinct reference grids to validate separately.
        for previous in list(_SLICE_MASK_CACHE):
            if previous[0] == str(path):
                del _SLICE_MASK_CACHE[previous]
        if mask.nbytes <= _SLICE_MASK_CACHE_BYTES:
            while _SLICE_MASK_CACHE and (len(_SLICE_MASK_CACHE) >= 2
                    or sum(value.nbytes for value in _SLICE_MASK_CACHE.values()) + mask.nbytes > _SLICE_MASK_CACHE_BYTES):
                _SLICE_MASK_CACHE.popitem(last=False)
            _SLICE_MASK_CACHE[key] = mask
        return mask


def _ct_array(path, image):
    """Cache immutable decoded CTs, bounded by both bytes and two volumes."""
    path = Path(path).resolve()
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    with _CT_CACHE_LOCK:
        if key in _CT_CACHE:
            _CT_CACHE.move_to_end(key)
            return _CT_CACHE[key]
        values = np.asarray(image.dataobj, dtype=np.float32)
        if not np.isfinite(values).all():
            raise ValueError("Reference CT contains nonfinite intensities")
        values.flags.writeable = False
        if values.nbytes <= _CT_CACHE_BYTES:
            while _CT_CACHE and (len(_CT_CACHE) >= 2 or sum(x.nbytes for x in _CT_CACHE.values())
                                 + values.nbytes > _CT_CACHE_BYTES):
                _CT_CACHE.popitem(last=False)
            _CT_CACHE[key] = values
        return values


def _image(path: Path):
    image = nib.load(str(path))
    if len(image.shape) != 3 or any(n < 1 for n in image.shape):
        raise ValueError("A nonempty, scalar three-dimensional image is required")
    affine = np.asarray(image.affine, dtype=np.float64).copy()
    units = image.header.get_xyzt_units()[0]
    factor = {"unknown": 1.0, "mm": 1.0, "meter": 1000.0, "micron": 0.001}.get(units)
    if factor is None:
        raise ValueError("Unsupported image spatial units")
    affine[:3, :] *= factor
    if (not np.isfinite(affine).all()
            or not np.allclose(affine[3], [0, 0, 0, 1])
            or abs(np.linalg.det(affine[:3, :3])) < 1e-9):
        raise ValueError("Image has an invalid voxel-to-patient transform")
    return image, affine


def _binary(mask_path: Path, ct_image=None, ct_affine=None):
    image, affine = _image(mask_path)
    if ct_image is not None and (image.shape != ct_image.shape
            or not np.allclose(affine, ct_affine, rtol=0, atol=1e-4)):
        raise ValueError("Mask geometry does not match the reference CT")
    values = np.asanyarray(image.dataobj)
    # Integer masks need only range checks: there are no representable integers
    # between zero and one. Avoid constructing a full-volume np.isin workspace.
    if values.dtype.kind not in "buif":
        raise ValueError("Per-structure masks must contain only binary values 0 and 1")
    minimum, maximum = values.min(), values.max()
    if (not np.isfinite(minimum) or not np.isfinite(maximum) or minimum < 0 or maximum > 1
            or (values.dtype.kind == "f" and not np.all((values == 0) | (values == 1)))):
        raise ValueError("Per-structure masks must contain only binary values 0 and 1")
    # NIfTI proxies commonly expose Fortran-order arrays. SciPy's numerical
    # kernels otherwise traverse these with costly strides or make extra copies.
    return np.ascontiguousarray(values, dtype=bool), image, affine


def _corners(low, high):
    return np.asarray(list(product(*zip(low, high))), dtype=np.float64)


def _world(affine, indices):
    return np.asarray(indices) @ affine[:3, :3].T + affine[:3, 3]


def _bounds(shape, affine):
    corners = _world(affine, _corners(np.full(3, -0.5), np.asarray(shape) - 0.5))
    return np.stack((corners.min(axis=0), corners.max(axis=0)))


def _foreground_bounds(mask):
    if not mask.any():
        return None
    axes = [np.flatnonzero(mask.any(axis=tuple(j for j in range(3) if j != axis)))
            for axis in range(3)]
    return np.array([x[0] for x in axes]), np.array([x[-1] for x in axes])


def _save_mask(mask, reference, output_path: Path):
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    header = reference.header.copy()
    header.set_data_dtype(np.uint8)
    header.set_slope_inter(1, 0)
    image = nib.Nifti1Image(np.asarray(mask, dtype=np.uint8), reference.affine, header)
    image.set_qform(reference.get_qform(), int(reference.header["qform_code"]))
    image.set_sform(reference.get_sform(), int(reference.header["sform_code"]))
    # Nibabel chooses compression from the extension, including temporary files.
    suffix = ".nii.gz" if str(output_path).endswith(".gz") else ".nii"
    temporary = output_path.with_name(output_path.name + ".writing" + suffix)
    try:
        nib.save(image, str(temporary))
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)


def geometry(ct_path: Path) -> dict:
    image, affine = _image(ct_path)
    return {
        "shape": list(image.shape),
        "affine_ras": affine.tolist(),
        "spacing": np.linalg.norm(affine[:3, :3], axis=0).tolist(),
        "bounds_ras": _bounds(image.shape, affine).tolist(),
        "center_ras": _world(affine, (np.asarray(image.shape) - 1) / 2).tolist(),
    }


def analyze_mask(ct_path: Path, mask_path: Path, decoded_mask=None) -> dict:
    ct, affine = _image(ct_path)
    if decoded_mask is None:
        mask, _, _ = _binary(mask_path, ct, affine)
    else:
        mask, _, mask_affine = decoded_mask
        if mask.dtype != np.bool_ or mask.shape != ct.shape or not np.allclose(mask_affine, affine, rtol=0, atol=1e-4):
            raise ValueError("Mask geometry does not match the reference CT")
    count = int(np.count_nonzero(mask))
    volume = count * abs(float(np.linalg.det(affine[:3, :3]))) / 1000
    qc = []
    if not count:
        return {"volume_ml": 0.0, "voxel_count": 0, "centroid_ras": None,
                "bounds_ras": None, "qc": ["Empty segmentation"]}
    low, high = _foreground_bounds(mask)
    cropped = np.ascontiguousarray(mask[tuple(slice(int(a), int(b) + 1) for a, b in zip(low, high))])
    center_index = np.asarray(ndi.center_of_mass(cropped), dtype=np.float64) + low
    # A voxel occupies a parallelepiped, not just its centre. Project its half
    # widths onto the patient axes; this remains correct for oblique images.
    selected = tuple(axis + offset for axis, offset in zip(np.nonzero(cropped), low))
    world_min = np.full(3, np.inf)
    world_max = np.full(3, -np.inf)
    for axis in range(3):
        values = (affine[axis, 0] * selected[0]
                  + affine[axis, 1] * selected[1]
                  + affine[axis, 2] * selected[2] + affine[axis, 3])
        half_width = np.abs(affine[axis, :3]).sum() / 2
        world_min[axis] = values.min() - half_width
        world_max[axis] = values.max() + half_width
    _, components = ndi.label(cropped)
    if components > 1:
        qc.append(f"{components} disconnected components; this can be expected anatomy")
    if np.any(low == 0) or np.any(high == np.asarray(mask.shape) - 1):
        qc.append("Segmentation touches the scan boundary; inspect truncated anatomy")
    return {"volume_ml": volume, "voxel_count": count,
            "centroid_ras": _world(affine, center_index).tolist(),
            "bounds_ras": [world_min.tolist(), world_max.tolist()], "qc": qc}


def _point(value, name="Point"):
    array = np.asarray(value, dtype=np.float64)
    if array.shape != (3,) or not np.isfinite(array).all():
        raise ValueError(f"{name} must contain three finite RAS coordinates")
    return array


def _voxel(affine, shape, ras):
    point = _point(ras)
    index = np.linalg.solve(affine[:3, :3], point - affine[:3, 3])
    voxel = np.floor(index + 0.5).astype(np.int64)
    if np.any(voxel < 0) or np.any(voxel >= shape):
        raise ValueError("Point lies outside the reference CT")
    return voxel


def refine_mask(ct_path: Path, mask_path: Path, output_path: Path, operation: dict) -> dict:
    if Path(output_path).resolve() in {Path(ct_path).resolve(), Path(mask_path).resolve()}:
        raise ValueError("A refinement must write a new mask revision")
    ct, affine = _image(ct_path)
    mask, _, _ = _binary(mask_path, ct, affine)
    result = mask.copy()
    kind = operation.get("type")
    if kind in {"keep_component", "remove_component"}:
        voxel = _voxel(affine, mask.shape, operation.get("seed_ras"))
        if not mask[tuple(voxel)]:
            raise ValueError("The seed must be inside the selected structure")
        labels, _ = ndi.label(mask)  # six-connected in three dimensions
        component = labels == labels[tuple(voxel)]
        result = component if kind == "keep_component" else mask & ~component
    elif kind in {"keep_region", "erase_region"}:
        bounds = np.asarray(operation.get("bounds_ras"), dtype=np.float64)
        if bounds.shape != (2, 3) or not np.isfinite(bounds).all() or np.any(bounds[0] >= bounds[1]):
            raise ValueError("Region bounds must be increasing finite RAS corners")
        index_corners = _world(np.linalg.inv(affine), _corners(*bounds))
        low = np.maximum(0, np.floor(index_corners.min(axis=0)).astype(int))
        high = np.minimum(mask.shape, np.ceil(index_corners.max(axis=0)).astype(int) + 1)
        region = np.zeros(mask.shape, dtype=bool)
        if np.all(high > low):
            indices = np.ogrid[tuple(slice(int(a), int(b)) for a, b in zip(low, high))]
            selected = np.ones(tuple(high - low), dtype=bool)
            for axis in range(3):
                coordinates = sum(affine[axis, j] * indices[j] for j in range(3)) + affine[axis, 3]
                selected &= (coordinates >= bounds[0, axis] - 1e-7) & (coordinates <= bounds[1, axis] + 1e-7)
            region[tuple(slice(int(a), int(b)) for a, b in zip(low, high))] = selected
        result = mask & (region if kind == "keep_region" else ~region)
    else:
        raise ValueError("Unknown refinement operation")
    changed = int(np.count_nonzero(result != mask))
    if not changed:
        raise ValueError("The operation does not change the selected mask")
    if not result.any():
        raise ValueError("The operation would remove the entire structure")
    voxel_ml = abs(float(np.linalg.det(affine[:3, :3]))) / 1000
    _save_mask(result, ct, output_path)
    return {"changed_voxels": changed, "before_volume_ml": float(mask.sum()) * voxel_ml,
            "after_volume_ml": float(result.sum()) * voxel_ml}


def probe(ct_path: Path, ras) -> dict:
    image, affine = _image(ct_path)
    voxel = _voxel(affine, image.shape, ras)
    hu = float(_ct_array(ct_path, image)[tuple(voxel)])
    if not math.isfinite(hu):
        raise ValueError("The selected voxel has no finite intensity")
    return {"hu": hu, "voxel": voxel.tolist()}


def _reslice(values, voxel_from_pixel, shape, order, cval):
    """Sample a patient plane without allocating three full coordinate grids.

    A plane parallel to a voxel axis needs only one or two native slices. Blend
    these in float64 before the 2D interpolation, so the intermediate operation
    does not round CT intensities. This substantially improves sagittal access
    to NIfTI's usual column-major arrays. Fully oblique planes retain the full
    three-dimensional patient transform and interpolation.
    """
    # Output arrays are indexed [row, column], whereas the public transform
    # maps [column, row, plane-depth] pixel coordinates to patient space.
    matrix = voxel_from_pixel[:3, [1, 0]]
    offset = voxel_from_pixel[:3, 3]
    fixed = np.flatnonzero(np.all(matrix == 0, axis=1))
    if len(fixed) == 1:
        axis = int(fixed[0])
        position = float(offset[axis])
        if not 0 <= position <= values.shape[axis] - 1:
            return np.full(shape, cval, dtype=values.dtype)
        indices = [slice(None)] * 3
        if order == 0:
            indices[axis] = int(math.floor(position + 0.5))
            plane = values[tuple(indices)]
        else:
            lower = int(math.floor(position))
            fraction = position - lower
            indices[axis] = lower
            plane = values[tuple(indices)]
            if fraction:
                plane = plane.astype(np.float64) * (1 - fraction)
                indices[axis] = lower + 1
                plane += values[tuple(indices)].astype(np.float64) * fraction
        keep = [i for i in range(3) if i != axis]
        return ndi.affine_transform(np.ascontiguousarray(plane), matrix[keep], offset[keep],
                                    output_shape=shape, output=values.dtype, order=order,
                                    mode="constant", cval=cval, prefilter=False)
    return ndi.affine_transform(values, matrix, offset, output_shape=shape,
                                order=order, mode="constant", cval=cval, prefilter=False)


def slice_geometry(shape, affine, plane, position_mm, *, native_axis=None, native_index=None):
    """Pixel-centre geometry shared by PNG and exact-HU frame delivery.

    Native editing planes keep one voxel axis fixed even on oblique/reflected
    scans. They never turn an anatomical MPR rectangle into a native-axis box.
    """
    if plane not in {"axial", "coronal", "sagittal"}:
        raise ValueError("Plane must be axial, coronal or sagittal")
    if native_axis is not None or native_index is not None:
        if (isinstance(native_axis, bool) or not isinstance(native_axis, int) or native_axis not in (0, 1, 2)
                or isinstance(native_index, bool) or not isinstance(native_index, int)
                or not 0 <= native_index < shape[native_axis]):
            raise ValueError("A native slice requires a valid voxel axis and integer index")
        u, v = [axis for axis in range(3) if axis != native_axis]
        spacing = np.linalg.norm(affine[:3, :3], axis=0)
        extent = [float(shape[u] * spacing[u]), float(shape[v] * spacing[v])]
        step = max(float(min(spacing[u], spacing[v])), max(extent) / 768)
        width, height = [max(1, int(math.ceil(value / step))) for value in extent]
        mapping = np.zeros((4, 4), dtype=np.float64)
        mapping[3, 3] = 1
        mapping[u, 0], mapping[v, 1] = shape[u] / width, shape[v] / height
        mapping[u, 3], mapping[v, 3] = -.5 + shape[u] / width / 2, -.5 + shape[v] / height / 2
        mapping[native_axis, 2], mapping[native_axis, 3] = 1, native_index
        return {"width": width, "height": height, "pixel_to_ras": (affine @ mapping).tolist(),
                "position_mm": float(native_index), "plane": plane, "extent_mm": extent,
                "native": {"axis": native_axis, "index": native_index}}, mapping
    if not math.isfinite(float(position_mm)):
        raise ValueError("Slice position must be finite")
    # (fixed axis, horizontal axis, vertical axis). All screen axes decrease RAS.
    fixed, horizontal, vertical = {"axial": (2, 0, 1), "coronal": (1, 0, 2),
                                   "sagittal": (0, 1, 2)}[plane]
    bounds = _bounds(shape, affine)
    if not bounds[0, fixed] <= position_mm <= bounds[1, fixed]:
        raise ValueError("Slice position lies outside the reference CT")
    extent = bounds[1] - bounds[0]
    step = max(float(np.linalg.norm(affine[:3, :3], axis=0).min()),
               float(max(extent[horizontal], extent[vertical])) / 768)
    width = max(1, int(math.ceil(extent[horizontal] / step)))
    height = max(1, int(math.ceil(extent[vertical] / step)))
    pixel_to_ras = np.zeros((4, 4), dtype=np.float64)
    pixel_to_ras[3, 3] = 1
    pixel_to_ras[horizontal, 0] = -extent[horizontal] / width
    pixel_to_ras[vertical, 1] = -extent[vertical] / height
    pixel_to_ras[fixed, 2] = 1
    pixel_to_ras[horizontal, 3] = bounds[1, horizontal] - extent[horizontal] / width / 2
    pixel_to_ras[vertical, 3] = bounds[1, vertical] - extent[vertical] / height / 2
    pixel_to_ras[fixed, 3] = position_mm
    return {"width": width, "height": height, "pixel_to_ras": pixel_to_ras.tolist(),
            "position_mm": float(position_mm), "plane": plane,
            "extent_mm": [float(extent[horizontal]), float(extent[vertical])]}, np.linalg.inv(affine) @ pixel_to_ras


def slice_image(ct_path: Path, mask_path_or_none: Path | None, plane: str,
                position_mm: float, window: float, level: float,
                compare_mask_path: Path | None = None, *, overlays=None,
                native_axis=None, native_index=None) -> dict:
    """Reslice patient planes or exact native editing planes on the source grid."""
    image, affine = _image(ct_path)
    if not all(math.isfinite(float(x)) for x in (window, level)) or window <= 0:
        raise ValueError("Slice level must be finite; window must be positive")
    meta, voxel_from_pixel = slice_geometry(image.shape, affine, plane, position_mm,
                                           native_axis=native_axis, native_index=native_index)
    width, height = meta["width"], meta["height"]
    values = _ct_array(ct_path, image)
    intensity = _reslice(values, voxel_from_pixel, (height, width), order=1, cval=-1024)
    grayscale = np.rint(np.clip((intensity - (level - window / 2)) / window, 0, 1) * 255).astype(np.uint8)
    rgb = np.repeat(grayscale[..., None], 3, axis=2).astype(np.float32)

    def sample_mask(path):
        mask = _slice_mask(path, image, affine)
        return _reslice(mask, voxel_from_pixel, (height, width), order=0, cval=0)

    def overlay(selected, color, alpha=0.22):
        nonlocal rgb
        contour = selected & ~ndi.binary_erosion(selected)
        rgb[selected] = rgb[selected] * (1 - alpha) + np.asarray(color) * alpha
        rgb[contour] = np.asarray(color)

    if overlays is None:
        current = sample_mask(mask_path_or_none) if mask_path_or_none else None
        if current is not None:
            overlay(current, (56, 189, 248))
        if compare_mask_path:
            if current is None:
                raise ValueError("A current mask is required for comparison")
            previous = sample_mask(compare_mask_path)
            overlay(previous & ~current, (244, 63, 94), 0.45)
            overlay(current & ~previous, (34, 197, 94), 0.45)
    else:
        # Each structure keeps its own binary layer, including overlapping
        # structures. Selected order is also the deterministic paint order.
        for structure_id, current_path, previous_path, color in overlays:
            current = sample_mask(current_path)
            overlay(current, tuple(int(color[i:i + 2], 16) for i in (1, 3, 5)))
            if previous_path:
                previous = sample_mask(previous_path)
                overlay(previous & ~current, (244, 63, 94), 0.45)
                overlay(current & ~previous, (34, 197, 94), 0.45)
    buffer = BytesIO()
    # Level 3 is lossless and closely matches level 6's size, with less work on
    # the interactive path (measured 1.6% larger and 25% faster on sagittal CT).
    Image.fromarray(np.rint(rgb).astype(np.uint8)).save(buffer, format="PNG", compress_level=3)
    meta["overlay_colors"] = {"current": "#38bdf8", "added": "#22c55e", "removed": "#f43f5e"}
    if overlays is not None:
        meta["overlays"] = [{"id": layer[0], "color": layer[3]} for layer in overlays]
    return {"image": buffer.getvalue(), "meta": meta}


def presentation_color(structure):
    """Match web/src/lib/appearance.ts for server-rendered CT overlays."""
    colors = {
        "lung_upper_lobe_right": "#f29919", "lung_middle_lobe_right": "#33b34d", "lung_lower_lobe_right": "#1a80e6",
        "lung_upper_lobe_left": "#f27fbf", "lung_lower_lobe_left": "#7f66e6", "heart": "#d95772",
        "liver": "#cc6619", "spleen": "#8019e6", "kidney_left": "#d86a6a", "kidney_right": "#d86a6a",
        "kidney_cyst_left": "#edbc85", "kidney_cyst_right": "#edbc85", "pancreas": "#f29919",
        "gallbladder": "#80cc19", "stomach": "#f27fbf", "small_bowel": "#c39ad9", "duodenum": "#d9a3c6",
        "colon": "#9c83cc", "urinary_bladder": "#e6c86a", "adrenal_gland_left": "#d9aa59",
        "adrenal_gland_right": "#d9aa59", "autochthon_left": "#a47782", "autochthon_right": "#a47782",
        "iliopsoas_left": "#b55c73", "iliopsoas_right": "#b55c73", "prostate": "#d9909d",
        "spinal_cord": "#dfcfa5", "esophagus": "#c881aa", "thyroid_gland": "#bd77bf",
    }
    name = structure["id"].rsplit(":", 1)[-1].lower()
    group = structure.get("group", "").lower()
    if group in {"arteries", "veins", "bones"}:
        return {"arteries": "#ed2222", "veins": "#224ce6", "bones": "#d8d2c5"}[group]
    if group == "airways":
        return "#cccccc" if "wall" in name else "#eeeeee"
    color = colors.get(name, structure.get("color", "#38bdf8"))
    if not isinstance(color, str) or len(color) != 7 or color[0] != "#":
        raise ValueError("Structure overlay requires a six-digit hex color")
    try:
        int(color[1:], 16)
    except ValueError as exc:
        raise ValueError("Structure overlay requires a six-digit hex color") from exc
    return color
