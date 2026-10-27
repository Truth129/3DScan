from collections import Counter
import os
from pathlib import Path
import threading

from scan3d.config import Settings
from scan3d.demo import create_demo
from scan3d.service import Service


def test_cold_measurements_and_surface_generation_decode_each_mask_once(tmp_path, monkeypatch):
    from scan3d import imaging, surfaces

    original = imaging._binary
    counts, lock = Counter(), threading.Lock()

    def decoded(path, *args, **kwargs):
        with lock:
            counts[str(path)] += 1
        return original(path, *args, **kwargs)

    monkeypatch.setattr(imaging, "_binary", decoded)
    monkeypatch.setattr(surfaces, "_binary", decoded)
    service = Service(Settings(tmp_path / "data", (tmp_path,)))
    case = create_demo(service)
    revision = service.store.revision(case["id"], case["active_revision_id"])
    assert len(counts) == 7 and set(counts.values()) == {1}
    assert revision["mesh_statistics"]["display"]["cache"]["surface_misses"] == 7
    assert revision["processing_timings"]["preparation_workers"] <= 4


def test_preparation_concurrency_reserves_memory_for_other_work(monkeypatch):
    monkeypatch.setenv("SCAN3D_POSTPROCESS_WORKERS", "4")
    # The request is also capped by CPU count; pin it so small CI runners agree.
    monkeypatch.setattr(os, "cpu_count", lambda: 32)
    geometry = {"shape": [512, 512, 729]}
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "MemAvailable: 8388608 kB\n")
    assert Service._preparation_workers(geometry) == 1
    monkeypatch.setattr(Path, "read_text", lambda *args, **kwargs: "MemAvailable: 134217728 kB\n")
    assert Service._preparation_workers(geometry) == 4
