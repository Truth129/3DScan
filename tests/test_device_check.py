"""The short-lived child that resolves SCAN3D_DEVICE outside the API process.

Every check runs a real child against tests/fake_torch, so nothing imports torch
or touches a GPU. test_imaging covers the selection rules in _device_info.
"""

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from inference_path_stub import GPU_NAME, activate, stand_in
from scan3d import segmentation
from scan3d.config import ROOT
from scan3d.inference_paths import binding, require
from scan3d.segmentation import SegmentationCancelled, _resolve_device

FAKE_TORCH = Path(__file__).resolve().parent / "fake_torch"
INTEGRATED = ["Test integrated GPU", 30 * 1024**3, 1]
DISCRETE = ["Test discrete GPU", 12 * 1024**3, 0]
CPU = {"device": "cpu", "selected_device": "cpu", "torch": "0.0+fake"}


@pytest.fixture
def fake_torch(monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(filter(None, (str(FAKE_TORCH), os.environ.get("PYTHONPATH")))))

    def configure(*gpus, failure=None):
        monkeypatch.setenv("SCAN3D_FAKE_GPUS", json.dumps(gpus))
        monkeypatch.setenv("SCAN3D_FAKE_TORCH", failure or "")

    return configure


def resolve(device):
    return _resolve_device(device, lambda: False)


def discrete(request, index, name):
    return {
        "device": request,
        "selected_device": f"gpu:{index}",
        "torch": "0.0+fake",
        "gpu_index": index,
        "gpu_name": name,
        "hip": "7.2.fake",
        "cuda": None,
    }


@pytest.fixture
def children(monkeypatch):
    started = []
    popen = subprocess.Popen

    def recorded(*args, **kwargs):
        started.append(popen(*args, **kwargs))
        return started[-1]

    monkeypatch.setattr(segmentation.subprocess, "Popen", recorded)
    return started


def test_child_returns_the_same_dictionary_as_device_info(fake_torch):
    fake_torch(INTEGRATED, DISCRETE)
    assert resolve("auto") == discrete("gpu", 1, DISCRETE[0])
    assert resolve("gpu") == discrete("gpu", 1, DISCRETE[0])
    assert resolve("gpu:0") == discrete("gpu:0", 0, INTEGRATED[0])
    assert resolve("cpu") == CPU


def test_auto_alone_falls_back_to_the_cpu(fake_torch):
    fake_torch()
    assert resolve("auto") == CPU
    with pytest.raises(RuntimeError, match="PyTorch cannot access a GPU"):
        resolve("gpu")
    with pytest.raises(RuntimeError, match="PyTorch cannot access a GPU"):
        resolve("gpu:0")


def test_invalid_requests_stay_value_errors(fake_torch):
    fake_torch(DISCRETE)
    with pytest.raises(ValueError, match="SCAN3D_DEVICE must be"):
        resolve("cuda")
    with pytest.raises(ValueError, match="Requested GPU index is unavailable"):
        resolve("gpu:1")


def test_installation_without_pytorch_keeps_its_message(fake_torch):
    fake_torch(failure="missing")
    with pytest.raises(RuntimeError, match="No module named 'torch'"):
        resolve("auto")


def test_child_crash_fails_clearly(fake_torch):
    fake_torch(failure="crash")
    with pytest.raises(RuntimeError, match=r"device check failed \(exit -9\)"):
        resolve("gpu")


def test_cancellation_stops_a_slow_child(fake_torch, children):
    fake_torch(failure="hang")
    with pytest.raises(SegmentationCancelled):
        _resolve_device("gpu", lambda: True)
    assert children[0].poll() is not None


def test_hung_child_fails_the_job_instead_of_holding_the_lane(fake_torch, children, monkeypatch):
    monkeypatch.setattr(segmentation, "DEVICE_CHECK_SECONDS", 0.5)
    fake_torch(failure="hang")
    with pytest.raises(RuntimeError, match="did not report a device within 0.5 s"):
        resolve("gpu")
    assert children[0].poll() is not None


# An added inference path, through a real second interpreter


@pytest.fixture
def stand_in_path(tmp_path, monkeypatch):
    stub = stand_in(tmp_path)
    activate(monkeypatch, stub)
    monkeypatch.setenv("SCAN3D_FAKE_TORCH_PREFIXES", str(tmp_path / "prefixes"))
    return stub, require(binding("nvidia"))


def test_a_path_answers_from_its_own_interpreter(stand_in_path, children, tmp_path):
    stub, path = stand_in_path
    runtime = _resolve_device(path.device, lambda: False, path)
    assert children[0].args == [stub.python, "-P", "-m", "scan3d.segmentation", "gpu"]
    assert (tmp_path / "prefixes").read_text().split() == [str(Path(stub.python).parents[1])]
    assert runtime == {**discrete("gpu", 0, GPU_NAME), "hip": None, "cuda": "13.0.fake"}


def test_the_default_path_keeps_the_api_interpreter(fake_torch, children, tmp_path, monkeypatch):
    fake_torch(DISCRETE)
    monkeypatch.setenv("SCAN3D_FAKE_TORCH_PREFIXES", str(tmp_path / "prefixes"))
    resolve("gpu")
    assert children[0].args == [sys.executable, "-P", "-m", "scan3d.segmentation", "gpu"]
    assert (tmp_path / "prefixes").read_text().split() == [sys.prefix]


def spawned_environment(monkeypatch):
    seen = {}
    popen = subprocess.Popen

    def recorded(command, **options):
        seen.update(options["env"])
        return popen(command, **options)

    monkeypatch.setattr(segmentation.subprocess, "Popen", recorded)
    return seen


def test_a_paths_device_check_gets_that_paths_environment_and_this_scan3d(stand_in_path, monkeypatch):
    _, path = stand_in_path
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/other-stack/lib")
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu:7")
    seen = spawned_environment(monkeypatch)
    _resolve_device(path.device, lambda: False, path)
    expected = path.environment()
    assert {key: value for key, value in seen.items() if key != "PYTHONPATH"} == {key: value for key, value in expected.items() if key != "PYTHONPATH"}
    assert "LD_LIBRARY_PATH" not in seen and seen["SCAN3D_DEVICE"] == "gpu"
    assert seen["PYTHONPATH"] == f"{ROOT}{os.pathsep}{expected['PYTHONPATH']}"


def test_the_default_paths_device_check_inherits_everything(fake_torch, monkeypatch):
    fake_torch(DISCRETE)
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/this-stack/lib")
    seen = spawned_environment(monkeypatch)
    resolve("gpu")
    assert seen == {**os.environ, "PYTHONPATH": f"{ROOT}{os.pathsep}{os.environ['PYTHONPATH']}"}


def test_a_path_can_still_be_cancelled_and_cannot_hold_the_lane(stand_in_path, children, monkeypatch):
    _, path = stand_in_path
    monkeypatch.setenv("SCAN3D_FAKE_TORCH", "hang")
    with pytest.raises(SegmentationCancelled):
        _resolve_device(path.device, lambda: True, path)
    monkeypatch.setattr(segmentation, "DEVICE_CHECK_SECONDS", 0.5)
    with pytest.raises(RuntimeError, match="did not report a device within 0.5 s"):
        _resolve_device(path.device, lambda: False, path)
    assert [child.poll() is not None for child in children] == [True, True]


def test_a_paths_failure_keeps_its_type_and_message(stand_in_path, monkeypatch):
    _, path = stand_in_path
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", "[]")
    with pytest.raises(RuntimeError, match="PyTorch cannot access a GPU"):
        _resolve_device(path.device, lambda: False, path)
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", json.dumps([DISCRETE]))
    with pytest.raises(ValueError, match="Requested GPU index is unavailable"):
        _resolve_device("gpu:1", lambda: False, path)
