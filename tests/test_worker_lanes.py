"""GPU inference stays serialized on one lane while CPU-only work runs beside it.

Lanes run as real threads here, with a blocked fake TotalSegmentator and the
fake nnInteractive engine. Claim order is also checked synchronously.
"""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

from fastapi.testclient import TestClient
import pytest

from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.service import Service
from scan3d.store import Store, identifier

from test_interactive_review import FakeRuntime, example  # noqa: F401 (fixture)


def erase(identity="erase", *, index=4, vertices=((2., 2.), (5., 5.))):
    return dict(id=identity, kind="scribble", include=False, axis=2, index=index,
                vertices=[list(vertex) for vertex in vertices], radius_mm=1.5)


def finished(service, job, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        current = service.store.get("jobs", job["id"])
        if current["status"] not in ("queued", "running"):
            return current
        time.sleep(.02)
    pytest.fail(f"{job['kind']} job did not finish")


def direct_draft(service, case, base, structure="total:liver", prompt=None):
    draft = service.reviews.create(case["id"], base["id"], structure, method="direct")
    return service.reviews.update(case["id"], draft["id"], 0, [prompt or erase()])


def fake_engine_only(service, monkeypatch, runtime=None):
    # Releasing before a segment job must never let a real engine replace the fake.
    runtime = runtime or service.interactive_runtime
    monkeypatch.setattr(service, "_interactive", lambda: runtime)
    return runtime


def test_direct_edit_and_acceptance_finish_while_gpu_lane_is_blocked(example, monkeypatch):
    service, case, base, _ = example
    fake_engine_only(service, monkeypatch)
    started, release = threading.Event(), threading.Event()

    def blocked_segmentation(job):
        started.set()
        if not release.wait(30):
            raise RuntimeError("The blocked fixture was never released")
        return {**base, "id": identifier()}

    monkeypatch.setattr(service, "_segment", blocked_segmentation)
    service.start()
    try:
        gpu = service.store.enqueue(case["id"], "segment", {"revision_id": base["id"]})
        assert started.wait(10)
        draft = direct_draft(service, case, base)
        predicted = finished(service, service.reviews.predict(case["id"], draft["id"], draft["generation"]))
        assert predicted["status"] == "completed", predicted
        ready = service.reviews.get(case["id"], draft["id"])
        assert ready["status"] == "ready" and ready["candidate"]["summary"]["removed_voxels"] > 0
        accepted = finished(service, service.reviews.accept(case["id"], draft["id"], ready["generation"]))
        assert accepted["status"] == "completed", accepted
        assert service.store.get("jobs", gpu["id"])["status"] == "running"
        assert service.store.get("cases", case["id"])["active_revision_id"] == accepted["revision_id"]
        assert service.reviews.get(case["id"], draft["id"])["status"] == "accepted"
    finally:
        release.set()
        result = finished(service, gpu)
        service.stop()
    # The transactional active-revision guard, not queue order, settles the race:
    # the saved correction stays active and the segmentation cannot overwrite it.
    assert result["status"] == "failed" and "active revision changed" in result["error"]
    assert service.store.get("cases", case["id"])["active_revision_id"] == accepted["revision_id"]


def test_gpu_jobs_never_overlap_and_interactive_predictions_precede_queued_segmentation(example, monkeypatch):
    service, case, base, prompt = example
    guard, active, peak, order = threading.Lock(), [0], [0], []

    def gpu_work(kind):
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            order.append(kind)
        time.sleep(.05)
        with guard:
            active[0] -= 1

    fake_engine_only(service, monkeypatch, FakeRuntime(during=lambda: gpu_work("review_predict")))

    def segmentation(job):
        gpu_work("segment")
        return {**base, "id": identifier()}

    monkeypatch.setattr(service, "_segment", segmentation)
    segment = service.store.enqueue(case["id"], "segment", {"revision_id": base["id"]})
    predictions = []
    for structure in ("total:liver", "total:liver", "total:kidney_left"):
        draft = service.reviews.create(case["id"], base["id"], structure)
        draft = service.reviews.update(case["id"], draft["id"], 0, [prompt])
        predictions.append(service.reviews.predict(case["id"], draft["id"], draft["generation"]))
    draft = direct_draft(service, case, base)
    direct = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    service.start()
    try:
        results = [finished(service, job) for job in (*predictions, direct, segment)]
    finally:
        service.stop()
    assert [result["status"] for result in results] == ["completed"] * 5, results
    assert peak[0] == 1
    assert order == ["review_predict"] * 3 + ["segment"]


def test_claim_orders_interactive_review_before_batch_work_within_each_lane(tmp_path):
    store = Store(tmp_path)
    case = dict(id=identifier(), label="Lane order", status="imported", series=[])
    store.save("cases", case)

    def queued(kind, **payload):
        return store.enqueue(case["id"], kind, payload)["id"]

    def drained(lane=None):
        return [job["id"] for job in iter(lambda: store.claim(lane), None)]

    process, segment, include_all = queued("process"), queued("segment"), queued("include_all")
    direct = queued("review_predict", method="direct")
    model = queued("review_predict", method="nninteractive")
    policy = queued("review_predict", method="nninteractive", relationship_only=True)
    accept = queued("review_accept", method="nninteractive")
    legacy = queued("review_predict")  # Sessions predating direct editing are nnInteractive.
    refine, correction = queued("refine"), queued("correction")
    # A malformed row must fail in its own job rather than wedge a lane's claims.
    malformed = store.enqueue(case["id"], "refine", None)["id"]
    assert drained("gpu") == [model, legacy, segment]
    assert drained("cpu") == [direct, policy, accept, process, include_all, refine, correction, malformed]
    assert store.claim() is None
    batch, review = queued("segment"), queued("review_accept")
    assert drained() == [review, batch]
    with pytest.raises(ValueError, match="lane"):
        store.claim("tpu")


def test_restart_recovers_running_jobs_on_both_lanes_then_both_lanes_resume(example, monkeypatch):
    service, case, base, prompt = example
    runtime = fake_engine_only(service, monkeypatch)
    model = service.reviews.create(case["id"], base["id"], "total:liver")
    model = service.reviews.update(case["id"], model["id"], 0, [prompt])
    direct = direct_draft(service, case, base, "total:kidney_left",
                          erase(index=3, vertices=((8., 2.), (10., 4.))))
    jobs = [service.reviews.predict(case["id"], draft["id"], draft["generation"]) for draft in (model, direct)]
    # A worker set stops mid-job on each lane without finishing either.
    assert [service.store.claim("gpu")["id"], service.store.claim("cpu")["id"]] == [job["id"] for job in jobs]
    restarted = Service(service.settings)
    fake_engine_only(restarted, monkeypatch, runtime)
    restarted.start()
    try:
        for job in jobs:
            recovered = restarted.store.get("jobs", job["id"])
            assert recovered["status"] == "failed" and "restarted" in recovered["error"]
        retries = []
        for draft in (model, direct):
            recovered = restarted.reviews.get(case["id"], draft["id"])
            assert recovered["status"] == "draft" and "restarted" in recovered["error"]
            assert recovered["prompts"] == draft["prompts"]
            retries.append(restarted.reviews.predict(case["id"], draft["id"], recovered["generation"]))
        assert [finished(restarted, job)["status"] for job in retries] == ["completed", "completed"]
    finally:
        restarted.stop()
    assert len(runtime.calls) == 1


def test_one_process_owns_both_lanes_until_every_lane_stops(tmp_path):
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist")
    first, second = Service(settings), Service(settings)
    try:
        first.start()
        second.start()
        assert set(first.threads) == {"gpu", "cpu"}
        assert all(thread.is_alive() for thread in first.threads.values())
        assert second.threads == {} and second.thread is None
        first.stop()
        assert not any(thread.is_alive() for thread in first.threads.values())
        assert first.worker_lock is None
        second.start()
        assert all(thread.is_alive() for thread in second.threads.values())
    finally:
        first.stop()
        second.stop()
    app = create_app(settings)
    with TestClient(app, base_url="http://127.0.0.1") as http:
        health = http.get("/api/health").json()
    assert health["worker"] and health["lanes"] == {"gpu": True, "cpu": True}


def test_single_lane_start_leaves_other_lane_work_queued(example, monkeypatch):
    service, case, base, _ = example

    def no_gpu(job):
        raise AssertionError("A CPU-only worker set ran GPU work")

    monkeypatch.setattr(service, "_segment", no_gpu)
    gpu = service.store.enqueue(case["id"], "segment", {"revision_id": base["id"]})
    draft = direct_draft(service, case, base)
    direct = service.reviews.predict(case["id"], draft["id"], draft["generation"])
    service.start(lanes=("cpu",))
    try:
        assert finished(service, direct)["status"] == "completed"
    finally:
        service.stop()
    assert set(service.threads) == {"cpu"}
    assert service.store.get("jobs", gpu["id"])["status"] == "queued"


def test_concurrent_revision_builds_share_one_preparation_budget(example, monkeypatch):
    service, case, base, _ = example
    monkeypatch.setenv("SCAN3D_POSTPROCESS_WORKERS", "1")
    builder = Service(service.settings)
    original = builder._analyze
    guard, active, peak = threading.Lock(), [0], [0]

    def measured(*args, **kwargs):
        # Each pool task decodes one dense mask here for QC and surfaces.
        with guard:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(.05)
            return original(*args, **kwargs)
        finally:
            with guard:
                active[0] -= 1

    monkeypatch.setattr(builder, "_analyze", measured)
    unmeasured = [{key: value for key, value in structure.items()
                   if key not in ("volume_ml", "voxel_count", "centroid_ras", "bounds_ras", "qc")}
                  for structure in base["structures"]]
    with ThreadPoolExecutor(max_workers=2) as pool:
        builds = [pool.submit(builder.build_revision, case["id"], Path(base["ct_file"]), unmeasured,
                              source="budget_fixture", parent=base["id"]) for _ in range(2)]
        revisions = [build.result() for build in builds]
    # Without the shared budget each build would run its own worker at once.
    assert peak[0] == 1
    assert [len(revision["structures"]) for revision in revisions] == [2, 2]
    assert revisions[0]["structures"][0]["volume_ml"] == base["structures"][0]["volume_ml"]
