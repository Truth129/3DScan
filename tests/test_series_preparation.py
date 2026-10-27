"""Unavailable acquisitions cannot bypass validation through an old conversion cache."""

import hashlib

import nibabel as nib
import numpy as np
import pytest

from scan3d.config import Settings
from scan3d.service import Service


def test_unsupported_series_is_rejected_before_queue_and_cached_reference_cannot_bypass_it(tmp_path):
    service = Service(Settings(tmp_path / "data", (tmp_path,)))
    case = service.new_case("Unavailable acquisition fixture")
    source = service.store.case_dir(case["id"]) / "source.dcm"
    source.write_bytes(b"Invalid source fixture; the converter must not run")
    reason = "Select a single temporal volume"
    case["series"] = [{"uid": "unavailable-series", "modality": "MR", "files": [str(source)], "supported": False, "unsupported_reason": reason}]
    service.store.save("cases", case)
    with pytest.raises(ValueError, match=reason):
        service.process(case["id"], "unavailable-series", "breast", "unknown")
    assert service.store.list("jobs") == []
    folder = service.store.case_dir(case["id"]) / "converted" / hashlib.sha256(source.read_bytes()).hexdigest()
    folder.mkdir(parents=True)
    nib.save(nib.Nifti1Image(np.ones((3, 4, 5), np.int16), np.eye(4)), folder / "reference.nii.gz")
    # Previously queued requests must recheck availability before using this cache.
    pending = service.store.enqueue(case["id"], "process", {"series_uid": "unavailable-series", "profile": "breast", "phase": "unknown"})
    claimed = service.store.claim()
    assert claimed["id"] == pending["id"]
    service.execute(claimed)
    finished = service.store.get("jobs", pending["id"])
    assert finished["status"] == "failed" and finished["error"] == reason
    assert service.store.get("cases", case["id"])["active_revision_id"] is None
    assert service.store.list("revisions", case["id"]) == []
