"""DICOMweb snapshot records and the quiet period that stands in for an archive's stability flag."""

from dataclasses import replace

import pytest

from scan3d.archive.models import WebInstance, WebSnapshot, fingerprint, json_record, parse_snapshot, patient_key
from scan3d.archive.stability import QuietPeriod

SOURCE = "a" * 64


BASE = WebSnapshot(
    version=2,
    kind="dicomweb",
    source_key=SOURCE,
    study_uid="1.2.3",
    series_uid="1.2.3.4",
    patient_key=patient_key(SOURCE, "SYNTHETIC"),
    frame_of_reference_uid="1.2.3.9",
    description="Synthetic axial CT",
    modality="CT",
    instance_count=2,
    reported_instances=None,
    stable=True,
    completeness="unknown",
    warnings=(),
    instances=(WebInstance("1.2.3.4.1", "1.2.840.10008.5.1.4.1.1.2"), WebInstance("1.2.3.4.2", "1.2.840.10008.5.1.4.1.1.2")),
    snapshot="0" * 64,
)


def snapshot(**values):
    record = replace(BASE, **values)
    return replace(record, snapshot=fingerprint(record))


def test_a_snapshot_binds_its_instances_and_patient_but_not_its_stability_or_text():
    base = snapshot()
    assert parse_snapshot(json_record(base)) == base
    unchanged = snapshot(stable=False, completeness="receiving", description="Renamed", reported_instances=3, warnings=("Synthetic",))
    assert unchanged.snapshot == base.snapshot
    assert snapshot(patient_key=patient_key(SOURCE, "ANOTHER")).snapshot != base.snapshot
    assert snapshot(instances=base.instances[:1], instance_count=1).snapshot != base.snapshot
    # The same patient ID at another archive is another key, so keys never link sites.
    assert patient_key("b" * 64, "SYNTHETIC") != patient_key(SOURCE, "SYNTHETIC")
    for broken in (replace(base, snapshot="1" * 64), replace(base, instances=base.instances[::-1])):
        with pytest.raises(ValueError, match="membership"):
            parse_snapshot(json_record(broken))


def test_a_series_is_stable_only_after_its_list_stays_unchanged_for_the_quiet_period():
    now = [0.0]
    quiet = QuietPeriod(60, clock=lambda: now[0], limit=2)
    assert quiet.observe(("archive", "study", "series"), "one") == 60
    now[0] = 45
    assert quiet.observe(("archive", "study", "series"), "one") == 15
    assert quiet.observe(("archive", "study", "series"), "two") == 60, "A changed list starts over"
    now[0] = 105
    assert quiet.observe(("archive", "study", "series"), "two") == 0
    quiet.observe(("archive", "study", "other"), "x")
    quiet.observe(("archive", "study", "third"), "y")
    assert quiet.observe(("archive", "study", "series"), "two") == 60, "A forgotten series waits again"
