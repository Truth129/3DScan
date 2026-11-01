"""Stands in for PyTorch in device-check children, so those tests never reach a GPU.

A test puts this directory on PYTHONPATH. SCAN3D_FAKE_GPUS is a JSON list of
[name, total_memory, is_integrated], SCAN3D_FAKE_TORCH selects a failure,
SCAN3D_FAKE_TORCH_BUILD=cuda reports a CUDA build instead of a ROCm one, and
SCAN3D_FAKE_TORCH_PREFIXES names a file that gets this interpreter's sys.prefix, which
proves where a child ran when its argv only shows what was asked for, and
SCAN3D_FAKE_TORCH_ENVIRONMENT names a file that gets the environment PyTorch would load under.
"""

from contextlib import nullcontext
import json
import os
import signal
import sys
import time
from types import SimpleNamespace

# Native runtimes log straight to the descriptor, past Python's sys.stdout.
os.write(1, b"fake native runtime log\n")

if prefixes := os.environ.get("SCAN3D_FAKE_TORCH_PREFIXES"):
    with open(prefixes, "a") as file:
        file.write(f"{sys.prefix}\n")

if environment := os.environ.get("SCAN3D_FAKE_TORCH_ENVIRONMENT"):
    with open(environment, "w") as file:
        json.dump(dict(os.environ), file)

failure = os.environ.get("SCAN3D_FAKE_TORCH")
if failure == "missing":
    raise ModuleNotFoundError("No module named 'torch'")
if failure == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
if failure == "hang":
    time.sleep(600)

gpus = json.loads(os.environ.get("SCAN3D_FAKE_GPUS", "[]"))
__version__ = "0.0+fake"
version = SimpleNamespace(hip=None, cuda="13.0.fake") if os.environ.get("SCAN3D_FAKE_TORCH_BUILD") == "cuda" else SimpleNamespace(hip="7.2.fake", cuda=None)
float16 = "float16"


def properties(index):
    _, memory, integrated = gpus[index]
    return SimpleNamespace(total_memory=memory, is_integrated=integrated)


class Finite:
    """All that scripts/check_gpu.py asks of a tensor, a module and a comparison."""

    def to(self, device):
        return self

    def __call__(self, values):
        return self

    def all(self):
        return True


def device(name):
    return name


def inference_mode():
    return nullcontext()


def autocast(device_type, dtype):
    return nullcontext()


def randn(shape, device):
    return Finite()


def isfinite(values):
    return values


cuda = SimpleNamespace(
    is_available=lambda: bool(gpus),
    device_count=lambda: len(gpus),
    get_device_name=lambda index: gpus[index][0],
    get_device_properties=properties,
    synchronize=lambda device: None,
)
nn = SimpleNamespace(Conv3d=lambda *args, **kwargs: Finite())
