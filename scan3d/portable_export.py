"""Bounded, self-contained exports of explicitly selected rendering layers.

Scalar images and labels use the existing lossless native review slabs. Export preparation
touches only derivative caches; original scans, masks and revision records stay
immutable. Base64 is streamed from files rather than assembling a huge HTML
string or decoding an entire set of masks at once.
"""

from __future__ import annotations

from dataclasses import dataclass
import base64
import math
import os
from pathlib import Path
import re
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

from .domain import Modality, PhotometricInterpretation


MIB = 1024**2
MAX_CT_BYTES = 512 * MIB
MAX_MASK_BYTES = 192 * MIB
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_WORKING_BYTES = 4 * 1024**3
MAX_MODEL_BYTES = 128 * MIB
MAX_HTML_BYTES = 512 * MIB
MAX_BUNDLE_BYTES = 16 * MIB
MAX_METADATA_BYTES = 8 * MIB
_BASE64_READ_BYTES = 48 * 1024  # A multiple of three: only the last block pads.

# IDs from the pinned generated Slicer preset catalog, plus the app's aliases.
# uCT-Skull is left out because the app does not offer it. A regression test
# checks this set against the generated frontend data.
SUPPORTED_VOLUME_PRESETS = frozenset(
    {
        "CT-AAA",
        "CT-AAA2",
        "CT-Bone",
        "CT-Bones",
        "CT-Cardiac",
        "CT-Cardiac2",
        "CT-Cardiac3",
        "CT-Chest-Contrast-Enhanced",
        "CT-Chest-Vessels",
        "CT-Coronary-Arteries",
        "CT-Coronary-Arteries-2",
        "CT-Coronary-Arteries-3",
        "CT-Cropped-Volume-Bone",
        "CT-Fat",
        "CT-Liver-Vasculature",
        "CT-Lung",
        "CT-MIP",
        "CT-Muscle",
        "CT-Pulmonary-Arteries",
        "CT-Soft-Tissue",
        "CT-Air",
        "CT-X-ray",
        "MR-Angio",
        "MR-Default",
        "MR-MIP",
        "MR-T2-Brain",
        "DTI-FA-Brain",
        "US-Fetal",
        "uCT-Bone-8bit",
        "uCT-Bone-16bit",
        "angiography",
        "bone",
        "soft-tissue",
        "lung",
        "CT-Cinematic-Heart",
        "MR-SignalWindow",
    }
)


class ExportTooLarge(ValueError):
    pass


class PortableInput(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)


StructureId = Annotated[str, Field(min_length=1, max_length=256)]
Unit = Annotated[float, Field(ge=0, le=1)]
Point3 = tuple[float, float, float]


class ExportLayers(PortableInput):
    segmentation: StrictBool = True
    ct_volume: StrictBool = False
    ct_plane: StrictBool = False

    @model_validator(mode="after")
    def has_layer(self):
        if not (self.segmentation or self.ct_volume or self.ct_plane):
            raise ValueError("Select at least one layer to export")
        return self


class ExportDisplay(PortableInput):
    visible: StrictBool = True
    opacity: Unit = 0.5
    color: str | None = Field(default=None, pattern=r"^#[a-fA-F0-9]{6}$")


class ExportCinematic(PortableInput):
    myocardiumHU: float = Field(default=80, ge=-500, le=1500)
    bloodHU: float = Field(default=400, ge=-400, le=3000)
    shadows: float = Field(default=1.5, ge=0, le=2)
    shadowReach: float = Field(default=0.3, ge=0, le=1)

    @model_validator(mode="after")
    def contrast(self):
        if self.bloodHU - self.myocardiumHU < 100:
            raise ValueError("Cinematic blood-pool HU must be at least 100 HU above myocardium")
        return self


class ExportSignalWindow(PortableInput):
    window: float = Field(gt=0, strict=True)
    level: float = Field(strict=True)
    invert: StrictBool = False


class ExportVolume(PortableInput):
    preset: str = Field(default="angiography", min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    shift: float = Field(default=0, ge=-500, le=500)
    density: float = Field(default=1, ge=0.1, le=3)
    quality: Literal["auto", "detail"] = "auto"
    crop: tuple[tuple[Unit, Unit, Unit], tuple[Unit, Unit, Unit]] = ((0, 0, 0), (1, 1, 1))
    cinematic: ExportCinematic = Field(default_factory=ExportCinematic)
    signal_window: ExportSignalWindow | None = Field(default=None, exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def positive_crop(self):
        if self.preset not in SUPPORTED_VOLUME_PRESETS:
            raise ValueError("Select a supported volume appearance preset")
        if any(low >= high for low, high in zip(*self.crop)):
            raise ValueError("Volume crop must have positive extent on every axis")
        return self


class ExportVolumeMask(PortableInput):
    enabled: StrictBool = False
    mode: Literal["inside", "outside"] = "inside"
    ids: list[StructureId] = Field(default_factory=list, max_length=128)
    margin_mm: float = Field(default=0, ge=0, le=5)

    @model_validator(mode="after")
    def canonical_margin(self):
        self.margin_mm = math.floor(self.margin_mm * 1000 + 0.5) / 1000
        return self


class ExportPlane(PortableInput):
    plane: Literal["axial", "coronal", "sagittal"] = "axial"
    axis: Annotated[StrictInt, Field(ge=0, le=2)] = 2
    index: Annotated[StrictInt, Field(ge=0)] = 0
    window: float = Field(default=400, gt=0, le=1_000_000)
    level: float = Field(default=40, ge=-10_000_000, le=10_000_000)
    overlays: list[StructureId] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def native_axis(self):
        if self.axis != {"axial": 2, "coronal": 1, "sagittal": 0}[self.plane]:
            raise ValueError("CT plane and native axis must agree")
        return self


class ExportCamera(PortableInput):
    position_ras: Point3
    target_ras: Point3
    up_ras: Point3
    zoom: float = Field(default=1, ge=0.25, le=20)
    height_mm: float = Field(gt=0, le=10_000_000)
    name: str | None = Field(default=None, max_length=32, pattern=r"^[A-Za-z_-]+$")

    @model_validator(mode="after")
    def finite_camera(self):
        if any(abs(value) > 10_000_000 for point in (self.position_ras, self.target_ras) for value in point):
            raise ValueError("Camera coordinates exceed the supported physical range")
        norm = math.sqrt(sum(value * value for value in self.up_ras))
        if not math.isfinite(norm) or norm < 1e-9:
            raise ValueError("Camera up direction must be finite and nonzero")
        if sum((left - right) ** 2 for left, right in zip(self.position_ras, self.target_ras)) < 1e-12:
            raise ValueError("Camera position and target must differ")
        self.up_ras = tuple(value / norm for value in self.up_ras)
        return self


class ExportState(PortableInput):
    display: dict[StructureId, ExportDisplay] = Field(default_factory=dict, max_length=4096)
    selected: str = Field(default="", max_length=256)
    segmentation_opacity: Unit = 1
    volume: ExportVolume = Field(default_factory=ExportVolume)
    volume_mask: ExportVolumeMask = Field(default_factory=ExportVolumeMask)
    plane: ExportPlane = Field(default_factory=ExportPlane)
    camera: ExportCamera | None = None


class ExportRequest(PortableInput):
    layers: ExportLayers = Field(default_factory=ExportLayers)
    state: ExportState = Field(default_factory=ExportState)


def _json(value):
    import json

    # Anatomical/custom names are data, including a literal </script> sequence.
    return json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":")).replace("<", "\\u003c").encode()


@dataclass(frozen=True)
class EncodedFile:
    path: Path
    byte_length: int

    @property
    def encoded_length(self):
        return 4 * ((self.byte_length + 2) // 3)

    def chunks(self):
        consumed = 0
        with self.path.open("rb") as stream:
            while data := stream.read(_BASE64_READ_BYTES):
                consumed += len(data)
                if consumed > self.byte_length:
                    raise ValueError("An export artifact changed while downloading; retry the export")
                yield base64.b64encode(data)
        if consumed != self.byte_length:
            raise ValueError("An export artifact changed while downloading; retry the export")


@dataclass(frozen=True)
class ExportPlan:
    parts: tuple[bytes | EncodedFile, ...]
    byte_length: int

    def chunks(self):
        for part in self.parts:
            if isinstance(part, EncodedFile):
                yield from part.chunks()
            else:
                yield part


def _portable_manifest(revision, case_id, image_context=None):
    # Use an allowlist, including nested structure fields. Raw DICOM labels,
    # acquisition IDs/dates, paths, operations and runtime/config are absent.
    fields = ("id", "name", "task", "group", "color", "volume_ml", "voxel_count", "centroid_ras", "bounds_ras", "relationship")
    structures = []
    for item in revision["structures"]:
        entry = {**{key: item[key] for key in fields if key in item}, "qc": []}
        if entry.get("relationship"):
            entry["relationship"] = {key: entry["relationship"][key] for key in ("kind", "parent_id", "exclusive_with") if key in entry["relationship"]}
        structures.append(entry)
    geometry = {key: revision["geometry"][key] for key in ("shape", "affine_ras", "spacing", "bounds_ras", "center_ras")}
    result = dict(
        label=f"Scan3D case {case_id[:8]}",
        review_status=revision.get("review_status", "unreviewed"),
        structures=structures,
        geometry=geometry,
        synthetic=bool(revision.get("synthetic")),
        source="synthetic_demo" if revision.get("synthetic") else "portable_export",
    )
    if revision.get("pipeline_version"):
        result["pipeline_version"] = revision["pipeline_version"]
    provenance = revision.get("inference_manifest") or {}
    if provenance:
        result["model_provenance"] = {key: provenance[key] for key in ("engine", "engine_version", "resolution") if key in provenance}
        result["model_provenance"]["tasks"] = [item["task"] for item in provenance.get("tasks", []) if "task" in item]
    if image_context is not None:
        result["image_context"] = image_context
    return result


def _image_context(revision):
    """Resolve a bound source, exporting display metadata without acquisition IDs.

    Surface-only legacy CT exports intentionally need no original image file.
    New revisions carry a validated image snapshot, including MRI display policy.
    """
    if revision.get("image_asset") is None:
        return None
    from .image_assets import reference_image
    from .modalities import normalize_presentation

    asset = reference_image(revision)
    return {
        "reference_image_id": asset["id"],
        "modality": asset["modality"],
        "scalar": {key: asset["scalar"][key] for key in ("dtype", "slope", "intercept", "units")},
        "presentation": normalize_presentation(asset["presentation"]),
    }


def _state_for_image(state, image_context):
    """Derive missing MR display defaults without mutating the caller's state."""
    if image_context is None or image_context["modality"] != Modality.MR:
        return state
    policy = image_context["presentation"]
    volume = state.volume
    if "preset" in volume.model_fields_set and volume.preset != "MR-SignalWindow":
        raise ValueError("MR exports require the MR signal-window appearance; CT appearances use HU calibration")
    if volume.shift != 0:
        raise ValueError("MR signal-window appearances do not support CT HU shifts")
    signal_window = volume.signal_window or ExportSignalWindow(
        window=policy["default_window"], level=policy["default_level"], invert=policy["photometric_interpretation"] == PhotometricInterpretation.MONOCHROME1
    )
    plane_defaults = {key: policy[f"default_{key}"] for key in ("window", "level") if key not in state.plane.model_fields_set}
    return state.model_copy(
        update={
            "volume": volume.model_copy(update={"preset": "MR-SignalWindow", "signal_window": signal_window}),
            "plane": state.plane.model_copy(update=plane_defaults),
        }
    )


def _file(path, maximum, label):
    path = Path(path)
    size = path.stat().st_size
    if size > maximum:
        raise ExportTooLarge(f"{label} exceeds the portable export size limit")
    return EncodedFile(path, size)


def _source_preflight(path, *, ct=False):
    from . import imaging

    image, affine = imaging._image(Path(path))
    voxels = math.prod(image.shape)
    itemsize = image.get_data_dtype().itemsize
    if image.get_data_dtype().kind not in "biuf":
        raise ValueError("Portable CT and masks must contain real scalar values")
    if voxels * max(itemsize, 1) > MAX_SOURCE_BYTES:
        raise ExportTooLarge("The source volume exceeds the portable export working-memory limit")
    working = voxels * (itemsize + (4 if ct else 2))
    available = _available_memory()
    if working > min(MAX_WORKING_BYTES, available // 4):
        raise ExportTooLarge("The workstation has insufficient available memory for this export; retry later or export segmentation alone")
    # The native transport keeps exact 16-bit CT samples/scaling, using scaled
    # float32 for other storage types. Check before any dense voxel read.
    if ct:
        raw_type = image.get_data_dtype().newbyteorder("=").name
        output_bytes = voxels * (2 if raw_type in ("int16", "uint16") else 4)
        if output_bytes > MAX_CT_BYTES:
            raise ExportTooLarge("The full CT exceeds the 512 MiB portable viewer limit; export segmentation alone")
    return image, affine


def _available_memory():
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
        return os.sysconf("SC_AVPHYS_PAGES") * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, AttributeError):
        return MAX_WORKING_BYTES * 4


def prepare_export(revision, case_id, settings, request: ExportRequest, *, legacy=False):
    """Validate budgets/references and return a ready-to-stream HTML plan."""
    from . import review_volume
    from .image_assets import reference_image
    import numpy as np

    layers = request.layers
    image_context = _image_context(revision)
    state = _state_for_image(request.state, image_context)
    structures = {item["id"]: item for item in revision["structures"]}
    ids = set(structures)
    referenced = set(state.display) | set(state.plane.overlays) | set(state.volume_mask.ids)
    if state.selected:
        referenced.add(state.selected)
    if referenced - ids:
        raise ValueError("Export settings refer to a segment outside this saved revision; save pending edits first")
    if layers.segmentation and not structures:
        raise ValueError("This CT has no segmentation surfaces to export")
    bundle = settings.web_dir / "offline-viewer.js"
    if not bundle.is_file():
        raise FileNotFoundError("The portable viewer must be built before exporting")
    _file(bundle, MAX_BUNDLE_BYTES, "The portable viewer bundle")
    script = re.sub(r"</script", r"<\\/script", bundle.read_text(), flags=re.I).encode()
    css_path = settings.web_dir / "offline-viewer.css"
    css = b""
    if css_path.is_file():
        _file(css_path, MAX_BUNDLE_BYTES, "The portable viewer styles")
        css = re.sub(r"</style", r"<\\/style", css_path.read_text(), flags=re.I).encode()
    model = _file(Path(revision["directory"]) / "model.glb", MAX_MODEL_BYTES, "The segmentation model") if layers.segmentation else None
    ct = None
    masks = []
    cache = settings.data_dir / "review-cache"
    if layers.ct_volume or layers.ct_plane:
        image, affine = _source_preflight(revision["ct_file"], ct=True)
        if list(image.shape) != revision["geometry"]["shape"] or not np.allclose(affine, revision["geometry"]["affine_ras"], rtol=0, atol=1e-4):
            raise ValueError("The CT geometry changed; the saved revision cannot be exported")
        if layers.ct_plane and state.plane.index >= image.shape[state.plane.axis]:
            raise ValueError("The exported CT plane lies outside the reference CT")
        # Exact plane overlays remain categorical native masks. The volume
        # clipping layer uses one bounded presentation-only union instead.
        mask_requests = [(identity, 0.0) for identity in state.plane.overlays] if layers.ct_plane else []
        union_ids = state.volume_mask.ids if layers.ct_volume and state.volume_mask.enabled else []
        mask_requests = list(dict.fromkeys(mask_requests))
        mask_ids = list(dict.fromkeys([*(identity for identity, _ in mask_requests), *union_ids]))
        if len(mask_requests) > 256:
            raise ValueError("A portable viewer supports at most 256 embedded exact and volume-margin masks")
        # All source headers are checked before the first dense preparation.
        for identity in mask_ids:
            _source_preflight(structures[identity]["mask_file"])
        reference_image_id = image_context["reference_image_id"] if image_context else None
        # Each entry is keyed by its image, so no other case can share it. A legacy revision resolves the image here.
        cache_image_id = reference_image_id or reference_image(revision)["id"]
        ct = review_volume.scalar_volume(
            revision["ct_file"],
            cache,
            "full",
            presentation=image_context["presentation"] if image_context else None,
            reference_image_id=cache_image_id,
            case_id=case_id,
        )
        if image_context is not None:
            ct = {**ct, **image_context}
        if ct["byte_length"] > MAX_CT_BYTES:
            raise ExportTooLarge("The CT exceeds the portable viewer memory limit")
        mask_bytes = 0
        for identity, margin_mm in mask_requests:
            descriptor = review_volume.mask_volume(
                revision["ct_file"],
                structures[identity]["mask_file"],
                cache,
                "full",
                margin_mm=margin_mm,
                reference_image_id=cache_image_id,
                case_id=case_id,
            )
            if reference_image_id is not None:
                descriptor = {**descriptor, "reference_image_id": reference_image_id}
            mask_bytes += descriptor["byte_length"]
            if mask_bytes > MAX_MASK_BYTES:
                raise ExportTooLarge("Selected mask crops exceed the 192 MiB portable viewer limit; select fewer masks")
            masks.append((review_volume.volume_mask_alias(identity, margin_mm), descriptor))
        if union_ids:
            from .volume_mask_union import volume_mask_union

            descriptor = volume_mask_union(
                revision["ct_file"],
                [(identity, structures[identity]["mask_file"]) for identity in union_ids],
                cache,
                state.volume_mask.margin_mm,
                reference_image_id=cache_image_id,
                case_id=case_id,
            )
            if reference_image_id is None:
                # The union stores its image in the descriptor, and a legacy bundle embeds none, as before.
                descriptor = {**descriptor, "reference_image_id": None}
            if mask_bytes + descriptor["byte_length"] > MAX_MASK_BYTES:
                raise ExportTooLarge("Selected mask crops exceed the 192 MiB portable viewer limit; select fewer plane overlays")
            masks.append((f"volume-mask-union:{descriptor['key']}", descriptor))

    manifest = _json(_portable_manifest(revision, case_id, image_context))
    state_json = _json(state.model_dump(mode="json", exclude_none=True))
    if len(manifest) + len(state_json) > MAX_METADATA_BYTES:
        raise ExportTooLarge("The viewer metadata exceeds the portable export size limit")
    policy = (
        "default-src 'none'; script-src 'unsafe-inline' blob:; worker-src blob:; "
        "style-src 'unsafe-inline'; img-src data: blob:; font-src data:; "
        "connect-src 'none'; object-src 'none'; base-uri 'none'"
    )
    parts = [
        b'<!doctype html><html lang="en"><head><meta charset="utf-8">'
        b'<meta name="viewport" content="width=device-width,initial-scale=1">'
        b'<meta name="referrer" content="no-referrer">'
        + f'<meta http-equiv="Content-Security-Policy" content="{policy}">'.encode()
        + b"<title>3DScan portable viewer</title><style>"
        + css
        + b'</style></head><body><div id="app"></div><script>'
        + script
        + b"</script><script>Scan3DOffline.initOffline(",
        manifest,
        b",",
    ]
    if model:
        parts.extend((b'"', model, b'"'))
    else:
        parts.append(b"null")
    if not legacy:
        parts.extend((b',{"schema_version":1,"layers":', _json(layers.model_dump()), b',"state":', state_json))

        def embedded_volume(descriptor, identity=None):
            # Native cached descriptors contain no paths/URLs. Retain only the
            # public transport schema, with chunk data separate from metadata.
            value = {
                key: descriptor[key]
                for key in (
                    "schema_version",
                    "key",
                    "kind",
                    "dtype",
                    "quality",
                    "approximate",
                    "preview_quantization_hu",
                    "preview_quantization_error",
                    "reference_image_id",
                    "modality",
                    "scalar",
                    "presentation",
                    "reference_geometry",
                    "shape",
                    "affine_ras",
                    "geometry",
                    "slope",
                    "intercept",
                    "byte_length",
                    "chunk_depth",
                    "chunks",
                    "origin_ijk",
                    "empty",
                    "display_margin_mm",
                    "presentation_only",
                    "volume_mask_union",
                    "selected_structure_ids",
                    "pooling",
                    "native_crop_origin_ijk",
                    "native_crop_shape",
                )
                if key in descriptor
            }
            value["chunks"] = [{key: chunk[key] for key in ("index", "start_k", "depth", "byte_offset", "byte_length")} for chunk in descriptor["chunks"]]
            opening = b'{"descriptor":' if identity is None else b'{"id":' + _json(identity) + b',"descriptor":'
            parts.extend((opening, _json(value), b',"chunks":['))
            for index, chunk in enumerate(descriptor["chunks"]):
                if index:
                    parts.append(b",")
                file = review_volume.chunk_path(cache, descriptor, descriptor["key"], chunk["index"])
                parts.extend((b'{"index":' + str(chunk["index"]).encode() + b',"gzip_base64":"', _file(file, MAX_HTML_BYTES, "A volume slab"), b'"}'))
            parts.append(b"]}")

        if ct is not None:
            parts.append(b',"ct":')
            embedded_volume(ct)
        parts.append(b',"masks":[')
        for index, (identity, descriptor) in enumerate(masks):
            if index:
                parts.append(b",")
            embedded_volume(descriptor, identity)
        parts.append(b"]}")
    parts.append(b");</script></body></html>")
    length = sum(part.encoded_length if isinstance(part, EncodedFile) else len(part) for part in parts)
    if length > MAX_HTML_BYTES:
        raise ExportTooLarge("The portable HTML exceeds the 512 MiB download limit; export fewer layers or masks")
    return ExportPlan(tuple(parts), length)
