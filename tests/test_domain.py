"""Domain values keep existing wire contracts while exposing typed Python APIs."""
from dataclasses import FrozenInstanceError, replace
import json

import pytest

from scan3d.acquisition import CARDIAC_PHASE_IDS, PROFILE_IDS, acquisition_labels
from scan3d.domain import (
    CardiacPhase, ImageCapability, ImageKind, Modality,
    PhotometricInterpretation, StudyProfile, VolumeQuality,
)
from scan3d.modalities import (
    CT_PRESENTATION, MR_PRESENTATION, ConventionalCTAdapter, ScalarPresentation,
    get_modality_adapter, modality_capabilities, normalize_presentation,
)


@pytest.mark.parametrize('kind', [Modality, ImageKind, ImageCapability,
                               PhotometricInterpretation, StudyProfile,
                               CardiacPhase, VolumeQuality])
def test_domain_enums_roundtrip_as_plain_json_strings(kind):
    values = [member.value for member in kind]
    assert json.loads(json.dumps(list(kind))) == values
    assert [kind(value) for value in values] == list(kind)
    assert all(str(member) == member.value for member in kind)
    assert len(kind.__members__) == len(values), 'Closed domain enums must not contain aliases'


def test_acquisition_enum_inputs_keep_the_existing_plain_string_payload():
    result = acquisition_labels('series', StudyProfile.CORCTA, 'coronary_arterial',
                                cardiac_phase=CardiacPhase.BEST_DIASTOLIC, rr_percent=69)
    assert result == {'series_uid': 'series', 'profile': 'corcta', 'phase': 'coronary_arterial',
                      'cardiac_phase': 'best_diastolic', 'rr_percent': 69}
    assert type(result['profile']) is str and type(result['cardiac_phase']) is str
    assert PROFILE_IDS == tuple(value.value for value in StudyProfile)
    assert CARDIAC_PHASE_IDS == tuple(value.value for value in CardiacPhase)


def test_scalar_policy_has_an_explicit_immutable_typed_interface():
    policy = ScalarPresentation.from_mapping(CT_PRESENTATION.to_dict())
    assert policy == CT_PRESENTATION
    assert policy.photometric_interpretation is PhotometricInterpretation.MONOCHROME2
    with pytest.raises(FrozenInstanceError):
        policy.default_window = 600
    changed = replace(policy, default_window=600)
    assert changed.default_window == 600 and policy.default_window == 400
    output = policy.to_dict()
    output['default_window'] = 1
    assert policy.default_window == 400
    assert normalize_presentation(policy) == policy.to_dict()


@pytest.mark.parametrize('change', [
    {'default_window': 0}, {'default_window': float('nan')}, {'default_level': True},
    {'background_value': float('inf')}, {'preview_quantization_step': -1},
    {'units': ''}, {'photometric_interpretation': 'RGB'},
])
def test_scalar_policy_rejects_invalid_numeric_or_photometric_metadata(change):
    with pytest.raises(ValueError):
        ScalarPresentation.from_mapping({**CT_PRESENTATION.to_dict(), **change})


def test_scalar_policy_requires_a_complete_explicit_boundary_record():
    missing = CT_PRESENTATION.to_dict()
    del missing['units']
    for invalid in (missing, {**CT_PRESENTATION.to_dict(), 'extra': 'ignored'}, []):
        with pytest.raises(ValueError, match='complete'):
            ScalarPresentation.from_mapping(invalid)


def test_typed_adapter_registry_keeps_existing_public_capabilities():
    adapter = get_modality_adapter(Modality.CT)
    assert adapter is get_modality_adapter('CT')
    assert adapter.modality is Modality.CT and adapter.kind is ImageKind.VOLUME3D
    assert ImageCapability.SEGMENTATION in adapter.capabilities
    with pytest.raises(FrozenInstanceError):
        adapter.display_policy = replace(CT_PRESENTATION, default_window=600)
    changed = replace(adapter, display_policy=replace(CT_PRESENTATION, default_window=600))
    assert changed.presentation()['default_window'] == 600
    assert adapter.presentation()['default_window'] == 400
    assert modality_capabilities() == {'schema_version': 1, 'modalities': [{
        'adapter': 'conventional_ct_v1', 'modality': 'CT', 'kind': 'volume3d',
        'capabilities': ['mpr', 'volume_rendering', 'segmentation', 'voxel_measurements'],
        'presentation': CT_PRESENTATION.to_dict(),
    }, {
        'adapter': 'conventional_mr_v1', 'modality': 'MR', 'kind': 'volume3d',
        'capabilities': ['mpr', 'volume_rendering', 'segmentation', 'voxel_measurements'],
        'presentation': MR_PRESENTATION.to_dict(),
    }]}
    with pytest.raises(ValueError, match='HU'):
        ConventionalCTAdapter(display_policy=replace(CT_PRESENTATION, units='signal'))
