"""The short-lived child that resolves SCAN3D_DEVICE outside the API process.

Every check runs a real child against tests/fake_torch, so nothing imports torch
or touches a GPU. test_imaging covers the selection rules in _device_info.
"""

import json
import os
from pathlib import Path
import subprocess

import pytest

from scan3d import segmentation
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
