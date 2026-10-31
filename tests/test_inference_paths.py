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
import subprocess

from fastapi.testclient import TestClient
import pytest

from inference_path_stub import FAMILIES, GPU_NAME, MEMORY_BYTES, TORCH, activate, add_package, stand_in
from scan3d import doctor, inference_paths
from scan3d.api import create_app
from scan3d.config import ROOT, Settings
from scan3d.inference_paths import InferencePath, binding, gpu_cards, identity, public_error, require, rows

UNSET_UP = "This GPU is not set up on this workstation."
UNREADABLE = "Its GPU check record cannot be read."
CHECK_FAILED = "Its last GPU check failed."
NO_PYTHON = "Its Python environment is missing."
PACKAGES_CHANGED = "Its installed packages changed after the last GPU check."
ENGINE_DIFFERS = "Its TotalSegmentator or nnInteractive version differs from this workstation's."
RESAMPLER = "It has cupy or cucim installed, which would change resampling."
HARDWARE_CHANGED = "The GPUs in this workstation changed after the last GPU check."
CAUTION = "Its last GPU check failed or is out of date."
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


def test_the_default_row_names_the_recommended_card_from_its_proof(tmp_path, fresh):
    default_proof(tmp_path)
    assert rows() == [{"id": "default", "label": "Test Default GPU", "memory_gib": 32, "shares_display": False, "available": True, "reason": None}]


def test_a_stale_device_list_does_not_name_the_default_card(tmp_path, fresh):
    # The integrated GPU this workstation lost is still in the live proof's device list.
    default_proof(tmp_path, recommended_device="gpu:1")
    assert rows()[0]["label"] == "Test Second GPU"


def test_an_explicit_device_request_outranks_the_proofs_recommendation(tmp_path, fresh, monkeypatch):
    default_proof(tmp_path)
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu:1")
    assert (rows()[0]["label"], rows()[0]["memory_gib"]) == ("Test Second GPU", 8)


def test_the_cpu_setting_names_no_card_even_with_a_proof(tmp_path, fresh, monkeypatch):
    monkeypatch.setenv("SCAN3D_DEVICE", "cpu")
    assert rows()[0]["label"] == "CPU"
    default_proof(tmp_path)
    assert rows() == [{"id": "default", "label": "CPU", "memory_gib": None, "shares_display": False, "available": True, "reason": None}]


@pytest.mark.parametrize(
    "stale",
    [
        lambda record: record.update(python_version="3.0.0"),
        lambda record: record["checks"].update(runtime_versions={"torch": "0.9"}),
        lambda record: record["checks"].update(torch_distribution_version="0.9"),
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
        "[]",
        '{"checks": []}',
        '{"checks": {"devices": 7, "recommended_device": "gpu:0"}}',
        '{"checks": {"devices": [], "recommended_device": "gpu:0"}}',
        '{"checks": {"devices": ["x"], "recommended_device": "gpu:0"}}',
        '{"checks": {"devices": [{"name": 7}], "recommended_device": "gpu:0"}}',
        '{"checks": {"devices": [{"name": ""}], "recommended_device": "gpu:0"}}',
        '{"checks": {"devices": [{"name": "x"}], "recommended_device": 0}}',
        '{"checks": {"devices": [{"name": "x"}], "recommended_device": "cuda:0"}}',
    ],
)
def test_a_malformed_default_proof_names_no_card_and_blocks_nothing(tmp_path, malformed):
    (tmp_path / "gpu-proof.json").write_text(malformed)
    row = rows()[0]
    assert (row["available"], row["reason"], row["label"], row["memory_gib"]) == (True, CAUTION, "Default device", None)


def test_a_proven_path_gets_its_own_row(stub):
    assert rows()[1:] == [{"id": "nvidia", "label": GPU_NAME, "memory_gib": 16, "shares_display": False, "available": True, "reason": None}]


def corrupt(stub):
    stub.proof.write_text("{not json")


def not_a_record(stub):
    stub.proof.write_text("[]")


def no_block(stub):
    stub.change(lambda record: record.pop("path"))


def no_checks(stub):
    stub.change(lambda record: record.pop("checks"))


def another_id(stub):
    stub.change(lambda record: record["path"].update(id="renamed"))


def partial_block(stub):
    stub.change(lambda record: record["path"].pop("python"))


def unknown_kind(stub):
    stub.change(lambda record: record["path"].update(kind="tpu"))


def cpu_device(stub):
    stub.change(lambda record: record["path"].update(device="cpu"))


def no_packages(stub):
    stub.change(lambda record: record["checks"].pop("runtime_versions"))


def untyped_packages(stub):
    stub.change(lambda record: record["checks"].update(runtime_versions=["torch"]))


def empty_packages(stub):
    stub.change(lambda record: record["checks"].update(runtime_versions={}))


def null_byte_in_a_field(stub):
    stub.change(lambda record: record["path"].update(python="/venv/bin/python\x00"))


def untyped_memory(stub):
    stub.change(lambda record: record["path"].update(memory_bytes="16"))


def untyped_hardware(stub):
    stub.change(lambda record: record["checks"].update(hardware=7))


def failed_check(stub):
    stub.change(lambda record: record.update(gpu_ready=False, errors=["PRIVATE failure at /private/home/venv/bin/python"]))


def missing_interpreter(stub):
    Path(stub.python).unlink()


def changed_family(stub):
    shutil.rmtree(stub.site_packages / f"cuda_toolkit-{FAMILIES['cuda']['cuda-toolkit']}.dist-info")
    add_package(stub.site_packages, "cuda-toolkit", "13.2.1")


def removed_family(stub):
    shutil.rmtree(stub.site_packages / f"torch-{TORCH}.dist-info")


def proof_of_other_packages(stub):
    stub.change(lambda record: record["checks"]["runtime_versions"].update({"torch": "2.14.0"}))


def cupy_wheel(stub):
    add_package(stub.site_packages, "cupy-cuda13x", "13.6.0")


def cupy_import(stub):
    (stub.site_packages / "cupy").mkdir()


def cucim_wheel(stub):
    add_package(stub.site_packages, "cucim", "25.2.0")


def cucim_import(stub):
    (stub.site_packages / "cucim").mkdir()


@pytest.mark.parametrize(
    "damage, reason",
    [
        (corrupt, UNREADABLE),
        (not_a_record, UNREADABLE),
        (no_block, UNREADABLE),
        (no_checks, UNREADABLE),
        (another_id, UNREADABLE),
        (partial_block, UNREADABLE),
        (unknown_kind, UNREADABLE),
        (cpu_device, UNREADABLE),
        (no_packages, UNREADABLE),
        (untyped_packages, UNREADABLE),
        (empty_packages, UNREADABLE),
        (null_byte_in_a_field, UNREADABLE),
        (untyped_memory, UNREADABLE),
        (untyped_hardware, UNREADABLE),
        (failed_check, CHECK_FAILED),
        (missing_interpreter, NO_PYTHON),
        (changed_family, PACKAGES_CHANGED),
        (removed_family, PACKAGES_CHANGED),
        (proof_of_other_packages, PACKAGES_CHANGED),
        (cupy_wheel, RESAMPLER),
        (cupy_import, RESAMPLER),
        (cucim_wheel, RESAMPLER),
        (cucim_import, RESAMPLER),
    ],
)
def test_each_failed_check_disables_its_row_with_one_fixed_sentence(stub, tmp_path, damage, reason):
    damage(stub)
    default, added = rows()
    assert default["available"] and (added["id"], added["available"], added["reason"]) == ("nvidia", False, reason)
    public = json.dumps(added)
    assert str(tmp_path) not in public and "PRIVATE" not in public and "/private" not in public
    with pytest.raises(ValueError, match="is unavailable: "):
        binding("nvidia")


@pytest.mark.parametrize("engine", ["TotalSegmentator", "nnunetv2", "nnInteractive", "nninteractive-client"])
def test_an_engine_pin_that_differs_from_the_api_disables_the_path(stub, monkeypatch, engine):
    assert rows()[1]["available"]
    version = doctor.metadata.version

    def api(package):
        return "9.9.9" if package == engine else version(package)

    monkeypatch.setattr(doctor.metadata, "version", api)
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


def test_a_proof_with_no_card_is_named_by_its_id(stub):
    stub.change(lambda record: (record.update(gpu_ready=False), record["path"].update(gpu_name=None, memory_bytes=None, kind=None)))
    added = rows()[1]
    assert (added["label"], added["memory_gib"], added["shares_display"]) == ("nvidia", None, False)


def test_a_failed_proof_with_any_field_shape_keeps_the_panel_working(stub):
    stub.change(lambda record: (record.update(gpu_ready=False), record["path"].update(gpu_name=[], memory_bytes={}, kind=[])))
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


@pytest.mark.parametrize("name", ["default.json", "Upper.json", "has space.json", "-dash.json", f"{'x' * 33}.json", "notes.txt", "nvidia.json.writing"])
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


# GPUs in sysfs


def test_gpu_cards_are_one_vendors_sorted_ids_and_connectors_are_not_cards(sysfs):
    build_sysfs(
        sysfs,
        {"card3": ("0x10de", "0x2c05"), "card1": ("0x1002", "0x7551"), "card2": ("0x10de", "0x2204")},
        {"card3-DP-5": "connected", "card1-DP-1": "disconnected"},
    )
    (sysfs / "renderD128").mkdir()
    (sysfs / "version").write_text("drm 1.1.0\n")
    assert gpu_cards("cuda") == ["10de:2204", "10de:2c05"]
    assert gpu_cards("rocm") == ["1002:7551"]


def test_twin_cards_are_both_counted(sysfs):
    build_sysfs(sysfs, {"card1": ("0x10de", "0x2c05"), "card2": ("0x10de", "0x2c05")})
    assert gpu_cards("cuda") == ["10de:2c05", "10de:2c05"]


def test_an_unknown_vendor_or_missing_sysfs_finds_no_cards(sysfs):
    assert gpu_cards("cuda") == []
    build_sysfs(sysfs, {"card1": ("0x8086", "0x1234")})
    assert gpu_cards("cuda") == gpu_cards("rocm") == gpu_cards("other") == gpu_cards(None) == []


def test_a_card_with_an_unreadable_id_is_skipped(sysfs):
    build_sysfs(sysfs, {"card1": ("0x10de", "0x2c05")})
    (sysfs / "card1" / "device" / "device").unlink()
    assert gpu_cards("cuda") == []


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


@pytest.mark.parametrize("name", ["absent", "../nvidia", "NVIDIA", "", None])
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


def test_a_removed_path_fails_its_queued_job_by_name(stub):
    bound = binding("nvidia")
    stub.proof.unlink()
    with pytest.raises(ValueError) as error:
        require(bound)
    assert str(error.value) == f"{GPU_NAME} is no longer available: {UNSET_UP} Nothing ran and no other GPU was tried."


def test_a_path_that_went_bad_fails_its_queued_job_by_name(stub):
    bound = binding("nvidia")
    missing_interpreter(stub)
    with pytest.raises(ValueError) as error:
        require(bound)
    assert str(error.value) == f"{GPU_NAME} is no longer available: {NO_PYTHON} Nothing ran and no other GPU was tried."


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


def test_a_path_is_a_frozen_value_compared_in_full(stub):
    path = require(binding("nvidia"))
    assert (path.label, path.identity, path.kind, path.gpu_name, path.memory_bytes) == (GPU_NAME, f"nvidia|gpu|{GPU_NAME}", "cuda", GPU_NAME, MEMORY_BYTES)
    assert path == require(binding("nvidia")) and hash(path) == hash(require(binding("nvidia")))
    assert dataclasses.replace(path, python="/another/python") != path
    with pytest.raises(dataclasses.FrozenInstanceError):
        path.device = "gpu:1"


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
    for info in stub.site_packages.glob(f"{name}-*.dist-info"):
        shutil.rmtree(info)


def test_versions_come_from_the_paths_own_site_packages(stub):
    path = require(binding("nvidia"))
    drop_package(stub, "nnunetv2")
    add_package(stub.site_packages, "nnunetv2", "0.0.1")  # Whatever this process has installed.
    versions = path.versions()
    assert versions["nnunetv2"] == "0.0.1" and versions["torch"] == TORCH
    assert {name: versions[name] for name in FAMILIES["cuda"]} == FAMILIES["cuda"]


def test_a_package_the_path_lacks_reads_as_unavailable(stub):
    path = require(binding("nvidia"))
    drop_package(stub, "TotalSegmentator")
    assert path.versions()["TotalSegmentator"] == "unavailable"


def test_a_package_record_without_a_version_is_not_installed(stub):
    (stub.site_packages / f"torch-{TORCH}.dist-info" / "METADATA").write_text("")
    assert rows()[1]["reason"] == PACKAGES_CHANGED


def test_confirm_passes_the_proven_card_on_any_index_of_a_plain_gpu_request(stub):
    path = require(binding("nvidia"))
    path.confirm("gpu:0", GPU_NAME, "13.0")
    path.confirm("gpu:3", GPU_NAME, "13.0")


def test_confirm_holds_a_path_to_its_explicit_index(tmp_path, monkeypatch):
    built = stand_in(tmp_path, device="gpu:1")
    activate(monkeypatch, built)
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


def test_a_timeout_never_shows_its_command_line():
    text = public_error(subprocess.TimeoutExpired(["/home/someone/venv/bin/python", "-m", "worker"], 10))
    assert text == "The AI process did not stop in time."


@pytest.mark.parametrize(
    "message, expected",
    [
        ("[Errno 2] No such file or directory: '/home/some one/venv/bin/python'", "[Errno 2] No such file or directory: <local path>"),
        ('cannot open "/data/case 1/ct.nii.gz"', "cannot open <local path>"),
        ("TotalSegmentator wrote /tmp/run-1/total.log before exit", "TotalSegmentator wrote <local path> before exit"),
        ("search path /opt/a/lib:/opt/b/lib was empty", "search path <local path>:<local path> was empty"),
        ("failed at --output=/tmp/x, retry", "failed at --output=<local path>, retry"),
        ("CUDA out of memory and/or 1/2 of ./relative/file", "CUDA out of memory and/or 1/2 of ./relative/file"),
    ],
)
def test_absolute_paths_leave_a_public_error(message, expected):
    assert public_error(RuntimeError(message)) == expected


def test_an_empty_error_still_says_something():
    assert public_error(RuntimeError()) == "RuntimeError"
