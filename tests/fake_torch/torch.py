"""Stands in for PyTorch in device-check children, so those tests never reach a GPU.

A test puts this directory on PYTHONPATH. SCAN3D_FAKE_GPUS is a JSON list of
[name, total_memory, is_integrated], and SCAN3D_FAKE_TORCH selects a failure.
"""

import json
import os
import signal
import time
from types import SimpleNamespace

# Native runtimes log straight to the descriptor, past Python's sys.stdout.
os.write(1, b"fake native runtime log\n")

failure = os.environ.get("SCAN3D_FAKE_TORCH")
if failure == "missing":
    raise ModuleNotFoundError("No module named 'torch'")
if failure == "crash":
    os.kill(os.getpid(), signal.SIGKILL)
if failure == "hang":
    time.sleep(600)

gpus = json.loads(os.environ.get("SCAN3D_FAKE_GPUS", "[]"))
__version__ = "0.0+fake"
version = SimpleNamespace(hip="7.2.fake", cuda=None)


def properties(index):
    _, memory, integrated = gpus[index]
    return SimpleNamespace(total_memory=memory, is_integrated=integrated)


cuda = SimpleNamespace(
    is_available=lambda: bool(gpus),
    device_count=lambda: len(gpus),
    get_device_name=lambda index: gpus[index][0],
    get_device_properties=properties,
)
