"""Task selection, native flags and checkpoint provenance without real inference."""

from dataclasses import replace
from importlib import metadata
from pathlib import Path
import json
import shutil
import subprocess
import sys
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest

from inference_path_stub import FAMILIES, GPU_NAME, MEMORY_BYTES, TORCH, activate, add_package, stand_in
from scan3d import segmentation
from scan3d.config import device_request
from scan3d.inference_paths import InferencePath, binding, require
from scan3d.segmentation_options import normalize_options, segmentation_catalog


@pytest.mark.parametrize(
    "name,task,group",
    [
        ("eye_left", "head_glands_cavities", "Head organs"),
        ("heart_myocardium", "heartchambers_highres", "Heart"),
        ("body_trunc", "body", "Body"),
        ("torso_fat", "tissue_types", "Tissues"),
        ("lung_nodules", "lung_nodules", "Lung findings"),
        ("lung_pleural", "pleural_pericard_effusion", "Chest findings"),
        ("aorta", "heartchambers_highres", "Arteries"),
        ("lung_veins", "lung_vessels", "Veins"),
        ("skull", "craniofacial_structures", "Bones"),
        ("liver", "total_v3", "Abdominal organs"),
    ],
)
def test_task_appearance_has_meaningful_fallback_and_keeps_anatomical_priorities(name, task, group):
    assert segmentation._appearance(name, task)[0] == group


def test_catalog_is_ct_only_stable_and_does_not_initialize_torch():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; "
            "from scan3d.segmentation_options import segmentation_catalog; "
            "c=segmentation_catalog(); "
            "assert 'torch' not in sys.modules; "
            "print(len(c['tasks']))",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert int(result.stdout) >= 30
    catalog = segmentation_catalog()
    tasks = {item["id"]: item for item in catalog["tasks"]}
    assert {"total", "body", "lung_vessels", "liver_vessels", "heartchambers_highres"} <= tasks.keys()
    assert {"total_v3", "renal_arteries", "aorta_annulus", "aortic_dissection", "pulmonary_artery_landmarks"} <= tasks.keys()
    assert not {"total_mr", "body_mr", "brain_aneurysm", "test", "total_highres_test", "lung_vessels_LEGACY"} & tasks.keys()
    assert tasks["total"]["class_count"] == 117
    assert tasks["lung_vessels"]["robust_crop_default"] is True
    assert tasks["lung_vessels"]["body_seg_supported"] is False
    assert tasks["total"]["body_seg_supported"] is True
    assert tasks["total"]["robust_crop_supported"] is False
    assert catalog["remove_small_blobs_mm3"] == 200


def test_options_defaults_are_explicit_and_historical_profiles_remain_compatible():
    assert normalize_options("chest", {}) == {"tasks": ["total"], "robust_crop": False, "body_seg": False, "remove_small_blobs": False}
    assert normalize_options("chest")["tasks"] == ["total", "lung_vessels"]
    assert normalize_options("abdomen")["tasks"] == ["total"]
    assert normalize_options("abdomen", {"tasks": ["lung_vessels"], "robust_crop": False})["robust_crop"] is True


def test_catalog_exposes_output_names_and_correct_vertebral_and_brain_labels():
    from totalsegmentator.registry import get_task_classes

    catalog = segmentation_catalog()
    tasks = {item["id"]: item for item in catalog["tasks"]}
    for item in tasks.values():
        assert item["modality"] == "CT"
        assert item["class_count"] == len(item["classes"])
        assert len(item["classes"]) == len(set(item["classes"]))
        assert set(get_task_classes(item["id"]).values()) <= set(item["classes"])
    assert "skull" in tasks["total"]["classes"]
    assert "skull" in tasks["craniofacial_structures"]["classes"]
    assert tasks["body"]["model_class_count"] == 2
    assert set(tasks["body"]["classes"]) == {"body", "body_trunc", "body_extremities", "skin"}
    assert tasks["vertebrae_pp"]["label"] == "Vertebrae C1–L5 — postprocessed"
    assert tasks["vertebrae_pp_refined"]["label"] == "Vertebrae C1–L5 — body-refined"
    assert "posterior" not in tasks["vertebrae_pp"]["label"].lower()
    assert "Brain" in tasks["ventricle_parts"]["label"]
    assert {"renal_arteries", "celiac_trunk", "superior_mesenteric_artery"} <= set(tasks["renal_arteries"]["classes"])


@pytest.mark.parametrize(
    "options",
    [
        {"tasks": []},
        {"tasks": "total"},
        {"tasks": ["total", "total"]},
        {"tasks": ["total_mr"]},
        {"tasks": ["--device=cpu"]},
        {"tasks": [123]},
        {"tasks": ["test"]},
        {"tasks": ["lung_vessels_LEGACY"]},
        {"body_seg": "false"},
        {"robust_crop": 1},
        {"remove_small_blobs": 200},
        {"fast": True},
        {"device": "cpu"},
    ],
)
def test_rejects_unknown_tasks_and_untyped_or_unapproved_options(options):
    with pytest.raises(ValueError):
        normalize_options("chest", options)


def test_rejects_body_crop_where_native_task_would_ignore_it():
    with pytest.raises(ValueError, match="already crops"):
        normalize_options("chest", {"tasks": ["lung_vessels"], "body_seg": True})
    assert normalize_options("chest", {"tasks": ["total"], "body_seg": True})["body_seg"] is True


def test_license_check_is_local_and_never_discloses_the_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALSEG_HOME_DIR", str(tmp_path))
    with pytest.raises(ValueError, match="license configured"):
        normalize_options("chest", {"tasks": ["heartchambers_highres"]})
    secret = "private-key-123456"  # same 18-character offline shape checked upstream
    assert len(secret) == 18
    (tmp_path / "config.json").write_text(json.dumps({"license_number": secret}))
    catalog = segmentation_catalog()
    assert secret not in json.dumps(catalog)
    licensed = next(item for item in catalog["tasks"] if item["id"] == "heartchambers_highres")
    assert licensed["license_available"] is True
    assert normalize_options("chest", {"tasks": ["heartchambers_highres"]})["robust_crop"] is True
    (tmp_path / "config.json").write_text(json.dumps({"license_number": "short-secret"}))
    with pytest.raises(ValueError) as error:
        normalize_options("chest", {"tasks": ["heartchambers_highres"]})
    assert "short-secret" not in str(error.value)


def test_fingerprint_records_exact_task_options_and_auxiliary_crop_models(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(tmp_path / "weights"))

    def fingerprint(task, **flags):
        return segmentation.model_fingerprint("chest", {"tasks": [task], **flags})

    total = fingerprint("total")
    assert {model["dataset_id"] for model in total["models"]} == {291, 292, 293, 294, 295}
    assert total["weights_complete"] is False
    assert fingerprint("total", body_seg=True)["digest"] != total["digest"]
    assert fingerprint("total", remove_small_blobs=True)["digest"] != total["digest"]
    assert fingerprint("total", robust_crop=True)["digest"] != total["digest"]
    assert {model["dataset_id"] for model in fingerprint("total", body_seg=True)["models"]} == {291, 292, 293, 294, 295, 300}
    assert {model["dataset_id"] for model in fingerprint("liver_vessels")["models"]} == {8, 298}
    assert {model["dataset_id"] for model in fingerprint("liver_vessels", robust_crop=True)["models"]} == {8, 297}
    assert {model["dataset_id"] for model in fingerprint("lung_vessels")["models"]} == {117, 297}
    assert fingerprint("lung_vessels", robust_crop=False)["digest"] == fingerprint("lung_vessels", robust_crop=True)["digest"]
    assert {model["dataset_id"] for model in fingerprint("abdominal_muscles")["models"]} == {952, 300}
    assert {model["dataset_id"] for model in fingerprint("teeth")["models"]} == {113, 115, 298}
    assert {model["dataset_id"] for model in fingerprint("vertebrae_pp_refined")["models"]} == {803, 305}
    assert str(tmp_path) not in json.dumps(total)


@pytest.mark.parametrize(
    "task,flags,auxiliary_id",
    [
        ("total", {"body_seg": True}, 300),
        ("liver_vessels", {"robust_crop": True}, 297),
        ("teeth", {}, 298),
    ],
)
def test_auxiliary_checkpoint_changes_invalidate_cache_identity(tmp_path, monkeypatch, task, flags, auxiliary_id):
    weights = tmp_path / "weights"
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(weights))
    options = {"tasks": [task], **flags}
    missing = segmentation.model_fingerprint("chest", options)
    for model in missing["models"]:
        directory = weights / f"Dataset{model['dataset_id']:03d}_fixture" / f"{model['trainer']}__{model['plans']}__{model['configuration']}"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "plans.json").write_text("{}")
        (directory / "dataset.json").write_text("{}")
        for fold in model["folds"]:
            (directory / f"fold_{fold}").mkdir(exist_ok=True)
            (directory / f"fold_{fold}" / "checkpoint_final.pth").write_bytes(b"fixture weights")
    first = segmentation.model_fingerprint("chest", options)
    assert first["weights_complete"] is True
    auxiliary = next((weights / f"Dataset{auxiliary_id:03d}_fixture").rglob("checkpoint_final.pth"))
    auxiliary.write_bytes(b"different auxiliary model")
    changed = segmentation.model_fingerprint("chest", options)
    assert changed["digest"] != first["digest"]
    unchanged = next(model for model in first["models"] if model["dataset_id"] != auxiliary_id)
    assert unchanged in changed["models"]


@pytest.mark.parametrize(
    "task,flags,expected_flags,names",
    [
        ("total", {"body_seg": True, "remove_small_blobs": True}, {"--body_seg", "--remove_small_blobs"}, ["liver", "skull"]),
        ("liver_vessels", {"robust_crop": True, "remove_small_blobs": True}, {"--robust_crop", "--remove_small_blobs"}, ["liver_vessels", "liver_tumor"]),
        ("lung_vessels", {"robust_crop": False}, {"--robust_crop"}, sorted(segmentation._LUNG)),
    ],
)
def test_selected_task_flags_and_masks_keep_native_patient_geometry(tmp_path, monkeypatch, task, flags, expected_flags, names):
    shape = (8, 9, 10)
    affine = np.array([[-0.7, -0.15, 0.0, 34.0], [0.1, 0.9, 0.2, -71.0], [0.0, 0.1, 1.8, 14.0], [0.0, 0.0, 0.0, 1.0]])
    ct_path = tmp_path / "ct.nii.gz"
    nib.save(nib.Nifti1Image(np.full(shape, 123, np.int16), affine), ct_path)
    commands = []
    fingerprints = []
    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: {"selected_device": "gpu:1"})
    monkeypatch.setenv("SCAN3D_DEVICE", "gpu")

    def fingerprint(profile, options):
        fingerprints.append((profile, options))
        return {"digest": "fixture", "weights_complete": True}

    monkeypatch.setattr(segmentation, "model_fingerprint", fingerprint)

    class Worker:
        pid = None  # Completed in-process fixture; no native worker to sample.
        returncode = 0

        def __init__(self, command, **_):
            commands.append(command)
            directory = Path(command[command.index("-o") + 1])
            for name in names:
                mask = np.zeros(shape, np.uint8)
                mask[2:5, 3:6, 4:7] = 1
                nib.save(nib.Nifti1Image(mask, affine), directory / f"{name}.nii.gz")
            Path(command[command.index("--report") + 1]).write_text(
                json.dumps(
                    {"device": "gpu", "fast": False, "fastest": False, "save_lowres": False, "classes": {str(i + 1): name for i, name in enumerate(names)}}
                )
            )

        def poll(self):
            return 0

    monkeypatch.setattr(segmentation.subprocess, "Popen", Worker)
    options = {"tasks": [task], **flags}
    output = tmp_path / "output"
    structures = segmentation.segment(ct_path, output, "chest", lambda _: None, lambda: False, options)
    assert len(commands) == 1
    command = commands[0]
    assert command[:4] == [sys.executable, "-u", "-m", "totalsegmentator.bin.TotalSegmentator"]
    assert command[command.index("--task") + 1] == task
    assert command[command.index("--device") + 1] == "gpu:1"
    actual_flags = {flag for flag in command if flag in {"--body_seg", "--robust_crop", "--remove_small_blobs"}}
    assert actual_flags == expected_flags
    assert not {"--fast", "--fastest", "--save_lowres"} & set(command)
    assert {entry["id"] for entry in structures} == {f"{task}:{name}" for name in names}
    for structure in structures:
        image = nib.load(structure["mask_file"])
        assert image.shape == shape
        np.testing.assert_allclose(image.affine, affine, rtol=1e-6, atol=1e-6)
    manifest = json.loads((output / "inference.json").read_text())
    assert manifest["status"] == "completed"
    assert manifest["options"] == normalize_options("chest", options)
    assert manifest["tasks"][0]["effective_options"]["remove_small_blobs_mm3"] == (200 if flags.get("remove_small_blobs") else None)
    assert fingerprints == [("chest", normalize_options("chest", options))]


# Identity on an added inference path


def test_a_paths_fingerprint_is_its_own_and_stable_and_names_nothing_local(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(tmp_path / "weights"))
    activate(monkeypatch, stand_in(tmp_path))
    path = require(binding("nvidia"))
    options = {"tasks": ["total"]}
    default = segmentation.model_fingerprint("chest", options)
    added = segmentation.model_fingerprint("chest", options, inference_path=path)
    assert added["digest"] != default["digest"]
    assert added == segmentation.model_fingerprint("chest", options, inference_path=path)
    assert set(added) == set(default)  # The cache layout and its readers see the same shape.
    assert added["device_request"] == f"nvidia|gpu|{GPU_NAME}"
    assert added["versions"] == path.versions()
    assert {name: added["versions"][name] for name in ("torch", *FAMILIES["cuda"])} == {"torch": TORCH, **FAMILIES["cuda"]}
    assert str(tmp_path) not in json.dumps(added)


def test_the_default_paths_fingerprint_does_not_move(tmp_path, monkeypatch):
    monkeypatch.setenv("TOTALSEG_WEIGHTS_PATH", str(tmp_path / "weights"))
    before = segmentation.model_fingerprint("chest", {"tasks": ["total"]})
    activate(monkeypatch, stand_in(tmp_path))
    assert segmentation.model_fingerprint("chest", {"tasks": ["total"]}) == before
    assert before["device_request"] == device_request()
    assert set(before["versions"]) == {"TotalSegmentator", "nnunetv2", "torch"}


def test_a_replaced_card_or_upgraded_stack_is_a_different_fingerprint(tmp_path, monkeypatch):
    stub = stand_in(tmp_path)
    activate(monkeypatch, stub)
    path = require(binding("nvidia"))
    options = {"tasks": ["total"]}
    first = segmentation.model_fingerprint("chest", options, inference_path=path)
    assert segmentation.model_fingerprint("chest", options, inference_path=replace(path, gpu_name="Test Replacement GPU"))["digest"] != first["digest"]
    shutil.rmtree(stub.site_packages / f"torch-{TORCH}.dist-info")
    add_package(stub.site_packages, "torch", "9.9.9")
    assert segmentation.model_fingerprint("chest", options, inference_path=path)["digest"] != first["digest"]


# Running a task on an added inference path, with both children replaced in this process

ADDED = InferencePath("nvidia", "/venv/bin/python", "/venv/lib/python/site-packages", "gpu", "cuda", GPU_NAME, MEMORY_BYTES, (("torch", "2.0"),))
OVERFLOW = b"Prediction on device was unsuccessful, probably due to a lack of memory. Moving results arrays to CPU\n"
QC = f"Part of total re-ran with results in system memory on {GPU_NAME}, usually because GPU memory ran out. The run took longer."


@pytest.fixture
def run(tmp_path, monkeypatch):
    """segment() with a scripted device check, log, exit code and report."""
    ct_path = tmp_path / "ct.nii.gz"
    nib.save(nib.Nifti1Image(np.full((8, 9, 10), 123, np.int16), np.eye(4)), ct_path)
    state = SimpleNamespace(
        output=tmp_path / "output",
        spawned=[],
        log=b"",
        returncode=0,
        report={"device": "gpu", "torch_version": "2.0", "classes": {"1": "liver"}},
        measured={"device": "gpu", "selected_device": "gpu:0", "torch": "2.0", "gpu_index": 0, "gpu_name": GPU_NAME, "hip": None, "cuda": "13.0"},
    )

    class Worker:
        pid = None

        def __init__(self, command, stdout, **options):
            state.spawned.append((command, options))
            self.returncode = state.returncode
            stdout.write(state.log)
            directory = Path(command[command.index("-o") + 1])
            mask = np.zeros((8, 9, 10), np.uint8)
            mask[2:5, 3:6, 4:7] = 1
            nib.save(nib.Nifti1Image(mask, np.eye(4)), directory / "liver.nii.gz")
            Path(command[command.index("--report") + 1]).write_text(json.dumps(state.report))

        def poll(self):
            return self.returncode

    def fingerprint(profile, options=None, **source):
        state.fingerprinted_with = source
        return {"digest": "fixture", "weights_complete": True}

    monkeypatch.setattr(segmentation, "_resolve_device", lambda *_: state.measured)
    monkeypatch.setattr(segmentation, "model_fingerprint", fingerprint)
    monkeypatch.setattr(segmentation.subprocess, "Popen", Worker)

    def segment(inference_path=ADDED, task="total"):
        source = {"inference_path": inference_path} if inference_path else {}
        return segmentation.segment(ct_path, state.output, "chest", lambda _: None, lambda: False, {"tasks": [task]}, **source)

    state.segment = segment
    state.manifest = lambda: json.loads((state.output / "inference.json").read_text())
    return state


def test_a_path_spawns_its_own_interpreter_with_its_own_environment(run):
    run.segment()
    ((command, options),) = run.spawned
    assert command[:4] == [ADDED.python, "-u", "-m", "totalsegmentator.bin.TotalSegmentator"]
    assert "--verbose" in command and command[command.index("--device") + 1] == "gpu:0"
    assert options["env"] == ADDED.environment()
    assert run.fingerprinted_with == {"inference_path": ADDED}


def test_the_default_path_spawns_exactly_what_it_did(run):
    run.report.pop("torch_version")
    run.segment(None)
    ((command, options),) = run.spawned
    assert command[0] == sys.executable and "--verbose" not in command and "env" not in options
    assert run.fingerprinted_with == {} and "inference_path" not in run.manifest()


def test_the_manifest_names_the_path_from_its_first_save_in_both_copies_and_never_the_interpreter(run, monkeypatch):
    saves, write = [], segmentation.atomic_text

    def recorded(destination, content):
        saves.append(json.loads(content))
        write(destination, content)

    monkeypatch.setattr(segmentation, "atomic_text", recorded)
    run.segment()
    assert saves[0]["tasks"] == [] and {save["inference_path"] for save in saves} == {"nvidia"}
    manifest = run.manifest()
    assert manifest["inference_path"] == "nvidia" and manifest["runtime"] == run.measured
    assert json.loads(next(run.output.glob("run-*/inference.json")).read_text()) == manifest
    assert ADDED.python not in json.dumps(manifest) and ADDED.site_packages not in json.dumps(manifest)


def nothing_ran(run):
    assert run.spawned == [] and list(run.output.glob("run-*")) == [] and not (run.output / "inference.json").exists()


@pytest.mark.parametrize(
    "measured, found",
    [
        ({"gpu_name": "Test Other GPU"}, r"Test Other GPU \(gpu:0, CUDA\)"),
        ({"cuda": None}, rf"{GPU_NAME} \(gpu:0, no CUDA\)"),
        ({"selected_device": "cpu", "gpu_name": None, "cuda": None}, r"no GPU \(cpu, no CUDA\)"),
    ],
)
def test_a_wrong_card_or_build_stops_before_a_run_exists(run, measured, found):
    run.measured.update(measured)
    with pytest.raises(RuntimeError, match=f"Expected {GPU_NAME}, but the GPU check found {found}. No result was kept and no other GPU was tried."):
        run.segment()
    nothing_ran(run)


def test_a_crop_model_task_is_refused_off_the_first_card_because_upstream_drops_the_index(run):
    run.measured.update(selected_device="gpu:1", gpu_index=1)
    with pytest.raises(ValueError) as error:
        run.segment(task="teeth")
    assert (
        str(error.value)
        == f"TotalSegmentator would run part of teeth on the first GPU, so it cannot run on {GPU_NAME}. Nothing ran and no other GPU was tried."
    )
    nothing_ran(run)


@pytest.mark.parametrize(
    "inference_path, device, task",
    [(ADDED, "gpu:0", "teeth"), (ADDED, "gpu:1", "total"), (None, "gpu:1", "teeth")],
    ids=["first-card", "no-crop-model", "default-path-unchanged"],
)
def test_only_a_crop_model_off_the_first_card_is_refused(run, inference_path, device, task):
    run.measured.update(selected_device=device, gpu_index=int(device[4:]))
    run.segment(inference_path, task)
    assert len(run.spawned) == 1


def test_a_report_from_another_torch_is_rejected(run):
    run.report["torch_version"] = "9.9.9"
    with pytest.raises(RuntimeError, match=f"ran on PyTorch 9.9.9, not the 2.0 that {GPU_NAME} was checked on. No result was kept and no other GPU was tried."):
        run.segment()
    assert run.manifest()["status"] == "failed"


def test_a_report_without_a_torch_version_is_rejected_on_a_path(run):
    run.report.pop("torch_version")
    with pytest.raises(RuntimeError, match="ran on PyTorch None"):
        run.segment()


@pytest.mark.parametrize("inference_path", [ADDED, None], ids=["added", "default"])
def test_a_run_that_fell_back_to_the_cpu_is_rejected(run, inference_path):
    run.report["device"] = "cpu"
    with pytest.raises(RuntimeError, match="did not complete on the requested GPU; CPU fallback results were rejected"):
        run.segment(inference_path)


@pytest.mark.parametrize(
    "log, returncode, cause",
    [
        (b"torch.OutOfMemoryError: CUDA out of memory. Tried to allocate 2.00 GiB\n", 1, "it ran out of GPU memory"),
        (b"RuntimeError: HIP Out Of Memory\n", 1, "it ran out of GPU memory"),
        (b"", -9, "the process was killed, usually because system memory ran out"),
        (b"CUDA out of memory on an earlier attempt\n", -9, "the process was killed, usually because system memory ran out"),
        (b"Traceback\n", 3, "exit 3"),
        (b"", -11, "exit -11"),
        (b"out of memory\n" + b"x" * (64 * 1024), 1, "exit 1"),
        (b"x" * (64 * 1024) + b"\nout of memory\n", 1, "it ran out of GPU memory"),
    ],
    ids=["cuda", "any-case", "killed", "killed-with-old-text", "plain", "another-signal", "older-than-the-tail", "end-of-a-long-log"],
)
def test_a_failed_task_says_what_stopped_it_on_which_card(run, log, returncode, cause):
    run.log, run.returncode = log, returncode
    with pytest.raises(RuntimeError) as error:
        run.segment()
    assert str(error.value) == f"TotalSegmentator total failed on {GPU_NAME}: {cause}. No other GPU was tried."
    assert run.manifest()["tasks"][0]["status"] == "failed"


def test_a_failure_on_the_default_path_keeps_its_message(run):
    run.log, run.returncode = b"CUDA out of memory\n", 1
    with pytest.raises(RuntimeError) as error:
        run.segment(None)
    assert str(error.value) == "TotalSegmentator total failed (exit 1); inspect the local worker log"


def test_results_moved_to_system_memory_are_recorded_and_explained(run):
    run.log = b"Predicting\n" + OVERFLOW + b"Done\n"
    run.segment()
    manifest = run.manifest()
    assert manifest["tasks"][0]["results_moved_to_cpu"] is True and manifest["qc"] == [QC]
    assert json.loads(next(run.output.glob("run-*/inference.json")).read_text()) == manifest


def test_a_move_is_found_anywhere_in_a_long_log(run):
    # Preprocessing prints before it and the rest of the task after it, so no window is enough.
    filler = b"x" * (64 * 1024)
    run.log = filler + OVERFLOW + filler
    run.segment()
    manifest = run.manifest()
    assert manifest["tasks"][0]["results_moved_to_cpu"] is True and manifest["qc"] == [QC]


def test_a_clean_log_records_no_move(run):
    run.log = b"Predicting\nDone\n"
    run.segment()
    manifest = run.manifest()
    assert "results_moved_to_cpu" not in manifest["tasks"][0] and manifest["qc"] == []


def test_the_default_path_cannot_see_a_move_so_it_claims_nothing(run):
    run.log = OVERFLOW
    run.report.pop("torch_version")
    run.segment(None)
    manifest = run.manifest()
    assert "results_moved_to_cpu" not in manifest["tasks"][0] and manifest["qc"] == []


def test_the_overflow_text_is_what_the_pinned_nnunet_prints():
    try:
        source = metadata.distribution("nnunetv2").locate_file("nnunetv2/inference/predict_from_raw_data.py")
    except metadata.PackageNotFoundError:
        pytest.skip("CI installs no nnU-Net")
    assert OVERFLOW.strip().decode() in " ".join(Path(source).read_text().split())


def test_vram_scope_describes_a_sample_only_when_there_is_one(run, tmp_path):
    run.segment()
    resources = run.manifest()["tasks"][0]["resources"]
    assert resources["vram_scope"] is None and resources["whole_device_vram_peak_bytes"] is None
    sampled = segmentation._Resources()
    sampled.vram_path = tmp_path / "mem_info_vram_used"
    assert sampled.result()["vram_scope"].startswith("Sampled whole GPU use")
