"""Deterministic engines for the supervised nnInteractive child in tests.

They import neither torch nor a GPU runtime, except MeasuredEngine, which asks
the fake PyTorch. A request's session key selects its behaviour, so one child
can serve warm, cancelled and failing requests.
"""

import os
import signal
import sys
import time

from scan3d import imaging
from scan3d.config import device_request
from scan3d.segmentation import _device_info

# How PyTorch's out-of-memory error ends, from the installed library's own string.
OUT_OF_MEMORY = (
    "CUDA out of memory. Tried to allocate 2.00 GiB. GPU 0 has a total capacity of 15.57 GiB of which 1.20 GiB is free. "
    "If reserved but unallocated memory is large try setting PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True to avoid fragmentation.  "
    "See documentation for Memory Management  (https://docs.pytorch.org/docs/stable/notes/cuda.html#optimizing-memory-usage-with-pytorch-cuda-alloc-conf)"
)


class Engine:
    def __init__(self):
        self.loads = self.requests = 0
        self.warm = False

    def predict(self, ct_path, mask_path, prompts, output_path, progress, cancelled, session_key, *, batches=None, options=None, stored=None):
        self.requests += 1
        if not self.warm:
            progress("Loading the fake correction model")
            self.loads += 1
            self.warm = True
        behaviour = session_key.partition(":")[0]
        try:
            if behaviour == "cooperative":
                progress("Running a cancellable step")
                while not cancelled():
                    time.sleep(0.01)
                raise InterruptedError("Fake correction cancelled")
            if behaviour == "uninterruptible":
                progress("Running a kernel that ignores cancellation")
                time.sleep(3600)
            if behaviour == "crash":
                progress("Faulting mid-request")
                os.kill(os.getpid(), signal.SIGKILL)
            if behaviour == "silent":
                time.sleep(3600)
            if behaviour == "invalid":
                raise ValueError("Fake prompt lies outside the reference CT")
            if behaviour == "fail":
                raise RuntimeError("Fake GPU prediction failed")
            fields = self.refine(prompts, progress, cancelled, session_key)
        except BaseException:
            self.warm = False  # Like the real engine, any failure drops warm state.
            raise
        progress("Writing the fake candidate")
        image, affine = imaging._image(ct_path)
        mask, _, _ = imaging._binary(mask_path, image, affine)
        mask[1, 1, 1] = True
        imaging._save_mask(mask, image, output_path)
        return {
            "engine": "fake-nninteractive",
            "pid": os.getpid(),
            "prefix": sys.prefix,  # The venv that actually ran this, which argv cannot prove.
            "loads": self.loads,
            "requests": self.requests,
            "prompts": prompts,
            "batches": batches,
            "options": options,
            "stored": stored,
            "session_key": session_key,
            "torch_imported": "torch" in sys.modules,
            **fields,
        }

    def refine(self, prompts, progress, cancelled, session_key):
        return {}

    def release(self):
        self.warm = False
        if marker := os.environ.get("SCAN3D_FAKE_ENGINE_RELEASES"):
            with open(marker, "a") as stream:
                stream.write(f"{os.getpid()}\n")


class MeasuredEngine(Engine):
    """Measures its card where the real engine does: its own SCAN3D_DEVICE, its own PyTorch.

    With fake PyTorch on the path, a review job's whole chain runs with no GPU.
    """

    def refine(self, prompts, progress, cancelled, session_key):
        device = _device_info(device_request())
        return {
            "device": device["selected_device"],
            "gpu_name": device.get("gpu_name"),
            "torch_version": device["torch"],
            "hip_version": device.get("hip"),
            "cuda_version": device.get("cuda"),
        }


class ReportingEngine(MeasuredEngine):
    """Says no more about itself than the real engine does, so a leak test sees only the code's own."""

    def refine(self, prompts, progress, cancelled, session_key):
        return {**super().refine(prompts, progress, cancelled, session_key), "prefix": None}


class OutOfMemoryEngine(Engine):
    """Fails as an allocator failure reaches the real engine: PyTorch's text in a RuntimeError."""

    def refine(self, prompts, progress, cancelled, session_key):
        raise RuntimeError(OUT_OF_MEMORY)
