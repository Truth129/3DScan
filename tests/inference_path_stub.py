"""A real second interpreter for inference-path tests, with no second PyTorch.

A venv gives a child its own sys.executable, sys.prefix and site-packages, so a test can
prove where a child ran instead of only what its argv said. tests/fake_torch stands in for
PyTorch and the parent's site-packages supplies numpy and the rest, so nothing here needs a
GPU or a download.

    stub = stand_in(tmp_path)      # The venv, its package stubs and a passing proof.
    activate(monkeypatch, stub)    # Discoverable, and its children can run.

The stand-in's site-packages precedes the parent's on PYTHONPATH, so its stubs win over a
real TotalSegmentator, and shadow() can replace any other module for these children alone.
Its proof has no `checks.hardware`: that compares with sysfs, so a test adds it deliberately.
"""

from dataclasses import dataclass
from importlib import metadata
import json
import os
from pathlib import Path
import sysconfig
import venv

FAKE_TORCH = Path(__file__).resolve().parent / "fake_torch"
GPU_NAME = "Test Added GPU"
MEMORY_BYTES = 16 * 1024**3
TORCH = "0.0+fake"  # fake_torch's __version__.
# The stand-in's own family, which a test can tell from anything in the parent.
FAMILIES = {"cuda": {"cuda-toolkit": "13.0.3", "nvidia-cudnn-cu13": "9.24.0.43"}, "rocm": {"rocm": "10.1.0"}}
ENGINES = ("TotalSegmentator", "nnunetv2", "nnInteractive", "nninteractive-client")


def add_package(site_packages, name, version):
    """Install a distribution as discovery reads one, which is only its metadata."""
    info = Path(site_packages) / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n")
    metadata.MetadataPathFinder.invalidate_caches()  # Its cache keys on a directory's mtime.


@dataclass
class StandIn:
    name: str
    python: str
    site_packages: Path
    proof: Path
    kind: str
    gpu_name: str
    memory_bytes: int

    def record(self):
        return json.loads(self.proof.read_text())

    def save(self, record):
        self.proof.parent.mkdir(parents=True, exist_ok=True)
        self.proof.write_text(json.dumps(record))

    def change(self, edit):
        """Rewrite the proof through `edit(record)`, as a later check or a hand edit would."""
        record = self.record()
        edit(record)
        self.save(record)

    def shadow(self, module, source=""):
        """Give this interpreter's children a module, creating the packages above it."""
        *packages, name = module.split(".")
        folder = self.site_packages
        for package in packages:
            folder = folder / package
            folder.mkdir(exist_ok=True)
            (folder / "__init__.py").touch()
        (folder / f"{name}.py").write_text(source)


def stand_in(root, name="nvidia", *, proofs=None, device="gpu", kind="cuda", gpu_name=GPU_NAME, memory_bytes=MEMORY_BYTES):
    """Build a stand-in under `root`. Its proof goes in `proofs`, by default `root`/paths."""
    environment = Path(root) / f"venv-{name}"
    venv.EnvBuilder(with_pip=False, symlinks=True).create(environment)
    site = environment / "lib" / f"python{sysconfig.get_python_version()}" / "site-packages"
    proof = Path(proofs or Path(root) / "paths") / f"{name}.json"
    stub = StandIn(name, str(environment / "bin" / "python"), site, proof, kind, gpu_name, memory_bytes)
    family = {"torch": TORCH, **FAMILIES[kind]}
    for package, version in family.items():
        add_package(site, package, version)
    for package in ENGINES:  # Discovery requires this process's own versions.
        try:
            add_package(site, package, metadata.version(package))
        except metadata.PackageNotFoundError:
            pass
    # scripts/check_gpu.py imports these on a path run, and the real ones need real PyTorch.
    stub.shadow("totalsegmentator.python_api")
    stub.shadow("nnInteractive.inference.inference_session")
    stub.save(
        {
            "gpu_ready": True,
            "checked_at": "stand-in",
            "python_version": "stand-in",
            "checks": {
                "torch_version": TORCH,
                "torch_distribution_version": TORCH,
                "hip_version": None if kind == "cuda" else "7.2.fake",
                "cuda_version": "13.0.fake" if kind == "cuda" else None,
                "gpu_available": True,
                "runtime_versions": family,
                "devices": [{"index": 0, "name": gpu_name, "memory_bytes": memory_bytes}],
                "recommended_device": "gpu:0",
            },
            "errors": [],
            "path": {
                "id": name,
                "python": stub.python,
                "site_packages": str(site),
                "device": device,
                "kind": kind,
                "gpu_name": gpu_name,
                "memory_bytes": memory_bytes,
            },
        }
    )
    return stub


def child_path(stub):
    """PYTHONPATH for this stand-in's children; the parent fills in what the venv lacks."""
    parents = dict.fromkeys(sysconfig.get_path(name) for name in ("purelib", "platlib"))
    return os.pathsep.join(filter(None, (str(FAKE_TORCH), str(stub.site_packages), *parents, os.environ.get("PYTHONPATH"))))


def activate(monkeypatch, stub, gpus=None):
    """Make this stand-in discoverable, with children on the fake PyTorch seeing `gpus`."""
    monkeypatch.setenv("SCAN3D_INFERENCE_PATH_DIR", str(stub.proof.parent))
    monkeypatch.setenv("PYTHONPATH", child_path(stub))
    monkeypatch.setenv("SCAN3D_FAKE_TORCH_BUILD", stub.kind)
    monkeypatch.setenv("SCAN3D_FAKE_TORCH", "")
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", json.dumps(gpus if gpus is not None else [[stub.gpu_name, stub.memory_bytes, 0]]))
