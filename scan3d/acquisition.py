"""Acquisition labels only; these fields never select or modify source pixels.

Study region, contrast timing and cardiac reconstruction timing describe the
selected DICOM series. Inference tasks remain an independent explicit choice.
"""

from __future__ import annotations

import math
from .domain import CardiacPhase, StudyProfile


PROFILE_IDS = tuple(profile.value for profile in StudyProfile)
CARDIAC_PHASE_IDS = tuple(phase.value for phase in CardiacPhase)
CARDIAC_PROFILES = frozenset({StudyProfile.CARDIAC, StudyProfile.CORCTA})


def acquisition_labels(
    series_uid: str, profile: StudyProfile | str, phase: str, *, cardiac_phase: CardiacPhase | str | None = None, rr_percent: int | float | None = None
) -> dict:
    """Validate display/provenance metadata for HTTP and direct CLI callers.

    Contrast labels remain free text for existing saved recipes and clients.
    Null cardiac values remain absent, preserving the old four-field payload.
    """
    try:
        profile = StudyProfile(profile)
    except (TypeError, ValueError):
        raise ValueError("Unknown scan region or study type") from None
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 80:
        raise ValueError("Specify the contrast phase or 'unknown'")
    if cardiac_phase is not None or rr_percent is not None:
        if profile not in CARDIAC_PROFILES:
            raise ValueError("Cardiac reconstruction timing applies only to cardiac CT or CorCTA")
    result = dict(series_uid=series_uid, profile=profile.value, phase=phase.strip())
    if cardiac_phase is not None:
        try:
            cardiac_phase = CardiacPhase(cardiac_phase)
        except (TypeError, ValueError):
            raise ValueError("Unknown cardiac reconstruction phase") from None
        result["cardiac_phase"] = cardiac_phase.value
    if rr_percent is not None:
        if isinstance(rr_percent, bool) or not isinstance(rr_percent, (int, float)) or not math.isfinite(rr_percent) or not 0 <= rr_percent <= 100:
            raise ValueError("Cardiac R–R percentage must be a finite number from 0 to 100")
        result["rr_percent"] = rr_percent
    return result
