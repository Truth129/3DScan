"""Inference paths: discovery, binding, the child environment and per-path proofs.

A real venv stands in for a second interpreter and a fake PyTorch answers for the GPU, so
nothing here needs a second stack, a GPU or a download. See tests/inference_path_stub.py.
"""

import dataclasses
import json
import os
from pathlib import Path
import platform
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest

from fake_interactive_engine import OUT_OF_MEMORY
from inference_path_stub import ENGINES, FAMILIES, GPU_NAME, MEMORY_BYTES, TORCH, activate, add_package, child_path, stand_in
from scan3d import doctor, inference_paths, segmentation
from scan3d.api import create_app
from scan3d.config import ROOT, Settings
from scan3d.demo import create_demo
from scan3d.inference_paths import binding, gpu_cards, identity, public_error, require, rows
from scan3d.interactive_worker import InteractiveWorker
from scan3d.store import Store, public_job, public_revision
from scan3d.review import public_review
from scripts.check_gpu import jobs_waiting

CHECK_GPU = ROOT / "scripts" / "check_gpu.py"
UNSET_UP = "This GPU is not set up on this workstation."
UNREADABLE = "Its GPU check record cannot be read."
CHECK_FAILED = "Its last GPU check failed."
INSTALL_STOPPED = "Its last install did not finish."
NO_PYTHON = "Its Python environment is missing."
PACKAGES_CHANGED = "Its PyTorch or GPU runtime packages changed after the last GPU check."
ENGINE_DIFFERS = "Its TotalSegmentator, nnU-Net or nnInteractive version differs from this workstation's."
RESAMPLER = "It has cupy or cucim installed, which would change resampling."
HARDWARE_CHANGED = "The GPUs in this workstation changed after the last GPU check."
CAUTION = "Its last GPU check failed or is out of date."
NO_GPU_ADDED = (
    "GPU inference was requested but PyTorch cannot access a GPU; check the driver, then repeat this GPU's check "
    "(see Add a second GPU in docs/INSTALL.md). No other GPU was tried."
)
NO_GPU_DEFAULT = "GPU inference was requested but PyTorch cannot access a GPU; check the CUDA or ROCm setup, or set SCAN3D_DEVICE=cpu"
GIB = 1024**3


@pytest.fixture(autouse=True)
def plain_workstation(tmp_path, monkeypatch):
    """No default proof, no device request, and an empty sysfs unless a test builds one."""
    monkeypatch.setenv("SCAN3D_GPU_PROOF", str(tmp_path / "gpu-proof.json"))
    monkeypatch.delenv("SCAN3D_DEVICE", raising=False)
    monkeypatch.setattr(inference_paths, "SYS_DRM", tmp_path / "no-sysfs")


@pytest.fixture
def stub(tmp_path, monkeypatch):
    built = stand_in(tmp_path)
    activate(monkeypatch, built)
    return built


def venv_of(stub):
    return str(Path(stub.python).parents[1])


def default_proof(tmp_path, **checks):
    """A default proof that matches this process, as far as `fresh` below makes it."""
    record = {
        "gpu_ready": True,
        "python_version": platform.python_version(),
        "checks": {
            "hip_version": "7.2",
            "cuda_version": None,
            "torch_distribution_version": "1.0",
            "runtime_versions": {"torch": "1.0"},
            "devices": [{"index": 0, "name": "Test Default GPU", "memory_bytes": 32 * GIB}, {"index": 1, "name": "Test Second GPU", "memory_bytes": 8 * GIB}],
            "recommended_device": "gpu:0",
            **checks,
        },
    }
    (tmp_path / "gpu-proof.json").write_text(json.dumps(record))
    return record


@pytest.fixture
def fresh(monkeypatch):
    """This process's torch is the one a default_proof names."""
    version = doctor.metadata.version
    monkeypatch.setattr(doctor.metadata, "version", lambda package: "1.0" if package == "torch" else version(package))


def build_sysfs(root, cards, connectors=None):
    """A /sys/class/drm with `cards` {card: (vendor, device)} and `connectors` {name: status}."""
    for card, (vendor, device) in cards.items():
        folder = root / card / "device"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "vendor").write_text(f"{vendor}\n")
        (folder / "device").write_text(f"{device}\n")
    for connector, status in (connectors or {}).items():
        # A real connector has a `device` link too, whose target holds no vendor file.
        (root / connector / "device").mkdir(parents=True, exist_ok=True)
        (root / connector / "status").write_text(f"{status}\n")
    return root


@pytest.fixture
def sysfs(tmp_path, monkeypatch):
    root = tmp_path / "sysfs"
    monkeypatch.setattr(inference_paths, "SYS_DRM", root)
    return root


# Discovery


def test_one_gpu_means_one_row_and_a_job_without_a_path_reads_no_file(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_INFERENCE_PATH_DIR", str(tmp_path / "absent"))
    assert rows() == [{"id": "default", "label": "Default device", "memory_gib": None, "shares_display": False, "available": True, "reason": None}]

    def refuse(*args, **kwargs):
        raise AssertionError("A job that named no path looked for one")

    monkeypatch.setattr(inference_paths, "directory", refuse)
    monkeypatch.setattr(Path, "read_text", refuse)
    assert require(None) is None


def test_the_directory_follows_the_setting_per_call(tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_INFERENCE_PATH_DIR", str(tmp_path / "elsewhere"))
    assert inference_paths.directory() == tmp_path / "elsewhere"
    monkeypatch.setenv("SCAN3D_INFERENCE_PATH_DIR", " ")
    assert inference_paths.directory() == ROOT / ".runtime" / "inference-paths"
    monkeypatch.delenv("SCAN3D_INFERENCE_PATH_DIR")
    assert inference_paths.directory() == ROOT / ".runtime" / "inference-paths"


@pytest.mark.parametrize(
    "recommended, setting, label, memory",
    [
        ("gpu:0", None, "Test Default GPU", 32),
        ("gpu:1", None, "Test Second GPU", 8),  # The proof's recommended card is named.
        ("gpu:0", "gpu:1", "Test Second GPU", 8),  # An explicit request outranks the recommendation.
        ("gpu:0", "cpu", "CPU", None),
    ],
)
def test_the_default_row_names_the_card_its_proof_and_setting_choose(tmp_path, fresh, monkeypatch, recommended, setting, label, memory):
    default_proof(tmp_path, recommended_device=recommended)
    if setting:
        monkeypatch.setenv("SCAN3D_DEVICE", setting)
    assert rows() == [{"id": "default", "label": label, "memory_gib": memory, "shares_display": False, "available": True, "reason": None}]


@pytest.mark.parametrize(
    "stale",
    [
        lambda record: record.update(python_version="3.0.0"),
        lambda record: record["checks"].update(runtime_versions={"torch": "0.9"}),
        lambda record: record.update(gpu_ready=False, errors=["boom at /private/home/python"]),
    ],
)
def test_a_stale_default_proof_stays_available_with_a_fixed_caution(tmp_path, fresh, stale):
    record = default_proof(tmp_path)
    stale(record)
    (tmp_path / "gpu-proof.json").write_text(json.dumps(record))
    row = rows()[0]
    assert (row["available"], row["reason"], row["label"]) == (True, CAUTION, "Test Default GPU")
    assert "boom" not in json.dumps(row) and "/private" not in json.dumps(row)


@pytest.mark.parametrize(
    "malformed",
    [
        "not json",
        '{"checks": []}',
        '{"checks": {"devices": 7, "recommended_device": "gpu:0"}}',
        '{"checks": {"devices": [{"name": 7}], "recommended_device": "gpu:0"}}',
        '{"checks": {"devices": [{"name": "x"}], "recommended_device": "cuda:0"}}',
        '{"checks": {"devices": [{"name": "\\ud800"}], "recommended_device": "gpu:0"}}',
        pytest.param(json.dumps({"checks": {"devices": [{"name": "x"}], "recommended_device": f"gpu:{'9' * 5000}"}}), id="an-index-no-integer-can-hold"),
        pytest.param("[" * 200_000, id="nested-too-deeply"),
    ],
)
def test_a_malformed_default_proof_names_no_card_and_blocks_nothing(tmp_path, malformed):
    (tmp_path / "gpu-proof.json").write_text(malformed)
    row = rows()[0]
    assert (row["available"], row["reason"], row["label"], row["memory_gib"]) == (True, CAUTION, "Default device", None)


AMD_CARD, NVIDIA_CARD = ("0x1002", "0x7551"), ("0x10de", "0x2c05")


def test_a_default_proof_that_still_describes_this_machine_keeps_its_card(tmp_path, fresh, sysfs):
    build_sysfs(sysfs, {"card1": AMD_CARD})
    default_proof(tmp_path, hardware=["1002:7551"])
    assert rows()[0] == {"id": "default", "label": "Test Default GPU", "memory_gib": 32, "shares_display": False, "available": True, "reason": None}
    build_sysfs(sysfs, {"card2": NVIDIA_CARD})  # Another vendor's card is another build's census.
    assert (rows()[0]["label"], rows()[0]["reason"]) == ("Test Default GPU", None)


@pytest.mark.parametrize(
    "cards, proven",
    [
        pytest.param({"card1": ("0x1002", "0x7552")}, ["1002:7551"], id="replaced"),
        pytest.param({"card1": AMD_CARD}, ["1002:7551", "1002:7551"], id="one-of-a-twin-removed"),
        pytest.param({"card1": AMD_CARD}, [], id="the-first-one-arrived"),
    ],
)
def test_a_changed_card_costs_the_default_row_its_name_and_never_its_availability(tmp_path, fresh, sysfs, cards, proven):
    build_sysfs(sysfs, cards)
    default_proof(tmp_path, hardware=proven)
    assert rows()[0] == {"id": "default", "label": "Default device", "memory_gib": None, "shares_display": False, "available": True, "reason": CAUTION}
    # A job's label is frozen from the row, so it stops naming a card that has left.
    assert binding("default") == {"id": "default", "label": "Default device", "identity": "default"}
    assert require(binding("default")) is None


def test_a_default_proof_from_before_the_census_is_not_compared(tmp_path, fresh, sysfs):
    build_sysfs(sysfs, {"card1": ("0x1002", "0x7552")})
    assert "hardware" not in default_proof(tmp_path)["checks"]
    assert (rows()[0]["label"], rows()[0]["reason"]) == ("Test Default GPU", None)


def test_a_default_proof_on_nvidia_is_compared_with_nvidia_cards(tmp_path, fresh, sysfs):
    build_sysfs(sysfs, {"card0": AMD_CARD, "card1": NVIDIA_CARD})
    default_proof(tmp_path, hip_version=None, cuda_version="13.0", hardware=["10de:2c05"])
    assert (rows()[0]["label"], rows()[0]["reason"]) == ("Test Default GPU", None)
    build_sysfs(sysfs, {"card1": ("0x10de", "0x2c06")})
    assert (rows()[0]["label"], rows()[0]["reason"]) == ("Default device", CAUTION)


def test_a_census_that_is_not_a_list_names_no_card_and_blocks_nothing(tmp_path, fresh, sysfs):
    # With no vendor it can trust, the row cannot say the monitor shares this card.
    build_sysfs(sysfs, {"card1": AMD_CARD}, {"card1-DP-1": "connected"})
    default_proof(tmp_path, hardware=7)
    assert rows()[0] == {"id": "default", "label": "Default device", "memory_gib": None, "shares_display": False, "available": True, "reason": CAUTION}
    assert binding("default")["identity"] == "default"


def test_a_proven_path_gets_its_own_row(stub):
    assert rows()[1:] == [{"id": "nvidia", "label": GPU_NAME, "memory_gib": 16, "shares_display": False, "available": True, "reason": None}]


def failed_check(stub):
    stub.change(lambda record: record.update(gpu_ready=False, errors=["PRIVATE failure at /private/home/venv/bin/python"]))


def missing_interpreter(stub):
    Path(stub.python).unlink()


def stopped_install(stub):
    """What the install script leaves from the moment it starts changing an environment."""
    (Path(venv_of(stub)) / ".scan3d-inference-path").write_text("Made by scripts/add_inference_path.sh nvidia.\n")


def finished_install(stub):
    stopped_install(stub)
    (Path(venv_of(stub)) / ".scan3d-inference-path-finished").write_text("The last install finished.\n")


def changed_family(stub):
    shutil.rmtree(stub.site_packages / f"cuda_toolkit-{FAMILIES['cuda']['cuda-toolkit']}.dist-info")
    add_package(stub.site_packages, "cuda-toolkit", "13.2.1")


def removed_family(stub):
    shutil.rmtree(stub.site_packages / f"torch-{TORCH}.dist-info")


def cupy_wheel(stub):
    add_package(stub.site_packages, "cupy-cuda13x", "13.6.0")


def torch_record(stub):
    return next(stub.site_packages.glob("torch-*.dist-info")) / "METADATA"


def a_looping_link(path):
    path.unlink()
    path.symlink_to(path)


DAMAGE = {
    "corrupt": (lambda stub: stub.proof.write_text("{not json"), UNREADABLE),
    "no-block": (lambda stub: stub.change(lambda record: record.pop("path")), UNREADABLE),
    "another-id": (lambda stub: stub.change(lambda record: record["path"].update(id="renamed")), UNREADABLE),
    "cpu-device": (lambda stub: stub.change(lambda record: record["path"].update(device="cpu")), UNREADABLE),
    "untyped-packages": (lambda stub: stub.change(lambda record: record["checks"].update(runtime_versions=["torch"])), UNREADABLE),
    "null-byte-in-a-field": (lambda stub: stub.change(lambda record: record["path"].update(python="/venv/bin/python\x00")), UNREADABLE),
    "unencodable-name": (lambda stub: stub.change(lambda record: record["path"].update(gpu_name="\ud800")), UNREADABLE),
    "nested-too-deeply": (lambda stub: stub.proof.write_text("[" * 200_000), UNREADABLE),
    "failed-check": (failed_check, CHECK_FAILED),
    "truthy-ready": (lambda stub: stub.change(lambda record: record.update(gpu_ready="yes")), CHECK_FAILED),
    "stopped-install": (stopped_install, INSTALL_STOPPED),
    "missing-interpreter": (missing_interpreter, NO_PYTHON),
    "changed-family": (changed_family, PACKAGES_CHANGED),
    "removed-family": (removed_family, PACKAGES_CHANGED),
    "looping-package-record": (lambda stub: a_looping_link(torch_record(stub)), PACKAGES_CHANGED),
    "bottomless-package-record": (
        lambda stub: torch_record(stub).write_text("".join(f'Content-Type: multipart/mixed; boundary="{level}"\n\n--{level}\n' for level in range(2000))),
        PACKAGES_CHANGED,
    ),
    "record-without-a-version": (lambda stub: torch_record(stub).write_text(""), PACKAGES_CHANGED),
    "cupy-wheel": (cupy_wheel, RESAMPLER),
    "cucim-import": (lambda stub: (stub.site_packages / "cucim").mkdir(), RESAMPLER),
}


@pytest.mark.parametrize("damage", DAMAGE)
def test_each_failed_check_disables_its_row_with_one_fixed_sentence(stub, tmp_path, damage):
    damage, reason = DAMAGE[damage]
    damage(stub)
    default, added = rows()
    assert default["available"] and (added["id"], added["available"], added["reason"]) == ("nvidia", False, reason)
    public = json.dumps(added, ensure_ascii=False)
    public.encode()  # What a response does with it.
    assert str(tmp_path) not in public and "PRIVATE" not in public and "/private" not in public
    with pytest.raises(ValueError, match="is unavailable: "):
        binding("nvidia")


def test_a_proof_that_cannot_be_opened_disables_its_row(stub):
    a_looping_link(stub.proof)
    assert (rows()[1]["available"], rows()[1]["reason"]) == (False, UNREADABLE)


@pytest.mark.parametrize("also_missing", [missing_interpreter, cupy_wheel])
def test_a_stopped_install_is_the_reason_however_little_of_it_is_left(stub, also_missing):
    stopped_install(stub)
    also_missing(stub)
    assert (rows()[1]["available"], rows()[1]["reason"]) == (False, INSTALL_STOPPED)


def test_a_failed_check_outranks_a_stopped_install(stub):
    # A failed proof is never judged further, and either way the path is withdrawn.
    stopped_install(stub)
    failed_check(stub)
    assert (rows()[1]["available"], rows()[1]["reason"]) == (False, CHECK_FAILED)


def test_a_finished_install_passes_and_its_mark_excuses_nothing_else(stub):
    finished_install(stub)
    assert (rows()[1]["available"], rows()[1]["reason"]) == (True, None)
    missing_interpreter(stub)
    assert (rows()[1]["available"], rows()[1]["reason"]) == (False, NO_PYTHON)


def test_a_hand_made_environment_needs_neither_file(stub):
    assert not list(Path(venv_of(stub)).glob(".scan3d-*"))
    assert (rows()[1]["available"], rows()[1]["reason"]) == (True, None)
    assert require(binding("nvidia")).python == stub.python


@pytest.mark.parametrize("python", ["python", "/python"])
def test_an_interpreter_with_no_environment_around_it_costs_only_its_own_row(stub, python):
    stub.change(lambda record: record["path"].update(python=python))
    assert (rows()[0]["available"], rows()[1]["reason"]) == (True, NO_PYTHON)


def test_the_install_script_writes_the_files_discovery_reads():
    script = (ROOT / "scripts" / "add_inference_path.sh").read_text()
    assert 'marker="$venv/.scan3d-inference-path"\n' in script and 'finished="$venv/.scan3d-inference-path-finished"\n' in script


@pytest.mark.parametrize("engine, lacks", [*((engine, False) for engine in ENGINES), ("nnunetv2", True)])
def test_an_engine_that_differs_from_the_api_or_that_the_path_lacks_disables_it(stub, monkeypatch, engine, lacks):
    assert rows()[1]["available"]
    version = doctor.metadata.version
    # Patched so the API has it on any machine: two absences would match.
    monkeypatch.setattr(doctor.metadata, "version", lambda package: "9.9.9" if package == engine else version(package))
    if lacks:
        drop_package(stub, engine)
    assert (rows()[1]["available"], rows()[1]["reason"]) == (False, ENGINE_DIFFERS)


def test_a_card_a_proof_never_saw_disables_it_and_the_other_vendors_cards_do_not(stub, sysfs):
    build_sysfs(sysfs, {"card2": ("0x10de", "0x2c05")})
    stub.change(lambda record: record["checks"].update(hardware=["10de:2c05"]))
    assert rows()[1]["available"]
    build_sysfs(sysfs, {"card1": ("0x1002", "0x7551")})  # Adding an AMD card changes nothing.
    assert rows()[1]["available"]
    build_sysfs(sysfs, {"card3": ("0x10de", "0x2204")})
    assert (rows()[1]["available"], rows()[1]["reason"]) == (False, HARDWARE_CHANGED)


def test_a_proof_without_a_census_is_not_compared_with_one(stub, sysfs):
    build_sysfs(sysfs, {"card2": ("0x10de", "0x2c05")})
    assert rows()[1]["available"]


def test_a_failed_proof_keeps_the_card_name_it_measured(stub):
    stub.change(lambda record: record.update(gpu_ready=False))
    assert (rows()[1]["label"], rows()[1]["memory_gib"]) == (GPU_NAME, 16)


@pytest.mark.parametrize(
    "fields",
    [{"gpu_name": None, "memory_bytes": None, "kind": None}, {"gpu_name": [], "memory_bytes": {}, "kind": []}, {"gpu_name": "\ud800"}],
    ids=["no-card", "any-field-shape", "a-name-no-response-can-carry"],
)
def test_a_failed_proof_with_no_usable_card_is_named_by_its_id(stub, fields):
    stub.change(lambda record: (record.update(gpu_ready=False), record["path"].update(fields)))
    assert (rows()[1]["label"], rows()[1]["reason"]) == ("nvidia", CHECK_FAILED)


def test_memory_rounds_to_whole_gibibytes(stub):
    stub.change(lambda record: record["path"].update(memory_bytes=15992 * 1024**2))
    assert rows()[1]["memory_gib"] == 16


def test_a_bad_file_disables_only_its_own_row_and_rows_sort_by_id(tmp_path, stub):
    proofs = stub.proof.parent
    second = stand_in(tmp_path, "amd-second", proofs=proofs, kind="rocm", gpu_name="Test Second AMD GPU")
    (proofs / "zz-broken.json").write_text("{")
    assert [(row["id"], row["available"]) for row in rows()] == [("default", True), ("amd-second", True), ("nvidia", True), ("zz-broken", False)]
    assert second.proof.is_file()


@pytest.mark.parametrize("name", ["default.json", "Upper.json", f"{'x' * 33}.json", "nvidia.json.writing"])
def test_a_file_that_is_not_a_path_id_is_not_a_path(tmp_path, monkeypatch, name):
    folder = tmp_path / "paths"
    folder.mkdir()
    (folder / name).write_text("{}")
    monkeypatch.setenv("SCAN3D_INFERENCE_PATH_DIR", str(folder))
    assert [row["id"] for row in rows()] == ["default"]


def test_a_healthy_added_path_does_not_touch_the_default_paths_health(stub, tmp_path, fresh):
    default_proof(tmp_path)
    assert stub.record()["checks"]["runtime_versions"]["torch"] != "1.0"
    assert doctor.diagnose(live=False)["ready"] and rows()[1]["available"] and rows()[0]["reason"] is None


def test_discovery_starts_no_process(stub, tmp_path, fresh, monkeypatch):
    default_proof(tmp_path)

    def refuse(*args, **kwargs):
        raise AssertionError("Discovery started a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    assert [row["available"] for row in rows()] == [True, True]
    binding("nvidia")


def test_discovery_imports_no_gpu_runtime_and_needs_only_the_standard_library(tmp_path):
    # Bare interpreter, no parent packages: exactly what a venv holding only the NVIDIA stack
    # gives this module, which the API process and check_gpu.py both import.
    stub = stand_in(tmp_path)
    code = """
import json, sys
from scan3d import inference_paths
sys.stdout.write(json.dumps([[row["id"] for row in inference_paths.rows()], sorted(name for name in sys.modules if name.partition(".")[0] in ("scan3d", "torch"))]))
"""
    environment = {**os.environ, "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1", "SCAN3D_INFERENCE_PATH_DIR": str(stub.proof.parent)}
    completed = subprocess.run([stub.python, "-P", "-c", code], env=environment, capture_output=True, text=True, timeout=60)
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == [["default", "nvidia"], ["scan3d", "scan3d.config", "scan3d.doctor", "scan3d.inference_paths"]]


def test_both_panel_routes_list_the_paths_and_health_does_not(stub, tmp_path):
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    client = TestClient(app, base_url="http://127.0.0.1")
    for route in ("/api/segmentation-options", "/api/interactive/status"):
        response = client.get(route)
        assert [row["id"] for row in response.json()["inference_paths"]] == ["default", "nvidia"]
        assert str(tmp_path) not in response.text
    assert set(client.get("/api/segmentation-options").json()) >= {"tasks", "inference_paths"}
    health = client.get("/api/health").json()
    assert "inference_paths" not in health and "inference_paths" not in health["gpu"]


BAD_PROOFS = {
    "nested-too-deeply": "[" * 200_000,
    "a-name-no-response-can-carry": json.dumps(
        {"checks": {"devices": [{"name": "\ud800"}], "recommended_device": "gpu:0"}, "path": {"id": "nvidia", "gpu_name": "\ud800"}}
    ),
    "an-index-no-integer-can-hold": json.dumps({"checks": {"devices": [{"name": "x"}], "recommended_device": f"gpu:{'9' * 5000}"}, "path": {"id": "nvidia"}}),
}


@pytest.mark.parametrize(
    "proof, bad", [("default", "nested-too-deeply"), ("added", "a-name-no-response-can-carry"), ("default", "an-index-no-integer-can-hold")]
)
def test_no_single_proof_can_take_down_a_panel(stub, tmp_path, proof, bad):
    (tmp_path / "gpu-proof.json" if proof == "default" else stub.proof).write_text(BAD_PROOFS[bad])
    client = TestClient(create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist")), base_url="http://127.0.0.1")
    for route in ("/api/segmentation-options", "/api/interactive/status"):
        response = client.get(route)
        assert response.status_code == 200, response.text
        assert [row["id"] for row in response.json()["inference_paths"]] == ["default", "nvidia"]


# GPUs in sysfs


def test_gpu_cards_are_one_vendors_sorted_ids_and_connectors_are_not_cards(sysfs):
    build_sysfs(
        sysfs,
        {"card3": ("0x10de", "0x2c05"), "card1": ("0x1002", "0x7551"), "card2": ("0x10de", "0x2204"), "card4": ("0x10de", "0x2c05")},
        {"card3-DP-5": "connected", "card1-DP-1": "disconnected"},
    )
    (sysfs / "renderD128").mkdir()
    (sysfs / "version").write_text("drm 1.1.0\n")
    assert gpu_cards("cuda") == ["10de:2204", "10de:2c05", "10de:2c05"]  # Twins are both counted.
    assert gpu_cards("rocm") == ["1002:7551"]


def test_an_unknown_vendor_an_unreadable_id_or_missing_sysfs_finds_no_cards(sysfs):
    assert gpu_cards("cuda") == []
    build_sysfs(sysfs, {"card1": ("0x8086", "0x1234"), "card2": ("0x10de", "0x2c05")})
    (sysfs / "card2" / "device" / "device").unlink()
    assert gpu_cards("cuda") == gpu_cards("rocm") == gpu_cards("other") == gpu_cards(None) == []


def test_shares_display_follows_a_connected_connector_of_that_vendor(stub, sysfs):
    build_sysfs(
        sysfs,
        {"card1": ("0x1002", "0x7551"), "card2": ("0x10de", "0x2c05")},
        {"card1-DP-1": "disconnected", "card1-Writeback-1": "unknown", "card2-DP-5": "connected"},
    )
    assert rows()[1]["shares_display"] is True
    (sysfs / "card2-DP-5" / "status").write_text("disconnected\n")  # Measured per call.
    assert rows()[1]["shares_display"] is False
    (sysfs / "card1-DP-1" / "status").write_text("connected\n")
    assert rows()[1]["shares_display"] is False


def test_the_default_row_takes_its_vendor_from_its_proof(tmp_path, fresh, sysfs):
    build_sysfs(sysfs, {"card1": ("0x1002", "0x7551"), "card2": ("0x10de", "0x2c05")}, {"card2-DP-5": "connected"})
    assert rows()[0]["shares_display"] is False  # No proof, so no vendor.
    default_proof(tmp_path)
    assert rows()[0]["shares_display"] is False  # ROCm build, and the monitor is on NVIDIA.
    default_proof(tmp_path, hip_version=None, cuda_version="13.0")
    assert rows()[0]["shares_display"] is True


def test_unreadable_connector_state_never_claims_the_display(stub, sysfs):
    build_sysfs(sysfs, {"card2": ("0x10de", "0x2c05")}, {"card2-DP-5": "connected"})
    (sysfs / "card2-DP-5" / "status").unlink()
    assert rows()[1]["shares_display"] is False
    build_sysfs(sysfs, {}, {"card9-DP-1": "connected"})  # A connector whose card has no vendor.
    assert rows()[1]["shares_display"] is False


# Binding a job to a path


def test_default_needs_no_proof_and_binds_to_its_constant_identity(tmp_path, fresh):
    default_proof(tmp_path)
    assert binding("default") == {"id": "default", "label": "Test Default GPU", "identity": "default"}
    assert identity(None) == identity(binding("default")) == inference_paths.DEFAULT == "default"
    assert require(binding("default")) is None


def test_an_added_path_binds_to_its_card_and_passes_its_recheck(stub):
    bound = binding("nvidia")
    assert bound == {"id": "nvidia", "label": GPU_NAME, "identity": f"nvidia|gpu|{GPU_NAME}"}
    assert identity(bound) == bound["identity"] != identity(None)
    assert require(bound).python == stub.python


@pytest.mark.parametrize("name", ["absent", "../nvidia", None])
def test_a_path_that_is_not_set_up_is_refused_with_a_fixed_sentence(stub, name):
    with pytest.raises(ValueError) as error:
        binding(name)
    assert str(error.value) == UNSET_UP


def test_a_request_cannot_reach_a_proof_outside_the_directory(stub, tmp_path):
    stand_in(tmp_path, "outside", proofs=tmp_path)
    assert (stub.proof.parent / ".." / "outside.json").is_file()
    with pytest.raises(ValueError) as error:
        binding("../outside")
    assert str(error.value) == UNSET_UP


def test_an_unavailable_path_is_refused_by_name(stub):
    failed_check(stub)
    with pytest.raises(ValueError) as error:
        binding("nvidia")
    assert str(error.value) == f"{GPU_NAME} is unavailable: {CHECK_FAILED}"


@pytest.mark.parametrize("change, reason", [(lambda stub: stub.proof.unlink(), UNSET_UP), (missing_interpreter, NO_PYTHON), (stopped_install, INSTALL_STOPPED)])
def test_a_path_that_went_away_fails_its_queued_job_by_name(stub, change, reason):
    bound = binding("nvidia")
    change(stub)
    with pytest.raises(ValueError) as error:
        require(bound)
    assert str(error.value) == f"{GPU_NAME} is no longer available: {reason} Nothing ran and no other GPU was tried."


def test_a_finished_install_gives_the_path_back_to_a_queued_job(stub):
    bound = binding("nvidia")
    stopped_install(stub)
    finished_install(stub)
    assert binding("nvidia") == bound and require(bound).python == stub.python


def test_a_path_whose_card_changed_fails_its_queued_job_and_never_reroutes(stub):
    bound = binding("nvidia")
    stub.change(lambda record: record["path"].update(gpu_name="Test Replacement GPU"))
    with pytest.raises(ValueError) as error:
        require(bound)
    assert str(error.value) == f"{GPU_NAME} changed after this job was queued. Nothing ran and no other GPU was tried. Start it again."


def test_a_reissued_proof_for_the_same_card_keeps_its_queued_job(stub):
    bound = binding("nvidia")
    before = require(bound)
    stub.change(lambda record: record["path"].update(memory_bytes=MEMORY_BYTES + 1))
    after = require(bound)
    assert after != before and after.identity == before.identity


# One path as a value


def test_the_interpreter_is_kept_as_the_venv_reported_it(stub):
    path = require(binding("nvidia"))
    assert Path(stub.python).is_symlink() and Path(stub.python).resolve() != Path(stub.python)
    assert path.python == stub.python and path.environment()["PATH"].startswith(f"{Path(stub.python).parent}{os.pathsep}")


WITHHELD = (
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
KEPT = {
    "TOTALSEG_HOME_DIR": "/shared/totalsegmentator",
    "TOTALSEG_WEIGHTS_PATH": "/shared/weights",
    "SCAN3D_NNINTERACTIVE_MODEL": "/shared/nninteractive",
    "SCAN3D_FAKE_GPUS": "[]",
    "SCAN3D_FAKE_ENGINE_RELEASES": "/shared/marker",
}


def test_the_environment_withholds_the_other_stacks_variables_and_keeps_the_rest(stub, monkeypatch):
    before_path = "/usr/bin"
    for name in WITHHELD:
        monkeypatch.setenv(name, "inherited")
    for name, value in KEPT.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("PATH", before_path)
    monkeypatch.setenv("XDG_CACHE_HOME", "/rocm/cache")
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu:1")
    inherited = dict(os.environ)
    path = require(binding("nvidia"))
    env = path.environment()
    assert not set(WITHHELD) & set(env)
    assert {name: env[name] for name in KEPT} == KEPT
    assert env["PATH"] == f"{Path(stub.python).parent}{os.pathsep}{before_path}"
    assert env["SCAN3D_DEVICE"] == "gpu"  # gpu:1 was the default path's.
    assert env["SCAN3D_INFERENCE_PATH"] == "nvidia"
    assert env["XDG_CACHE_HOME"] == str(ROOT / ".cache" / "inference-paths" / "nvidia")
    assert env["PYTHONPATH"] == os.environ["PYTHONPATH"]
    assert dict(os.environ) == inherited


def test_the_environment_needs_no_inherited_path(stub, monkeypatch):
    path = require(binding("nvidia"))
    monkeypatch.delenv("PATH")
    assert path.environment()["PATH"] == str(Path(stub.python).parent)


def test_a_device_request_on_a_path_reaches_its_children(tmp_path, monkeypatch):
    built = stand_in(tmp_path, device="gpu:1")
    activate(monkeypatch, built)
    assert require(binding("nvidia")).environment()["SCAN3D_DEVICE"] == "gpu:1"


def drop_package(stub, name):
    for info in stub.site_packages.glob(f"{name.replace('-', '_')}-*.dist-info"):
        shutil.rmtree(info)


def test_versions_come_from_the_paths_own_site_packages(stub):
    path = require(binding("nvidia"))
    drop_package(stub, "nnunetv2")
    add_package(stub.site_packages, "nnunetv2", "0.0.1")  # Whatever this process has installed.
    drop_package(stub, "TotalSegmentator")
    versions = path.versions()
    assert versions["nnunetv2"] == "0.0.1" and versions["torch"] == TORCH and versions["TotalSegmentator"] == "unavailable"
    assert {name: versions[name] for name in FAMILIES["cuda"]} == FAMILIES["cuda"]


def test_confirm_passes_any_index_of_a_plain_gpu_request_and_holds_an_explicit_one(stub, tmp_path, monkeypatch):
    path = require(binding("nvidia"))
    path.confirm("gpu:0", GPU_NAME, "13.0")
    path.confirm("gpu:3", GPU_NAME, "13.0")
    activate(monkeypatch, stand_in(tmp_path / "explicit", device="gpu:1"))
    path = require(binding("nvidia"))
    path.confirm("gpu:1", GPU_NAME, "13.0")
    with pytest.raises(RuntimeError, match=r"found Test Added GPU \(gpu:0, CUDA\)"):
        path.confirm("gpu:0", GPU_NAME, "13.0")


@pytest.mark.parametrize(
    "device, gpu_name, cuda, found",
    [
        ("gpu:0", "Test Other GPU", "13.0", r"Test Other GPU \(gpu:0, CUDA\)"),
        ("gpu:0", GPU_NAME, None, rf"{GPU_NAME} \(gpu:0, no CUDA\)"),
        ("cpu", None, None, r"no GPU \(cpu, no CUDA\)"),
    ],
)
def test_confirm_refuses_any_other_card_or_build_by_name(stub, device, gpu_name, cuda, found):
    with pytest.raises(RuntimeError, match=f"Expected {GPU_NAME}, but the GPU check found {found}. No result was kept and no other GPU was tried."):
        require(binding("nvidia")).confirm(device, gpu_name, cuda)


def test_confirm_knows_a_rocm_path_has_no_cuda_build(tmp_path, monkeypatch):
    built = stand_in(tmp_path, kind="rocm")
    activate(monkeypatch, built)
    path = require(binding("nvidia"))
    path.confirm("gpu:0", GPU_NAME, None)
    with pytest.raises(RuntimeError, match="found"):
        path.confirm("gpu:0", GPU_NAME, "13.0")


# Public text


def test_a_timeout_never_shows_its_command_line_and_an_empty_error_still_says_something():
    assert public_error(subprocess.TimeoutExpired(["/home/someone/venv/bin/python", "-m", "worker"], 10)) == "The AI process did not stop in time."
    assert public_error(RuntimeError()) == "RuntimeError"


@pytest.mark.parametrize(
    "message, expected",
    [
        ("[Errno 2] No such file or directory: '/home/some one/venv/bin/python'", "[Errno 2] No such file or directory: <local path>"),
        ('cannot open "/data/case 1/ct.nii.gz"', "cannot open <local path>"),
        ("search path /opt/a/lib:/opt/b/lib was empty", "search path <local path>:<local path> was empty"),
        ("failed at --output=/tmp/x, retry", "failed at --output=<local path>, retry"),
        ("/home/some user/.venv/lib/foo.so: undefined symbol: x", "<local path>: undefined symbol: x"),
        ("cannot find /home/some long name/venv/bin/python in the path", "cannot find <local path> in the path"),
        ("first /tmp/a b/c\nsecond line /tmp/d", "first <local path>\nsecond line <local path>"),
        ("CUDA out of memory and/or 1/2 of ./relative/file", "CUDA out of memory and/or 1/2 of ./relative/file"),
        ("built with 3.14/rocm and/or 13.0/cuda on x86-64/linux", "built with 3.14/rocm and/or 13.0/cuda on x86-64/linux"),
        (r"child said: b'starting\n/home/someone/lib/libfoo.so: cannot open\n'", r"child said: b'starting\n<local path>: cannot open\n'"),
        (r"child said: b'columns\t/home/someone/ct.nii.gz\tlast'", r"child said: b'columns\t<local path>'"),
        ("g++ -L/home/someone/lib -o a.out failed", "g++ -L<local path> -o a.out failed"),
        ("cc '-I/home/someone/include' failed", "cc '-I<local path>' failed"),
        pytest.param(OUT_OF_MEMORY, OUT_OF_MEMORY, id="pytorch-out-of-memory-keeps-its-address"),
        ("https://example.org:8443/help", "https://example.org:8443/help"),
        ("https:///home/someone/ct.nii.gz", "https:<local path>"),  # An address names a host, and this one holds only a path.
        ("see https://example.org/help, then open /home/someone/ct.nii.gz", "see https://example.org/help, then open <local path>"),
    ],
)
def test_absolute_paths_leave_a_public_error(message, expected):
    assert public_error(RuntimeError(message)) == expected


# Whole segmentation jobs, in real children of the stand-in

TOTALSEGMENTATOR = """
import json, os, signal, sys
from pathlib import Path

import nibabel as nib
import numpy as np
import torch

plan = json.loads(os.environ["SCAN3D_FAKE_TOTALSEG"])
option = lambda name: sys.argv[sys.argv.index(name) + 1]
with open(os.environ["SCAN3D_FAKE_TOTALSEG_RUNS"], "a") as runs:
    print(json.dumps({"prefix": sys.prefix, "executable": sys.executable, "arguments": sys.argv[1:], "environment": dict(os.environ)}), file=runs)
print(plan.get("log", "Running"))
if plan.get("kill"):
    os.kill(os.getpid(), signal.SIGKILL)
if plan.get("exit"):
    sys.exit(plan["exit"])
reference = nib.load(option("-i"))
mask = np.zeros(reference.shape, np.uint8)
mask[10:20, 10:20, 10:20] = 1
nib.save(nib.Nifti1Image(mask, reference.affine, reference.header.copy()), Path(option("-o")) / "liver.nii.gz")
report = {"device": "gpu", "torch_version": torch.__version__, "classes": {"1": "liver"}, **plan.get("report", {})}
Path(option("--report")).write_text(json.dumps(report))
"""
OVERFLOW = "Prediction on device was unsuccessful, probably due to a lack of memory. Moving results arrays to CPU"
OVERFLOW_QC = f"Part of total re-ran with results in system memory on {GPU_NAME}, usually because GPU memory ran out. The run took longer."


@pytest.fixture
def segmenting(request, tmp_path, monkeypatch):
    """A demo case whose jobs start real children: the device check and a shadow TotalSegmentator.

    The stand-in sits under a PRIVATE- folder, so any public text that names its interpreter
    or site-packages shows up. The default path's child finds the shadow too, on PYTHONPATH.
    No weights exist here, so the fingerprint is told they are complete. Its digest stays the
    real one, which is what the job's call and the run's call must agree on. Indirect
    parametrization passes other `stand_in` keywords, and the build they name is process-wide.
    """
    stub = stand_in(tmp_path / "PRIVATE-home", **getattr(request, "param", {}))
    stub.shadow("totalsegmentator.bin.TotalSegmentator", TOTALSEGMENTATOR)
    activate(monkeypatch, stub)
    runs = tmp_path / "totalsegmentator-runs"
    monkeypatch.setenv("SCAN3D_FAKE_TOTALSEG_RUNS", str(runs))
    monkeypatch.setenv("SCAN3D_FAKE_TORCH_PREFIXES", str(tmp_path / "prefixes"))
    real = segmentation.model_fingerprint
    monkeypatch.setattr(segmentation, "model_fingerprint", lambda *args, **kwargs: {**real(*args, **kwargs), "weights_complete": True})
    app = create_app(Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist"))
    service = app.state.service
    case = create_demo(service)

    def queue(name=None, **plan):
        monkeypatch.setenv("SCAN3D_FAKE_TOTALSEG", json.dumps(plan))
        return service.segment_case(case["id"], service.store.get("cases", case["id"])["active_revision_id"], "total", inference_path=name)

    def finish(queued):
        service.execute(service.store.claim())
        return service.store.get("jobs", queued["id"])

    fixture = SimpleNamespace(
        stub=stub,
        service=service,
        case=case,
        http=TestClient(app, base_url="http://127.0.0.1"),
        queue=queue,
        finish=finish,
        run=lambda name=None, **plan: finish(queue(name, **plan)),
        revision=lambda job: service.store.revision(case["id"], job["revision_id"]),
        runs=lambda: [json.loads(line) for line in runs.read_text().splitlines()] if runs.exists() else [],
        prefixes=lambda: (tmp_path / "prefixes").read_text().split() if (tmp_path / "prefixes").exists() else [],
    )
    yield fixture
    fixture.http.close()


def saved_runs(segmenting):
    return list(segmenting.service.store.case_dir(segmenting.case["id"]).glob("inference/*/run-*"))


def test_a_job_on_a_path_runs_in_that_interpreter_with_that_environment(segmenting, monkeypatch):
    other_stack = {"CUDA_VISIBLE_DEVICES": "GPU-other", "LD_LIBRARY_PATH": "/opt/other-stack/lib", "TORCHINDUCTOR_CACHE_DIR": "/opt/other-stack/cache"}
    for name, value in other_stack.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu:7")  # The default path's own choice.
    monkeypatch.setenv("TOTALSEG_HOME_DIR", "/shared/totalsegmentator")
    job = segmenting.run("nvidia")
    assert job["status"] == "completed", job
    (run,) = segmenting.runs()
    assert (run["prefix"], run["executable"]) == (venv_of(segmenting.stub), segmenting.stub.python)
    assert segmenting.prefixes() == [venv_of(segmenting.stub)] * 2  # The device check, then the run.
    assert "--verbose" in run["arguments"] and run["arguments"][run["arguments"].index("--device") + 1] == "gpu:0"
    assert not set(other_stack) & set(run["environment"])
    assert run["environment"]["SCAN3D_DEVICE"] == "gpu"
    assert run["environment"]["TOTALSEG_HOME_DIR"] == "/shared/totalsegmentator"
    assert run["environment"]["XDG_CACHE_HOME"] == str(ROOT / ".cache" / "inference-paths" / "nvidia")
    assert run["environment"]["PATH"].startswith(f"{Path(segmenting.stub.python).parent}{os.pathsep}")


def test_the_default_paths_children_keep_todays_interpreter_and_environment(segmenting, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/this-stack/lib")
    monkeypatch.setenv("XDG_CACHE_HOME", "/opt/this-stack/cache")
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu:0")
    job = segmenting.run()
    assert job["status"] == "completed", job
    (run,) = segmenting.runs()
    assert (run["prefix"], run["executable"]) == (sys.prefix, sys.executable)
    assert segmenting.prefixes() == [sys.prefix] * 2
    assert "--verbose" not in run["arguments"]
    assert {name: run["environment"].get(name) for name in ("LD_LIBRARY_PATH", "XDG_CACHE_HOME", "SCAN3D_DEVICE")} == {
        "LD_LIBRARY_PATH": "/opt/this-stack/lib",
        "XDG_CACHE_HOME": "/opt/this-stack/cache",
        "SCAN3D_DEVICE": "gpu:0",
    }
    assert "SCAN3D_INFERENCE_PATH" not in run["environment"]


def test_each_path_reuses_only_its_own_saved_result(segmenting):
    measured = {
        "gpu_name": GPU_NAME,
        "device": "gpu:0",
        "torch_version": TORCH,
        "hip_version": None,
        "cuda_version": "13.0.fake",
        "results_moved_to_cpu": False,
    }
    added = segmenting.run("nvidia")
    first = segmenting.revision(added)
    assert first["operation"]["runtime"] == {"inference_path": "nvidia", "reused": False, **measured}
    # Without both fingerprints agreeing, this run would miss and rerun, forever and silently.
    again = segmenting.revision(segmenting.run("nvidia"))
    assert len(segmenting.runs()) == 1 and again["operation"]["runtime"] == {"inference_path": "nvidia", "reused": True, **measured}
    assert again["inference_manifest"] == first["inference_manifest"]
    default = segmenting.revision(segmenting.run("default"))
    assert [run["prefix"] for run in segmenting.runs()] == [venv_of(segmenting.stub), sys.prefix]
    assert default["operation"]["runtime"] == {"inference_path": "default", "reused": False, **measured}
    assert default["inference_manifest"]["run_id"] != first["inference_manifest"]["run_id"]
    absent = segmenting.revision(segmenting.run())  # The same key as naming default.
    assert len(segmenting.runs()) == 2 and absent["inference_manifest"] == default["inference_manifest"]
    assert "runtime" not in absent["operation"]
    assert segmenting.revision(segmenting.run("nvidia"))["inference_manifest"] == first["inference_manifest"]
    assert len(segmenting.runs()) == 2


@pytest.mark.parametrize("segmenting", [{"name": "amd", "kind": "rocm"}], indirect=True)
def test_a_rocm_build_records_its_hip_version_and_no_cuda_version(segmenting, monkeypatch):
    # The other jobs here measure CUDA, yet naming default on a ROCm workstation gives this.
    sample = segmentation._Resources
    monkeypatch.setattr(segmentation, "_Resources", lambda track_amd_vram: sample())  # A HIP build would read this machine's real card.
    measured = {
        "gpu_name": GPU_NAME,
        "device": "gpu:0",
        "torch_version": TORCH,
        "hip_version": "7.2.fake",
        "cuda_version": None,
        "results_moved_to_cpu": False,
    }
    for name in ("amd", "default"):
        job = segmenting.run(name)
        assert job["status"] == "completed", job
        assert segmenting.revision(job)["operation"]["runtime"] == {"inference_path": name, "reused": False, **measured}


@pytest.mark.parametrize("damage", ["lose-a-mask", "change-the-digest"])
def test_a_saved_result_that_fails_its_checks_is_run_again_and_not_called_reused(segmenting, damage):
    first = segmenting.revision(segmenting.run("nvidia"))
    (saved,) = segmenting.service.store.case_dir(segmenting.case["id"]).rglob("complete.json")
    record = json.loads(saved.read_text())
    if damage == "lose-a-mask":
        Path(record["structures"][0]["mask_file"]).unlink()
    else:
        saved.write_text(json.dumps({**record, "model_fingerprint": {**record["model_fingerprint"], "digest": "another"}}))
    job = segmenting.run("nvidia")
    assert job["status"] == "completed", job
    again = segmenting.revision(job)
    assert len(segmenting.runs()) == 2 and again["operation"]["runtime"]["reused"] is False
    assert again["inference_manifest"]["run_id"] != first["inference_manifest"]["run_id"]


def test_a_replaced_card_never_reuses_the_old_cards_result(segmenting, monkeypatch):
    first = segmenting.revision(segmenting.run("nvidia"))
    segmenting.stub.change(lambda record: record["path"].update(gpu_name="Test Replacement GPU"))
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", json.dumps([["Test Replacement GPU", MEMORY_BYTES, 0]]))
    second = segmenting.revision(segmenting.run("nvidia"))
    assert len(segmenting.runs()) == 2
    assert second["inference_manifest"]["runtime"]["gpu_name"] == "Test Replacement GPU" != first["inference_manifest"]["runtime"]["gpu_name"]


def test_an_upgraded_stack_on_the_same_card_does_not_reuse_the_old_result(segmenting):
    # Its identity has not changed, so only the versions it reports can tell these runs apart.
    assert segmenting.run("nvidia")["status"] == "completed"
    stub, before = segmenting.stub, binding("nvidia")
    shutil.rmtree(stub.site_packages / f"cuda_toolkit-{FAMILIES['cuda']['cuda-toolkit']}.dist-info")
    add_package(stub.site_packages, "cuda-toolkit", "13.2.1")
    stub.change(lambda record: record["checks"]["runtime_versions"].update({"cuda-toolkit": "13.2.1"}))
    assert binding("nvidia") == before
    assert segmenting.run("nvidia")["status"] == "completed"
    assert len(segmenting.runs()) == 2


def test_a_saved_result_is_not_reachable_from_a_path_that_went_bad(segmenting):
    assert segmenting.run("nvidia")["status"] == "completed"
    queued = segmenting.queue("nvidia")
    Path(segmenting.stub.python).unlink()
    job = segmenting.finish(queued)
    assert (job["status"], job["error"]) == ("failed", f"{GPU_NAME} is no longer available: {NO_PYTHON} Nothing ran and no other GPU was tried.")
    assert len(segmenting.runs()) == 1


def test_the_saved_run_stays_byte_for_byte_what_include_all_compares(segmenting):
    job = segmenting.run("nvidia", log=OVERFLOW)
    revision = segmenting.revision(job)
    (run,) = saved_runs(segmenting)
    assert json.loads((run / "inference.json").read_text()) == revision["inference_manifest"]
    included = segmenting.service.include_all_outputs(segmenting.case["id"])
    assert segmenting.finish(included)["status"] == "completed"


def test_a_failed_check_and_a_wrong_report_leave_nothing_to_reuse(segmenting, monkeypatch):
    def nothing_saved():
        return not list(segmenting.service.store.case_dir(segmenting.case["id"]).rglob("complete.json"))

    good = json.dumps([[GPU_NAME, MEMORY_BYTES, 0]])
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", json.dumps([["Test Other GPU", MEMORY_BYTES, 0]]))
    wrong_card = segmenting.run("nvidia")
    assert wrong_card["status"] == "failed", wrong_card
    assert wrong_card["error"] == (f"Expected {GPU_NAME}, but the GPU check found Test Other GPU (gpu:0, CUDA). No result was kept and no other GPU was tried.")
    assert segmenting.runs() == [] and saved_runs(segmenting) == [] and nothing_saved()
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", good)
    wrong_torch = segmenting.run("nvidia", report={"torch_version": "9.9.9"})
    assert wrong_torch["status"] == "failed" and f"ran on PyTorch 9.9.9, not the {TORCH} that {GPU_NAME} was checked on" in wrong_torch["error"], wrong_torch
    assert len(segmenting.runs()) == 1 and nothing_saved()
    fell_back = segmenting.run("nvidia", report={"device": "cpu"})
    assert (
        fell_back["status"] == "failed" and fell_back["error"] == "TotalSegmentator did not complete on the requested GPU; CPU fallback results were rejected"
    )
    assert len(segmenting.runs()) == 2 and nothing_saved()
    assert segmenting.run("nvidia")["status"] == "completed" and len(segmenting.runs()) == 3


@pytest.mark.parametrize(
    "plan, cause",
    [
        ({"log": "torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB", "exit": 1}, "it ran out of GPU memory"),
        ({"kill": True}, "the process was killed, usually because system memory ran out"),
    ],
    ids=["out-of-memory", "killed"],
)
def test_a_failed_run_names_its_card_and_what_stopped_it(segmenting, plan, cause):
    job = segmenting.run("nvidia", **plan)
    assert (job["status"], job["stage"]) == ("failed", "Failed")
    assert job["error"] == f"TotalSegmentator total failed on {GPU_NAME}: {cause}. No other GPU was tried."
    assert job["inference_path"]["label"] == GPU_NAME and job["revision_id"] is None


def test_a_run_that_moved_results_says_so_and_a_reuse_keeps_saying_so(segmenting):
    first = segmenting.revision(segmenting.run("nvidia", log=OVERFLOW))
    assert first["operation"]["runtime"]["results_moved_to_cpu"] is True and OVERFLOW_QC in first["qc"]
    assert first["inference_manifest"]["tasks"][0]["results_moved_to_cpu"] is True
    again = segmenting.revision(segmenting.run("nvidia"))
    assert again["operation"]["runtime"]["reused"] is True and again["operation"]["runtime"]["results_moved_to_cpu"] is True and OVERFLOW_QC in again["qc"]


def gone(tmp_path):
    return dataclasses.replace(require(binding("nvidia")), python=str(tmp_path / "PRIVATE-gone" / "bin" / "python"))


def test_no_public_surface_names_the_interpreter(segmenting, tmp_path, monkeypatch):
    http = segmenting.http
    route = f"/api/cases/{segmenting.case['id']}"
    with monkeypatch.context() as patch:
        patch.setattr(inference_paths, "require", lambda bound: gone(tmp_path))
        failed = segmenting.run("nvidia")  # First, because a saved result would start no child.
    assert failed["status"] == "failed"
    completed = segmenting.run("nvidia", log=OVERFLOW)
    revision = segmenting.revision(completed)
    public = http.get(f"{route}/revisions/{completed['revision_id']}")
    assert public.json()["operation"]["runtime"]["gpu_name"] == GPU_NAME
    surfaces = [
        public.text,
        json.dumps(public_job(completed)),
        json.dumps(public_revision(revision)),
        json.dumps(revision["inference_manifest"]),
        http.get(f"/api/jobs/{failed['id']}").text,
        http.get(f"/api/jobs/{completed['id']}").text,
        http.get(f"{route}/jobs").text,
        http.get("/api/segmentation-options").text,
        http.get("/api/interactive/status").text,
    ]
    for surface in surfaces:
        assert "PRIVATE" not in surface and str(tmp_path) not in surface


def test_no_public_surface_of_a_correction_names_the_interpreter(segmenting, tmp_path, monkeypatch):
    service, case, http = segmenting.service, segmenting.case, segmenting.http
    tests = str(Path(__file__).resolve().parent)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((tests, os.environ["PYTHONPATH"])))
    service.interactive_runtime = runtime = InteractiveWorker("fake_interactive_engine:ReportingEngine", stall_seconds=30.0)
    base = service.store.revision(case["id"], case["active_revision_id"])
    draft = service.reviews.create(case["id"], base["id"], "synthetic:liver")
    mark = dict(id="point-1", kind="point", ras=[0.0, 0.0, 0.0], include=True)
    draft = service.reviews.update(case["id"], draft["id"], 0, [mark])
    route = f"/api/cases/{case['id']}"

    def predict():
        queued = service.reviews.predict(case["id"], draft["id"], service.reviews.get(case["id"], draft["id"])["generation"], inference_path="nvidia")
        service.execute(service.store.claim())
        return service.store.get("jobs", queued["id"])

    try:
        with monkeypatch.context() as patch:
            patch.setattr(inference_paths, "require", lambda bound: gone(tmp_path))
            failed = predict()
        assert (failed["status"], failed["error"]) == ("failed", "[Errno 2] No such file or directory: <local path>")
        assert service.reviews.get(case["id"], draft["id"])["error"] == failed["error"]
        completed = predict()
        assert completed["status"] == "completed", completed
        ready = service.reviews.get(case["id"], draft["id"])
        saved = service.reviews.accept(case["id"], draft["id"], ready["generation"])
        service.execute(service.store.claim())
    finally:
        runtime.release()
    accepted = service.store.get("jobs", saved["id"])
    assert accepted["status"] == "completed", accepted
    public = http.get(f"{route}/revisions/{accepted['revision_id']}")
    assert public.json()["operation"]["runtime"]["gpu_name"] == GPU_NAME
    surfaces = [
        public.text,
        json.dumps(public_review(ready)),
        http.get(f"{route}/reviews/{draft['id']}").text,
        http.get(f"{route}/reviews").text,
        http.get(f"{route}/revisions/{ready['candidate']['revision_id']}").text,
        http.get(f"/api/jobs/{failed['id']}").text,
        http.get(f"/api/jobs/{completed['id']}").text,
        http.get(f"{route}/jobs").text,
    ]
    for surface in surfaces:
        assert "PRIVATE" not in surface and str(tmp_path) not in surface


def correct_with(segmenting, monkeypatch, engine, name):
    """One correction in a real child running `engine`: the job and the review it leaves."""
    service, case = segmenting.service, segmenting.case
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join((str(Path(__file__).resolve().parent), os.environ["PYTHONPATH"])))
    service.interactive_runtime = runtime = InteractiveWorker(f"fake_interactive_engine:{engine}", stall_seconds=30.0)
    base = service.store.revision(case["id"], case["active_revision_id"])
    draft = service.reviews.create(case["id"], base["id"], "synthetic:liver")
    draft = service.reviews.update(case["id"], draft["id"], 0, [dict(id="point-1", kind="point", ras=[0.0, 0.0, 0.0], include=True)])
    try:
        queued = service.reviews.predict(case["id"], draft["id"], draft["generation"], inference_path=name)
        service.execute(service.store.claim())
    finally:
        runtime.release()
    return service.store.get("jobs", queued["id"]), service.reviews.get(case["id"], draft["id"])


@pytest.mark.parametrize("name", ["nvidia", "default"])
def test_an_out_of_memory_correction_keeps_its_address_from_child_to_review(segmenting, monkeypatch, name):
    # The expected failure on a smaller card, and the address is the help it offers.
    job, review = correct_with(segmenting, monkeypatch, "OutOfMemoryEngine", name)
    assert (job["status"], job["error"]) == ("failed", OUT_OF_MEMORY)
    assert review["error"] == OUT_OF_MEMORY


@pytest.mark.parametrize("name, sentence", [("nvidia", NO_GPU_ADDED), (None, NO_GPU_DEFAULT)])
def test_a_segmentation_with_no_gpu_gives_the_remedy_that_works_on_its_path(segmenting, monkeypatch, name, sentence):
    # SCAN3D_DEVICE=cpu would do nothing for an added path and would move the default one.
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", "[]")
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu")  # auto would settle for the CPU.
    job = segmenting.run(name)
    assert (job["status"], job["error"]) == ("failed", sentence)
    assert segmenting.runs() == [] and len(segmenting.prefixes()) == 1  # The device check, and nothing after it.


@pytest.mark.parametrize("name, sentence", [("nvidia", NO_GPU_ADDED), ("default", NO_GPU_DEFAULT)])
def test_a_correction_with_no_gpu_gives_the_remedy_that_works_on_its_path(segmenting, monkeypatch, name, sentence):
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", "[]")
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu")
    job, review = correct_with(segmenting, monkeypatch, "ReportingEngine", name)
    assert (job["status"], job["error"]) == ("failed", sentence)
    assert review["error"] == sentence


def test_a_job_on_a_path_leaves_the_api_process_free_of_torch(tmp_path, monkeypatch):
    # As test_segmentation_job_never_imports_torch_in_the_api_process, with no child replaced.
    script = """
import builtins, json, os, subprocess, sys
from pathlib import Path
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.partition(".")[0] == "torch":
        raise AssertionError("The API process imported " + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
os.environ["PYTHONPATH"] = sys.argv[2]  # For its children: this process's own path is settled.
from scan3d import segmentation
from scan3d.config import Settings
from scan3d.demo import create_demo
from scan3d.service import Service
commands = []
run = subprocess.Popen
def popen(command, **kwargs):
    commands.append(command)
    return run(command, **kwargs)
subprocess.Popen = popen
real = segmentation.model_fingerprint
segmentation.model_fingerprint = lambda *args, **kwargs: {**real(*args, **kwargs), "weights_complete": True}
service = Service(Settings(Path(sys.argv[1]) / "data", (Path(sys.argv[1]),)))
case = create_demo(service)
job = service.segment_case(case["id"], case["active_revision_id"], task="total", inference_path="nvidia")
service.execute(service.store.claim())
job = service.store.get("jobs", job["id"])
assert job["status"] == "completed", job
assert "torch" not in sys.modules
print(json.dumps(commands))
"""
    parent_path = os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH"))))
    stub = stand_in(tmp_path)
    stub.shadow("totalsegmentator.bin.TotalSegmentator", TOTALSEGMENTATOR)
    activate(monkeypatch, stub)
    monkeypatch.setenv("SCAN3D_FAKE_TOTALSEG", "{}")
    monkeypatch.setenv("SCAN3D_FAKE_TOTALSEG_RUNS", str(tmp_path / "runs"))
    monkeypatch.setenv("SCAN3D_FAKE_TORCH_PREFIXES", str(tmp_path / "prefixes"))
    environment = {**os.environ, "PYTHONPATH": parent_path}
    completed = subprocess.run([sys.executable, "-c", script, str(tmp_path), child_path(stub)], env=environment, capture_output=True, text=True, timeout=120)
    assert completed.returncode == 0, completed.stderr
    check, run = json.loads(completed.stdout.splitlines()[-1])
    assert check == [stub.python, "-P", "-m", "scan3d.segmentation", "gpu"]
    assert run[:4] == [stub.python, "-u", "-m", "totalsegmentator.bin.TotalSegmentator"] and "--verbose" in run
    assert Path(run[0]).is_symlink()  # As the venv reported it: its target would run outside the venv.
    assert (tmp_path / "prefixes").read_text().split() == [venv_of(stub)] * 2


def test_the_command_line_can_name_a_path_and_is_refused_like_the_api(tmp_path, monkeypatch):
    (tmp_path / "no-settings.env").touch()
    monkeypatch.setenv("SCAN3D_CONFIG", str(tmp_path / "no-settings.env"))
    settings = Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist")
    monkeypatch.setenv("SCAN3D_DATA_DIR", str(settings.data_dir))
    app = create_app(settings)
    case = create_demo(app.state.service)
    command = [sys.executable, "-m", "scan3d", "segment", case["id"], "--task", "total", "--inference-path", "absent"]
    environment = {**os.environ, "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH"))))}
    completed = subprocess.run(command, env=environment, capture_output=True, text=True, timeout=60)
    assert completed.returncode != 0 and UNSET_UP in completed.stderr
    assert app.state.service.store.list("jobs") == []


# Proofs, from scripts/check_gpu.py on the fake PyTorch


@pytest.fixture
def check(tmp_path, monkeypatch):
    """Run the real script in a stand-in with no proof yet, on a scratch library.

    Both sides read real sysfs here: the child has no patched root, and a census is only ever
    compared with itself, so this holds on any machine.
    """
    built = stand_in(tmp_path)
    built.proof.unlink()
    activate(monkeypatch, built)
    monkeypatch.setattr(inference_paths, "SYS_DRM", Path("/sys/class/drm"))
    (tmp_path / "no-settings.env").touch()
    monkeypatch.setenv("SCAN3D_CONFIG", str(tmp_path / "no-settings.env"))
    monkeypatch.setenv("SCAN3D_DATA_DIR", str(tmp_path / "data"))

    def run(*arguments, python=built.python):
        return subprocess.run([python, str(CHECK_GPU), *arguments], cwd=tmp_path, capture_output=True, text=True, timeout=120)

    run.stub = built
    return run


def test_a_path_check_writes_only_its_own_proof_and_lists_the_path(check, tmp_path):
    stub = check.stub
    default = tmp_path / "gpu-proof.json"
    default.write_text("the default path's proof")
    completed = check("--path", "nvidia")
    assert completed.returncode == 0, completed.stderr
    assert default.read_text() == "the default path's proof"
    record = stub.record()
    assert record["gpu_ready"] is True and record["errors"] == []
    assert record["path"] == {
        "id": "nvidia",
        "python": stub.python,
        "site_packages": str(stub.site_packages),
        "device": "gpu",
        "kind": "cuda",
        "gpu_name": GPU_NAME,
        "memory_bytes": MEMORY_BYTES,
    }
    checks = record["checks"]
    # Only the stand-in's family, even with the parent's packages importable in this child.
    assert checks["runtime_versions"] == {"torch": TORCH, **FAMILIES["cuda"]}
    assert checks["torch_version"] == checks["torch_distribution_version"] == TORCH and checks["recommended_device"] == "gpu:0"
    assert checks["hardware"] == gpu_cards("cuda")
    added = rows()[1]
    assert (added["id"], added["label"], added["memory_gib"], added["available"], added["reason"]) == ("nvidia", GPU_NAME, 16, True, None)
    assert [file.name for file in stub.proof.parent.iterdir()] == ["nvidia.json"]


def test_a_checked_path_passes_its_own_recheck_with_the_venvs_interpreter(check):
    assert check("--path", "nvidia").returncode == 0
    path = require(binding("nvidia"))
    assert path.python == check.stub.python and dict(path.packages) == {"torch": TORCH, **FAMILIES["cuda"]}


def test_a_rocm_runtime_records_its_own_family(check, tmp_path, monkeypatch):
    built = stand_in(tmp_path, "amd", proofs=check.stub.proof.parent, kind="rocm")
    built.proof.unlink()
    activate(monkeypatch, built)
    assert check("--path", "amd", python=built.python).returncode == 0
    record = built.record()
    assert record["path"]["kind"] == "rocm" and record["checks"]["runtime_versions"] == {"torch": TORCH, **FAMILIES["rocm"]}


def test_a_failed_check_still_lists_the_path_with_a_fixed_reason(check, monkeypatch):
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", "[]")
    completed = check("--path", "nvidia")
    assert completed.returncode == 1
    record = check.stub.record()
    assert record["gpu_ready"] is False and "cannot access a GPU" in record["errors"][0]
    assert record["path"]["id"] == "nvidia" and record["path"]["gpu_name"] is None
    assert (rows()[1]["label"], rows()[1]["reason"], rows()[1]["available"]) == ("nvidia", CHECK_FAILED, False)
    assert "cannot access" not in json.dumps(rows())


def test_a_runtime_with_no_torch_still_leaves_a_failing_proof(check, monkeypatch):
    monkeypatch.setenv("SCAN3D_FAKE_TORCH", "missing")
    assert check("--path", "nvidia").returncode == 1
    assert check.stub.record()["checks"] == {} and rows()[1]["reason"] == CHECK_FAILED


@pytest.mark.parametrize("module", ["nnInteractive.inference.inference_session", "requests"])
def test_a_broken_or_trimmed_runtime_fails_its_check_instead_of_its_first_job(check, module):
    # requests stands for every package a child imports at module level through scan3d.
    check.stub.shadow(module, "raise ImportError('this runtime is incomplete')")
    assert check("--path", "nvidia").returncode == 1
    record = check.stub.record()
    assert record["errors"] == ["this runtime is incomplete"] and rows()[1]["reason"] == CHECK_FAILED


def test_a_runtime_needs_no_scan3d_install(check, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(part for part in os.environ["PYTHONPATH"].split(os.pathsep) if Path(part) != ROOT))
    completed = check("--path", "nvidia")
    assert completed.returncode == 0, completed.stderr


def test_a_second_card_on_the_same_interpreter_is_its_own_path(check, monkeypatch):
    gpus = [["Test First GPU", 8 * GIB, 0], ["Test Second GPU", 24 * GIB, 0]]
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", json.dumps(gpus))
    assert check("--path", "second", "--device", "gpu:0").returncode == 0
    second = json.loads((check.stub.proof.parent / "second.json").read_text())["path"]
    assert (second["device"], second["gpu_name"], second["memory_bytes"]) == ("gpu:0", "Test First GPU", 8 * GIB)
    assert check("--path", "nvidia").returncode == 0  # gpu picks the most memory.
    assert (check.stub.record()["path"]["device"], check.stub.record()["path"]["gpu_name"]) == ("gpu", "Test Second GPU")


def test_a_path_check_ignores_the_default_paths_device_setting(check, monkeypatch):
    # gpu:N counts within one build, so the default path's index means nothing here.
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu:7")
    assert check("--path", "nvidia").returncode == 0
    assert check.stub.record()["path"]["device"] == "gpu"


def test_an_unavailable_index_fails_the_check(check):
    assert check("--path", "nvidia", "--device", "gpu:1").returncode == 1
    assert check.stub.record()["errors"] == ["Requested GPU index is unavailable"]


@pytest.mark.parametrize(
    "arguments",
    [
        ("--path", "default"),
        ("--path", "../x"),
        ("--path", "x" * 33),
        ("--device", "cpu"),
        ("--path", "nvidia", "--output", "elsewhere.json"),  # A path's proof cannot be sent anywhere else.
        ("--queue", "--path", "nvidia"),
        ("--queue", "--output", "elsewhere.json"),
    ],
)
def test_a_bad_request_is_refused_before_anything_is_written(check, tmp_path, arguments):
    assert check(*arguments).returncode == 2
    assert not (tmp_path / "gpu-proof.json").exists() and not (tmp_path / "elsewhere.json").exists()
    assert list(check.stub.proof.parent.iterdir()) == []


# An empty LD_PRELOAD, because the loader would complain about a file that is not there.
FROM_ANOTHER_STACK = {name: "" if name == "LD_PRELOAD" else "1" for name in inference_paths._WITHHELD}


def what_pytorch_saw(tmp_path):
    return json.loads((tmp_path / "environment-seen.json").read_text())


@pytest.mark.parametrize("source", ["shell", "settings-file"])
def test_a_proof_measures_the_view_a_job_gets(check, tmp_path, monkeypatch, source):
    monkeypatch.setenv("SCAN3D_FAKE_TORCH_ENVIRONMENT", str(tmp_path / "environment-seen.json"))
    monkeypatch.setenv("KEPT_FOR_THE_CHILDREN", "1")
    if source == "shell":
        for name, value in FROM_ANOTHER_STACK.items():
            monkeypatch.setenv(name, value)
    else:
        (tmp_path / "settings.env").write_text("".join(f"{name}={value}\n" for name, value in FROM_ANOTHER_STACK.items()))
        monkeypatch.setenv("SCAN3D_CONFIG", str(tmp_path / "settings.env"))
    completed = check("--path", "nvidia")
    assert completed.returncode == 0, completed.stderr
    seen = what_pytorch_saw(tmp_path)
    assert not FROM_ANOTHER_STACK.keys() & seen.keys() and seen["KEPT_FOR_THE_CHILDREN"] == "1"
    assert not FROM_ANOTHER_STACK.keys() & require(binding("nvidia")).environment().keys()


def test_a_default_check_keeps_the_environment_its_workers_keep(check, tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_FAKE_TORCH_ENVIRONMENT", str(tmp_path / "environment-seen.json"))
    for name, value in FROM_ANOTHER_STACK.items():
        monkeypatch.setenv(name, value)
    assert check("--output", str(tmp_path / "proof.json")).returncode == 0
    assert FROM_ANOTHER_STACK.items() <= what_pytorch_saw(tmp_path).items()


def test_a_default_check_writes_to_the_setting_and_adds_a_census_but_no_path(check, tmp_path):
    assert check().returncode == 0
    record = json.loads((tmp_path / "gpu-proof.json").read_text())
    assert record["gpu_ready"] is True and "path" not in record and record["checks"]["hardware"] == gpu_cards("cuda")
    assert not check.stub.proof.exists()


def test_a_default_check_does_not_need_the_engines(check, tmp_path):
    # The [ai] engines are optional on a default install, and this check certifies a convolution.
    check.stub.shadow("totalsegmentator.python_api", "raise ImportError('this install has no engines')")
    completed = check()
    assert completed.returncode == 0, completed.stderr
    assert json.loads((tmp_path / "gpu-proof.json").read_text())["gpu_ready"] is True


def test_output_outranks_the_setting(check, tmp_path):
    assert check("--output", str(tmp_path / "chosen" / "proof.json")).returncode == 0
    assert (tmp_path / "chosen" / "proof.json").is_file() and not (tmp_path / "gpu-proof.json").exists()


def test_a_default_check_proves_the_configured_card(check, tmp_path, monkeypatch):
    monkeypatch.setenv("SCAN3D_FAKE_GPUS", json.dumps([["Test Large GPU", 24 * GIB, 0], ["Test Small GPU", 8 * GIB, 0]]))
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu:1")
    assert check().returncode == 0
    assert json.loads((tmp_path / "gpu-proof.json").read_text())["checks"]["recommended_device"] == "gpu:1"


@pytest.mark.parametrize("setting", ["auto", "cpu"])
def test_a_gpu_check_needs_a_gpu_whatever_the_device_setting(check, tmp_path, monkeypatch, setting):
    monkeypatch.setenv("SCAN3D_DEVICE", setting)
    assert check().returncode == 0
    assert json.loads((tmp_path / "gpu-proof.json").read_text())["checks"]["recommended_device"] == "gpu:0"


def test_settings_come_from_scan3d_env_with_no_launcher(check, tmp_path, monkeypatch):
    folder, proof = tmp_path / "from-env", tmp_path / "from-env.json"
    (tmp_path / "settings.env").write_text(f"SCAN3D_INFERENCE_PATH_DIR={folder}\nSCAN3D_GPU_PROOF={proof}\nSCAN3D_DEVICE=gpu:0\n")
    monkeypatch.setenv("SCAN3D_CONFIG", str(tmp_path / "settings.env"))
    monkeypatch.delenv("SCAN3D_INFERENCE_PATH_DIR")
    monkeypatch.delenv("SCAN3D_GPU_PROOF")
    assert check("--path", "nvidia").returncode == 0
    assert (folder / "nvidia.json").is_file()
    assert check().returncode == 0
    assert proof.is_file()


def library(tmp_path, *statuses):
    store = Store(tmp_path / "data")
    with store.connection() as connection:
        for number, status in enumerate(statuses):
            connection.execute("INSERT INTO jobs(id, case_id, status, data) VALUES (?, ?, ?, ?)", (f"{number:032x}", "0" * 32, status, "{}"))
    return store


def test_a_check_refuses_to_run_beside_work_and_keeps_the_last_proof(check, tmp_path):
    library(tmp_path, "completed", "running")
    (tmp_path / "gpu-proof.json").write_text("the last proof")
    for arguments in (("--path", "nvidia"), ()):
        completed = check(*arguments)
        assert completed.returncode == 1 and "queued or running" in completed.stderr
    assert (tmp_path / "gpu-proof.json").read_text() == "the last proof"
    assert not check.stub.proof.exists()


def test_the_queue_is_only_ever_opened_read_only(tmp_path, monkeypatch):
    store = library(tmp_path, "queued")
    monkeypatch.setenv("SCAN3D_DATA_DIR", str(store.directory))
    opened, connect = [], sqlite3.connect
    monkeypatch.setattr(sqlite3, "connect", lambda database, **options: opened.append(database) or connect(database, **options))
    assert jobs_waiting() is True
    assert opened == [f"{store.db.as_uri()}?mode=ro"]


def test_a_library_with_no_database_has_no_jobs_to_wait_for(check, tmp_path):
    assert not (tmp_path / "data").exists()
    assert check("--path", "nvidia").returncode == 0
    assert not (tmp_path / "data").exists()


BUSY = "Jobs are queued or running, so no GPU check ran. Run it again when the queue is empty."


def ask_the_queue(stub):
    """As the install script asks: a Python with no packages, and no way to write into this checkout."""
    environment = {name: value for name, value in os.environ.items() if name != "PYTHONPATH"}
    return subprocess.run([stub.python, "-B", "-P", str(CHECK_GPU), "--queue"], env=environment, capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize(
    "statuses, code, said",
    [
        (("queued",), 1, BUSY),
        (("completed", "running"), 1, BUSY),
        (("completed", "failed", "cancelled"), 0, ""),
    ],
    ids=["queued", "done-and-running", "all-done"],
)
def test_the_queue_answer_comes_from_the_library_and_writes_nothing(check, tmp_path, statuses, code, said):
    store = library(tmp_path, *statuses)
    before = store.db.read_bytes()
    completed = ask_the_queue(check.stub)
    assert (completed.returncode, completed.stdout, completed.stderr.strip()) == (code, "", said)
    assert store.db.read_bytes() == before
    assert not (tmp_path / "gpu-proof.json").exists() and list(check.stub.proof.parent.iterdir()) == []


def test_settings_that_cannot_be_read_are_not_an_empty_queue(check, monkeypatch):
    monkeypatch.setenv("SCAN3D_MODALITIES", "XRAY")
    completed = ask_the_queue(check.stub)
    assert completed.returncode == 1
    assert completed.stderr.strip() == "Could not read the job queue, so no GPU check ran: SCAN3D_MODALITIES must list one or more of: CT, MR"
    full = check("--path", "nvidia")
    assert full.returncode == 1 and "Could not read the job queue" in full.stderr and "Traceback" not in full.stderr
    assert not check.stub.proof.exists()


@pytest.mark.parametrize(
    "build",
    [
        lambda data: (data.mkdir(), (data / "scan3d.sqlite3").write_text("this is not a database, and it is long enough to be read as one")),
        lambda data: (data / "scan3d.sqlite3").mkdir(parents=True),
        lambda data: data.touch(),
    ],
    ids=["not-a-database", "a-directory-for-the-database", "a-file-for-the-library"],
)
def test_a_database_that_is_there_and_cannot_be_opened_is_not_an_empty_queue(check, tmp_path, build):
    build(tmp_path / "data")
    completed = ask_the_queue(check.stub)
    assert completed.returncode == 1 and completed.stderr.startswith("Could not read the job queue, so no GPU check ran: ")
    full = check("--path", "nvidia")
    assert full.returncode == 1 and "Could not read the job queue" in full.stderr
    assert not check.stub.proof.exists()


@pytest.mark.parametrize("relative", [False, True], ids=["a-link-for-the-library", "a-link-in-a-relative-setting"])
def test_a_library_behind_a_link_whose_target_has_gone_is_not_an_empty_queue(check, tmp_path, monkeypatch, relative):
    # Resolved, this reads as a missing directory, which is what a fresh install has.
    if relative:
        (tmp_path / "mount").symlink_to(tmp_path / "unmounted")
        monkeypatch.setenv("SCAN3D_DATA_DIR", "mount/library")
        monkeypatch.chdir(tmp_path)
    else:
        (tmp_path / "data").symlink_to(tmp_path / "unmounted" / "library")
    completed = ask_the_queue(check.stub)
    assert completed.returncode == 1 and completed.stderr.startswith("Could not read the job queue, so no GPU check ran: ")
    assert "a link whose target has gone" in completed.stderr
    assert not (tmp_path / "unmounted").exists()
    assert check("--path", "nvidia").returncode == 1 and not check.stub.proof.exists()


def test_a_working_link_to_a_library_is_read_through(check, tmp_path, monkeypatch):
    library(tmp_path, "running")
    (tmp_path / "link").symlink_to(tmp_path / "data")
    monkeypatch.setenv("SCAN3D_DATA_DIR", str(tmp_path / "link"))
    assert ask_the_queue(check.stub).stderr.strip() == BUSY
    (tmp_path / "empty").mkdir()
    (tmp_path / "link-to-empty").symlink_to(tmp_path / "empty")
    monkeypatch.setenv("SCAN3D_DATA_DIR", str(tmp_path / "link-to-empty"))
    assert ask_the_queue(check.stub).returncode == 0


@pytest.mark.skipif(os.geteuid() == 0, reason="root can enter any directory")
def test_a_library_this_user_cannot_enter_is_not_an_empty_queue(check, tmp_path):
    # One with a running job, which `Path.is_file()` would call no database at all.
    store = library(tmp_path, "running")
    store.directory.chmod(0)
    try:
        completed = ask_the_queue(check.stub)
    finally:
        store.directory.chmod(0o700)
    assert completed.returncode == 1 and completed.stderr.startswith("Could not read the job queue, so no GPU check ran: ")


def test_the_queue_question_loads_no_gpu_runtime(check, tmp_path, monkeypatch):
    # Importing this PyTorch records the import and then kills the process.
    monkeypatch.setenv("SCAN3D_FAKE_TORCH", "crash")
    monkeypatch.setenv("SCAN3D_FAKE_TORCH_PREFIXES", str(tmp_path / "prefixes"))
    assert check("--queue").returncode == 0
    assert not (tmp_path / "prefixes").exists()
