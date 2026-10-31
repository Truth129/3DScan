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


class Engine:
    def __init__(self):
        self.loads = self.requests = 0
        self.warm = False

    def predict(self, ct_path, mask_path, prompts, output_path, progress, cancelled, session_key, *, batches=None, options=None):
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


class PointwiseEngine(Engine):
    """Reports pointwise refinement and prefix reuse as the real runtime does,
    with a cancellable step per new point, for the validation harness."""

    def __init__(self):
        super().__init__()
        self.session, self.applied = None, []

    def refine(self, prompts, progress, cancelled, session_key):
        reused = self.session == session_key and prompts[: len(self.applied)] == self.applied
        if not reused:
            self.session, self.applied = session_key, []
        start = len(self.applied)
        try:
            for index in range(start, len(prompts)):
                progress(f"Refining segmentation with point {index + 1} of {len(prompts)}")
                deadline = time.monotonic() + 0.2
                while time.monotonic() < deadline:
                    if cancelled():
                        raise InterruptedError("Fake correction cancelled")
                    time.sleep(0.005)
                self.applied = prompts[: index + 1]
        except BaseException:
            self.session, self.applied = None, []
            raise
        return {"reused_prompt_prefix": reused, "predictions_this_request": len(prompts) - start}

    def release(self):
        super().release()
        self.session, self.applied = None, []
