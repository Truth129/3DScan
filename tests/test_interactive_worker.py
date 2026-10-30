"""The supervised nnInteractive child: warm reuse, cancellation, crashes and release.

A deterministic engine runs in a real child process; nothing imports torch or
touches a GPU. Review jobs then drive the same child through the service.
"""

import os
from pathlib import Path
import subprocess
import sys
import time

import nibabel as nib
import numpy as np
import pytest

from scan3d import imaging
from scan3d.config import Settings
from scan3d.interactive_worker import ENGINE as DEFAULT_ENGINE, InteractiveWorker
from scan3d.segmentation import SegmentationCancelled
from scan3d.service import Service
from scan3d.store import identifier

from test_interactive_review import example, execute_next  # noqa: F401 (fixture)

TESTS = Path(__file__).resolve().parent
ENGINE = "fake_interactive_engine:Engine"
PROMPTS = [{"id": "point-1", "kind": "point", "ras": [0.0, 0.0, 0.0], "include": True}]
BATCHES = [{"end": 1, "autozoom": True, "full_refine": False}]


@pytest.fixture
def child_environment(tmp_path, monkeypatch):
    # The child imports the fake engine from this directory, and never torch.
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, (str(TESTS), os.environ.get("PYTHONPATH")))))
    monkeypatch.setenv("SCAN3D_FAKE_ENGINE_RELEASES", str(tmp_path / "releases"))
    return tmp_path / "releases"


@pytest.fixture
def worker(child_environment, tmp_path):
    image = nib.Nifti1Image(np.arange(6 * 7 * 8, dtype=np.int16).reshape((6, 7, 8)), np.diag([0.8, 0.9, 2.0, 1.0]))
    image.header.set_xyzt_units("mm")
    nib.save(image, tmp_path / "ct.nii.gz")
    mask = np.zeros(image.shape, np.uint8)
    mask[2:4, 2:5, 3:6] = 1
    imaging._save_mask(mask, image, tmp_path / "mask.nii.gz")
    worker = InteractiveWorker(ENGINE, cancel_grace=1.0, stall_seconds=30.0)
    yield worker
    worker.release()


def predict(worker, directory, session_key="warm", name="candidate.nii.gz", *, progress=lambda message: None, cancelled=lambda: False):
    return worker.predict(
        directory / "ct.nii.gz",
        directory / "mask.nii.gz",
        PROMPTS,
        directory / name,
        progress,
        cancelled,
        session_key,
        batches=BATCHES,
        options={"autozoom": True, "start_empty": False},
    )


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_child_keeps_one_warm_engine_and_returns_its_provenance(worker, tmp_path):
    messages = []
    first = predict(worker, tmp_path, progress=messages.append)
    second = predict(worker, tmp_path, name="second.nii.gz", progress=messages.append)
    assert first["pid"] == second["pid"] == worker.pid != os.getpid()
    assert (first["loads"], second["loads"], second["requests"]) == (1, 1, 2)
    assert messages.count("Loading the fake correction model") == 1
    assert messages[-1] == "Writing the fake candidate"
    assert (second["prompts"], second["batches"]) == (PROMPTS, BATCHES)
    assert second["options"] == {"autozoom": True, "start_empty": False} and not second["torch_imported"]
    assert imaging._binary(tmp_path / "second.nii.gz")[0][1, 1, 1]


def test_cooperative_cancellation_stops_the_request_and_keeps_the_child(worker, tmp_path):
    pid = predict(worker, tmp_path)["pid"]
    messages = []
    started = time.monotonic()
    with pytest.raises(SegmentationCancelled, match="was cancelled"):
        predict(worker, tmp_path, "cooperative", "cancelled.nii.gz", progress=messages.append, cancelled=lambda: bool(messages))
    assert time.monotonic() - started < worker.cancel_grace
    assert worker.pid == pid and not (tmp_path / "cancelled.nii.gz").exists()
    retry = predict(worker, tmp_path, name="retry.nii.gz")
    assert retry["pid"] == pid and retry["loads"] == 2  # Cancellation dropped warm state only.


def test_progress_callback_errors_cancel_the_request_in_the_child(worker, tmp_path):
    pid = predict(worker, tmp_path)["pid"]

    def superseded(message):
        raise InterruptedError("Processing cancelled")

    with pytest.raises(InterruptedError, match="Processing cancelled"):
        predict(worker, tmp_path, "cooperative", progress=superseded)
    assert worker.pid == pid and predict(worker, tmp_path, name="next.nii.gz")["pid"] == pid


def test_ignored_cancellation_is_forced_after_the_grace_period(worker, tmp_path):
    pids, worker.cancel_grace = [], 0.5
    started = time.monotonic()
    with pytest.raises(SegmentationCancelled, match="did not stop within 0.5 s"):
        predict(worker, tmp_path, "uninterruptible", progress=lambda message: pids.append(worker.pid), cancelled=lambda: bool(pids))
    assert worker.cancel_grace <= time.monotonic() - started < 15
    assert worker.pid is None and not alive(pids[0])
    retry = predict(worker, tmp_path, name="retry.nii.gz")
    assert retry["pid"] != pids[0] and retry["loads"] == 1


def test_child_crash_fails_only_that_request_and_the_next_starts_fresh(worker, tmp_path):
    pid = predict(worker, tmp_path)["pid"]
    with pytest.raises(RuntimeError, match="terminated by SIGKILL during this correction"):
        predict(worker, tmp_path, "crash", "crashed.nii.gz")
    assert worker.pid is None and not alive(pid) and not (tmp_path / "crashed.nii.gz").exists()
    retry = predict(worker, tmp_path, name="retry.nii.gz")
    assert retry["pid"] != pid and (retry["loads"], retry["requests"]) == (1, 1)


def test_engine_failure_replaces_the_child_but_invalid_input_keeps_it(worker, tmp_path):
    pid = predict(worker, tmp_path)["pid"]
    with pytest.raises(ValueError, match="outside the reference CT"):
        predict(worker, tmp_path, "invalid")
    assert worker.pid == pid
    with pytest.raises(RuntimeError, match="Fake GPU prediction failed"):
        predict(worker, tmp_path, "fail")
    # A GPU error can poison a HIP context, so the next request starts clean.
    assert worker.pid is None and not alive(pid)
    assert predict(worker, tmp_path, name="retry.nii.gz")["pid"] != pid


def test_silent_child_is_stopped_as_hung(worker, tmp_path):
    pid = predict(worker, tmp_path)["pid"]
    worker.stall_seconds = 0.5
    with pytest.raises(RuntimeError, match="no progress for 0.5 s"):
        predict(worker, tmp_path, "silent")
    assert worker.pid is None and not alive(pid)


def test_cancellation_while_the_child_starts_sends_no_request(worker, tmp_path):
    answers = iter([False])
    with pytest.raises(SegmentationCancelled):
        predict(worker, tmp_path, cancelled=lambda: next(answers, True))
    pid = worker.pid
    assert pid is not None and not (tmp_path / "candidate.nii.gz").exists()
    result = predict(worker, tmp_path)
    assert result["pid"] == pid and result["requests"] == 1


def test_release_stops_the_child_after_the_engine_releases(worker, tmp_path, child_environment):
    pid = predict(worker, tmp_path)["pid"]
    worker.release()
    assert worker.pid is None and not alive(pid)
    assert child_environment.read_text().split() == [str(pid)]
    worker.release()
    assert predict(worker, tmp_path, name="after.nii.gz")["loads"] == 1


def test_child_that_cannot_start_fails_clearly(child_environment, tmp_path):
    worker = InteractiveWorker("fake_interactive_engine:Missing", stall_seconds=30.0)
    with pytest.raises(RuntimeError, match="could not start; it exited with status 1"):
        worker.predict(tmp_path / "ct.nii.gz", tmp_path / "mask.nii.gz", [], tmp_path / "out.nii.gz", lambda message: None, lambda: False, "warm")
    assert worker.pid is None


def test_child_running_other_code_is_refused(worker, tmp_path, monkeypatch):
    from scan3d import interactive_worker

    monkeypatch.setattr(interactive_worker, "PROTOCOL", interactive_worker.PROTOCOL + 1)
    with pytest.raises(RuntimeError, match="different scan3d code"):
        predict(worker, tmp_path)
    assert worker.pid is None


def test_api_process_never_imports_torch(child_environment, tmp_path):
    script = """
import builtins, os, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.partition(".")[0] in ("torch", "nnInteractive"):
        raise AssertionError("The API process imported " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import nibabel as nib, numpy as np
from fastapi.testclient import TestClient
from scan3d import imaging
from scan3d.api import create_app
from scan3d.config import Settings
from scan3d.interactive_worker import InteractiveWorker
directory = Path(sys.argv[1])
image = nib.Nifti1Image(np.zeros((4, 4, 4), np.int16), np.eye(4))
nib.save(image, directory / "ct.nii.gz")
imaging._save_mask(np.ones((4, 4, 4), np.uint8), image, directory / "mask.nii.gz")
app = create_app(Settings(directory / "data", (directory,)))
service = app.state.service
assert TestClient(app, base_url="http://127.0.0.1").get("/api/interactive/status").status_code == 200
assert service.interactive_runtime is None  # Readiness starts no worker process.
assert isinstance(service._interactive(), InteractiveWorker) and service._interactive().pid is None
service.interactive_runtime = InteractiveWorker("fake_interactive_engine:Engine")
result = service._interactive().predict(directory / "ct.nii.gz", directory / "mask.nii.gz", [],
    directory / "out.nii.gz", lambda message: None, lambda: False, "warm")
service._release_interactive()
assert result["pid"] != os.getpid() and service.interactive_runtime is None
assert not {"torch", "nnInteractive"} & set(sys.modules)
print("parent stayed torch-free")
"""
    completed = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "parent stayed torch-free"


def test_segmentation_job_never_imports_torch_in_the_api_process(tmp_path, monkeypatch):
    # The device check runs for real, in a child that imports the fake PyTorch.
    # TotalSegmentator is the only subprocess replaced.
    script = """
import builtins, json, subprocess, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.partition(".")[0] == "torch":
        raise AssertionError("The API process imported " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
import nibabel as nib, numpy as np
from scan3d import imaging, segmentation
from scan3d.config import Settings
from scan3d.demo import create_demo
from scan3d.service import Service
commands = []
class TotalSegmentator:
    pid, returncode = None, 0
    def __init__(self, command):
        commands.append(command)
        reference = nib.load(command[command.index("-i") + 1])
        mask = np.zeros(reference.shape, np.uint8)
        mask[10:20, 10:20, 10:20] = 1
        imaging._save_mask(mask, reference, Path(command[command.index("-o") + 1]) / "liver.nii.gz")
        Path(command[command.index("--report") + 1]).write_text(json.dumps({"device": "gpu", "classes": {"1": "liver"}}))
    def poll(self):
        return 0
run = subprocess.Popen
def popen(command, **kwargs):
    return TotalSegmentator(command) if "totalsegmentator.bin.TotalSegmentator" in command else run(command, **kwargs)
subprocess.Popen = popen
def fingerprint(profile, options=None, **source):
    return dict(versions={}, device_request="auto", wrapper_sha256="fake", weights_complete=True, digest="fake")
segmentation.model_fingerprint = fingerprint
service = Service(Settings(Path(sys.argv[1]) / "data", (Path(sys.argv[1]),)))
case = create_demo(service)
job = service.segment_case(case["id"], case["active_revision_id"], task="total")
service.execute(service.store.claim())
job = service.store.get("jobs", job["id"])
assert job["status"] == "completed", job
assert commands[0][commands[0].index("--device") + 1] == "gpu:0"
inference = next(Path(sys.argv[1], "data").rglob("inference.json"))
runtime = json.loads(inference.read_text())["runtime"]
assert (runtime["gpu_name"], runtime["torch"]) == ("Test GPU", "0.0+fake"), runtime
assert "torch" not in sys.modules
print("parent stayed torch-free")
"""
    paths = (str(TESTS / "fake_torch"), os.environ.get("PYTHONPATH"))
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, paths)))
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", '[["Test GPU", 1024, 0]]')
    monkeypatch.delenv("SCAN3D_DEVICE", raising=False)
    completed = subprocess.run([sys.executable, "-c", script, str(tmp_path)], capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "parent stayed torch-free"


def test_review_predictions_use_the_child_and_segmentation_releases_it(example, child_environment, monkeypatch):
    service, case, base, prompt = example
    service.interactive_runtime = runtime = InteractiveWorker(ENGINE)
    draft = service.reviews.create(case["id"], base["id"], "total:liver")
    draft = service.reviews.update(case["id"], draft["id"], 0, [prompt])
    assert execute_next(service, service.reviews.predict(case["id"], draft["id"], 1))["status"] == "completed"
    ready = service.reviews.get(case["id"], draft["id"])
    provenance = ready["candidate"]["provenance"]
    pid = runtime.pid
    assert provenance["engine"] == "fake-nninteractive" and provenance["pid"] == pid != os.getpid()
    assert provenance["prompts"] == ready["prompts"] and provenance["batches"] == ready["batches"]
    assert ready["candidate"]["summary"]["added_voxels"] == 1
    accepted = execute_next(service, service.reviews.accept(case["id"], draft["id"], ready["generation"]))
    assert accepted["status"] == "completed" and runtime.pid == pid  # Saving runs no inference.
    assert service.store.revision(case["id"], accepted["revision_id"])["operation"]["runtime"]["pid"] == pid

    def segmentation(job):
        assert service.interactive_runtime is None and runtime.pid is None and not alive(pid)
        return {**base, "id": identifier()}

    monkeypatch.setattr(service, "_segment", segmentation)
    revision = service.store.get("cases", case["id"])["active_revision_id"]
    segmented = execute_next(service, service.store.enqueue(case["id"], "segment", {"revision_id": revision}))
    assert segmented["status"] == "completed", segmented
    assert child_environment.read_text().split() == [str(pid)]


def test_default_worker_runs_the_pinned_engine_and_starts_nothing_until_predicting(tmp_path):
    service = Service(Settings(tmp_path / "data", (tmp_path,)))
    runtime = service._interactive()
    assert isinstance(runtime, InteractiveWorker) and runtime.pid is None
    assert runtime.engine == DEFAULT_ENGINE == "scan3d.interactive:InteractiveRuntime"
    service._release_interactive()
    assert service.interactive_runtime is None
