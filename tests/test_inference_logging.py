"""Reproduce nnU-Net's hidden-stdout manager buffering without inference."""

import os
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.skipif(not hasattr(os, "fork"), reason="Linux fork-buffer regression")
def test_unbuffered_worker_prevents_manager_replaying_hidden_stdout():
    # nnU-Net creates its preprocessing Manager while TotalSegmentator's
    # nostdout context hides sys.stdout. multiprocessing cannot flush the real
    # stream before fork; the manager restores it at shutdown and replays its
    # inherited buffer. The GPU/model never runs in this reproduction.
    program = textwrap.dedent("""
        import contextlib
        import multiprocessing
        import sys

        class DummyFile:
            def write(self, value): pass
            def flush(self): pass

        @contextlib.contextmanager
        def nostdout():
            saved = sys.stdout
            sys.stdout = DummyFile()
            try:
                yield
            finally:
                sys.stdout = saved

        if __name__ == "__main__":
            for part in range(1, 4):
                print(f"Predicting part {part}")
                with nostdout():
                    with multiprocessing.get_context("fork").Manager() as manager:
                        manager.Event()
    """)
    environment = {key: value for key, value in os.environ.items() if key != "PYTHONUNBUFFERED"}

    def lines(flags):
        result = subprocess.run([sys.executable, *flags, "-c", program],
                                env=environment, capture_output=True, text=True,
                                timeout=30, check=True)
        return result.stdout.splitlines()

    expected = ["Predicting part 1", "Predicting part 2", "Predicting part 3"]
    buffered = lines([])
    assert buffered.count(expected[0]) > 1, "Control must reproduce inherited log output"
    assert lines(["-u"]) == expected
