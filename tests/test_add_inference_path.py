"""scripts/add_inference_path.sh, run from a sealed copy of a checkout.

PATH holds only the fakes below and a few coreutils, and every fake records its call, so nothing
real can run. uv keeps a record of what it installed and treats it as real uv treats dist-info. A
test can attach code to any step, which is how a second run or a directory arrives when it chooses.
"""

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

SCRIPT = ROOT / "scripts" / "add_inference_path.sh"
SETTINGS_CODE = ("__init__.py", "config.py", "doctor.py", "inference_paths.py", "archive/__init__.py", "archive/settings.py")
GIB = 1024**3
LOCKED = {"alpha": "1.0", "beta": "2.0.post1"}
COREUTILS = ("bash", "cat", "dirname", "flock", "grep", "mkdir", "mktemp", "realpath", "rm", "sort", "stat", "tail")
PREFLIGHT = ["uv python find", "nvidia-smi", "df", "df", "system-python"]
FIRST_RUN = [*PREFLIGHT, "find", "uv venv", "uv pip sync", "venv-python"]
LATER_PREFLIGHT = ["uv python find", "nvidia-smi", "system-python"]  # A later run needs no room.
RE_RUN = [*LATER_PREFLIGHT, "uv pip freeze", "venv-python"]  # It equals the lock: no index.
RESTARTED = [*LATER_PREFLIGHT, "find", "uv venv", "uv pip sync", "venv-python"]  # No install has finished.
TRIAL = ["queue", "uv cache dir", "uv trial", "uv offline trial", "queue"]  # A finished environment is at stake.
TRIED = [*TRIAL, "find", "uv venv", "uv pip sync", "venv-python"]
REBUILT = [*LATER_PREFLIGHT, "uv pip freeze", *TRIED]
MARKER = ".scan3d-inference-path"
FINISHED = ".scan3d-inference-path-finished"
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

# Every tool the script can reach. Its first two arguments come from the shim that starts it. A step
# fails, or kills the script, by a test's own code in `during`.
FAKE_TOOL = r"""
import json
import os
from pathlib import Path
import re
import shutil
import signal  # For a test's own code.
import sys
import time

rig, tool, *arguments = sys.argv[1:]
rig = Path(rig)
scenario = json.loads((rig / "scenario.json").read_text())
if "--queue" in arguments:
    step = "queue"
elif "--target" in arguments:
    step = f"{tool} offline trial" if "--offline" in arguments else f"{tool} trial"
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
if "--target" in arguments:
    folder = Path(arguments[arguments.index("--target") + 1])
    call["held"] = sorted(entry.name for entry in folder.iterdir()) if folder.exists() else []
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
    sys.exit(scenario.get("driver_status"))
if tool == "df":
    print("Avail")
    print(scenario["free_at"].get(arguments[-1], scenario["free"]))
    sys.exit()
if tool == "find":
    os.execv(rig / "find", ["find", *arguments])
if arguments[:2] == ["cache", "dir"]:
    print(os.environ["UV_CACHE_DIR"])
    sys.exit()
if arguments[:2] == ["python", "find"]:
    if scenario.get("no_python"):
        sys.exit("error: No interpreter found for Python 3.14 in search path")
    print(rig / "system-python")
    sys.exit()
if arguments[0] == "venv":
    (target / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(rig / "venv-python", target / "bin" / "python")
    (target / "pyvenv.cfg").write_text("home = fake\n")
if arguments[:2] == ["pip", "sync"]:
    # What real uv does: offline it has only what it kept.
    if "--offline" in arguments and (scenario.get("cache_keeps_nothing") or not (rig / "uv-kept-the-lock").exists()):
        sys.exit("error: Because alpha was not found in the cache and you require alpha==1.0, ...")
    if not scenario.get("cache_keeps_nothing"):
        (rig / "uv-kept-the-lock").touch()
if "--target" in arguments:
    (folder / "alpha").mkdir(parents=True, exist_ok=True)  # uv makes the folder if it has gone.
    sys.exit()
if arguments[0] == "pip":
    environment = Path(arguments[arguments.index("--python") + 1]).parents[1]
    record = environment / "installed.json"
    packages = json.loads(record.read_text()) if record.exists() else {}
if arguments[:2] == ["pip", "freeze"]:
    print("".join(f"{name}=={version}\n" for name, version in sorted(packages.items())), end="")
if arguments[:2] == ["pip", "sync"]:
    # What a stop inside a package's RECORD leaves: real uv can neither read it nor remove it.
    if (environment / "torn-record").exists():
        sys.exit("error: RECORD file is invalid")
    locked = dict(re.findall(r"^([A-Za-z0-9][\w.-]*)(?:\[[^\]]*\])?==([^\s;\\]+)", Path(arguments[-1]).read_text(), re.M))
    # What real uv does: ==1.0 accepts an installed 1.0+cu126, and what the lock does not list goes.
    kept = {name: packages[name] for name, version in locked.items() if packages.get(name, "").partition("+")[0] == version}
    record.write_text(json.dumps(locked | kept))
    if scenario.get("sync_tears_a_record"):
        (environment / "torn-record").touch()
        sys.exit(130)
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
        self.cache = self.root / ".cache" / "uv"
        # What the fakes answer. Every other flag a test sets is off until then.
        self.scenario = {"during": {}, "driver": "580.82.09\n", "free": 21 * GIB, "free_at": {}, "check": "passed"}
        shutil.copy2(SCRIPT, self.root / "scripts")
        (self.root / "scripts" / "check_gpu.py").write_text(STUB_CHECK)
        self.lock.write_text(STUB_LOCK)
        for name in SETTINGS_CODE:
            (self.root / "scan3d" / name).parent.mkdir(exist_ok=True)
            shutil.copy(ROOT / "scan3d" / name, self.root / "scan3d" / name)
        # The checkout's own environment, which the script must never adopt.
        (self.root / ".venv" / "bin").mkdir(parents=True)
        (self.root / ".venv" / "pyvenv.cfg").write_text("home = another stack\n")
        (self.fakes / "fake_tool.py").write_text(FAKE_TOOL)
        for tool in ("uv", "nvidia-smi", "df", "find"):
            self.shim(self.bin / tool, tool)
        (self.fakes / "find").symlink_to(shutil.which("find"))
        self.shim(self.fakes / "system-python", "system-python")
        self.shim(self.fakes / "venv-python", "venv-python")
        for name in COREUTILS:
            (self.bin / name).symlink_to(shutil.which(name))

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

        def default_ctrl_c():
            # `pytest &` hands down an ignored SIGINT, and bash cannot trap a signal it starts with ignored.
            signal.signal(signal.SIGINT, signal.SIG_DFL)

        with (self.fakes / "background.log").open("w") as log:
            return subprocess.Popen(
                [script, *arguments], cwd=self.elsewhere, env=environment, stdout=log, stderr=log, start_new_session=True, preexec_fn=default_ctrl_c
            )

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
        (call,) = [call for call in self.calls() if call["step"] == name]
        return call

    def packages(self):
        """What the fake uv left installed."""
        return json.loads((self.venv / "installed.json").read_text())

    def snapshot(self):
        """Everything a run could have changed, so a test can show that it changed nothing. uv's cache
        stands apart, because a trial is there to fill it. `in_the_cache` tells what a trial left."""
        return {str(entry): _state(entry) for folder in (self.root, self.elsewhere) for entry in sorted(folder.rglob("*")) if ".cache" not in entry.parts}

    def in_the_cache(self):
        return sorted(entry.name for entry in self.cache.iterdir()) if self.cache.exists() else []


def _state(entry):
    if entry.is_symlink():
        return os.readlink(entry)
    return (entry.stat().st_mode, entry.stat().st_mtime_ns, entry.read_bytes() if entry.is_file() else None)


@pytest.fixture
def rig(tmp_path):
    rig = Rig(tmp_path / "a work space")  # So that every test shows the quoting too.
    yield rig
    rig.go_on()  # Whatever a failed test was holding.


def installed(rig):
    """A finished first run, with its calls forgotten so that a test sees only the next one."""
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    rig.forget_calls()


def a_rebuild(rig):
    """A finished environment that needs a rebuild. PEP 440 lets 1.0+cu126 satisfy alpha==1.0."""
    installed(rig)
    (rig.venv / "installed.json").write_text(json.dumps({**rig.packages(), "alpha": "1.0+cu126"}))


def busy_queue(rig):
    rig.scenario["check"] = "refused"  # The real check's guard answers both of its questions.


def said(words):
    """Text as a person reads it, whichever way the script wrapped it."""
    return " ".join(words.split())


# The command line


@pytest.mark.parametrize("arguments", [(), ("amd",), ("nvidia", "--venv"), ("nvidia", "--venv", "--dry-run")])
def test_a_bad_command_line_is_a_usage_error(rig, arguments):
    before = rig.snapshot()
    completed = rig.run(*arguments)
    assert completed.returncode == 2 and completed.stderr.startswith("Usage:")
    assert "unbound variable" not in completed.stderr
    assert rig.calls() == [] and rig.snapshot() == before


def test_options_go_anywhere_a_relative_venv_is_the_callers_and_help_is_not_a_failure(rig):
    # Read after moving to the checkout, .venv would name the checkout's own environment.
    completed = rig.run("--dry-run", "--venv", ".venv", "nvidia")
    assert completed.returncode == 0, completed.stderr
    assert str(rig.elsewhere / ".venv") in completed.stdout
    completed = rig.run("--help")
    assert completed.returncode == 0 and completed.stdout.startswith("Usage:") and completed.stderr == ""


# Preflight


PREFLIGHT_FAILURES = {
    "no-uv": (lambda rig: (rig.bin / "uv").unlink(), "uv was not found"),
    "no-python": (lambda rig: rig.scenario.update(no_python=True), "uv could not find Python 3.14"),
    "no-nvidia-smi": (lambda rig: (rig.bin / "nvidia-smi").unlink(), "nvidia-smi was not found"),
    "unreachable-driver": (lambda rig: rig.scenario.update(driver="NVIDIA-SMI has failed\n", driver_status=9), "nvidia-smi could not read the driver version"),
    "unknown-driver": (lambda rig: rig.scenario.update(driver="[N/A]\n"), "nvidia-smi gave no driver version: [N/A]"),
    # nvidia-smi prints a line per card.
    "two-cards-on-an-old-driver": (lambda rig: rig.scenario.update(driver="579.99.01\n579.99.01\n"), "NVIDIA driver 579.99.01 is too old"),
    "unreadable-settings": (lambda rig: (rig.root / "scan3d.env").write_text("this is not a setting\n"), "The settings could not be read"),
    # It would read as a difference from any environment, and a difference starts a rebuild.
    "a-lock-that-pins-nothing": (lambda rig: rig.lock.write_text("# Truncated.\n"), "is missing or pins nothing"),
}


@pytest.mark.parametrize("failure", PREFLIGHT_FAILURES)
def test_each_preflight_failure_says_its_own_reason_and_leaves_a_finished_environment_alone(rig, failure):
    installed(rig)
    cause, reason = PREFLIGHT_FAILURES[failure]
    cause(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert [message for _, message in PREFLIGHT_FAILURES.values() if message in completed.stderr] == [reason]
    assert "Adding the NVIDIA" not in completed.stdout
    assert rig.snapshot() == before


def test_room_is_measured_where_the_environment_and_the_cache_will_go(rig):
    venv = rig.elsewhere / "new" / "venv"
    cache = rig.top / "other-disk" / "uv"
    cache.parent.mkdir()
    rig.scenario["free_at"] = {str(cache.parent): 3 * GIB}
    completed = rig.run("nvidia", "--venv", str(venv), env={"UV_CACHE_DIR": str(cache)})
    assert completed.returncode == 1
    assert f"Only 3 GiB is free for {cache}" in completed.stderr and not venv.parent.exists()
    assert [call["arguments"][-1] for call in rig.calls() if call["tool"] == "df"] == [str(rig.elsewhere), str(cache.parent)]


# A dry run


@pytest.mark.parametrize("earlier", [False, True], ids=["first", "after-an-install"])
def test_a_dry_run_prints_the_plan_and_changes_nothing(rig, earlier):
    if earlier:
        installed(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia", "--dry-run")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == (LATER_PREFLIGHT if earlier else PREFLIGHT)
    assert rig.snapshot() == before
    for words in ("will not touch", str(rig.proof), "nothing was created"):
        assert words in said(completed.stdout)
    assert (f"{rig.venv} (from an earlier run)" in completed.stdout) == earlier
    # The folder question can neither write nor load from the working directory.
    assert rig.step("system-python")["arguments"][:3] == ["-B", "-P", "-c"]


# The target


def an_environment_whose_marker_is_a_link(rig, target):
    # A copy of an environment made of links looks like this, and this script did not make it.
    (target / "bin").mkdir(parents=True)
    (target / "pyvenv.cfg").write_text("home = another stack\n")
    (target / "a-file-of-someone-elses").touch()
    (target / MARKER).symlink_to(target / "a-file-of-someone-elses")


TAKEN = {
    "another-environment-through-a-link": (lambda rig, target: target.symlink_to(rig.root / ".venv"), "{target}/."),
    "an-empty-directory": (lambda rig, target: target.mkdir(), "{target}/"),
    "a-file": (lambda rig, target: target.touch(), "{target}"),
    # A link that points nowhere still holds the name.
    "a-dangling-link": (lambda rig, target: target.symlink_to(rig.top / "nowhere"), "{target}/"),
    "a-marker-that-is-a-link": (an_environment_whose_marker_is_a_link, "{target}"),
}


@pytest.mark.parametrize("taken", TAKEN)
def test_a_target_this_script_did_not_make_is_refused(rig, taken):
    build, spelling = TAKEN[taken]
    target = rig.elsewhere / "target"
    build(rig, target)
    before = rig.snapshot()
    completed = rig.run("nvidia", "--venv", spelling.format(target=target))
    assert completed.returncode == 1 and "already exists and this script did not make it" in completed.stderr
    assert rig.calls() == [] and rig.snapshot() == before


# A first run


def test_a_first_run_builds_syncs_and_checks_and_registers_the_path(rig):
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == FIRST_RUN
    assert rig.step("uv python find")["arguments"] == ["python", "find", "--no-cache", "--system", "--color", "never", "3.14"]
    assert rig.step("uv venv")["arguments"] == ["venv", "--python", str(rig.fakes / "system-python"), "--allow-existing", str(rig.venv)]
    assert rig.step("uv pip sync")["arguments"] == ["pip", "sync", "--python", f"{rig.venv}/bin/python", "--require-hashes", str(rig.lock)]
    assert rig.step("venv-python")["arguments"] == [f"{rig.root}/scripts/check_gpu.py", "--path", "nvidia"]
    assert rig.packages() == LOCKED and json.loads(rig.proof.read_text()) == {"gpu_ready": True}
    for mark in (MARKER, FINISHED):
        assert (rig.venv / mark).stat().st_mode & 0o777 == 0o600  # The script's umask.
    assert "no restart" in completed.stdout and str(rig.proof) in completed.stdout


def test_the_checkouts_own_uv_comes_before_the_one_on_path(rig):
    rig.shim(rig.root / ".tools" / "bin" / "uv", "checkout-uv")
    assert rig.run("nvidia").returncode == 0
    assert [step for step in rig.steps() if "uv" in step] == ["checkout-uv python find", "checkout-uv venv", "checkout-uv pip sync"]


def test_the_check_runs_without_the_launchers_library_variables(rig):
    variables = {"LD_LIBRARY_PATH": str(rig.top / "rocm-lib"), "LD_PRELOAD": str(rig.top / "hook.so")}
    assert rig.run("nvidia", env=variables).returncode == 0
    assert not variables.keys() & rig.step("venv-python")["env"].keys()
    # They reached the script, and only that child lost them.
    assert variables.items() <= rig.step("uv pip sync")["env"].items()


def test_uv_settings_that_would_break_a_rebuild_are_overridden(rig):
    # A hard link shares its file with the cache, a clone loses a package tool's execute bit, and uv
    # would fetch a Python nobody chose. The other three stop `uv venv` or the offline install,
    # after the wipe.
    a_rebuild(rig)
    unset = {"UV_VENV_CLEAR", "UV_VENV_SEED", "UV_NO_CACHE"}
    completed = rig.run("nvidia", env={"UV_LINK_MODE": "clone", "UV_PYTHON_DOWNLOADS": "automatic", **dict.fromkeys(unset, "1")})
    assert completed.returncode == 0, completed.stderr
    uv = [call["env"] for call in rig.calls() if call["tool"] == "uv"]
    assert {(env["UV_LINK_MODE"], env["UV_PYTHON_DOWNLOADS"]) for env in uv} == {("copy", "never")}
    assert not any(unset & env.keys() for env in uv)


# Running it again


def test_a_re_run_that_equals_the_real_lock_keeps_the_environment_and_repeats_the_check(rig):
    # A marker, an extra or another spelling on a pin would make every re-run reinstall everything,
    # and so would colour, which bolds every name uv prints.
    shutil.copy(ROOT / "requirements" / "cuda-py314.lock", rig.root / "requirements")
    installed(rig)
    assert len(rig.packages()) > 100
    package = rig.venv / "lib" / "installed-package"
    package.parent.mkdir()
    package.touch()
    rig.scenario["free"] = GIB  # The first install may have used the room it asked for.
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == RE_RUN
    assert rig.step("uv pip freeze")["arguments"][-3:] == ["--quiet", "--color", "never"]
    assert package.exists() and "nothing was installed" in completed.stdout


def test_an_environment_without_pyvenv_cfg_is_rebuilt_without_asking_uv_what_it_holds(rig):
    # Without pyvenv.cfg the link is the Python it points at, so what uv says would be about that.
    installed(rig)
    (rig.venv / "pyvenv.cfg").unlink()
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == [*LATER_PREFLIGHT, *TRIED]
    assert (rig.venv / "pyvenv.cfg").is_file() and rig.packages() == LOCKED


def test_a_rebuild_empties_the_directory_but_the_marker_and_never_follows_a_link_out(rig):
    a_rebuild(rig)
    leftovers = [rig.venv / "lib" / "old-module.py", rig.venv / ".gitignore"]
    leftovers[0].parent.mkdir()
    for leftover in leftovers:
        leftover.touch()
    # A real environment's interpreter is a link, and its target is the base Python.
    base = rig.top / "base-python"
    base.mkdir()
    (base / "python3.14").touch()
    (rig.venv / "lib64").symlink_to(base)
    link = rig.elsewhere / "link"
    link.symlink_to(rig.venv)
    assert rig.run("nvidia", "--venv", str(link)).returncode == 0
    assert FINISHED not in rig.step("find")["held"]  # The path is withdrawn before the wipe starts.
    assert rig.step("find")["cwd"] == str(rig.venv) and rig.step("uv venv")["held"] == [MARKER]
    # uv reads its settings from where it starts, so a rebuild starts where a first install does.
    assert rig.step("uv venv")["cwd"] == rig.step("uv pip sync")["cwd"] == str(rig.root)
    assert not any(leftover.exists() for leftover in leftovers) and (base / "python3.14").exists()
    assert {entry.name for entry in rig.venv.iterdir()} == {MARKER, FINISHED, "bin", "installed.json", "pyvenv.cfg"}
    assert link.is_symlink()


# A stopped install


@pytest.mark.parametrize(
    "stop, reason",
    [
        ({"during": {"find": "sys.exit(1)"}}, "The old environment could not be cleared."),
        ({"during": {"uv venv": "sys.exit(1)"}}, "The environment could not be created."),
        # Real uv refuses this state on every later sync, so only an empty environment gets past it.
        ({"sync_tears_a_record": True}, "The install did not finish, so no GPU check ran."),
    ],
    ids=["wipe", "uv-venv", "uv-pip-sync"],
)
def test_a_stopped_first_install_says_so_and_the_next_run_finishes_it(rig, stop, reason):
    calm = dict(rig.scenario)
    rig.scenario.update(stop)
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert said(completed.stderr).startswith(f"{reason} Until an install finishes, the app shows this GPU as unavailable ")
    assert "venv-python" not in rig.steps() and not rig.proof.exists()
    assert (rig.venv / MARKER).is_file() and not (rig.venv / FINISHED).exists()
    rig.forget_calls()
    rig.scenario = {**calm, "free": 3 * GIB}  # The cache holds what the first run fetched, which took the room.
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == RESTARTED
    assert rig.packages() == LOCKED and (rig.venv / FINISHED).is_file()


def test_a_rebuild_killed_after_its_wipe_is_finished_by_the_next_run_with_nothing_at_stake(rig):
    a_rebuild(rig)
    rig.scenario["during"] = {"uv venv": "os.kill(os.getppid(), signal.SIGKILL); sys.exit(137)"}  # Before uv makes anything.
    assert rig.run("nvidia").returncode == -signal.SIGKILL
    assert [entry.name for entry in rig.venv.iterdir()] == [MARKER]
    rig.forget_calls()
    # Discovery refuses an unfinished environment, so no job can run from it, and neither a trial
    # that would fail nor a busy queue holds the run. Only the check's own guard answers.
    rig.scenario["during"] = {"uv trial": "sys.exit(1)"}
    busy_queue(rig)
    completed = rig.run("nvidia")
    assert rig.steps() == RESTARTED
    assert "--offline" not in rig.step("uv pip sync")["arguments"]  # No trial, so nothing says uv holds it.
    assert rig.packages() == LOCKED and (rig.venv / FINISHED).is_file()
    assert completed.returncode == 1 and "recorded nothing" in completed.stderr


# What a rebuild must show before it starts


def test_an_environment_uv_cannot_read_is_left_exactly_as_it_is(rig):
    # A failed read says nothing about the lock, and only an environment that equals it stays.
    installed(rig)
    rig.scenario["during"] = {"uv pip freeze": "sys.exit('error: Failed to read metadata')"}
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert "error: Failed to read metadata" in completed.stderr  # What the person has to go on.
    assert "move the directory aside" in said(completed.stderr)  # The way out of a read that always fails.
    assert rig.steps() == [*LATER_PREFLIGHT, "uv pip freeze"]
    assert rig.snapshot() == before


def test_a_rebuild_is_tried_in_uvs_cache_then_offline_before_anything_is_wiped(rig):
    # A dry run reads index pages alone, and passes with every wheel gone. Links within the cache's
    # disk cost no data, and uv refuses a base Python that is not given a target. The offline trial
    # installs again from empty, as the rebuild then does, so no index can stop it after its wipe.
    a_rebuild(rig)
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == REBUILT  # The queue is asked before the trial and again after it.
    assert rig.packages() == LOCKED
    # Forced colour would otherwise put escapes in the name uv gives its cache.
    assert rig.step("uv cache dir")["arguments"] == ["cache", "dir", "--color", "never"]
    trial, offline = rig.step("uv trial")["arguments"], rig.step("uv offline trial")
    folder = Path(trial[trial.index("--target") + 1])
    assert folder.parent == rig.cache and rig.in_the_cache() == []
    assert trial == [
        "pip",
        "sync",
        "--link-mode",
        "hardlink",
        "--python",
        str(rig.fakes / "system-python"),
        "--target",
        str(folder),
        "--require-hashes",
        str(rig.lock),
    ]
    assert offline["arguments"] == ["pip", "sync", "--offline", *trial[2:]] and offline["held"] == []
    assert "--offline" in rig.step("uv pip sync")["arguments"]
    # By a Python that writes nothing and loads nothing from here.
    queue = [(call["tool"], call["arguments"]) for call in rig.calls() if call["step"] == "queue"]
    assert queue == [("system-python", ["-B", "-P", f"{rig.root}/scripts/check_gpu.py", "--queue"])] * 2


@pytest.mark.parametrize(
    "obstacle, steps",
    [({"during": {"uv trial": "sys.exit(1)"}}, TRIAL[:3]), ({"cache_keeps_nothing": True}, TRIAL[:4])],
    # The second is no-cache in a uv.toml, or an index that forbids storing.
    ids=["uv-cannot-reach-a-package", "a-cache-that-keeps-nothing"],
)
def test_a_rebuild_that_fails_its_trial_leaves_a_finished_environment_alone(rig, obstacle, steps):
    a_rebuild(rig)
    rig.scenario.update(obstacle)
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert "uv cannot rebuild this environment from what it can reach, so it was left as it is" in said(completed.stderr)
    assert rig.steps() == [*LATER_PREFLIGHT, "uv pip freeze", *steps]
    assert rig.snapshot() == before
    assert list(rig.tmp.iterdir()) == [] and rig.in_the_cache() == []


@pytest.mark.parametrize("stop, left", [(signal.SIGINT, 0), (signal.SIGKILL, 1)], ids=["ctrl-c", "kill"])
def test_a_stopped_trial_leaves_the_environment_for_the_next_run(rig, stop, left):
    # To the whole group, as a terminal sends it. Ctrl-C takes the trial's folder with it, and a kill
    # leaves it, as links that `uv cache prune` removes.
    a_rebuild(rig)
    rig.scenario["during"] = {"uv trial": "hold()"}
    first = rig.start("nvidia")
    rig.wait_until_held()
    before = rig.snapshot()
    os.killpg(first.pid, stop)
    assert first.wait(timeout=30) == -stop
    assert rig.snapshot() == before and len(rig.in_the_cache()) == left
    rig.scenario["during"] = {}
    assert rig.run("nvidia").returncode == 0 and rig.packages() == LOCKED


# The job queue


def test_a_busy_queue_stops_a_rebuild_before_anything_changes(rig):
    a_rebuild(rig)
    busy_queue(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert "Jobs are queued or running" in completed.stderr  # What the check found.
    assert "can start only when the job queue can be read and is empty, so it was left as it is" in said(completed.stderr)
    assert rig.steps() == [*LATER_PREFLIGHT, "uv pip freeze", "queue"]  # So it sets off no download.
    assert rig.snapshot() == before and rig.in_the_cache() == []


# One run at a time

REFUSED_FOR_ANOTHER_RUN = "Another run is using this environment, so nothing was changed. Run this again when it has finished."


def test_another_run_holds_the_environment_until_its_uv_has_gone(rig):
    # Here only the script dies, and its uv goes on writing. Starting again would wipe around it.
    a_rebuild(rig)
    link = rig.elsewhere / "link"
    link.symlink_to(rig.venv)
    rig.scenario["during"] = {"uv pip sync": "hold()"}
    first = rig.start("nvidia")
    rig.wait_until_held()
    first.kill()
    first.wait()
    rig.scenario["during"] = {}
    rig.forget_calls()
    before = rig.snapshot()
    second = rig.run("nvidia", "--venv", f"{link}/")  # The lock is the marker's, however it is named.
    assert second.returncode == 1 and REFUSED_FOR_ANOTHER_RUN in said(second.stderr)
    assert rig.steps() == LATER_PREFLIGHT
    assert rig.run("nvidia", "--dry-run").returncode == 0  # Looking needs no lock.
    assert rig.snapshot() == before
    rig.go_on()
    end = time.monotonic() + 30
    while REFUSED_FOR_ANOTHER_RUN in said((completed := rig.run("nvidia")).stderr):
        assert time.monotonic() < end, "the lock outlived the run that held it"
        time.sleep(0.01)
    assert completed.returncode == 0, completed.stderr
    assert rig.packages() == LOCKED


def test_a_lock_that_fails_is_not_called_another_run(rig):
    a_rebuild(rig)
    (rig.bin / "flock").unlink()
    (rig.bin / "flock").write_text('#!/usr/bin/env bash\necho "flock: 9: No locks available" >&2\nexit 1\n')
    (rig.bin / "flock").chmod(0o755)
    before = rig.snapshot()
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and "No locks available" in completed.stderr
    assert "The lock on this environment failed, so nothing was changed." in completed.stderr
    assert "Another run" not in completed.stderr
    assert rig.snapshot() == before


# What can happen to the target during a run


def swapped_in(rig, step, marker=None):
    """The environment moves aside the first time `step` starts, and another directory takes its name."""
    aside = rig.elsewhere / "aside"
    arrival = f"venv = Path({str(rig.venv)!r}); venv.rename({str(aside)!r}); venv.mkdir(); (venv / 'precious').touch()"
    if marker == "another":
        arrival += f"; (venv / {MARKER!r}).touch()"
    if marker == "linked":
        arrival += f"; (venv / {MARKER!r}).symlink_to({str(aside / MARKER)!r})"
    rig.scenario["during"] = {step: f"if not Path({str(aside)!r}).exists():\n    {arrival}"}
    return aside


# Another run's marker is another file. A link to the locked one is the same file to `-ef`, and
# `marked` refuses it.
@pytest.mark.parametrize("marker", ["another", "linked"], ids=["another-runs-marker", "a-link-to-the-locked-marker"])
def test_a_directory_swapped_in_before_the_wipe_is_left_alone(rig, marker):
    a_rebuild(rig)
    aside = swapped_in(rig, "queue", marker)
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and f"{rig.venv} changed during this run, so nothing was changed" in completed.stderr
    assert "find" not in rig.steps() and (rig.venv / "precious").exists()
    assert (aside / MARKER).is_file() and (aside / FINISHED).is_file()  # Neither was touched.


def test_the_wipe_stays_in_the_directory_it_checked_whatever_becomes_of_the_name(rig):
    a_rebuild(rig)
    aside = swapped_in(rig, "find")
    rig.run("nvidia")
    assert (rig.venv / "precious").exists()
    assert [entry.name for entry in aside.iterdir()] == [MARKER]


# What the result says


@pytest.mark.parametrize(
    "outcome, says",
    [
        ("failed", "The GPU check failed and wrote {proof}. A running app that reads its folder now shows this GPU as unavailable, with its reason."),
        ("died", "The GPU check stopped with exit status 139 after writing {proof}."),
        # It writes nothing, and the proof from before is not taken for a new one.
        ("refused", "The GPU check recorded nothing, so the app still goes by the last proof, if there is one."),
    ],
)
def test_a_check_that_does_not_pass_says_what_the_app_goes_by(rig, outcome, says):
    installed(rig)
    rig.scenario["check"] = outcome
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert says.format(proof=rig.proof) in said(completed.stderr)


# Where the proof goes


def test_a_failed_check_is_found_in_a_folder_named_only_by_scan3d_env(rig):
    # Reading the variable alone would look in the wrong place and call this "recorded nothing".
    folder = rig.elsewhere / "configured"
    (rig.root / "scan3d.env").write_text(f"SCAN3D_INFERENCE_PATH_DIR={folder}\n")
    rig.scenario["check"] = "failed"
    completed = rig.run("nvidia")
    assert completed.returncode == 1
    assert (folder / "nvidia.json").is_file() and not rig.proof.exists()
    assert f"failed and wrote {folder / 'nvidia.json'}" in completed.stderr


# The script's text


def test_the_script_is_executable_and_the_files_it_names_exist_in_the_real_checkout():
    # The rig writes its own, so a rename here would otherwise surface only on a workstation.
    assert os.access(SCRIPT, os.X_OK)
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
        r"--reinstall",  # Either would fetch again what uv already holds, on a rebuild that may have no network.
        r"--refresh",
        r"\bcurl\b",
        r"\bwget\b",
        r"scan3d\.env\.example",
        r">>?\s*\S*scan3d\.env",
        r"(^|[\s\"\'])/(usr|bin|sbin|opt)/",  # PATH is what keeps a test away from real tools.
    ):
        assert re.search(forbidden, code) is None, forbidden
