"""Dependency-free domain checks runnable on both Python 3.12 and 3.14.

Run without the imaging/GPU environment:
    python3.14 -B -m unittest discover -s tests -p test_domain_compat.py
"""

from dataclasses import FrozenInstanceError, replace
import json
import re
import sys
import unittest
import uuid

from scan3d.acquisition import acquisition_labels
from scan3d.domain import CardiacPhase, ImageCapability, ImageKind, Modality, StudyProfile, VolumeQuality
from scan3d.identifiers import identifier
from scan3d.modalities import CT_PRESENTATION, ScalarPresentation, get_modality_adapter


class DomainCompatibilityTests(unittest.TestCase):
    def test_enum_values_retain_the_existing_json_contract(self):
        result = json.loads(
            json.dumps(
                {
                    "modality": Modality.CT,
                    "kind": ImageKind.VOLUME3D,
                    "capabilities": [ImageCapability.MPR],
                    "quality": VolumeQuality.FULL,
                }
            )
        )
        self.assertEqual(result, {"modality": "CT", "kind": "volume3d", "capabilities": ["mpr"], "quality": "full"})

    def test_cardiac_labels_accept_enums_and_emit_plain_strings(self):
        result = acquisition_labels("series", StudyProfile.CORCTA, "coronary_arterial", cardiac_phase=CardiacPhase.BEST_DIASTOLIC, rr_percent=69)
        self.assertEqual(
            result, {"series_uid": "series", "profile": "corcta", "phase": "coronary_arterial", "cardiac_phase": "best_diastolic", "rr_percent": 69}
        )
        self.assertIs(type(result["profile"]), str)
        self.assertIs(type(result["cardiac_phase"]), str)

    def test_display_policy_is_immutable_and_explicitly_replaceable(self):
        self.assertEqual(ScalarPresentation.from_mapping(CT_PRESENTATION.to_dict()), CT_PRESENTATION)
        with self.assertRaises(FrozenInstanceError):
            CT_PRESENTATION.background_value = 0
        changed = replace(CT_PRESENTATION, background_value=0, default_window=200)
        self.assertEqual(changed.background_value, 0)
        self.assertEqual(CT_PRESENTATION.background_value, -1024)
        self.assertEqual(get_modality_adapter(Modality.CT).presentation(), CT_PRESENTATION.to_dict())

    def test_record_ids_keep_their_wire_format_and_uniqueness(self):
        values = [identifier() for _ in range(256)]
        self.assertEqual(len(set(values)), len(values))
        expected_version = 7 if sys.version_info >= (3, 14) else 4
        self.assertTrue(all(re.fullmatch(r"[a-f0-9]{32}", value) for value in values))
        self.assertTrue(all(uuid.UUID(hex=value).version == expected_version for value in values))

    @unittest.skipUnless(sys.version_info >= (3, 14), "UUID7 is available starting in Python 3.14")
    def test_uuid7_record_ids_are_time_ordered(self):
        values = [identifier() for _ in range(256)]
        self.assertEqual(values, sorted(values))
        self.assertTrue(all(uuid.UUID(hex=value).version == 7 for value in values))


if __name__ == "__main__":
    unittest.main()
