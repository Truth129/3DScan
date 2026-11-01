"""scripts/add_inference_path.sh, run from a sealed copy of a checkout.

Nothing real can run here. The script is copied into a temporary root, PATH holds only the
fakes below and a few coreutils, and every fake records its call, so these tests behave the
same on a workstation with an NVIDIA card as on CI. nvidia-smi and df only answer, and uv keeps
a record of what it installed, which it treats the way real uv treats dist-info, a torn RECORD
included. find runs for real once it has been recorded. The two interpreters are this test's own
Python running a stub check, with the real settings code copied beside it, so the folder the
script names is the one the code would use. A few tests swap in the real check to ask it about the
queue, and past that its PyTorch is a stand-in that kills itself on import. A test can attach code
to any step, which is how a second run or a directory arrives at a moment of its choosing.
"""

import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time

import pytest

from scan3d.config import ROOT
from scan3d.store import Store

SCRIPT = ROOT / "scripts" / "add_inference_path.sh"
SETTINGS_CODE = ("__init__.py", "config.py", "doctor.py", "inference_paths.py")
GIB = 1024**3
LOCKED = {"alpha": {"version": "1.0", "complete": True}, "beta": {"version": "2.0.post1", "complete": True}}
COREUTILS = ("bash", "cat", "dirname", "flock", "grep", "mkdir", "mktemp", "realpath", "rm", "sort", "stat", "tail")
PREFLIGHT = ["uv python find", "nvidia-smi", "df", "df", "system-python"]
FIRST_RUN = [*PREFLIGHT, "find", "uv venv", "uv pip sync", "venv-python"]
LATER_PREFLIGHT = ["uv python find", "nvidia-smi", "system-python"]  # A later run needs no room.
RE_RUN = [*LATER_PREFLIGHT, "uv pip freeze", "venv-python"]  # It equals the lock: no index.
RESTARTED = [*LATER_PREFLIGHT, "find", "uv venv", "uv pip sync", "venv-python"]  # No install has finished.
PLANNED = ["uv plan", "queue", "find", "uv venv", "uv pip sync", "venv-python"]  # A finished environment is at stake.
REBUILT = [*LATER_PREFLIGHT, "uv pip freeze", *PLANNED]
MARKER = ".scan3d-inference-path"
FINISHED = ".scan3d-inference-path-finished"
PLAN_ENDS = "so it cannot compare them."
# In the real lock's format, with what the script must read past, and in an order uv does not
# print, so that the comparison cannot rest on one.
STUB_LOCK = """# A stub: only the fake uv installs from this.
--only-binary alpha

beta==2.0.post1 \\
    --hash=sha256:bbbb \\
    --hash=sha256:cccc
    # via -r requirements/cuda-py314.in
alpha==1.0 \\
    --hash=sha256:aaaa
    # via beta
"""

# Every tool the script can reach. Its first two arguments come from the shim that starts it.
FAKE_TOOL = r"""
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sys
import time

rig, tool, *arguments = sys.argv[1:]
rig = Path(rig)
scenario = json.loads((rig / "scenario.json").read_text())
if "--queue" in arguments:
    step = "queue"
elif "--dry-run" in arguments:
    step = f"{tool} plan"
elif tool.endswith("uv"):  # What ran, with its subcommand named.
    step = " ".join((tool, *(word for word in arguments[:2] if not word.startswith("-"))))
else:
    step = tool
call = {"tool": tool, "step": step, "arguments": arguments, "cwd": os.getcwd(), "env": dict(os.environ)}
# What the environment held when the wipe and the rebuild began, which fixes the order of what
# comes before them.
if tool == "find" or arguments[:1] == ["venv"]:
    target = Path(arguments[0] if tool == "find" else arguments[-1])
    call["held"] = sorted(entry.name for entry in target.iterdir())
with (rig / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps(call) + "\n")


# Stays here until the test lets go, with its descriptors open, as a real step's are.
def hold():
    (rig / "held").touch()
    end = time.monotonic() + 60
    while not (rig / "go-on").exists() and time.monotonic() < end:
        time.sleep(0.01)


exec(scenario["during"].get(step, ""))  # A test's own code, as this step starts.

if tool.endswith("python"):
    os.execv(sys.executable, [sys.executable, *arguments])
if tool == "nvidia-smi":
    print(scenario["driver"], end="")
    sys.exit(scenario["driver_status"])
if tool == "df":
    print("Avail")
    print(scenario["free_at"].get(arguments[-1], scenario["free"]))
    sys.exit()
if tool == "find":
    if scenario["wipe_status"]:
        sys.exit(scenario["wipe_status"])
    os.execv(rig / "find", ["find", *arguments])
if tool in ("mkdir", "rm"):
    os.execv(rig / tool, [tool, *arguments])
if arguments[:2] == ["python", "find"]:
    if scenario["python"]:
        print(rig / "system-python")
    sys.exit(0 if scenario["python"] else 2)
if arguments[0] == "venv":
    # The script itself dies, so nothing of its own runs after this moment.
    if scenario["killed_after_the_wipe"]:
        os.kill(os.getppid(), signal.SIGKILL)
        sys.exit(137)
    if scenario["venv_status"]:
        sys.exit(scenario["venv_status"])
    (target / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(rig / "venv-python", target / "bin" / "python")
    if scenario["venv_stops"]:  # uv writes the link before pyvenv.cfg.
        sys.exit(130)
    (target / "pyvenv.cfg").write_text("home = fake\n")
if arguments[:2] == ["pip", "sync"] and "<<<<<<<" in Path(arguments[-1]).read_text():
    print("error: Unexpected '<', expected '-c', '-e', '-r' or the start of a requirement", file=sys.stderr)
    sys.exit(2)
if "--dry-run" in arguments:
    (Path(arguments[arguments.index("--target") + 1]) / ".lock").touch()  # All that real uv leaves there.
    sys.exit(scenario["plan_status"])
if arguments[0] == "pip":
    environment = Path(arguments[arguments.index("--python") + 1]).parents[1]
    record = environment / "installed.json"
    packages = json.loads(record.read_text()) if record.exists() else {}
if arguments[:2] == ["pip", "freeze"]:
    if "--quiet" not in arguments:
        print("Using Python 3.14 environment at: .venv-nvidia-py314", file=sys.stderr)
    if scenario["freeze_status"]:
        print("error: Failed to read metadata", file=sys.stderr)
        sys.exit(scenario["freeze_status"])
    print("".join(f"{name}=={package['version']}\n" for name, package in sorted(packages.items())), end="")
if arguments[:2] == ["pip", "sync"]:
    if scenario["sync_status"]:
        sys.exit(scenario["sync_status"])
    # With no pyvenv.cfg the link is the Python it points at, and uv acts on that.
    if not (environment / "pyvenv.cfg").is_file():
        (rig / "base-python-changed").touch()
    # What a stop inside a package's RECORD leaves: real uv can neither read it nor remove it.
    if any("torn" in package for package in packages.values()):
        print("error: RECORD file is invalid", file=sys.stderr)
        sys.exit(2)
    locked = dict(re.findall(r"^([A-Za-z0-9][\w.-]*)(?:\[[^\]]*\])?==([^\s;\\]+)", Path(arguments[-1]).read_text(), re.M))
    packages = {name: package for name, package in packages.items() if name in locked}
    # What real uv does: a dist-info is an installed package however few files are beside it, and
    # ==1.0 accepts 1.0+cu126.
    accepted = {name for name, package in packages.items() if package["version"].partition("+")[0] == locked[name]}
    linked = [name for name in locked if name not in accepted]
    packages |= {name: {"version": locked[name], "complete": True} for name in linked}
    # Stopped on the last package: every dist-info is there, so the versions alone look right.
    if scenario["sync_stops"]:
        packages[linked[-1]]["complete"] = False
    if scenario["sync_tears_a_record"]:
        packages[linked[-1]]["torn"] = True
    record.write_text(json.dumps(packages))
    sys.exit(130 if scenario["sync_stops"] or scenario["sync_tears_a_record"] else 0)
"""

# Writes its proof the way scripts/check_gpu.py does, by replacing the file.
STUB_CHECK = r"""
import json
from pathlib import Path
import sys

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from scan3d.config import load_environment
from scan3d.inference_paths import directory

load_environment()
outcome = json.loads((root.parent / "rig" / "scenario.json").read_text())["check"]
if outcome == "refused":
    sys.exit("Jobs are queued or running, so no GPU check ran. Run it again when the queue is empty.")
if "--queue" in sys.argv:
    sys.exit(0)
if outcome == "crashed":
    raise RuntimeError("the check stopped before it wrote anything")
if outcome == "silent":
    sys.exit(0)
output = directory() / "nvidia.json"
output.parent.mkdir(parents=True, exist_ok=True)
pending = output.with_suffix(".json.writing")
pending.write_text(json.dumps({"gpu_ready": outcome == "passed"}))
pending.replace(output)
sys.exit({"passed": 0, "failed": 1, "died": 139}[outcome])
"""


class Rig:
    """A temporary checkout with nothing real on PATH. `scenario` says what the fakes answer."""

    def __init__(self, top):
        self.top = Path(top).resolve()
        self.root = self.top / "checkout"
        self.fakes = self.top / "rig"
        self.bin = self.top / "bin"
        self.elsewhere = self.top / "elsewhere"
        self.tmp = self.top / "tmp"
        for folder in (self.root / "scripts", self.root / "requirements", self.root / "scan3d", self.fakes, self.bin, self.elsewhere, self.tmp):
            folder.mkdir(parents=True)
        self.venv = self.root / ".venv-nvidia-py314"
        self.lock = self.root / "requirements" / "cuda-py314.lock"
        self.proof = self.root / ".runtime" / "inference-paths" / "nvidia.json"
        self.scenario = {
            "during": {},
            "python": True,
            "driver": "580.82.09\n",
            "driver_status": 0,
            "free": 13 * GIB,
            "free_at": {},
            "freeze_status": 0,
            "plan_status": 0,
            "wipe_status": 0,
            "killed_after_the_wipe": False,
            "venv_status": 0,
            "venv_stops": False,
            "sync_status": 0,
            "sync_stops": False,
            "sync_tears_a_record": False,
            "check": "passed",
        }
        shutil.copy2(SCRIPT, self.root / "scripts")
        (self.root / "scripts" / "check_gpu.py").write_text(STUB_CHECK)
        self.lock.write_text(STUB_LOCK)
        for name in SETTINGS_CODE:
            shutil.copy(ROOT / "scan3d" / name, self.root / "scan3d")
        for name in (".venv", ".venv-rocm10-py314"):  # What the script must never adopt.
            (self.root / name / "bin").mkdir(parents=True)
            (self.root / name / "pyvenv.cfg").write_text("home = another stack\n")
        (self.fakes / "fake_tool.py").write_text(FAKE_TOOL)
        self.shim(self.bin / "uv", "uv")
        self.shim(self.bin / "nvidia-smi", "nvidia-smi")
        self.shim(self.bin / "df", "df")
        self.shim(self.bin / "find", "find")
        (self.fakes / "find").symlink_to(shutil.which("find"))
        self.shim(self.fakes / "system-python", "system-python")
        self.shim(self.fakes / "venv-python", "venv-python")
        for name in COREUTILS:
            (self.bin / name).symlink_to(shutil.which(name))

    def watch(self, name):
        """A coreutil that still does its work, and is now a step a test can attach code to."""
        (self.fakes / name).symlink_to(shutil.which(name))
        (self.bin / name).unlink()
        self.shim(self.bin / name, name)

    def shim(self, location, tool):
        location.parent.mkdir(parents=True, exist_ok=True)
        command = shlex.join((sys.executable, "-I", str(self.fakes / "fake_tool.py"), str(self.fakes), tool))
        location.write_text(f'#!/usr/bin/env bash\nexec {command} "$@"\n')
        location.chmod(0o755)

    def prepared(self, env=None):
        """The scenario on disk, and the command and environment of a run."""
        (self.fakes / "scenario.json").write_text(json.dumps(self.scenario))
        return self.root / "scripts" / SCRIPT.name, {"PATH": str(self.bin), "HOME": str(self.top / "home"), "TMPDIR": str(self.tmp), **(env or {})}

    def run(self, *arguments, cwd=None, env=None):
        script, environment = self.prepared(env)
        return subprocess.run([script, *arguments], cwd=cwd or self.elsewhere, env=environment, capture_output=True, text=True, timeout=60)

    def start(self, *arguments):
        """A run in the background. Its output is a file, so that nothing waits on what it leaves behind."""
        script, environment = self.prepared()
        with (self.fakes / "background.log").open("w") as log:
            return subprocess.Popen([script, *arguments], cwd=self.elsewhere, env=environment, stdout=log, stderr=log)

    def wait_until_held(self):
        end = time.monotonic() + 30
        while not (self.fakes / "held").exists():
            assert time.monotonic() < end, "no step was held"
            time.sleep(0.01)

    def go_on(self):
        (self.fakes / "go-on").touch()

    def calls(self):
        log = self.fakes / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def forget_calls(self):
        (self.fakes / "calls.jsonl").unlink()

    def steps(self):
        """What ran, in order."""
        return [call["step"] for call in self.calls()]

    def step(self, name):
        """The one call of a step."""
        (call,) = [call for call, step in zip(self.calls(), self.steps()) if step == name]
        return call

    def packages(self):
        """What the fake uv left installed."""
        return json.loads((self.venv / "installed.json").read_text())

    def set_packages(self, packages):
        (self.venv / "installed.json").write_text(json.dumps(packages))

    def with_a_real_check(self):
        """What a run needs once the real check replaces the stub. Past its queue guard the real
        check imports PyTorch, and here that is a stand-in that records the import and kills its
        own process, so no test can reach a real runtime."""
        return {
            "SCAN3D_DATA_DIR": str(self.top / "data"),
            "PYTHONPATH": str(ROOT / "tests" / "fake_torch"),
            "SCAN3D_FAKE_TORCH": "crash",
            "SCAN3D_FAKE_TORCH_PREFIXES": str(self.top / "torch-imports"),
        }

    def snapshot(self):
        """Everything a run could have changed, so a test can show that it changed nothing."""
        return {str(entry): _state(entry) for folder in (self.root, self.elsewhere) for entry in sorted(folder.rglob("*"))}


def _state(entry):
    if entry.is_symlink():
        return os.readlink(entry)
    return (entry.stat().st_mode, entry.stat().st_mtime_ns, entry.read_bytes() if entry.is_file() else None)


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path)
    yield rig
    rig.go_on()  # Whatever a failed test was holding.


def installed(rig):
    """A finished first run, with its calls forgotten so that a test sees only the next one."""
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    rig.forget_calls()


def said(words):
    """Text as a person reads it, whichever way the script wrapped it."""
    return " ".join(words.split())


def the_result(completed):
    """What the script said about this run. The plan comes first, and speaks only of what could happen."""
    return said(completed.stdout).rpartition(PLAN_ENDS)[2] + said(completed.stderr)


# The command line


@pytest.mark.parametrize(
    "arguments",
    [
        (),
        ("amd",),
        ("nvidia", "nvidia"),
        ("nvidia", "--unknown"),
        ("--dry-run",),
        ("--venv",),
        ("nvidia", "--venv"),
        ("nvidia", "--venv", ""),
        ("nvidia", "--venv", "--dry-run"),
        ("nvidia", "--venv=elsewhere"),
    ],
)
def test_a_bad_command_line_is_a_usage_error(rig, arguments):
    before = rig.snapshot()
    completed = rig.run(*arguments)
    assert completed.returncode == 2 and completed.stderr.startswith("Usage:")
    assert "unbound variable" not in completed.stderr
    assert rig.calls() == [] and rig.snapshot() == before


def test_options_may_come_before_the_path(rig):
    assert rig.run("--dry-run", "--venv", "target", "nvidia").returncode == 0
    assert str(rig.elsewhere / "target") in rig.run("--venv", "target", "--dry-run", "nvidia").stdout


def test_help_is_not_a_failure(rig):
    completed = rig.run("--help")
    assert completed.returncode == 0 and completed.stdout.startswith("Usage:") and completed.stderr == ""


# Preflight


def no_uv(rig):
    (rig.bin / "uv").unlink()


def no_python(rig):
    rig.scenario["python"] = False


def no_nvidia_smi(rig):
    (rig.bin / "nvidia-smi").unlink()


def unreachable_driver(rig):
    rig.scenario.update(driver="NVIDIA-SMI has failed because it could not reach the driver.\n", driver_status=9)


def unknown_driver(rig):
    rig.scenario["driver"] = "[N/A]\n"


def old_driver(rig):
    rig.scenario["driver"] = "579.99.01\n"


def two_cards_on_an_old_driver(rig):
    rig.scenario["driver"] = "579.99.01\n579.99.01\n"


def small_disk(rig):
    rig.scenario["free"] = 12 * GIB - 1


def no_lock(rig):
    rig.lock.unlink()


def a_lock_with_no_pins(rig):
    rig.lock.write_text("# Truncated.\n")


PREFLIGHT_FAILURES = {
    no_uv: "uv was not found",
    no_python: "Python 3.14 was not found",
    no_nvidia_smi: "nvidia-smi was not found",
    unreachable_driver: "nvidia-smi could not read the driver version: NVIDIA-SMI has failed",
    unknown_driver: "nvidia-smi gave no driver version: [N/A]",
    old_driver: "NVIDIA driver 579.99.01 is too old",
    two_cards_on_an_old_driver: "NVIDIA driver 579.99.01 is too old",
    small_disk: "Only 11 GiB is free",
    no_lock: "is missing or pins nothing",
    a_lock_with_no_pins: "is missing or pins nothing",
}


@pytest.mark.parametrize("arguments", [("nvidia",), ("nvidia", "--dry-run")], ids=["run", "dry-run"])
@pytest.mark.parametrize("failure", PREFLIGHT_FAILURES, ids=lambda failure: failure.__name__)
def test_each_preflight_failure_says_its_own_reason_and_changes_nothing(rig, failure, arguments):
    failure(rig)
    before = rig.snapshot()
    completed = rig.run(*arguments)
    assert completed.returncode == 1
    assert [message for message in set(PREFLIGHT_FAILURES.values()) if message in completed.stderr] == [PREFLIGHT_FAILURES[failure]]
    assert "Adding the NVIDIA" not in completed.stdout
    assert rig.snapshot() == before
    assert not {"uv venv", "uv pip sync", "venv-python"} & set(rig.steps())


def test_the_driver_message_names_the_floor(rig):
    old_driver(rig)
    assert "580 or newer" in rig.run("nvidia").stderr


def test_the_limits_themselves_are_enough(rig):
    rig.scenario.update(driver="580.0.0\n", free=12 * GIB)
    assert rig.run("nvidia", "--dry-run").returncode == 0


def test_a_two_card_listing_reads_as_one_driver(rig):
    rig.scenario["driver"] = "580.82.09\n580.82.09\n"
    completed = rig.run("nvidia", "--dry-run")
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.count("580.82.09") == 1


def test_the_driver_query_asks_for_nothing_but_the_version(rig):
    rig.run("nvidia", "--dry-run")
    assert rig.step("nvidia-smi")["arguments"] == ["--query-gpu=driver_version", "--format=csv,noheader"]


def test_room_is_measured_where_the_environment_and_the_cache_will_go(rig):
    venv = rig.elsewhere / "new" / "venv"
    cache = rig.top / "other-disk" / "uv"
    cache.parent.mkdir()
    rig.scenario["free_at"] = {str(cache.parent): 3 * GIB}
    completed = rig.run("nvidia", "--venv", str(venv), env={"UV_CACHE_DIR": str(cache)})
    assert completed.returncode == 1
    assert f"Only 3 GiB is free for {cache}" in completed.stderr
    assert [call["arguments"][-1] for call in rig.calls() if call["tool"] == "df"] == [str(rig.elsewhere), str(cache.parent)]


def test_uv_cannot_fetch_or_cache_a_python_during_preflight(rig):
    rig.run("nvidia", "--dry-run")
    find = rig.step("uv python find")
    assert find["arguments"] == ["python", "find", "--no-cache", "--system", "3.14"]
    assert find["env"]["UV_PYTHON_DOWNLOADS"] == "never"


# A dry run


def test_a_dry_run_prints_the_plan_and_changes_nothing(rig):
    before = rig.snapshot()
    completed = rig.run("nvidia", "--dry-run")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == PREFLIGHT
    assert rig.snapshot() == before
    for words in (
        "about 3.3 GiB",
        "about 9 GiB",
        "needs no network",
        "acvl-utils",
        "without hashes",
        "will not touch",
        "no restart",
        str(rig.venv),
        str(rig.proof),
        "nothing was created",
    ):
        assert words in said(completed.stdout)


def test_a_dry_run_on_an_installed_environment_changes_nothing_either(rig):
    installed(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia", "--dry-run")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == LATER_PREFLIGHT
    assert rig.snapshot() == before
    assert f"{rig.venv} (from an earlier run)" in completed.stdout
    assert "kept" not in completed.stdout  # It cannot tell yet, and it may be rebuilt.


def test_the_folder_question_cannot_write_or_load_from_the_working_directory(rig):
    rig.run("nvidia", "--dry-run")
    assert rig.step("system-python")["arguments"][:3] == ["-B", "-P", "-c"]


# The target


def refused(completed):
    return completed.returncode == 1 and "already exists and this script did not make it" in completed.stderr


@pytest.mark.parametrize("name", [".venv", ".venv-rocm10-py314"])
@pytest.mark.parametrize(
    "spelling",
    ["{name}", "./{name}", "{name}/", "{name}//", "scripts/../{name}", "{root}/{name}", "{root}/{name}/", "{link}", "{link}/", "{link}/."],
)
def test_another_environment_is_refused_however_it_is_spelled(rig, name, spelling):
    link = rig.elsewhere / "link"
    link.symlink_to(rig.root / name)
    before = rig.snapshot()
    completed = rig.run("nvidia", "--venv", spelling.format(name=name, root=rig.root, link=link), cwd=rig.root)
    assert refused(completed)
    assert rig.calls() == [] and rig.snapshot() == before


@pytest.mark.parametrize("name", [".venv", ".venv-rocm10-py314"])
def test_another_environment_is_refused_from_another_directory(rig, name):
    before = rig.snapshot()
    assert refused(rig.run("nvidia", "--venv", f"../checkout/{name}"))
    assert rig.calls() == [] and rig.snapshot() == before


def a_directory(rig, target):
    target.mkdir()


def a_file(rig, target):
    target.touch()


def a_link_to_a_directory(rig, target):
    (rig.top / "empty").mkdir()
    target.symlink_to(rig.top / "empty")


def a_dangling_link(rig, target):
    target.symlink_to(rig.top / "nowhere")


@pytest.mark.parametrize("arguments", [(), ("--dry-run",)], ids=["run", "dry-run"])
@pytest.mark.parametrize("ending", ["", "/"], ids=["bare", "slash"])
@pytest.mark.parametrize("build", [a_directory, a_file, a_link_to_a_directory, a_dangling_link])
def test_a_target_without_the_marker_is_refused_however_empty(rig, build, ending, arguments):
    target = rig.elsewhere / "target"
    build(rig, target)
    before = rig.snapshot()
    assert refused(rig.run("nvidia", "--venv", f"{target}{ending}", *arguments))
    assert rig.calls() == [] and rig.snapshot() == before


def test_the_default_target_needs_the_marker_too(rig):
    rig.venv.mkdir()
    assert refused(rig.run("nvidia"))


def no_marker(target):
    pass


def a_marker_that_is_a_directory(target):
    (target / MARKER).mkdir()


def a_marker_that_points_nowhere(target):
    (target / MARKER).symlink_to(target / "nowhere")


def a_marker_that_points_at_a_file(target):
    # A copy of an environment made of links looks like this, and this script did not make it.
    (target / MARKER).symlink_to(target / "a-file-of-someone-elses")


@pytest.mark.parametrize("marker", [no_marker, a_marker_that_is_a_directory, a_marker_that_points_nowhere, a_marker_that_points_at_a_file])
def test_the_wipe_never_reaches_a_directory_without_the_marker(rig, marker):
    # This one looks like a stopped install in every other way.
    target = rig.elsewhere / "target"
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "python").touch(mode=0o755)
    (target / "pyvenv.cfg").write_text("home = another stack\n")
    (target / "a-file-of-someone-elses").touch()
    marker(target)
    before = rig.snapshot()
    assert refused(rig.run("nvidia", "--venv", str(target)))
    assert rig.calls() == [] and rig.snapshot() == before


def test_a_relative_venv_means_the_callers_directory(rig):
    # Read after moving to the checkout, this would name the checkout's own .venv.
    assert rig.run("nvidia", "--venv", ".venv").returncode == 0
    assert rig.step("uv venv")["arguments"][-1] == str(rig.elsewhere / ".venv")
    assert (rig.elsewhere / ".venv" / MARKER).is_file()
    assert (rig.root / ".venv" / "pyvenv.cfg").read_text() == "home = another stack\n" and not (rig.root / ".venv" / MARKER).exists()


def test_a_link_to_this_scripts_own_environment_is_followed(rig):
    installed(rig)
    link = rig.elsewhere / "link"
    link.symlink_to(rig.venv)
    assert rig.run("nvidia", "--venv", f"{link}/").returncode == 0
    assert rig.step("uv pip freeze")["arguments"][3] == f"{rig.venv}/bin/python"
    assert "uv venv" not in rig.steps()


# A first run


def test_a_first_run_builds_syncs_and_checks_and_registers_the_path(rig):
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == FIRST_RUN
    python = f"{rig.venv}/bin/python"
    assert rig.step("uv venv")["arguments"] == ["venv", "--python", str(rig.fakes / "system-python"), "--allow-existing", str(rig.venv)]
    assert rig.step("uv pip sync")["arguments"] == [
        "pip",
        "sync",
        "--python",
        python,
        "--require-hashes",
        f"{rig.root}/requirements/cuda-py314.lock",
    ]
    assert rig.step("venv-python")["arguments"] == [f"{rig.root}/scripts/check_gpu.py", "--path", "nvidia"]
    assert json.loads(rig.proof.read_text()) == {"gpu_ready": True}
    assert (rig.venv / MARKER).is_file()
    assert (rig.venv / MARKER).stat().st_mode & 0o777 == 0o600  # The script's umask.
    assert "no restart" in completed.stdout and str(rig.proof) in completed.stdout


@pytest.mark.parametrize("rebuilt", [False, True], ids=["first-run", "rebuild"])
def test_a_run_stays_inside_the_rig(rig, rebuilt):
    if rebuilt:
        installed(rig)
        a_local_build(rig)
    assert rig.run("nvidia").returncode == 0
    calls = rig.calls()
    assert {call["env"]["PATH"] for call in calls} == {str(rig.bin)}
    assert all(Path(call["cwd"]).is_relative_to(rig.top) for call in calls)
    arguments = [word for call in calls for word in call["arguments"] if word.startswith("/")]
    assert arguments and all(Path(word).is_relative_to(rig.top) for word in arguments)
    assert list(rig.tmp.iterdir()) == []
    assert {entry.name for entry in rig.root.iterdir()} == {
        ".runtime",
        ".venv",
        ".venv-nvidia-py314",
        ".venv-rocm10-py314",
        "requirements",
        "scan3d",
        "scripts",
    }


def test_only_a_finished_install_is_marked(rig):
    rig.scenario["sync_status"] = 1
    rig.run("nvidia")
    assert not (rig.venv / FINISHED).exists()
    rig.scenario["sync_status"] = 0
    rig.run("nvidia")
    assert (rig.venv / FINISHED).is_file()
    assert (rig.venv / FINISHED).stat().st_mode & 0o777 == 0o600


def test_every_uv_call_is_barred_from_fetching_a_python(rig):
    rig.run("nvidia")
    uv = [call for call in rig.calls() if call["tool"] == "uv"]
    assert len(uv) == 3 and {call["env"]["UV_PYTHON_DOWNLOADS"] for call in uv} == {"never"}


def test_the_checkouts_own_uv_comes_before_the_one_on_path(rig):
    rig.shim(rig.root / ".tools" / "bin" / "uv", "checkout-uv")
    assert rig.run("nvidia").returncode == 0
    assert [step for step in rig.steps() if "uv" in step] == ["checkout-uv python find", "checkout-uv venv", "checkout-uv pip sync"]


def test_a_checkouts_own_python_is_searched_only_when_it_has_one(rig):
    rig.run("nvidia", "--dry-run")
    assert "UV_PYTHON_INSTALL_DIR" not in rig.step("uv python find")["env"]
    rig.forget_calls()
    (rig.root / ".tools" / "python").mkdir(parents=True)
    rig.run("nvidia", "--dry-run")
    assert rig.step("uv python find")["env"]["UV_PYTHON_INSTALL_DIR"] == str(rig.root / ".tools" / "python")


def test_uv_keeps_its_cache_in_the_checkout_unless_told_otherwise(rig):
    rig.run("nvidia", "--dry-run")
    assert rig.step("uv python find")["env"]["UV_CACHE_DIR"] == str(rig.root / ".cache" / "uv")
    rig.forget_calls()
    rig.run("nvidia", "--dry-run", env={"UV_CACHE_DIR": str(rig.top / "shared-cache")})
    assert rig.step("uv python find")["env"]["UV_CACHE_DIR"] == str(rig.top / "shared-cache")


def test_spaces_in_every_path(tmp_path):
    rig = Rig(tmp_path / "a work space")
    venv = rig.elsewhere / "nvidia env"
    completed = rig.run("nvidia", "--venv", str(venv))
    assert completed.returncode == 0, completed.stderr
    assert rig.step("uv venv")["arguments"][-1] == str(venv)
    assert rig.step("uv pip sync")["arguments"][3] == f"{venv}/bin/python"
    assert rig.step("venv-python")["arguments"][0] == f"{rig.root}/scripts/check_gpu.py"
    assert (venv / MARKER).is_file() and rig.proof.is_file()
    assert str(rig.proof) in completed.stdout


def test_the_check_runs_without_the_launchers_library_variables(rig):
    variables = {"LD_LIBRARY_PATH": str(rig.top / "rocm-lib"), "LD_PRELOAD": str(rig.top / "hook.so")}
    assert rig.run("nvidia", env=variables).returncode == 0
    environment = rig.step("venv-python")["env"]
    assert not variables.keys() & environment.keys()
    # They reached the script, and only that child lost them.
    assert variables.items() <= rig.step("uv pip sync")["env"].items()


# Running it again


def test_a_re_run_keeps_the_environment_and_repeats_the_check(rig):
    installed(rig)
    package = rig.venv / "lib" / "installed-package"
    package.parent.mkdir()
    package.touch()
    interpreter = (rig.venv / "bin" / "python").stat()
    rig.scenario["free"] = GIB  # The first install may have used the room it asked for.
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == RE_RUN
    assert package.exists() and (rig.venv / "bin" / "python").stat().st_ino == interpreter.st_ino
    assert "from an earlier run" in completed.stdout
    assert "nothing was installed" in completed.stdout


def test_a_re_run_does_not_name_the_environment_it_read(rig):
    installed(rig)
    assert "Using Python" not in rig.run("nvidia").stderr


def test_a_re_run_repairs_a_failed_check(rig):
    rig.scenario["check"] = "failed"
    assert rig.run("nvidia").returncode == 1
    rig.forget_calls()
    rig.scenario["check"] = "passed"
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RE_RUN
    assert json.loads(rig.proof.read_text()) == {"gpu_ready": True}


def test_a_run_stopped_inside_uv_venv_leaves_a_directory_the_next_run_owns(rig):
    rig.scenario["venv_status"] = 2
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and "could not be created" in completed.stderr
    assert (rig.venv / MARKER).is_file()
    assert "uv pip sync" not in rig.steps()
    rig.forget_calls()
    rig.scenario["venv_status"] = 0
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RESTARTED


def test_a_failed_sync_runs_no_check_and_the_next_run_starts_again(rig):
    rig.scenario["sync_status"] = 1
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and "did not finish" in completed.stderr
    assert "venv-python" not in rig.steps() and not rig.proof.exists()
    rig.forget_calls()
    rig.scenario["sync_status"] = 0
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RESTARTED


def test_a_stopped_install_is_finished_by_the_next_run(rig):
    # uv links a dist-info beside the files with no finish mark, so its next sync would say
    # "Audited" and leave the package with what it had.
    rig.scenario["sync_stops"] = True
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and "did not finish" in completed.stderr
    assert "venv-python" not in rig.steps()
    assert rig.packages() == {**LOCKED, "alpha": {"version": "1.0", "complete": False}}
    rig.forget_calls()
    rig.scenario["sync_stops"] = False
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RESTARTED
    assert rig.packages() == LOCKED


def test_a_stop_inside_a_record_is_cleared_by_the_next_run(rig):
    # Real uv refuses this state on every later sync, so only an empty environment gets past it.
    rig.scenario["sync_tears_a_record"] = True
    assert "did not finish" in rig.run("nvidia").stderr
    rig.scenario["sync_tears_a_record"] = False
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.packages() == LOCKED


def test_a_rebuild_stopped_halfway_is_not_taken_for_a_finished_install(rig):
    installed(rig)
    a_local_build(rig)
    rig.scenario["sync_stops"] = True
    assert rig.run("nvidia").returncode == 1
    rig.forget_calls()
    rig.scenario["sync_stops"] = False
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RESTARTED
    assert rig.packages() == LOCKED


def a_local_build(rig):
    # The app says its packages changed, and PEP 440 lets this satisfy alpha==1.0.
    rig.set_packages({**rig.packages(), "alpha": {"version": "1.0+cu126", "complete": True}})


def another_version(rig):
    rig.set_packages({**rig.packages(), "alpha": {"version": "0.9", "complete": True}})


def a_package_gone(rig):
    rig.set_packages({"beta": rig.packages()["beta"]})


def a_package_the_lock_does_not_list(rig):
    rig.set_packages({**rig.packages(), "cupy": {"version": "14.0", "complete": True}})


@pytest.mark.parametrize("change", [a_local_build, another_version, a_package_gone, a_package_the_lock_does_not_list])
def test_a_re_run_puts_any_difference_back_to_the_lock(rig, change):
    installed(rig)
    change(rig)
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == REBUILT
    assert rig.packages() == LOCKED


def test_a_rebuild_leaves_nothing_of_the_old_environment_but_the_marker(rig):
    installed(rig)
    a_local_build(rig)
    made_by = (rig.venv / MARKER).read_text()
    leftovers = [rig.venv / name for name in ("lib/python3.14/old/module.py", "bin/old-script", ".gitignore", "share/.hidden/file")]
    for leftover in leftovers:
        leftover.parent.mkdir(parents=True, exist_ok=True)
        leftover.touch()
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.step("uv venv")["held"] == [MARKER]
    assert not any(leftover.exists() for leftover in leftovers) and not (rig.venv / "share").exists()
    assert {entry.name for entry in rig.venv.iterdir()} == {MARKER, FINISHED, "bin", "installed.json", "pyvenv.cfg"}
    assert (rig.venv / MARKER).read_text() == made_by


def test_the_finished_mark_goes_before_the_wipe_starts(rig):
    installed(rig)
    a_local_build(rig)
    assert rig.run("nvidia").returncode == 0
    assert set(rig.step("find")["held"]) == {MARKER, "bin", "installed.json", "pyvenv.cfg"}


def test_a_wipe_never_follows_a_link_out_of_the_environment(rig):
    # A real environment's interpreter is a link, and its target is the base Python.
    installed(rig)
    a_local_build(rig)
    base = rig.elsewhere / "base-python"
    (base / "lib").mkdir(parents=True)
    (base / "lib" / "python3.14").touch()
    (rig.venv / "lib64").symlink_to(base / "lib")
    (rig.venv / "bin" / "python3").symlink_to(base / "lib" / "python3.14")
    assert rig.run("nvidia").returncode == 0
    assert (base / "lib" / "python3.14").exists()
    assert not (rig.venv / "lib64").is_symlink() and not (rig.venv / "bin" / "python3").is_symlink()


def test_a_rebuild_through_a_link_empties_the_directory_and_keeps_the_link(rig):
    installed(rig)
    a_local_build(rig)
    link = rig.elsewhere / "link"
    link.symlink_to(rig.venv)
    assert rig.run("nvidia", "--venv", str(link)).returncode == 0
    assert rig.step("find")["cwd"] == str(rig.venv)
    assert link.is_symlink() and rig.packages() == LOCKED


def stopped_inside_uv_venv(rig):
    rig.scenario["venv_stops"] = True


def stopped_inside_uv_pip_sync(rig):
    rig.scenario["sync_stops"] = True


def killed_right_after_the_wipe(rig):
    rig.scenario["killed_after_the_wipe"] = True


@pytest.mark.parametrize("stop", [stopped_inside_uv_venv, stopped_inside_uv_pip_sync, killed_right_after_the_wipe])
def test_a_stop_anywhere_in_a_rebuild_leaves_a_directory_the_next_run_owns(rig, stop):
    installed(rig)
    a_local_build(rig)
    calm = dict(rig.scenario)
    stop(rig)
    assert rig.run("nvidia").returncode != 0
    assert (rig.venv / MARKER).is_file() and not (rig.venv / FINISHED).exists()
    assert "venv-python" not in rig.steps()
    rig.forget_calls()
    rig.scenario = calm
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == RESTARTED
    assert rig.packages() == LOCKED and (rig.venv / FINISHED).is_file()


def test_nothing_is_left_after_a_kill_right_after_the_wipe_but_the_marker(rig):
    installed(rig)
    a_local_build(rig)
    killed_right_after_the_wipe(rig)
    assert rig.run("nvidia").returncode == -signal.SIGKILL
    assert [entry.name for entry in rig.venv.iterdir()] == [MARKER]


def test_a_wipe_that_fails_stops_the_run_with_the_path_already_withdrawn(rig):
    installed(rig)
    a_local_build(rig)
    rig.scenario["wipe_status"] = 1
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and "could not be cleared" in completed.stderr
    assert not {"uv venv", "uv pip sync", "venv-python"} & set(rig.steps())
    assert (rig.venv / MARKER).is_file() and not (rig.venv / FINISHED).exists()
    rig.forget_calls()
    rig.scenario["wipe_status"] = 0
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RESTARTED


def no_reinstall(rig):
    return not any(word in call["arguments"] for call in rig.calls() for word in ("--reinstall", "--refresh"))


def test_uv_is_never_told_to_reinstall_or_refresh(rig):
    # Either would fetch again what uv already holds, on a rebuild that may have no network.
    rig.run("nvidia")
    assert no_reinstall(rig)
    rig.forget_calls()
    a_local_build(rig)
    rig.run("nvidia")
    assert no_reinstall(rig)


def test_a_re_run_that_equals_the_lock_reaches_the_check_with_no_index(rig):
    # The driver is fixed or the queue is empty, and the index cannot be reached: uv would fail.
    rig.scenario["check"] = "failed"
    rig.run("nvidia")
    rig.forget_calls()
    rig.scenario.update(check="passed", sync_status=2)
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == RE_RUN
    assert "The GPU check passed" in completed.stdout


def test_the_real_lock_reads_the_way_uv_prints_what_it_installed(rig):
    # A marker, an extra or another spelling on a pin would make every re-run reinstall everything.
    shutil.copy(ROOT / "requirements" / "cuda-py314.lock", rig.root / "requirements")
    installed(rig)
    assert len(rig.packages()) > 100
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RE_RUN


def test_a_run_stopped_after_uv_venv_made_the_link_never_reaches_the_python_it_points_at(rig):
    # uv writes bin/python before pyvenv.cfg, and the marker is written before uv starts.
    (rig.venv / "bin").mkdir(parents=True)
    (rig.venv / MARKER).touch()
    shutil.copy(rig.fakes / "venv-python", rig.venv / "bin" / "python")
    assert "from an earlier run" not in rig.run("nvidia", "--dry-run").stdout
    rig.forget_calls()
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps()[-3:] == ["uv venv", "uv pip sync", "venv-python"]
    assert (rig.venv / "pyvenv.cfg").is_file()
    assert not (rig.fakes / "base-python-changed").exists()


def test_an_environment_that_lost_pyvenv_cfg_gets_it_back(rig):
    installed(rig)
    package = rig.venv / "lib" / "installed-package"
    package.parent.mkdir()
    package.touch()
    (rig.venv / "pyvenv.cfg").unlink()
    assert rig.run("nvidia").returncode == 0
    # Not a freeze: without that file the link is the Python it points at.
    assert rig.steps() == [*LATER_PREFLIGHT, *PLANNED]
    assert not package.exists()
    assert (rig.venv / "pyvenv.cfg").is_file() and rig.packages() == LOCKED
    assert not (rig.fakes / "base-python-changed").exists()


def test_an_environment_that_lost_its_interpreter_gets_one_back(rig):
    installed(rig)
    package = rig.venv / "lib" / "installed-package"
    package.parent.mkdir()
    package.touch()
    (rig.venv / "bin" / "python").unlink()
    # Usual after a 9 GiB install on a nearly full disk, and a new link needs no room.
    rig.scenario["free"] = 10 * GIB
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == [*LATER_PREFLIGHT, *PLANNED]
    assert not package.exists()
    assert (rig.venv / "bin" / "python").is_file() and rig.packages() == LOCKED


@pytest.mark.parametrize("stopped", ["venv_status", "sync_status"])
def test_a_stopped_install_is_rebuilt_in_the_room_it_left(rig, stopped):
    # The cache already holds what the first run fetched, which is what took the room.
    rig.scenario[stopped] = 1
    assert rig.run("nvidia").returncode == 1
    rig.forget_calls()
    rig.scenario.update({stopped: 0, "free": 3 * GIB})
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert "df" not in rig.steps()


# What a rebuild must show before it starts


def test_an_environment_uv_cannot_read_is_left_exactly_as_it_is(rig):
    # A failed read says nothing about the lock, and only an environment that equals it stays.
    installed(rig)
    rig.scenario["freeze_status"] = 2
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert "uv could not read this environment, so nothing was changed" in said(completed.stderr)
    assert "error: Failed to read metadata" in completed.stderr  # What the person has to go on.
    assert "move the directory aside" in said(completed.stderr)  # The way out of a read that always fails.
    assert rig.steps() == [*LATER_PREFLIGHT, "uv pip freeze"]
    assert rig.snapshot() == before


@pytest.mark.parametrize("damage", [no_lock, a_lock_with_no_pins], ids=lambda damage: damage.__name__)
def test_a_lock_that_pins_nothing_never_reaches_a_finished_environment(rig, damage):
    installed(rig)
    damage(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and "is missing or pins nothing, so nothing was changed" in completed.stderr
    assert rig.calls() == [] and rig.snapshot() == before


def uv_cannot_reach_a_package(rig):
    rig.scenario["plan_status"] = 1


def a_merge_conflict_in_the_lock(rig):
    rig.lock.write_text(f"<<<<<<< HEAD\n{STUB_LOCK}=======\nalpha==1.1 \\\n    --hash=sha256:dddd\n>>>>>>> other\n")


@pytest.mark.parametrize("obstacle", [uv_cannot_reach_a_package, a_merge_conflict_in_the_lock])
def test_a_rebuild_uv_cannot_plan_leaves_a_finished_environment_alone(rig, obstacle):
    installed(rig)
    a_local_build(rig)
    obstacle(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert "uv cannot rebuild this environment from what it can reach, so nothing was changed" in said(completed.stderr)
    assert rig.steps() == [*LATER_PREFLIGHT, "uv pip freeze", "uv plan"]
    assert rig.snapshot() == before
    assert list(rig.tmp.iterdir()) == []


def test_a_rebuild_is_planned_by_the_real_sync_for_a_directory_holding_nothing(rig):
    # Asked about the environment itself, uv plans only the difference, and passes with every wheel gone.
    installed(rig)
    a_local_build(rig)
    assert rig.run("nvidia").returncode == 0
    plan = rig.step("uv plan")
    empty = plan["arguments"][plan["arguments"].index("--target") + 1]
    assert Path(empty).parent == rig.tmp
    assert plan["arguments"] == [
        "pip",
        "sync",
        "--quiet",
        "--dry-run",
        "--python",
        str(rig.fakes / "system-python"),  # uv refuses a base Python that is not given a target.
        "--target",
        empty,
        "--require-hashes",
        str(rig.lock),
    ]
    assert plan["env"]["UV_PYTHON_DOWNLOADS"] == "never"


def test_an_environment_past_reading_is_planned_for_too(rig):
    # With its interpreter gone nothing can be read, and a lock uv cannot use must still stop the run.
    installed(rig)
    (rig.venv / "bin" / "python").unlink()
    a_merge_conflict_in_the_lock(rig)
    before = rig.snapshot()
    assert rig.run("nvidia").returncode == 1
    assert rig.steps() == [*LATER_PREFLIGHT, "uv plan"]
    assert rig.snapshot() == before


@pytest.mark.parametrize("stop", [stopped_inside_uv_venv, stopped_inside_uv_pip_sync, killed_right_after_the_wipe])
def test_nothing_is_at_stake_after_a_stop_so_no_plan_is_asked_for(rig, stop):
    installed(rig)
    a_local_build(rig)
    calm = dict(rig.scenario)
    stop(rig)
    rig.run("nvidia")
    rig.forget_calls()
    rig.scenario = calm
    uv_cannot_reach_a_package(rig)  # A plan would stop this run, and it has nothing to protect.
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RESTARTED


# One run at a time


def a_first_install(rig):
    pass


def a_rebuild(rig):
    installed(rig)
    a_local_build(rig)


REFUSED_FOR_ANOTHER_RUN = "Another run is using this environment, so nothing was changed. Run this again when it has finished."


@pytest.mark.parametrize(
    "start, step",
    [
        *((a_first_install, step) for step in ("find", "uv venv", "uv pip sync", "venv-python")),
        *((a_rebuild, step) for step in ("uv pip freeze", *PLANNED)),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_a_second_run_is_refused_for_as_long_as_the_first_is_running(rig, start, step):
    start(rig)
    rig.scenario["during"] = {step: "hold()"}
    first = rig.start("nvidia")
    rig.wait_until_held()
    rig.scenario["during"] = {}
    rig.forget_calls()
    before = rig.snapshot()
    second = rig.run("nvidia")
    assert second.returncode == 1 and REFUSED_FOR_ANOTHER_RUN in said(second.stderr)
    assert rig.steps() == LATER_PREFLIGHT
    assert rig.snapshot() == before
    rig.go_on()
    assert first.wait(timeout=60) == 0
    assert rig.packages() == LOCKED and (rig.venv / FINISHED).is_file()
    assert json.loads(rig.proof.read_text()) == {"gpu_ready": True}


def test_a_second_run_is_refused_through_any_spelling_of_the_directory(rig):
    a_rebuild(rig)
    link = rig.elsewhere / "link"
    link.symlink_to(rig.venv)
    rig.scenario["during"] = {"uv pip sync": "hold()"}
    first = rig.start("nvidia")
    rig.wait_until_held()
    rig.scenario["during"] = {}
    assert REFUSED_FOR_ANOTHER_RUN in said(rig.run("nvidia", "--venv", f"{link}/").stderr)
    rig.go_on()
    assert first.wait(timeout=60) == 0


def test_a_dry_run_can_look_while_another_run_works(rig):
    a_rebuild(rig)
    rig.scenario["during"] = {"uv pip sync": "hold()"}
    first = rig.start("nvidia")
    rig.wait_until_held()
    rig.scenario["during"] = {}
    before = rig.snapshot()
    assert rig.run("nvidia", "--dry-run").returncode == 0
    assert rig.snapshot() == before
    rig.go_on()
    assert first.wait(timeout=60) == 0


def test_a_killed_run_keeps_the_environment_until_its_uv_has_gone(rig):
    # Only the script dies, and its uv goes on writing. Starting again now would wipe around it.
    a_rebuild(rig)
    rig.scenario["during"] = {"uv pip sync": "hold()"}
    first = rig.start("nvidia")
    rig.wait_until_held()
    first.kill()
    first.wait()
    rig.scenario["during"] = {}
    before = rig.snapshot()
    assert REFUSED_FOR_ANOTHER_RUN in said(rig.run("nvidia").stderr)
    assert rig.snapshot() == before
    rig.go_on()
    end = time.monotonic() + 30
    while REFUSED_FOR_ANOTHER_RUN in said((completed := rig.run("nvidia")).stderr):
        assert time.monotonic() < end, "the lock outlived the run that held it"
        time.sleep(0.01)
    assert completed.returncode == 0, completed.stderr
    assert rig.packages() == LOCKED


def test_a_finished_run_leaves_the_environment_free(rig):
    installed(rig)
    assert rig.run("nvidia").returncode == 0


# What can happen to the target during a run


LATE_ARRIVALS = {
    "a-directory": "target.mkdir(); (target / 'lib').mkdir(); (target / 'lib' / 'precious').touch(); (target / 'pyvenv.cfg').touch()",
    "a-directory-whose-marker-is-a-link": f"target.mkdir(); (target / 'precious').touch(); (target / {MARKER!r}).symlink_to('precious')",
    "an-empty-directory": "target.mkdir()",
    "a-file": "target.touch()",
    "a-link-to-a-directory": "elsewhere = target.with_name('elsewhere-entirely'); elsewhere.mkdir(); target.symlink_to(elsewhere)",
    "a-dangling-link": "target.symlink_to(target.with_name('nowhere'))",
}


@pytest.mark.parametrize("step", ["nvidia-smi", "system-python"], ids=["early", "the-last-step-before-the-plan"])
@pytest.mark.parametrize("arrival", LATE_ARRIVALS.values(), ids=LATE_ARRIVALS)
def test_a_target_that_arrives_after_the_guard_is_refused_too(rig, step, arrival):
    target = rig.elsewhere / "target"
    rig.scenario["during"] = {step: f"target = Path({str(target)!r}); {arrival}"}
    completed = rig.run("nvidia", "--venv", str(target))
    assert refused(completed)
    assert not {"find", "uv venv", "uv pip sync", "venv-python"} & set(rig.steps())
    if arrival is LATE_ARRIVALS["a-directory"]:
        assert (target / "lib" / "precious").exists()
    if arrival is LATE_ARRIVALS["a-directory-whose-marker-is-a-link"]:
        assert (target / "precious").exists() and (target / MARKER).is_symlink()
    else:
        assert not (target / MARKER).exists()
    assert not list(rig.tmp.iterdir())


def test_a_run_that_loses_mkdir_to_another_run_is_refused_by_the_lock(rig):
    # The other run made the directory and wrote the marker after this run looked for it.
    target, made = rig.elsewhere / "target", rig.elsewhere / "made-by-the-other-run"
    made.mkdir()
    (made / MARKER).touch()
    rig.watch("mkdir")
    rig.scenario["during"] = {"mkdir": f"if '-p' not in arguments:\n    Path({str(made)!r}).rename({str(target)!r})"}
    with (made / MARKER).open() as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        completed = rig.run("nvidia", "--venv", str(target))
    assert completed.returncode == 1 and REFUSED_FOR_ANOTHER_RUN in said(completed.stderr)
    assert "find" not in rig.steps()


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write anywhere")
def test_a_directory_that_cannot_be_made_is_not_called_someone_elses(rig):
    (rig.elsewhere / "locked").mkdir(mode=0o500)
    completed = rig.run("nvidia", "--venv", str(rig.elsewhere / "locked" / "target"))
    assert completed.returncode == 1 and "Permission denied" in completed.stderr
    assert "did not make it" not in completed.stderr and "cannot be read" not in completed.stderr


@pytest.mark.skipif(os.geteuid() == 0, reason="root can read anything")
def test_a_marker_that_cannot_be_opened_stops_the_run(rig):
    a_rebuild(rig)
    before = rig.snapshot()
    (rig.venv / MARKER).chmod(0)
    completed = rig.run("nvidia")
    (rig.venv / MARKER).chmod(0o600)
    assert completed.returncode == 1 and f"{rig.venv / MARKER} cannot be read, so nothing was changed" in completed.stderr
    assert rig.steps() == LATER_PREFLIGHT
    assert rig.snapshot() == before


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to anything")
def test_the_lock_only_reads_the_marker(rig):
    # Opened to write, it could create a marker in a directory that took the name a moment ago.
    a_rebuild(rig)
    (rig.venv / MARKER).chmod(0o400)
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.packages() == LOCKED


def swapped_in(rig, step, marked=False):
    """The environment moves aside as `step` starts, and another directory takes its name."""
    aside = rig.elsewhere / "aside"
    arrival = f"venv = Path({str(rig.venv)!r}); venv.rename({str(aside)!r}); venv.mkdir(); (venv / 'precious').touch()"
    rig.scenario["during"] = {step: arrival + (f"; (venv / {MARKER!r}).touch()" if marked else "")}
    return aside


@pytest.mark.parametrize("marked", [False, True], ids=["unmarked", "marked-by-another-run"])
@pytest.mark.parametrize("step", ["uv pip freeze", "uv plan", "queue"])
def test_a_directory_swapped_in_during_a_run_is_left_alone(rig, step, marked):
    a_rebuild(rig)
    aside = swapped_in(rig, step, marked)
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and f"{rig.venv} changed during this run, so nothing was changed" in completed.stderr
    assert "find" not in rig.steps()
    assert (rig.venv / "precious").exists()
    assert (aside / MARKER).is_file() and (aside / FINISHED).is_file()  # Neither was touched.


def test_the_wipe_stays_in_the_directory_it_checked_whatever_becomes_of_the_name(rig):
    a_rebuild(rig)
    aside = swapped_in(rig, "find")
    rig.run("nvidia")
    assert (rig.venv / "precious").exists()
    assert [entry.name for entry in aside.iterdir()] == [MARKER]


def test_the_finished_mark_goes_from_the_directory_it_checked_too(rig):
    a_rebuild(rig)
    rig.watch("rm")
    aside = swapped_in(rig, "rm", marked=True)
    arrival = rig.scenario["during"]["rm"]
    rig.scenario["during"]["rm"] = f"if arguments[0] == '-f':\n    {arrival}; (venv / {FINISHED!r}).touch()"
    rig.scenario["venv_status"] = 1  # Stops the run before it could write that mark itself.
    rig.run("nvidia")
    assert (rig.venv / FINISHED).exists()  # It belongs to the directory that took the name.
    assert not (aside / FINISHED).exists()


def test_uv_rebuilds_from_the_checkout_as_it_installs_from_it(rig):
    # It reads its settings from where it starts, and a rebuild must read what a first install read.
    a_rebuild(rig)
    assert rig.run("nvidia").returncode == 0
    assert rig.step("uv venv")["cwd"] == rig.step("uv pip sync")["cwd"] == str(rig.root)


# The job queue


def busy_queue(rig):
    rig.scenario["check"] = "refused"  # The real check's guard answers both of its questions.


def test_a_busy_queue_stops_a_rebuild_before_anything_changes(rig):
    installed(rig)
    a_local_build(rig)
    busy_queue(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert "Jobs are queued or running" in completed.stderr
    assert "nothing was changed" in completed.stderr and "Run this again when the queue is empty" in completed.stderr
    assert rig.steps() == [*LATER_PREFLIGHT, "uv pip freeze", "uv plan", "queue"]
    assert rig.snapshot() == before
    assert (rig.venv / FINISHED).is_file()


def test_the_queue_is_asked_by_a_python_that_writes_nothing_and_loads_nothing_from_here(rig):
    installed(rig)
    a_local_build(rig)
    assert rig.run("nvidia").returncode == 0
    question = rig.step("queue")
    assert question["tool"] == "system-python"
    assert question["arguments"] == ["-B", "-P", f"{rig.root}/scripts/check_gpu.py", "--queue"]


@pytest.mark.parametrize("stop", [stopped_inside_uv_venv, stopped_inside_uv_pip_sync, killed_right_after_the_wipe])
def test_a_stopped_install_does_not_wait_for_the_queue(rig, stop):
    # Discovery refuses an unfinished environment, so no job can be running from this one.
    installed(rig)
    a_local_build(rig)
    calm = dict(rig.scenario)
    stop(rig)
    rig.run("nvidia")
    rig.forget_calls()
    rig.scenario = calm
    busy_queue(rig)
    completed = rig.run("nvidia")
    assert rig.steps() == RESTARTED
    assert rig.packages() == LOCKED and (rig.venv / FINISHED).is_file()
    assert completed.returncode == 1 and "recorded nothing" in completed.stderr  # The check's own guard.


def test_a_first_install_does_not_wait_for_the_queue(rig):
    busy_queue(rig)
    completed = rig.run("nvidia")
    assert rig.steps() == FIRST_RUN
    assert rig.packages() == LOCKED and (rig.venv / FINISHED).is_file()
    assert completed.returncode == 1 and "recorded nothing" in completed.stderr  # The check's own guard.


def test_an_environment_that_equals_the_lock_goes_straight_to_the_check_and_its_guard(rig):
    installed(rig)
    busy_queue(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert rig.steps() == RE_RUN
    assert completed.returncode == 1 and "recorded nothing" in completed.stderr
    assert rig.snapshot() == before


def a_library(rig, statuses):
    """A real job table outside the checkout, or a file that is not a database when `statuses` is None."""
    data = rig.top / "data"
    if statuses is None:
        data.mkdir()
        (data / "scan3d.sqlite3").write_text("this is not a database, and it is long enough to be read as one")
        return
    with Store(data).connection() as connection:
        for number, status in enumerate(statuses):
            connection.execute("INSERT INTO jobs(id, case_id, status, data) VALUES (?, ?, ?, ?)", (f"{number:032x}", "0" * 32, status, "{}"))


@pytest.mark.parametrize(
    "statuses, reply",
    [
        (("queued",), "Jobs are queued or running"),
        (("completed", "running"), "Jobs are queued or running"),
        (None, "Could not read the job queue"),
    ],
    ids=["queued", "running", "unreadable"],
)
def test_the_real_check_answers_for_a_rebuild(rig, statuses, reply):
    installed(rig)
    a_local_build(rig)
    a_library(rig, statuses)
    shutil.copy2(ROOT / "scripts" / "check_gpu.py", rig.root / "scripts")
    before = rig.snapshot()
    completed = rig.run("nvidia", env=rig.with_a_real_check())
    assert completed.returncode == 1 and reply in completed.stderr and "nothing was changed" in completed.stderr
    assert rig.steps() == [*LATER_PREFLIGHT, "uv pip freeze", "uv plan", "queue"]
    assert rig.snapshot() == before


def test_the_real_check_lets_a_rebuild_through_when_all_work_has_finished(rig):
    installed(rig)
    a_local_build(rig)
    a_library(rig, ("completed", "failed", "cancelled"))
    shutil.copy2(ROOT / "scripts" / "check_gpu.py", rig.root / "scripts")
    rig.run("nvidia", env=rig.with_a_real_check())
    assert rig.steps() == REBUILT
    assert rig.packages() == LOCKED and (rig.top / "torch-imports").is_file()


# What the result says


def test_a_failed_check_that_wrote_a_proof_says_the_app_shows_it(rig):
    rig.scenario["check"] = "failed"
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert json.loads(rig.proof.read_text()) == {"gpu_ready": False}
    assert f"failed and wrote {rig.proof}" in completed.stderr and "unavailable, with its reason" in completed.stderr
    assert "recorded nothing" not in completed.stderr


@pytest.mark.parametrize("outcome", ["refused", "crashed", "silent"])
def test_a_check_that_wrote_nothing_claims_no_row(rig, outcome):
    rig.scenario["check"] = outcome
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert not rig.proof.exists()
    assert "recorded nothing, so the app still goes by the last proof" in completed.stderr
    assert "unavailable" not in the_result(completed)


@pytest.mark.parametrize("earlier", ["passed", "failed"])
def test_a_refused_re_run_is_not_mistaken_for_the_proof_already_there(rig, earlier):
    rig.scenario["check"] = earlier
    rig.run("nvidia")
    proof = rig.proof.read_bytes()
    rig.scenario["check"] = "refused"
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert rig.proof.read_bytes() == proof
    assert "recorded nothing" in completed.stderr and "unavailable" not in completed.stderr


def test_a_check_that_dies_after_writing_is_reported_as_that(rig):
    rig.scenario["check"] = "died"
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert f"exit status 139 after writing {rig.proof}" in completed.stderr
    assert "unavailable" not in completed.stderr and "recorded nothing" not in completed.stderr


OFFERED = "provided the existing install's TotalSegmentator, nnU-Net and nnInteractive versions equal {}. If they differ, {} as unavailable and says why."


def test_the_plan_offers_the_card_only_on_a_condition_it_cannot_check(rig):
    plan = said(rig.run("nvidia", "--dry-run").stdout)
    assert OFFERED.format("the lock's", "the app shows the card") in plan
    assert "cannot tell which Python serves the app" in plan


def test_the_result_offers_the_card_only_on_the_same_condition(rig):
    assert OFFERED.format("this environment's", "the app shows this GPU") in said(rig.run("nvidia").stdout)


def test_the_plan_says_what_a_re_run_does_to_anything_but_an_environment_equal_to_the_lock(rig):
    plan = said(rig.run("nvidia", "--dry-run").stdout)
    assert "is rebuilt from empty, from what uv has already fetched where it can" in plan
    assert "A finished environment waits for an empty queue and stays unless uv can plan the rebuild" in plan


def test_usage_says_what_a_re_run_does_and_what_it_cannot_fix(rig):
    usage = said(rig.run("--help").stdout)
    assert "whatever the reason" not in usage and "keeps the environment" not in usage
    for words in (
        "after a driver change or when the app shows this GPU as unavailable",
        "An environment that equals the lock stays, anything else is rebuilt from empty",
        "the GPU check runs again",
        "If the reason is a TotalSegmentator, nnU-Net or nnInteractive version, update the existing install as well",
    ):
        assert words in usage


UNAVAILABLE_UNTIL_FINISHED = (
    "Until an install finishes, the app shows this GPU as unavailable if it was last checked on this environment, or not at all if it was never checked."
)


@pytest.mark.parametrize(
    "stop, reason",
    [
        ("wipe_status", "The old environment could not be cleared."),
        ("venv_status", "The environment could not be created."),
        ("sync_status", "The install did not finish, so no GPU check ran."),
        ("sync_stops", "The install did not finish, so no GPU check ran."),
    ],
)
def test_a_stopped_install_says_what_the_app_shows_until_a_run_finishes(rig, stop, reason):
    rig.scenario[stop] = 1
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert said(completed.stderr).startswith(f"{reason} {UNAVAILABLE_UNTIL_FINISHED} ")
    assert "then run this again" in completed.stderr


# Where the proof goes


def test_the_proof_follows_the_variable_with_the_venv_and_the_checkout_apart(rig):
    folder = rig.elsewhere / "qualifying"
    venv = rig.top / "main checkout" / ".venv-nvidia-py314"
    completed = rig.run("nvidia", "--venv", str(venv), env={"SCAN3D_INFERENCE_PATH_DIR": str(folder)})
    assert completed.returncode == 0, completed.stderr
    assert (folder / "nvidia.json").is_file() and not rig.proof.exists()
    assert str(folder / "nvidia.json") in completed.stdout
    assert rig.step("venv-python")["env"]["SCAN3D_INFERENCE_PATH_DIR"] == str(folder)


def test_a_failed_check_is_found_in_a_folder_named_only_by_scan3d_env(rig):
    # Reading the variable alone would look in the wrong place and call this "recorded nothing".
    folder = rig.elsewhere / "configured"
    (rig.root / "scan3d.env").write_text(f"SCAN3D_INFERENCE_PATH_DIR={folder}\n")
    rig.scenario["check"] = "failed"
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert (folder / "nvidia.json").is_file() and not rig.proof.exists()
    assert f"failed and wrote {folder / 'nvidia.json'}" in completed.stderr


def test_a_real_variable_wins_over_scan3d_env(rig):
    chosen = rig.elsewhere / "chosen"
    (rig.root / "scan3d.env").write_text(f"SCAN3D_INFERENCE_PATH_DIR={rig.elsewhere / 'configured'}\n")
    assert rig.run("nvidia", env={"SCAN3D_INFERENCE_PATH_DIR": str(chosen)}).returncode == 0
    assert (chosen / "nvidia.json").is_file()


def test_a_blank_variable_means_the_default_folder(rig):
    assert rig.run("nvidia", env={"SCAN3D_INFERENCE_PATH_DIR": "  "}).returncode == 0
    assert rig.proof.is_file()


def test_settings_that_cannot_be_read_stop_the_run_before_it_starts(rig):
    (rig.root / "scan3d.env").write_text("this is not a setting\n")
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and "settings could not be read" in completed.stderr
    assert rig.snapshot() == before


# The script's text


def test_the_script_is_executable():
    assert os.access(SCRIPT, os.X_OK)


def test_the_files_the_script_names_exist_in_the_real_checkout():
    # The rig writes its own, so a rename here would otherwise surface only on a workstation.
    named = re.findall(r'"\$root/((?:requirements|scripts)/[\w./-]+)"', SCRIPT.read_text())
    assert {"requirements/cuda-py314.lock", "scripts/check_gpu.py"} <= set(named)
    assert all((ROOT / name).is_file() for name in named)


def test_the_script_never_runs_what_it_must_leave_alone():
    code = "\n".join(line for line in SCRIPT.read_text().splitlines() if not line.lstrip().startswith("#"))
    for forbidden in (
        r"python install",
        r"pip install",
        r"\bnpm\b",
        r"\bvite\b",
        r"configure_totalseg",
        r"build_dcm2niix",
        r"bootstrap",
        r"--clear",
        r"--reinstall",
        r"--refresh",
        r"\bcurl\b",
        r"\bwget\b",
        r"scan3d\.env\.example",
        r">>?\s*\S*scan3d\.env",
        r"(^|[\s\"\'])/(usr|bin|sbin|opt)/",  # PATH is what keeps a test away from real tools.
    ):
        assert re.search(forbidden, code) is None, forbidden
