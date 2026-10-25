"""Acquisition labels only; these fields never select or modify CT pixels.

Study region, contrast timing and cardiac reconstruction timing describe the
selected DICOM series. Inference tasks remain an independent explicit choice.
"""
from __future__ import annotations

import math
from typing import Literal, get_args


StudyProfile = Literal[
    "chest", "abdomen", "chest_abdomen", "corcta", "cardiac",
    "chest_abdomen_pelvis", "abdomen_pelvis", "pelvis", "head_neck",
    "whole_body", "other", "unknown",
]
CardiacPhase = Literal[
    "unknown", "best_diastolic", "best_systolic", "diastolic", "systolic", "other",
]
PROFILE_IDS = get_args(StudyProfile)
CARDIAC_PHASE_IDS = get_args(CardiacPhase)
CARDIAC_PROFILES = frozenset({"cardiac", "corcta"})
CONTRAST_PHASE_LABELS = {
    "noncontrast": "Non-contrast / unenhanced",
    "coronary_arterial": "Coronary arterial (CorCTA)",
    "arterial": "Arterial",
    "pulmonary_arterial": "Pulmonary arterial",
    "portal_venous": "Portal venous",
    "venous": "Venous",
    "delayed": "Delayed",
    "equilibrium": "Equilibrium",
    "unknown": "Unknown",
    "other": "Other",
}


def acquisition_labels(series_uid, profile, phase, *, cardiac_phase=None, rr_percent=None):
    """Validate display/provenance metadata for HTTP and direct CLI callers.

    Contrast labels remain free text for existing saved recipes and clients.
    Null cardiac values remain absent, preserving the old four-field payload.
    """
    if profile not in PROFILE_IDS:
        raise ValueError("Unknown scan region or study type")
    if not isinstance(phase, str) or not phase.strip() or len(phase) > 80:
        raise ValueError("Specify the contrast phase or 'unknown'")
    if cardiac_phase is not None or rr_percent is not None:
        if profile not in CARDIAC_PROFILES:
            raise ValueError("Cardiac reconstruction timing applies only to cardiac CT or CorCTA")
    result = dict(series_uid=series_uid, profile=profile, phase=phase.strip())
    if cardiac_phase is not None:
        if cardiac_phase not in CARDIAC_PHASE_IDS:
            raise ValueError("Unknown cardiac reconstruction phase")
        result["cardiac_phase"] = cardiac_phase
    if rr_percent is not None:
        if isinstance(rr_percent, bool) or not isinstance(rr_percent, (int, float)) or not math.isfinite(rr_percent) or not 0 <= rr_percent <= 100:
            raise ValueError("Cardiac R–R percentage must be a finite number from 0 to 100")
        result["rr_percent"] = rr_percent
    return result
