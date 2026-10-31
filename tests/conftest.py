"""Suite-wide isolation from this machine's own configuration."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def no_machine_inference_state(tmp_path_factory):
    """One unproven path, whatever this checkout holds, here and in every child a test spawns.

    A test that wants added paths or a default proof points the variable at its own. The proof
    has its own folder, so a test that writes one can never read as an added path.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SCAN3D_INFERENCE_PATH_DIR", str(tmp_path_factory.mktemp("no-inference-paths")))
        patch.setenv("SCAN3D_GPU_PROOF", str(tmp_path_factory.mktemp("no-gpu-proof") / "gpu-proof.json"))
        yield
