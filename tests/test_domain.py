"""Domain values keep existing wire contracts while exposing typed Python APIs."""

from dataclasses import FrozenInstanceError, replace
import json
import re
import sys
import uuid

import pytest

from scan3d.acquisition import CARDIAC_PHASE_IDS, PROFILE_IDS, acquisition_labels
from scan3d.domain import (
    CardiacPhase,
    ImageCapability,
    ImageKind,
    Modality,
    PhotometricInterpretation,
    StudyProfile,
    VolumeQuality,
)
from scan3d.identifiers import identifier
from scan3d.modalities import (
    CT_PRESENTATION,
    MR_PRESENTATION,
    ConventionalCTAdapter,
    ScalarPresentation,
    get_modality_adapter,
    modality_capabilities,
    normalize_presentation,
)


def test_domain_enums_and_acquisition_labels_stay_plain_json_strings():
    for kind in (Modality, ImageKind, ImageCapability, PhotometricInterpretation, StudyProfile, CardiacPhase, VolumeQuality):
        values = [member.value for member in kind]
        assert json.loads(json.dumps(list(kind))) == values and [kind(value) for value in values] == list(kind)
        assert len(kind.__members__) == len(values), "Closed domain enums must not contain aliases"
    result = acquisition_labels("series", StudyProfile.CORCTA, "coronary_arterial", cardiac_phase=CardiacPhase.BEST_DIASTOLIC, rr_percent=69)
    assert result == {"series_uid": "series", "profile": "corcta", "phase": "coronary_arterial", "cardiac_phase": "best_diastolic", "rr_percent": 69}
    assert type(result["profile"]) is str and type(result["cardiac_phase"]) is str
    assert PROFILE_IDS == tuple(value.value for value in StudyProfile)
    assert CARDIAC_PHASE_IDS == tuple(value.value for value in CardiacPhase)


def test_record_ids_are_32_hex_and_time_ordered_on_python_3_14():
    values = [identifier() for _ in range(256)]
    assert len(set(values)) == len(values) and all(re.fullmatch(r"[a-f0-9]{32}", value) for value in values)
    if sys.version_info >= (3, 14):
        assert values == sorted(values) and all(uuid.UUID(hex=value).version == 7 for value in values)


def test_scalar_policy_is_immutable_and_round_trips():
    policy = ScalarPresentation.from_mapping(CT_PRESENTATION.to_dict())
    assert policy == CT_PRESENTATION
    with pytest.raises(FrozenInstanceError):
        policy.default_window = 600
    output = policy.to_dict()
    output["default_window"] = 1
    assert policy.default_window == 400
    assert normalize_presentation(policy) == policy.to_dict()


@pytest.mark.parametrize(
    "change",
    [
        {"default_window": 0},
        {"default_level": True},
        {"background_value": float("inf")},
        {"units": ""},
        {"photometric_interpretation": "RGB"},
    ],
)
def test_scalar_policy_rejects_invalid_numeric_or_photometric_metadata(change):
    with pytest.raises(ValueError):
        ScalarPresentation.from_mapping({**CT_PRESENTATION.to_dict(), **change})


def test_scalar_policy_requires_a_complete_explicit_boundary_record():
    missing = CT_PRESENTATION.to_dict()
    del missing["units"]
    for invalid in (missing, {**CT_PRESENTATION.to_dict(), "extra": "ignored"}, []):
        with pytest.raises(ValueError, match="complete"):
            ScalarPresentation.from_mapping(invalid)


def test_typed_adapter_registry_keeps_existing_public_capabilities():
    adapter = get_modality_adapter(Modality.CT)
    assert adapter is get_modality_adapter("CT")
    changed = replace(adapter, display_policy=replace(CT_PRESENTATION, default_window=600))
    assert changed.presentation()["default_window"] == 600 and adapter.presentation()["default_window"] == 400
    assert modality_capabilities() == {
        "schema_version": 1,
        "modalities": [
            {
                "adapter": "conventional_ct_v1",
                "modality": "CT",
                "kind": "volume3d",
                "capabilities": ["mpr", "volume_rendering", "segmentation", "voxel_measurements"],
                "presentation": CT_PRESENTATION.to_dict(),
            },
            {
                "adapter": "conventional_mr_v1",
                "modality": "MR",
                "kind": "volume3d",
                "capabilities": ["mpr", "volume_rendering", "segmentation", "voxel_measurements"],
                "presentation": MR_PRESENTATION.to_dict(),
            },
        ],
    }
    with pytest.raises(ValueError, match="HU"):
        ConventionalCTAdapter(display_policy=replace(CT_PRESENTATION, units="signal"))
