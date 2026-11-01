"""Inference paths: the interpreter and GPU that TotalSegmentator and nnInteractive run on.

A torch build drives one vendor's GPUs, never both, so on a workstation with two vendors'
cards a choice of card is a choice of interpreter. `default` is the API's own interpreter
with SCAN3D_DEVICE, and is implicit, so adding a path never changes what it means. An added
path is one proof file, <directory()>/<id>.json, written by `scripts/check_gpu.py --path ID`
from inside that interpreter. The file is the registration: there is no list to edit and no
restart, a failed check still appears with its reason, and moving the file out removes the
path. Anyone who can write that directory can already change this checkout, so it adds no
trust boundary.

This runs in the API process and in a venv holding only the NVIDIA stack, so it imports the
standard library and two torch-free scan3d modules, and it starts no process: opening a
panel must never load torch or touch a busy card. The device check at job start is the live
authority. 'Available' means that what the proof records is still installed and its last check
passed, never that a scan will fit.
"""

from dataclasses import dataclass
from functools import partial
from importlib import metadata
import json
import os
from pathlib import Path
import re
import subprocess

from . import doctor
from .config import ROOT, device_request

DEFAULT = "default"
ID_PATTERN = r"^[a-z0-9][a-z0-9-]{0,31}$"
GPU_REQUEST = r"gpu(:\d+)?"
SYS_DRM = Path("/sys/class/drm")

_VENDORS = {"rocm": "0x1002", "cuda": "0x10de"}
# Each pin must match the API's, so weights, the task catalog and SEG AlgorithmVersion stay true.
_ENGINES = ("TotalSegmentator", "nnunetv2", "nnInteractive", "nninteractive-client")
# Once importable, either switches TotalSegmentator to a different resampler on one vendor only,
# and no version the fingerprint records would show it.
_RESAMPLERS = ("cupy", "cucim")
# A child inherits the API's environment except what selects another stack's card, breaks its
# libraries or points at its caches. Only these can change a result, while an allowlist would
# drop settings a person put in scan3d.env. A visibility variable is never set either: each
# stack sees only its own vendor, and HIP reads CUDA_VISIBLE_DEVICES too.
_WITHHELD = (
    "CUDA_VISIBLE_DEVICES",
    "HIP_VISIBLE_DEVICES",
    "ROCR_VISIBLE_DEVICES",
    "GPU_DEVICE_ORDINAL",
    "LD_LIBRARY_PATH",
    "LD_PRELOAD",
    "nnUNet_compile",
    "TORCHINDUCTOR_CACHE_DIR",
    "TRITON_CACHE_DIR",
)

# What scripts/add_inference_path.sh keeps in an environment it made. The second file goes before
# the script changes anything and comes back when it has finished.
_MADE_BY_SCRIPT = ".scan3d-inference-path"
_FINISHED = ".scan3d-inference-path-finished"

# Rows, refusals and job errors carry fixed sentences only. Proof text can hold local paths and
# raw errors, and these surfaces are public.
_NOT_SET_UP = "This GPU is not set up on this workstation."
_UNREADABLE = "Its GPU check record cannot be read."
_CHECK_FAILED = "Its last GPU check failed."
_INSTALL_STOPPED = "Its last install did not finish."
_NO_PYTHON = "Its Python environment is missing."
_PACKAGES_CHANGED = "Its PyTorch or GPU runtime packages changed after the last GPU check."
_ENGINE_DIFFERS = "Its TotalSegmentator, nnU-Net or nnInteractive version differs from this workstation's."
_RESAMPLER_PRESENT = "It has cupy or cucim installed, which would change resampling."
_HARDWARE_CHANGED = "The GPUs in this workstation changed after the last GPU check."
_DEFAULT_STALE = "Its last GPU check failed or is out of date."
_NOTHING_RAN = "Nothing ran and no other GPU was tried."
# What a damaged or hand-made proof can raise while it is judged. RecursionError is how a parser
# gives up on deep nesting.
_BAD_RECORD = (AttributeError, KeyError, OSError, RecursionError, TypeError, ValueError)

# ImportError and dlopen text leaves paths unquoted, and a directory can hold a space, so a space
# ends a path only when no later word on its line has a slash. What follows a path's last slash
# cannot be told from prose, and stays. A web address is matched first only so that it is kept,
# because its `//host/...` reads as a path: PyTorch's out-of-memory error ends with one. With no
# host it is only a path with a prefix, so it falls through to the path rule.
_PATH_CHAR = r"""[^\s'"`,;:()<>\[\]{}]"""
_LOCAL_PATH = re.compile(
    rf"""(https?://(?!/){_PATH_CHAR}+)|'/[^']*'|"/[^"]*"|(?<![\w.~-])/{_PATH_CHAR}+(?:(?: +{_PATH_CHAR}+)* +{_PATH_CHAR}*/{_PATH_CHAR}*)?"""
)


def installed(site, name):
    """A package's version in one site-packages, read without importing anything from it."""
    for distribution in metadata.distributions(name=name, path=[str(site)]):
        # Not .version, which warns about the record a broken install can leave.
        if version := (distribution.metadata or {}).get("Version"):
            return version
    raise metadata.PackageNotFoundError(name)


@dataclass(frozen=True)
class InferencePath:
    """One proven added path. Equality is full identity, so a proof that changes a field is a new path.

    `python` is what sys.executable reported inside that venv. It is never resolved: a venv's
    interpreter is a symlink, and its target would run outside the venv.
    """

    id: str
    python: str
    site_packages: str
    device: str
    kind: str
    gpu_name: str
    memory_bytes: int
    packages: tuple[tuple[str, str], ...]
    hardware: tuple[str, ...] | None = None

    @property
    def label(self):
        return self.gpu_name

    @property
    def identity(self):
        """Keys cached results, so a replaced card is a different key. Nothing parses this."""
        return f"{self.id}|{self.device}|{self.gpu_name}"

    def environment(self):
        """The one environment every child on this path gets; children never load scan3d.env.

        XDG_CACHE_HOME is replaced because a launcher may aim it at another stack's compiled
        kernels. SCAN3D_DEVICE is explicit because nnInteractive reads it from its own
        environment while TotalSegmentator is told on its command line, so inheriting it
        could split the two.
        """
        env = {key: value for key, value in os.environ.items() if key not in _WITHHELD}
        env["PATH"] = os.pathsep.join(filter(None, (str(Path(self.python).parent), env.get("PATH"))))
        env["XDG_CACHE_HOME"] = str(ROOT / ".cache" / "inference-paths" / self.id)
        env["SCAN3D_DEVICE"] = self.device
        # Means only that this child belongs to an added path. Only the device check's remedy reads it.
        env["SCAN3D_INFERENCE_PATH"] = self.id
        return env

    def versions(self):
        """The fingerprint's versions, from this path's site-packages, because that is what runs."""
        versions = {}
        for name in ("TotalSegmentator", "nnunetv2", *dict(self.packages)):
            try:
                versions[name] = installed(self.site_packages, name)
            except metadata.PackageNotFoundError:
                versions[name] = "unavailable"
        return versions

    def confirm(self, device, gpu_name, cuda):
        """Refuse a run unless this interpreter measured the card and build its proof named.

        A proof can outlive its hardware or its torch, and any surprise here must block a run
        rather than mislabel it. nnInteractive measures while it runs, hence the wording.
        """
        selected = device == self.device or (self.device == "gpu" and re.fullmatch(GPU_REQUEST, str(device)) is not None)
        if not selected or gpu_name != self.gpu_name or bool(cuda) != (self.kind == "cuda"):
            found = f"{gpu_name or 'no GPU'} ({device}, {'CUDA' if cuda else 'no CUDA'})"
            raise RuntimeError(f"Expected {self.label}, but the GPU check found {found}. No result was kept and no other GPU was tried.")


def directory():
    """Where added paths keep their proofs, read per call so a change needs no restart."""
    return Path(os.environ.get("SCAN3D_INFERENCE_PATH_DIR", "").strip() or ROOT / ".runtime" / "inference-paths")


def _card_file(card, name):
    try:
        return (SYS_DRM / card / "device" / name).read_text().strip()
    except OSError:
        return None


def gpu_cards(kind):
    """Sorted vendor:device ids of one vendor's cards, read with no GPU runtime loaded.

    One vendor's census, so installing the other vendor's card does not invalidate a proof.
    A twin card repeats its id, so removing one of a pair still counts as a change.
    """
    vendor = _VENDORS.get(kind)
    ids = [(_card_file(card.name, "vendor"), _card_file(card.name, "device")) for card in SYS_DRM.glob("card*")]  # Connectors have neither.
    return sorted(f"{found.removeprefix('0x')}:{model.removeprefix('0x')}" for found, model in ids if vendor and found == vendor and model)


def _shares_display(kind):
    """Whether a card of this vendor drives a monitor now, so its free memory follows the screen.

    Connector state rather than boot_vga, which names the firmware's first card and survives
    a cable moving to another one.
    """
    vendor = _VENDORS.get(kind)
    for connector in SYS_DRM.glob("card*-*"):
        try:
            connected = (connector / "status").read_text().strip() == "connected"
        except OSError:
            continue
        if connected and vendor and _card_file(connector.name.partition("-")[0], "vendor") == vendor:
            return True
    return False


def _showable(name):
    """Whether a card name from a proof can reach a response.

    JSON can spell a lone surrogate and UTF-8 cannot encode one, so showing it would fail the
    whole response, even for someone using only the default path.
    """
    try:
        return isinstance(name, str) and bool(name.encode())
    except UnicodeEncodeError:
        return False


def _row(name, label, memory, kind, reason=None, available=True):
    return {
        "id": name,
        "label": label,
        "memory_gib": (memory + 2**29) // 2**30 if type(memory) is int else None,
        "shares_display": _shares_display(kind),
        "available": available,
        "reason": reason,
    }


def _default_card(checks):
    """The proof's entry for the card the default path uses, or nothing."""
    request = device_request()
    chosen = request if request.startswith("gpu:") else checks.get("recommended_device")
    devices = checks.get("devices")
    if isinstance(chosen, str) and re.fullmatch(r"gpu:\d+", chosen) and isinstance(devices, list) and int(chosen[4:]) < len(devices):
        card = devices[int(chosen[4:])]
        if isinstance(card, dict) and _showable(card.get("name")):
            return card
    return {}


def _default_row():
    """Always available, because a stale proof blocks nothing today.

    A Python patch bump makes a proof stale on a rolling distribution, and that must never
    block or displace the card that is working. It earns a caution, and the job-start device
    check still decides. A proof that cannot be parsed names no card and blocks nothing either,
    and nor does one taken before this vendor's cards changed, whose card may have left. A
    proof with no census predates it and is not compared.
    """
    if device_request() == "cpu":
        return _row(DEFAULT, "CPU", None, None)
    proof = doctor.proof_path()
    try:
        checks = json.loads(proof.read_text())["checks"]
        card = _default_card(checks)
        kind = "cuda" if checks.get("cuda_version") else "rocm" if checks.get("hip_version") else None
        stale = not doctor.diagnose(live=False)["ready"]
        hardware = checks.get("hardware")
        if hardware is not None and list(hardware) != gpu_cards(kind):
            card, stale = {}, True
    except _BAD_RECORD:
        card, kind, stale = {}, None, proof.exists()
    return _row(DEFAULT, card.get("name") or "Default device", card.get("memory_bytes"), kind, _DEFAULT_STALE if stale else None)


def _inference_path(record, checks):
    """A passing proof as a path. Anything malformed raises, and only this proof's row suffers."""
    hardware = checks.get("hardware")
    path = InferencePath(
        record["id"],
        record["python"],
        record["site_packages"],
        record["device"],
        record["kind"],
        record["gpu_name"],
        record["memory_bytes"],
        tuple(sorted(checks["runtime_versions"].items())),
        None if hardware is None else tuple(hardware),
    )
    named = all(isinstance(value, str) and value for value in (path.python, path.site_packages)) and _showable(path.gpu_name)
    if not named or not path.packages or path.kind not in _VENDORS or type(path.memory_bytes) is not int or re.fullmatch(GPU_REQUEST, path.device) is None:
        raise ValueError("Invalid inference path record")
    return path


def _version(lookup, name):
    try:
        return lookup(name)
    except metadata.PackageNotFoundError:
        return None


def _stale(path):
    """The first installed fact that no longer matches the proof, as a fixed sentence, or None."""
    # First, because everything below can pass on a half-installed environment: a package's
    # dist-info is no sign that its files arrived. A hand-made environment has neither file.
    environment = Path(path.python).parent.parent
    if (environment / _MADE_BY_SCRIPT).exists() and not (environment / _FINISHED).exists():
        return _INSTALL_STOPPED
    if not os.access(path.python, os.X_OK):
        return _NO_PYTHON
    here = partial(installed, path.site_packages)
    if not doctor._matching_versions(dict(path.packages), here):
        return _PACKAGES_CHANGED
    if any(_version(here, name) != _version(metadata.version, name) for name in _ENGINES):
        return _ENGINE_DIFFERS
    # By file name, not distribution: upstream's switch is whether cupy imports, and CuPy's
    # wheels are named cupy-cuda13x and the like.
    if any(next(Path(path.site_packages).glob(f"{name}*"), None) for name in _RESAMPLERS):
        return _RESAMPLER_PRESENT
    if path.hardware is not None and list(path.hardware) != gpu_cards(path.kind):
        return _HARDWARE_CHANGED
    return None


def _examine(name):
    """Judge one proof: (path or None, reason or None, its `path` block as written)."""
    try:
        proof = json.loads((directory() / f"{name}.json").read_text())
        record, checks = proof["path"], proof["checks"]
        if record["id"] != name or not isinstance(checks, dict):
            raise ValueError("Invalid inference path record")
    except _BAD_RECORD:
        return None, _UNREADABLE, {}
    if proof.get("gpu_ready") is not True:
        return None, _CHECK_FAILED, record
    try:
        path = _inference_path(record, checks)
        reason = _stale(path)
    except (OSError, RecursionError, UnicodeDecodeError):  # The proof is sound: a package record its check read no longer reads.
        return None, _PACKAGES_CHANGED, record
    except _BAD_RECORD:
        return None, _UNREADABLE, record
    return None if reason else path, reason, record


def _label(name, record):
    gpu_name = record.get("gpu_name")
    return gpu_name if _showable(gpu_name) else name


def _is_added(name):
    return re.fullmatch(ID_PATTERN, name) is not None and name != DEFAULT


def rows():
    """Every path a person could pick: default first, then added paths by id.

    Merged into the two panel routes, which are re-read when a panel opens. No local path or
    proof text appears here, and a bad file disables its own row only.
    """
    result = [_default_row()]
    for name in sorted(file.stem for file in directory().glob("*.json") if _is_added(file.stem)):
        _, reason, record = _examine(name)
        kind = record.get("kind")
        result.append(_row(name, _label(name, record), record.get("memory_bytes"), kind if isinstance(kind, str) else None, reason, reason is None))
    return result


def binding(name):
    """Freeze a requested path as {id, label, identity}, or refuse it with a fixed sentence."""
    if name == DEFAULT:
        return {"id": DEFAULT, "label": _default_row()["label"], "identity": DEFAULT}
    if not isinstance(name, str) or not _is_added(name) or not (directory() / f"{name}.json").is_file():
        raise ValueError(_NOT_SET_UP)
    path, reason, record = _examine(name)
    if path is None:
        raise ValueError(f"{_label(name, record)} is unavailable: {reason}")
    return {"id": path.id, "label": path.label, "identity": path.identity}


def require(bound):
    """Re-check a frozen binding as its job starts; None means the default path.

    A removed, unavailable or changed path fails the job by name. It never re-routes, and it
    reads no file for a job that named no path.
    """
    if bound is None or bound["id"] == DEFAULT:
        return None
    if not (directory() / f"{bound['id']}.json").is_file():
        raise ValueError(f"{bound['label']} is no longer available: {_NOT_SET_UP} {_NOTHING_RAN}")
    path, reason, _ = _examine(bound["id"])
    if path is None:
        raise ValueError(f"{bound['label']} is no longer available: {reason} {_NOTHING_RAN}")
    if path.identity != bound["identity"]:
        raise ValueError(f"{bound['label']} changed after this job was queued. {_NOTHING_RAN} Start it again.")
    return path


def replaced(bound):
    """Whether a frozen binding's ID now names another card, one that passed its own check.

    A job on it could only fail as it starts, on every attempt. A path that is gone or unavailable
    is not replaced: its job says so by name.
    """
    if bound is None or bound["id"] == DEFAULT:
        return False
    path, _, _ = _examine(bound["id"])
    return path is not None and path.identity != bound["identity"]


def identity(bound):
    """Compare two bindings. Absent and explicit default are one path."""
    return bound["identity"] if bound else DEFAULT


def public_error(exc):
    """What a failed job on an added path may say publicly; the journal keeps the full detail."""
    if isinstance(exc, subprocess.TimeoutExpired):  # Its text holds the whole command line.
        return "The AI process did not stop in time."
    return _LOCAL_PATH.sub(lambda found: found[1] or "<local path>", str(exc) or type(exc).__name__)
