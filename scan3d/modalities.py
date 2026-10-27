"""Typed modality adapters and immutable scalar-image presentation policies.

Supporting an image representation does not imply that every acquisition can be
imported or segmented. Adapters validate their supported acquisition subset.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from pathlib import Path
from typing import Any, ClassVar, Protocol, TYPE_CHECKING

import nibabel as nib
import numpy as np

from .domain import ImageCapability, ImageKind, Modality, PhotometricInterpretation

if TYPE_CHECKING:
    from pydicom.dataset import Dataset


def _text(value: str, field: str) -> None:
    if not isinstance(value, str) or not value.strip() or len(value) > 80:
        raise ValueError(f"Invalid image presentation {field}")


def _number(value: float, field: str) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Image presentation {field} must be finite")


@dataclass(frozen=True, slots=True)
class ScalarPresentation:
    units: str
    background_value: float
    default_window: float
    default_level: float
    photometric_interpretation: PhotometricInterpretation
    preset_group: str
    preview_quantization_step: float

    def __post_init__(self) -> None:
        _text(self.units, "units")
        _text(self.preset_group, "preset_group")
        _number(self.background_value, "background_value")
        _number(self.default_window, "default_window")
        _number(self.default_level, "default_level")
        _number(self.preview_quantization_step, "preview_quantization_step")
        if not isinstance(self.photometric_interpretation, PhotometricInterpretation):
            raise ValueError("Scalar image presentation requires MONOCHROME1 or MONOCHROME2")
        if self.default_window <= 0 or self.preview_quantization_step < 0:
            raise ValueError("Image window must be positive and preview quantization must be nonnegative")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ScalarPresentation:
        fields = {"units", "background_value", "default_window", "default_level", "photometric_interpretation", "preset_group", "preview_quantization_step"}
        if not isinstance(value, Mapping) or set(value) != fields:
            raise ValueError("Image presentation requires a complete scalar display policy")
        try:
            photometric = PhotometricInterpretation(value["photometric_interpretation"])
        except (TypeError, ValueError):
            raise ValueError("Scalar image presentation requires MONOCHROME1 or MONOCHROME2") from None
        return cls(
            units=value["units"],
            background_value=value["background_value"],
            default_window=value["default_window"],
            default_level=value["default_level"],
            photometric_interpretation=photometric,
            preset_group=value["preset_group"],
            preview_quantization_step=value["preview_quantization_step"],
        )

    def to_dict(self) -> dict[str, str | float]:
        return {
            "units": self.units,
            "background_value": self.background_value,
            "default_window": self.default_window,
            "default_level": self.default_level,
            "photometric_interpretation": self.photometric_interpretation.value,
            "preset_group": self.preset_group,
            "preview_quantization_step": self.preview_quantization_step,
        }


CT_PRESENTATION = ScalarPresentation(
    units="HU",
    background_value=-1024.0,
    default_window=400.0,
    default_level=40.0,
    photometric_interpretation=PhotometricInterpretation.MONOCHROME2,
    preset_group="CT",
    preview_quantization_step=1.0,
)

MR_PRESENTATION = ScalarPresentation(
    units="signal",
    background_value=0.0,
    default_window=1.0,
    default_level=0.5,
    photometric_interpretation=PhotometricInterpretation.MONOCHROME2,
    preset_group="MR",
    preview_quantization_step=0.0,
)


def _mr_signal_window(file_path: Path) -> tuple[float, float]:
    """Estimate signal display bounds with at most 64³ scaled samples.

    ArrayProxy slicing applies NIfTI scaling without materializing a full
    float64 volume. Signal samples and the source file are never modified.
    """
    image = nib.load(str(file_path))
    if len(image.shape) != 3 or any(size < 1 for size in image.shape):
        raise ValueError("MR presentation requires one scalar 3D image")
    slices = tuple(slice(None, None, max(1, math.ceil(size / 64))) for size in image.shape)
    sample = np.asarray(image.dataobj[slices], dtype=np.float64)
    finite = sample[np.isfinite(sample)]
    if not finite.size:
        raise ValueError("MR image has no finite signal samples")
    # Zero padding can cover most of an MR image and hide the tissue range.
    foreground = finite[finite != 0]
    values = foreground if foreground.size else finite
    low, high = (float(value) for value in np.percentile(values, [1, 99]))
    level = low / 2 + high / 2
    window = high - low
    if not math.isfinite(window):
        window = np.finfo(np.float64).max
    elif window <= 0:
        window = abs(level) * 0.1 if level else 1.0
        window = max(window, np.finfo(np.float64).tiny)
    return float(window), float(level)


def _metadata_window(metadata: Mapping[str, Any]) -> tuple[float, float] | None:
    function = metadata.get("voi_lut_function")
    if function is not None and str(function).strip().upper() not in {"", "LINEAR", "LINEAR_EXACT"}:
        return None
    try:
        center, width = metadata.get("window_center"), metadata.get("window_width")
        if isinstance(center, bool) or isinstance(width, bool):
            return None
        level, window = float(center), float(width)
    except (TypeError, ValueError, OverflowError):
        return None
    return (window, level) if math.isfinite(window) and window > 0 and math.isfinite(level) else None


class ModalityAdapter(Protocol):
    @property
    def id(self) -> str: ...
    @property
    def modality(self) -> Modality: ...
    @property
    def kind(self) -> ImageKind: ...
    @property
    def units(self) -> str: ...
    @property
    def capabilities(self) -> tuple[ImageCapability, ...]: ...
    def presentation(self) -> dict[str, str | float]: ...
    def presentation_for_image(self, file_path: Path, metadata: Mapping[str, Any] | None = None) -> dict[str, str | float]: ...
    def validate_series(self, items: list[tuple[Path, Dataset]]) -> tuple[list[tuple[Path, Dataset]], list[float] | None, list[str]]: ...


@dataclass(frozen=True, slots=True)
class ConventionalCTAdapter:
    display_policy: ScalarPresentation = CT_PRESENTATION
    id: ClassVar[str] = "conventional_ct_v1"
    modality: ClassVar[Modality] = Modality.CT
    kind: ClassVar[ImageKind] = ImageKind.VOLUME3D
    units: ClassVar[str] = "HU"
    capabilities: ClassVar[tuple[ImageCapability, ...]] = (
        ImageCapability.MPR,
        ImageCapability.VOLUME_RENDERING,
        ImageCapability.SEGMENTATION,
        ImageCapability.VOXEL_MEASUREMENTS,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.display_policy, ScalarPresentation) or self.display_policy.units != self.units:
            raise ValueError("CT presentation requires HU units")

    def presentation(self) -> dict[str, str | float]:
        return self.display_policy.to_dict()

    def presentation_for_image(self, file_path: Path, metadata: Mapping[str, Any] | None = None) -> dict[str, str | float]:
        return self.presentation()

    def validate_series(self, items: list[tuple[Path, Dataset]]) -> tuple[list[tuple[Path, Dataset]], list[float] | None, list[str]]:
        from .dicom import _validate_conventional_ct

        return _validate_conventional_ct(items)


@dataclass(frozen=True, slots=True)
class ConventionalMRAdapter:
    display_policy: ScalarPresentation = MR_PRESENTATION
    id: ClassVar[str] = "conventional_mr_v1"
    modality: ClassVar[Modality] = Modality.MR
    kind: ClassVar[ImageKind] = ImageKind.VOLUME3D
    units: ClassVar[str] = "signal"
    capabilities: ClassVar[tuple[ImageCapability, ...]] = (
        ImageCapability.MPR,
        ImageCapability.VOLUME_RENDERING,
        ImageCapability.SEGMENTATION,
        ImageCapability.VOXEL_MEASUREMENTS,
    )

    def __post_init__(self) -> None:
        if not isinstance(self.display_policy, ScalarPresentation) or self.display_policy.units != self.units:
            raise ValueError("MR presentation requires signal units")

    def presentation(self) -> dict[str, str | float]:
        return self.display_policy.to_dict()

    def presentation_for_image(self, file_path: Path, metadata: Mapping[str, Any] | None = None) -> dict[str, str | float]:
        metadata = metadata or {}
        try:
            photometric = PhotometricInterpretation(metadata.get("photometric_interpretation", self.display_policy.photometric_interpretation))
        except (TypeError, ValueError):
            raise ValueError("MR presentation requires MONOCHROME1 or MONOCHROME2") from None
        window, level = _metadata_window(metadata) or _mr_signal_window(Path(file_path))
        return ScalarPresentation(
            units=self.units,
            background_value=0.0,
            default_window=window,
            default_level=level,
            photometric_interpretation=photometric,
            preset_group="MR",
            preview_quantization_step=0.0,
        ).to_dict()

    def validate_series(self, items: list[tuple[Path, Dataset]]) -> tuple[list[tuple[Path, Dataset]], list[float] | None, list[str]]:
        from .dicom import _validate_conventional_mr

        return _validate_conventional_mr(items)


_CT = ConventionalCTAdapter()
_MR = ConventionalMRAdapter()
_ADAPTERS: dict[Modality, ModalityAdapter] = {Modality.CT: _CT, Modality.MR: _MR}


def get_modality_adapter(modality: Modality | str = Modality.CT) -> ModalityAdapter:
    try:
        return _ADAPTERS[Modality(modality)]
    except (KeyError, TypeError, ValueError):
        raise ValueError(f"Modality {modality!r} is not supported by an installed image adapter") from None


def modality_capabilities() -> dict:
    return {
        "schema_version": 1,
        "modalities": [
            {
                "adapter": adapter.id,
                "modality": adapter.modality.value,
                "kind": adapter.kind.value,
                "capabilities": [value.value for value in adapter.capabilities],
                "presentation": adapter.presentation(),
            }
            for adapter in _ADAPTERS.values()
        ],
    }


def normalize_presentation(value: Mapping[str, Any] | ScalarPresentation | None = None) -> dict[str, str | float]:
    """Validate a scalar policy and return the stable dictionary wire format."""
    if value is None:
        return get_modality_adapter(Modality.CT).presentation()
    policy = value if isinstance(value, ScalarPresentation) else ScalarPresentation.from_mapping(value)
    return policy.to_dict()
