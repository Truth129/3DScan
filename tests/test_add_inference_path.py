"""scripts/add_inference_path.sh, run from a sealed copy of a checkout.

Nothing real can run here. The script is copied into a temporary root, PATH holds only the
fakes below and a few coreutils, and every fake records its call, so these tests behave the
same on a workstation with an NVIDIA card as on CI. uv, nvidia-smi and df only answer. The two
interpreters are this test's own Python running a stub check, with the real settings code
copied beside it, so the folder the script names is the one the code would use.
"""

import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys

import pytest

from scan3d.config import ROOT

SCRIPT = ROOT / "scripts" / "add_inference_path.sh"
SETTINGS_CODE = ("__init__.py", "config.py", "doctor.py", "inference_paths.py")
GIB = 1024**3
COREUTILS = ("bash", "cat", "dirname", "mkdir", "realpath", "stat", "tail")
PREFLIGHT = ["uv python find", "nvidia-smi", "df", "df", "system-python"]
FIRST_RUN = [*PREFLIGHT, "uv venv", "uv pip sync", "venv-python"]
RE_RUN = ["uv python find", "nvidia-smi", "system-python", "uv pip sync", "venv-python"]
MARKER = ".scan3d-inference-path"

# Every tool the script can reach. Its first two arguments come from the shim that starts it.
FAKE_TOOL = r"""
import json
import os
from pathlib import Path
import shutil
import sys

rig, tool, *arguments = sys.argv[1:]
rig = Path(rig)
scenario = json.loads((rig / "scenario.json").read_text())
with (rig / "calls.jsonl").open("a") as calls:
    calls.write(json.dumps({"tool": tool, "arguments": arguments, "cwd": os.getcwd(), "env": dict(os.environ)}) + "\n")

if tool.endswith("python"):
    os.execv(sys.executable, [sys.executable, *arguments])
if tool == "nvidia-smi":
    print(scenario["driver"], end="")
    sys.exit(scenario["driver_status"])
if tool == "df":
    print("Avail")
    print(scenario["free_at"].get(arguments[-1], scenario["free"]))
    sys.exit()
if arguments[:2] == ["python", "find"]:
    if scenario["python"]:
        print(rig / "system-python")
    sys.exit(0 if scenario["python"] else 2)
if arguments[0] == "venv":
    if scenario["venv_status"]:
        sys.exit(scenario["venv_status"])
    target = Path(arguments[-1])
    (target / "bin").mkdir(parents=True, exist_ok=True)
    shutil.copy(rig / "venv-python", target / "bin" / "python")
    (target / "pyvenv.cfg").write_text("home = fake\n")
if arguments[:2] == ["pip", "sync"]:
    if scenario["sync_status"]:
        sys.exit(scenario["sync_status"])
    # With no pyvenv.cfg the link is the Python it points at, and uv acts on that.
    if not (Path(arguments[arguments.index("--python") + 1]).parents[1] / "pyvenv.cfg").is_file():
        (rig / "base-python-changed").touch()
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
        for folder in (self.root / "scripts", self.root / "requirements", self.root / "scan3d", self.fakes, self.bin, self.elsewhere):
            folder.mkdir(parents=True)
        self.venv = self.root / ".venv-nvidia-py314"
        self.proof = self.root / ".runtime" / "inference-paths" / "nvidia.json"
        self.scenario = {
            "python": True,
            "driver": "580.82.09\n",
            "driver_status": 0,
            "free": 13 * GIB,
            "free_at": {},
            "venv_status": 0,
            "sync_status": 0,
            "check": "passed",
        }
        shutil.copy2(SCRIPT, self.root / "scripts")
        (self.root / "scripts" / "check_gpu.py").write_text(STUB_CHECK)
        (self.root / "requirements" / "cuda-py314.lock").write_text("# A stub: only the fake uv reads this.\n")
        for name in SETTINGS_CODE:
            shutil.copy(ROOT / "scan3d" / name, self.root / "scan3d")
        for name in (".venv", ".venv-rocm10-py314"):  # What the script must never adopt.
            (self.root / name / "bin").mkdir(parents=True)
            (self.root / name / "pyvenv.cfg").write_text("home = another stack\n")
        (self.fakes / "fake_tool.py").write_text(FAKE_TOOL)
        self.shim(self.bin / "uv", "uv")
        self.shim(self.bin / "nvidia-smi", "nvidia-smi")
        self.shim(self.bin / "df", "df")
        self.shim(self.fakes / "system-python", "system-python")
        self.shim(self.fakes / "venv-python", "venv-python")
        for name in COREUTILS:
            (self.bin / name).symlink_to(shutil.which(name))

    def shim(self, location, tool):
        location.parent.mkdir(parents=True, exist_ok=True)
        command = shlex.join((sys.executable, "-I", str(self.fakes / "fake_tool.py"), str(self.fakes), tool))
        location.write_text(f'#!/usr/bin/env bash\nexec {command} "$@"\n')
        location.chmod(0o755)

    def run(self, *arguments, cwd=None, env=None):
        (self.fakes / "scenario.json").write_text(json.dumps(self.scenario))
        environment = {"PATH": str(self.bin), "HOME": str(self.top / "home"), **(env or {})}
        script = self.root / "scripts" / SCRIPT.name
        return subprocess.run([script, *arguments], cwd=cwd or self.elsewhere, env=environment, capture_output=True, text=True, timeout=60)

    def calls(self):
        log = self.fakes / "calls.jsonl"
        return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

    def forget_calls(self):
        (self.fakes / "calls.jsonl").unlink()

    def steps(self):
        """What ran, in order, with uv's subcommand named."""
        return [_step(call["tool"], call["arguments"]) for call in self.calls()]

    def step(self, name):
        """The one call of a step."""
        (call,) = [call for call, step in zip(self.calls(), self.steps()) if step == name]
        return call

    def snapshot(self):
        """Everything a run could have changed, so a test can show that it changed nothing."""
        return {str(entry): _state(entry) for folder in (self.root, self.elsewhere) for entry in sorted(folder.rglob("*"))}


def _step(tool, arguments):
    return " ".join((tool, *(word for word in arguments[:2] if not word.startswith("-")))) if tool.endswith("uv") else tool


def _state(entry):
    if entry.is_symlink():
        return os.readlink(entry)
    return (entry.stat().st_mode, entry.read_bytes() if entry.is_file() else None)


@pytest.fixture
def rig(tmp_path):
    return Rig(tmp_path)


def installed(rig):
    """A finished first run, with its calls forgotten so that a test sees only the next one."""
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    rig.forget_calls()


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


PREFLIGHT_FAILURES = {
    no_uv: "uv was not found",
    no_python: "Python 3.14 was not found",
    no_nvidia_smi: "nvidia-smi was not found",
    unreachable_driver: "nvidia-smi could not read the driver version: NVIDIA-SMI has failed",
    unknown_driver: "nvidia-smi gave no driver version: [N/A]",
    old_driver: "NVIDIA driver 579.99.01 is too old",
    two_cards_on_an_old_driver: "NVIDIA driver 579.99.01 is too old",
    small_disk: "Only 11 GiB is free",
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
        "acvl-utils",
        "without hashes",
        "will not touch",
        "no restart",
        str(rig.venv),
        str(rig.proof),
        "nothing was created",
    ):
        assert words in " ".join(completed.stdout.split())


def test_a_dry_run_on_an_installed_environment_changes_nothing_either(rig):
    installed(rig)
    before = rig.snapshot()
    completed = rig.run("nvidia", "--dry-run")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == ["uv python find", "nvidia-smi", "system-python"]
    assert rig.snapshot() == before
    assert "from an earlier run, kept" in completed.stdout


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
    assert rig.step("uv pip sync")["arguments"][3] == f"{rig.venv}/bin/python"
    assert "uv venv" not in rig.steps()


# A first run


def test_a_first_run_builds_syncs_and_checks_and_registers_the_path(rig):
    completed = rig.run("nvidia")
    assert completed.returncode == 0, completed.stderr
    assert rig.steps() == FIRST_RUN
    python = f"{rig.venv}/bin/python"
    assert rig.step("uv venv")["arguments"] == ["venv", "--python", str(rig.fakes / "system-python"), "--allow-existing", str(rig.venv)]
    assert rig.step("uv pip sync")["arguments"] == ["pip", "sync", "--python", python, "--require-hashes", f"{rig.root}/requirements/cuda-py314.lock"]
    assert rig.step("venv-python")["arguments"] == [f"{rig.root}/scripts/check_gpu.py", "--path", "nvidia"]
    assert json.loads(rig.proof.read_text()) == {"gpu_ready": True}
    assert (rig.venv / MARKER).is_file()
    assert (rig.venv / MARKER).stat().st_mode & 0o777 == 0o600  # The script's umask.
    assert "no restart" in completed.stdout and str(rig.proof) in completed.stdout


def test_a_run_stays_inside_the_rig(rig):
    assert rig.run("nvidia").returncode == 0
    calls = rig.calls()
    assert {call["env"]["PATH"] for call in calls} == {str(rig.bin)}
    assert all(Path(call["cwd"]).is_relative_to(rig.top) for call in calls)
    arguments = [word for call in calls for word in call["arguments"] if word.startswith("/")]
    assert arguments and all(Path(word).is_relative_to(rig.top) for word in arguments)
    assert {entry.name for entry in rig.root.iterdir()} == {
        ".runtime",
        ".venv",
        ".venv-nvidia-py314",
        ".venv-rocm10-py314",
        "requirements",
        "scan3d",
        "scripts",
    }


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
    assert "from an earlier run, kept" in completed.stdout


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
    assert rig.steps() == FIRST_RUN


def test_a_failed_sync_runs_no_check_and_the_next_run_carries_on(rig):
    rig.scenario["sync_status"] = 1
    completed = rig.run("nvidia")
    assert completed.returncode == 1 and "did not finish" in completed.stderr
    assert "venv-python" not in rig.steps() and not rig.proof.exists()
    rig.forget_calls()
    rig.scenario["sync_status"] = 0
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == RE_RUN


def test_a_run_stopped_after_uv_venv_made_the_link_never_reaches_the_python_it_points_at(rig):
    # uv writes bin/python before pyvenv.cfg, and the marker is written before uv starts.
    (rig.venv / "bin").mkdir(parents=True)
    (rig.venv / MARKER).touch()
    shutil.copy(rig.fakes / "venv-python", rig.venv / "bin" / "python")
    assert "kept" not in rig.run("nvidia", "--dry-run").stdout
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
    assert rig.steps()[-3:] == ["uv venv", "uv pip sync", "venv-python"]
    assert package.exists()
    assert not (rig.fakes / "base-python-changed").exists()


def test_an_environment_that_lost_its_interpreter_gets_one_back(rig):
    installed(rig)
    package = rig.venv / "lib" / "installed-package"
    package.parent.mkdir()
    package.touch()
    (rig.venv / "bin" / "python").unlink()
    assert rig.run("nvidia").returncode == 0
    assert rig.steps() == FIRST_RUN
    assert package.exists()


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
    assert "recorded nothing, so the app shows what it showed before" in completed.stderr
    assert "unavailable" not in completed.stdout + completed.stderr


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
        r"\bcurl\b",
        r"\bwget\b",
        r"scan3d\.env\.example",
        r">>?\s*\S*scan3d\.env",
        r"(^|[\s\"\'])/(usr|bin|sbin|opt)/",  # PATH is what keeps a test away from real tools.
    ):
        assert re.search(forbidden, code) is None, forbidden
