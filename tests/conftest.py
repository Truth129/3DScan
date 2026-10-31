"""Suite-wide isolation from this machine's own configuration."""

import pytest


@pytest.fixture(scope="session", autouse=True)
def no_added_inference_paths(tmp_path_factory):
    """One path, whatever this checkout has activated, here and in every child a test spawns.

    A test that wants added paths points the variable at its own directory.
    """
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("SCAN3D_INFERENCE_PATH_DIR", str(tmp_path_factory.mktemp("no-inference-paths")))
        yield
