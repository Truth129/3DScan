"""Keep the NVIDIA lock honest without installing it.

requirements/cuda-py314.lock is installed only on a workstation with an NVIDIA card, so nothing
on CI would see it drift from the AMD stack, or see a child start importing a package it lacks.
These read text and run a short child, so they need no torch and no AI package.
"""

import json
import os
import re
import subprocess
import sys
import tomllib

from scan3d.config import ROOT

REQUIREMENTS = ROOT / "requirements"
LOCK = REQUIREMENTS / "cuda-py314.lock"
SOURCE = REQUIREMENTS / "cuda-py314.in"
AMD_LOCK = REQUIREMENTS / "rocm10-py314.lock"
CONSTRAINTS = REQUIREMENTS / "constraints.txt"
CHILD_MODULES = ("scan3d.segmentation", "scan3d.interactive", "scan3d.interactive_worker")
# Named one by one, so a package the two stacks do not share has to be explained here.
GPU_FAMILY = frozenset(
    {
        "torch",
        "torchvision",
        "triton",
        "cuda-bindings",
        "cuda-pathfinder",
        "cuda-toolkit",
        "nvidia-cublas",
        "nvidia-cuda-cupti",
        "nvidia-cuda-nvrtc",
        "nvidia-cuda-runtime",
        "nvidia-cudnn-cu13",
        "nvidia-cufft",
        "nvidia-cufile",
        "nvidia-curand",
        "nvidia-cusolver",
        "nvidia-cusparse",
        "nvidia-cusparselt-cu13",
        "nvidia-nccl-cu13",
        "nvidia-nvjitlink",
        "nvidia-nvshmem-cu13",
        "nvidia-nvtx",
    }
)
# pydicom imports this decoder under `except ImportError` and no child decodes DICOM, so the
# lock leaves it out. The child below blocks it, so an unguarded import fails there as it would
# on the NVIDIA runtime, and this list cannot hide one.
GUARDED = ("pyjpegls",)

# A child, so that only these imports count, not pytest's or another test's. A module is judged
# by where its file is, because extension modules appear under bare names, numpy's among them,
# and Cython and SWIG add modules with no file, which nothing installs.
CLOSURE = r"""
import importlib
from importlib import metadata
import json
from pathlib import Path
import re
import site
import sys

before = set(sys.modules)
modules, guarded = json.loads(sys.argv[1]), json.loads(sys.argv[2])
owners = metadata.packages_distributions()


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


blocked = {top for top, names in owners.items() if any(canonical(name) in guarded for name in names)}


class Absent:
    @staticmethod
    def find_spec(name, path=None, target=None):
        if name.partition(".")[0] in blocked:
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, Absent)
for module in modules:
    importlib.import_module(module)
found = {}
for name, module in list(sys.modules.items()):
    origin = getattr(module, "__file__", None)
    if name in before or not origin:
        continue
    for folder in (*site.getsitepackages(), site.getusersitepackages()):
        if Path(origin).is_relative_to(folder):
            top = Path(origin).relative_to(folder).parts[0].partition(".")[0]
            found[top] = sorted(canonical(owner) for owner in owners.get(top, ()))
found.pop("scan3d", None)
print(json.dumps(found))
"""


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def pins(path):
    """Every `name==version` in a requirements file, by normalized name."""
    found = {}
    for line in path.read_text().splitlines():
        if match := re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:\[[^\]]*\])?==([^\s;\\]+)", line):
            found[canonical(match[1])] = match[2]
    return found


def requirements():
    """The .in file's requirement lines, with None for a package named without a version."""
    found = {}
    for line in SOURCE.read_text().splitlines():
        line = line.partition("#")[0].strip()
        if line and not line.startswith("-"):
            name, _, version = line.partition("==")
            found[canonical(name)] = version or None
    return found


def closure(*modules):
    """The third-party modules that importing `modules` loads, each with its distributions."""
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH"))))}
    arguments = [json.dumps(modules), json.dumps(GUARDED)]
    completed = subprocess.run([sys.executable, "-P", "-c", CLOSURE, *arguments], env=environment, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    return json.loads(completed.stdout)


def unpinned(found):
    locked = pins(LOCK)
    return {module: owners for module, owners in found.items() if not locked.keys() & set(owners)}


def test_everything_a_child_imports_is_in_the_lock():
    found = closure(*CHILD_MODULES)
    # The packages the addendum names, so an empty result cannot pass.
    assert {"numpy", "scipy", "nibabel", "pydicom", "requests"} <= found.keys()
    assert unpinned(found) == {}


def test_the_closure_reports_an_import_the_lock_lacks():
    # pytest is in every environment that runs this, and no child needs it.
    assert unpinned(closure("pytest"))["pytest"] == ["pytest"]


def test_a_guarded_package_is_one_the_lock_really_lacks():
    assert not pins(LOCK).keys() & set(GUARDED)


def test_every_pin_outside_the_gpu_family_equals_the_amd_lock():
    amd = pins(AMD_LOCK)
    assert {name: (version, amd.get(name)) for name, version in pins(LOCK).items() if name not in GPU_FAMILY and amd.get(name) != version} == {}


def test_the_gpu_family_lists_only_what_is_locked():
    assert GPU_FAMILY <= pins(LOCK).keys()


def test_no_pin_has_a_local_version_tag():
    # A +cu126 wheel also satisfies torch==2.14.0 and has no sm_120 code, and the hashes that
    # exclude it only mean something for a bare PyPI version.
    locked = pins(LOCK)
    assert locked["torch"] == "2.14.0"
    assert {name: version for name, version in locked.items() if "+" in version} == {}


def test_nothing_locked_would_change_resampling():
    assert [name for name in pins(LOCK) if name.startswith(("cupy", "cucim"))] == []


def test_every_pin_carries_a_hash():
    # --require-hashes refuses a pin with none, which would only show up on the workstation.
    lines = LOCK.read_text().splitlines()
    assert [line for line, following in zip(lines, lines[1:]) if re.match(r"[a-z0-9]", line) and not following.startswith("    --hash=sha256:")] == []


def test_every_line_of_the_source_is_honoured():
    locked = pins(LOCK)
    wanted = requirements()
    assert {"numpy", "scipy", "nibabel", "pydicom", "requests", "torch", "torchvision"} <= wanted.keys()
    assert {name: version for name, version in wanted.items() if locked.get(name) is None or version not in (None, locked[name])} == {}


def test_the_lock_keeps_the_constraints():
    assert "-c constraints.txt" in SOURCE.read_text().splitlines()
    locked = pins(LOCK)
    assert {name: version for name, version in pins(CONSTRAINTS).items() if name in locked and locked[name] != version} == {}


def test_the_ai_pins_equal_the_project_extra():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    extra = dict(pin.split("==") for pin in project["optional-dependencies"]["ai"])
    assert len(extra) == 3
    wanted = requirements()
    assert {canonical(name): version for name, version in extra.items()} == {canonical(name): wanted[canonical(name)] for name in extra}
