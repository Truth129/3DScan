import builtins
from contextlib import nullcontext
from importlib import metadata
from pathlib import Path
from types import SimpleNamespace

import nibabel as nib
import numpy as np
import pytest
import sys

from scan3d import imaging, interactive


def save(path, values, affine, units="mm"):
    image = nib.Nifti1Image(np.asarray(values), affine)
    image.header.set_xyzt_units(units)
    image.set_sform(affine, code=2)
    image.set_qform(affine, code=1)
    nib.save(image, path)
    return path


class FakeSession:
    def __init__(self):
        self.images = []
        self.initials = []
        self.points = []
        self.supports_initial_label = True
        self.supported_interactions = {"points": True}
        self.fail = False
        self.after_point = None
        self.executor = SimpleNamespace(shutdown=lambda **kw: None)

    def initialize_from_trained_model_folder(self, folder, use_fold):
        self.folder, self.fold = folder, use_fold

    def set_image(self, image):
        self.images.append(image.copy())

    def set_target_buffer(self, target):
        self.target = target

    def add_initial_seg_interaction(self, baseline, run_prediction):
        assert run_prediction is False
        self.initials.append(baseline.copy())
        self.target[:] = baseline

    def add_point_interaction(self, voxel, include_interaction, run_prediction):
        assert run_prediction is True
        self.points.append((voxel.copy(), include_interaction))
        self.target[tuple(voxel)] = int(include_interaction)
        if self.after_point:
            self.after_point()
        if self.fail:
            raise RuntimeError("GPU prediction failed")
        return [[i, i + 1] for i in voxel]


@pytest.fixture
def runtime_files(tmp_path, monkeypatch):
    # Runtime state tests use a deterministic model double and require no torch.
    monkeypatch.setattr(interactive, "_kernel_settings", nullcontext)
    directory = tmp_path / "model"
    (directory / "fold_0").mkdir(parents=True)
    for name in ("fold_0/checkpoint_final.pth", "plans.json", "dataset.json", "inference_session_class.json"):
        (directory / name).write_text(name)
    monkeypatch.setenv("SCAN3D_NNINTERACTIVE_MODEL", str(directory))
    monkeypatch.setattr(interactive.metadata, "version", lambda name: interactive.ENGINE_VERSION)
    monkeypatch.setattr(interactive, "CHECKPOINT_SHA256", interactive._sha256(interactive._identity(directory / "fold_0/checkpoint_final.pth")))
    device = {"selected_device": "gpu:0", "gpu_index": 0, "gpu_name": "Test GPU", "torch": "2.8.0+rocm7.2.4", "hip": "7.2.4"}
    monkeypatch.setattr(interactive, "_device_info", lambda request: device)
    sessions = []

    def factory(info):
        session = FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(interactive, "_new_session", factory)
    angle = np.deg2rad(29)
    rotate = np.array([[np.cos(angle), -np.sin(angle), 0], [np.sin(angle), np.cos(angle), 0], [0, 0, 1]])
    affine = np.eye(4)
    affine[:3, :3] = rotate @ np.diag([-0.8, 1.4, 2.2])
    affine[:3, 3] = [31, -52, 87]
    values = np.arange(11 * 12 * 13, dtype=np.int16).reshape((11, 12, 13)) - 900
    mask = np.zeros(values.shape, np.uint8)
    mask[2:5, 3:7, 4:8] = 1
    ct = save(tmp_path / "ct.nii.gz", values, affine)
    baseline = save(tmp_path / "mask.nii.gz", mask, affine)
    # Prompts must be transformed with the stored, float32 NIfTI affine.
    affine = imaging._image(ct)[1]

    def point(identifier, voxel, include=True):
        ras = (affine @ np.r_[voxel, 1])[:3].tolist()
        return {"id": identifier, "ras": ras, "include": include}

    return SimpleNamespace(directory=directory, ct=ct, mask=baseline, affine=affine, values=values, baseline=mask, sessions=sessions, point=point)


def predict(runtime, fixture, tmp_path, prompts, name="candidate.nii.gz", **kwargs):
    return runtime.predict(
        fixture.ct,
        fixture.mask,
        prompts,
        tmp_path / name,
        kwargs.get("progress", lambda message: None),
        kwargs.get("cancelled", lambda: False),
        kwargs.get("session_key", "session-1"),
        batches=kwargs.get("batches"),
        options=kwargs.get("options"),
    )


class BatchRuntimeSession(FakeSession):
    """Deterministic binary model double retaining real batch scheduling calls."""

    MAX_AUTOZOOM_FACTOR = 4

    def __init__(self):
        super().__init__()
        self.supports_zero_shot_label_refinement = True
        self.supported_interactions.update(bbox2d=True, scribble=True, lasso=True)
        self.configuration_manager = SimpleNamespace(patch_size=[16, 16, 16])
        self.new_interaction_centers, self.new_interaction_zoom_out_factors = [], []
        self.do_autozoom = True
        self.evidence, self.calls, self.full_calls, self.initial_modes = [], [], [], []
        self.after_batch = self.after_full = None

    def set_image(self, image):
        super().set_image(image)
        self.original_image_shape = image.shape

    def set_target_buffer(self, target):
        super().set_target_buffer(target)
        self.target_buffer = target

    def set_do_autozoom(self, enabled):
        self.do_autozoom = enabled

    def add_initial_seg_interaction(self, baseline, run_prediction):
        self.initials.append(baseline.copy())
        self.initial_modes.append(run_prediction)
        self.target.fill(0)
        self.target[:] = baseline
        self.evidence.clear()
        self.new_interaction_centers, self.new_interaction_zoom_out_factors = [], []
        if run_prediction:
            self.full_calls.append({"autozoom": self.do_autozoom})
            self.target[1, 1, 1] = 1  # Distinct deterministic whole-mask refinement.
            if self.after_full:
                self.after_full()
            return [[0, int(size)] for size in self.target.shape]
        return None

    def _register(self, kind, payload, include):
        self.evidence.append({"kind": kind, "payload": payload, "include": include})
        self.new_interaction_centers.append([0, 0, 0])
        self.new_interaction_zoom_out_factors.append(1)

    def add_point_interaction(self, voxel, include_interaction, run_prediction):
        if run_prediction:
            return super().add_point_interaction(voxel, include_interaction, run_prediction)
        self.points.append((voxel.copy(), include_interaction))
        self._register("point", voxel.copy(), include_interaction)

    def add_bbox_interaction(self, bbox, include_interaction, run_prediction):
        assert not run_prediction
        self._register("bbox", bbox, include_interaction)

    def add_scribble_interaction(self, image, include_interaction, run_prediction, interaction_bbox):
        assert not run_prediction
        self._register("scribble", (image.copy(), interaction_bbox), include_interaction)

    def add_lasso_interaction(self, image, include_interaction, run_prediction, interaction_bbox):
        assert not run_prediction
        self._register("lasso", (image.copy(), interaction_bbox), include_interaction)

    def _predict(self):
        assert len(self.new_interaction_centers) == len(self.new_interaction_zoom_out_factors) == 1
        center = np.asarray(self.new_interaction_centers[0])
        size = np.rint(np.asarray(self.configuration_manager.patch_size) * self.new_interaction_zoom_out_factors[0]).astype(int)
        low, high = center - size // 2, center + size // 2 + size % 2
        self.calls.append({"evidence": [dict(item) for item in self.evidence], "autozoom": self.do_autozoom})
        for item in self.evidence:
            if item["kind"] == "point":
                voxel = np.asarray(item["payload"])
                if np.all(voxel >= low) and np.all(voxel < high):
                    self.target[tuple(voxel)] = int(item["include"])
        self.new_interaction_centers, self.new_interaction_zoom_out_factors = [], []
        if self.after_batch:
            self.after_batch()
        if self.fail:
            raise RuntimeError("GPU prediction failed")
        return np.stack((np.maximum(0, low), np.minimum(self.target.shape, high)), axis=1).tolist()


@pytest.fixture
def batch_runtime_files(runtime_files, monkeypatch):
    def factory(info):
        session = BatchRuntimeSession()
        runtime_files.sessions.append(session)
        return session

    monkeypatch.setattr(interactive, "_new_session", factory)
    return runtime_files


def boundary(end, *, autozoom=True, full=False):
    return {"end": end, "autozoom": autozoom, "full_refine": full}


def test_readiness_does_not_import_gpu_or_expose_model_path(runtime_files, monkeypatch):
    original = builtins.__import__

    def import_guard(name, *args, **kwargs):
        if name.startswith(("torch", "nnInteractive")):
            raise AssertionError("Readiness imported a GPU dependency")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_guard)
    result = interactive.status()
    assert result["available"] and result["installed"] and result["model_available"]
    assert result["license"] == "CC BY-NC-SA 4.0"
    assert str(runtime_files.directory) not in str(result)


def test_initial_mask_unchanged_then_oblique_reflected_point_refines_only_requested_voxel(runtime_files, tmp_path):
    fixture, runtime = runtime_files, interactive.InteractiveRuntime()
    result = predict(runtime, fixture, tmp_path, [])
    np.testing.assert_array_equal(nib.load(tmp_path / "candidate.nii.gz").dataobj, fixture.baseline)
    assert result["changed_voxels"] == result["predictions_this_request"] == 0
    assert fixture.sessions[0].images[0].shape == (1, *fixture.values.shape)
    np.testing.assert_array_equal(fixture.sessions[0].images[0][0], fixture.values)
    prompt = fixture.point("add", [7, 8, 9])
    result = predict(runtime, fixture, tmp_path, [prompt], "second.nii.gz")
    expected = fixture.baseline.copy()
    expected[7, 8, 9] = 1
    np.testing.assert_array_equal(nib.load(tmp_path / "second.nii.gz").dataobj, expected)
    np.testing.assert_array_equal(nib.load(fixture.mask).dataobj, fixture.baseline)
    assert result["prompts"][0]["voxel"] == [7, 8, 9]
    assert result["changed_voxels"] == result["predictions_this_request"] == 1
    assert result["reused_prompt_prefix"]
    assert result["after_volume_ml"] - result["before_volume_ml"] == pytest.approx(0.8 * 1.4 * 2.2 / 1000)
    assert result["checkpoint_sha256"] == interactive.CHECKPOINT_SHA256
    assert result["input_mask_sha256"] != result["output_mask_sha256"]
    assert len(fixture.sessions) == len(fixture.sessions[0].images) == len(fixture.sessions[0].initials) == 1
    original, candidate = nib.load(fixture.ct), nib.load(tmp_path / "second.nii.gz")
    np.testing.assert_array_equal(original.get_qform(), candidate.get_qform())
    np.testing.assert_array_equal(original.get_sform(), candidate.get_sform())
    assert candidate.header.get_xyzt_units()[0] == "mm"
    runtime.release()


def test_upstream_initial_mask_changes_are_rejected_before_prediction(runtime_files, tmp_path, monkeypatch):
    fixture, runtime = runtime_files, interactive.InteractiveRuntime()
    original = FakeSession.add_initial_seg_interaction

    def altered_initialization(session, baseline, run_prediction):
        original(session, baseline, run_prediction)
        session.target[0, 0, 0] ^= 1

    monkeypatch.setattr(FakeSession, "add_initial_seg_interaction", altered_initialization)
    with pytest.raises(RuntimeError, match="changed the saved mask during initialization"):
        predict(runtime, fixture, tmp_path, [fixture.point("one", [7, 8, 9])])
    assert not fixture.sessions[0].points
    assert runtime._session is None
    assert not (tmp_path / "candidate.nii.gz").exists()
    np.testing.assert_array_equal(nib.load(fixture.mask).dataobj, fixture.baseline)


def test_prompt_prefix_is_incremental_and_undo_and_session_switch_replay_saved_baseline(runtime_files, tmp_path):
    fixture, runtime = runtime_files, interactive.InteractiveRuntime()
    first, second = fixture.point("one", [7, 8, 9]), fixture.point("two", [2, 3, 4], False)
    predict(runtime, fixture, tmp_path, [first])
    result = predict(runtime, fixture, tmp_path, [first, second], "two.nii.gz")
    assert result["predictions_this_request"] == 1
    session = fixture.sessions[0]
    assert len(session.points) == 2 and len(session.initials) == 1
    result = predict(runtime, fixture, tmp_path, [first], "undo.nii.gz")
    assert result["predictions_this_request"] == 1 and not result["reused_prompt_prefix"]
    assert len(session.initials) == 2
    assert nib.load(tmp_path / "undo.nii.gz").get_fdata()[2, 3, 4] == 1
    result = predict(runtime, fixture, tmp_path, [], "reset.nii.gz")
    np.testing.assert_array_equal(nib.load(tmp_path / "reset.nii.gz").dataobj, fixture.baseline)
    assert result["changed_voxels"] == 0
    predict(runtime, fixture, tmp_path, [second], "other.nii.gz", session_key="other-session")
    assert len(session.initials) == 4
    assert len(session.images) == 1  # Source CT preparation is reused across objects.
    runtime.release()


def test_replaced_baseline_invalidates_cache_even_for_same_prompt_ids(runtime_files, tmp_path):
    fixture, runtime = runtime_files, interactive.InteractiveRuntime()
    point = fixture.point("one", [7, 8, 9])
    predict(runtime, fixture, tmp_path, [point])
    modified = fixture.baseline.copy()
    modified[0, 0, 0] = 1
    save(fixture.mask, modified, fixture.affine)
    result = predict(runtime, fixture, tmp_path, [point], "new-baseline.nii.gz")
    assert not result["reused_prompt_prefix"]
    assert len(fixture.sessions[0].initials) == 2
    assert nib.load(tmp_path / "new-baseline.nii.gz").get_fdata()[0, 0, 0] == 1
    runtime.release()


def test_replaced_ct_reprepares_image_and_edited_prompt_replays(runtime_files, tmp_path):
    fixture, runtime = runtime_files, interactive.InteractiveRuntime()
    point = fixture.point("one", [7, 8, 9])
    predict(runtime, fixture, tmp_path, [point])
    altered = fixture.point("one", [8, 8, 9])
    result = predict(runtime, fixture, tmp_path, [altered], "edited.nii.gz")
    assert not result["reused_prompt_prefix"]
    edited = nib.load(tmp_path / "edited.nii.gz").get_fdata()
    assert edited[7, 8, 9] == 0 and edited[8, 8, 9] == 1
    save(fixture.ct, fixture.values + 1, fixture.affine)
    result = predict(runtime, fixture, tmp_path, [altered], "changed-ct.nii.gz")
    assert not result["reused_prompt_prefix"]
    assert len(fixture.sessions) == 1 and len(fixture.sessions[0].images) == 2
    np.testing.assert_array_equal(fixture.sessions[0].images[-1][0], fixture.values + 1)
    runtime.release()


def test_metre_native_grid_uses_millimetre_prompts_and_preserves_header(runtime_files, tmp_path):
    fixture, runtime = runtime_files, interactive.InteractiveRuntime()
    affine = fixture.affine.copy()
    affine[:3] /= 1000
    save(fixture.ct, fixture.values, affine, "meter")
    save(fixture.mask, fixture.baseline, affine, "meter")
    prompt = fixture.point("millimetres", [7, 8, 9])
    result = predict(runtime, fixture, tmp_path, [prompt])
    assert result["prompts"][0]["voxel"] == [7, 8, 9]
    candidate = nib.load(tmp_path / "candidate.nii.gz")
    np.testing.assert_allclose(candidate.affine, affine)
    assert candidate.header.get_xyzt_units()[0] == "meter"
    assert result["after_volume_ml"] - result["before_volume_ml"] == pytest.approx(0.8 * 1.4 * 2.2 / 1000)
    runtime.release()


@pytest.mark.parametrize("ras", [[float("nan"), 0, 0], [0, 0], [1e6, 0, 0]])
def test_invalid_patient_points_rejected_before_model_load(runtime_files, tmp_path, ras):
    runtime = interactive.InteractiveRuntime()
    with pytest.raises(ValueError, match="finite|outside"):
        predict(runtime, runtime_files, tmp_path, [{"id": "bad", "ras": ras, "include": True}])
    assert runtime_files.sessions == []
    assert not (tmp_path / "candidate.nii.gz").exists()


def test_cancellation_and_gpu_failure_invalidate_warm_state_without_publishing(runtime_files, tmp_path):
    fixture, runtime = runtime_files, interactive.InteractiveRuntime()
    point = fixture.point("one", [7, 8, 9])
    predict(runtime, fixture, tmp_path, [])
    stopped = [False]
    fixture.sessions[0].after_point = lambda: stopped.__setitem__(0, True)
    with pytest.raises(interactive.SegmentationCancelled):
        predict(runtime, fixture, tmp_path, [point], "cancelled.nii.gz", cancelled=lambda: stopped[0])
    assert runtime._session is None
    assert not (tmp_path / "cancelled.nii.gz").exists()
    predict(runtime, fixture, tmp_path, [])
    fixture.sessions[-1].fail = True
    with pytest.raises(RuntimeError, match="GPU prediction failed"):
        predict(runtime, fixture, tmp_path, [point], "failed.nii.gz")
    assert runtime._session is None
    assert not (tmp_path / "failed.nii.gz").exists()
    result = predict(runtime, fixture, tmp_path, [point], "retry.nii.gz")
    assert len(fixture.sessions) == 3 and result["predictions_this_request"] == 1
    runtime.release()


def test_integrity_failure_never_loads_a_session(runtime_files, tmp_path, monkeypatch):
    runtime = interactive.InteractiveRuntime()
    monkeypatch.setattr(interactive, "CHECKPOINT_SHA256", "bad")
    with pytest.raises(RuntimeError, match="integrity"):
        predict(runtime, runtime_files, tmp_path, [])
    assert not runtime_files.sessions


def test_cpu_device_loads_and_releases_without_gpu_state(runtime_files, tmp_path, monkeypatch):
    requests = []

    def cpu_only(request):
        requests.append(request)
        return {"device": "cpu", "selected_device": "cpu", "torch": "2.14.0+cpu"}

    monkeypatch.setattr(interactive, "_device_info", cpu_only)
    monkeypatch.setenv("SCAN3D_DEVICE", "cpu")
    runtime = interactive.InteractiveRuntime()
    result = predict(runtime, runtime_files, tmp_path, [])
    assert requests == ["cpu"] and result["predictions_this_request"] == 0
    assert runtime._model_provenance["device"] == "cpu" and runtime._model_provenance["gpu_name"] is None
    runtime.release()


@pytest.mark.parametrize(
    "device, recorded",
    [
        ({"selected_device": "gpu:0", "gpu_index": 0, "gpu_name": "Test GPU", "torch": "2.14.0+rocm7.2", "hip": "7.2.4", "cuda": None}, "FAST"),
        ({"selected_device": "gpu:0", "gpu_index": 0, "gpu_name": "Test GPU", "torch": "2.14.0+cu130", "hip": None, "cuda": "13.0"}, None),
        ({"device": "cpu", "selected_device": "cpu", "torch": "2.14.0+cpu"}, None),
    ],
    ids=["hip", "cuda", "cpu"],
)
def test_the_miopen_setting_is_recorded_only_where_a_build_reads_it(runtime_files, tmp_path, monkeypatch, device, recorded):
    monkeypatch.setattr(interactive, "_device_info", lambda request: device)
    runtime = interactive.InteractiveRuntime()
    assert predict(runtime, runtime_files, tmp_path, [])["miopen_find_mode"] == recorded
    runtime.release()


def test_mask_geometry_and_original_path_protected(runtime_files, tmp_path):
    fixture, runtime = runtime_files, interactive.InteractiveRuntime()
    with pytest.raises(ValueError, match="new candidate"):
        runtime.predict(fixture.ct, fixture.mask, [], fixture.mask, lambda m: None, lambda: False, "one")
    incorrect = fixture.affine.copy()
    incorrect[0, 3] += 10
    save(fixture.mask, fixture.baseline, incorrect)
    with pytest.raises(ValueError, match="geometry"):
        predict(runtime, fixture, tmp_path, [])
    assert runtime._session is None


def test_readiness_missing_package_and_incomplete_model(runtime_files, monkeypatch):
    def missing(name):
        raise metadata.PackageNotFoundError(name)

    monkeypatch.setattr(interactive.metadata, "version", missing)
    assert not interactive.status()["installed"]
    monkeypatch.setattr(interactive.metadata, "version", lambda name: interactive.ENGINE_VERSION)
    (runtime_files.directory / "fold_0/checkpoint_final.pth").unlink()
    result = interactive.status()
    assert not result["available"] and not result["model_available"]


def test_mismatched_package_version_is_explicit(runtime_files, monkeypatch):
    monkeypatch.setattr(interactive.metadata, "version", lambda name: "2.5.0")
    result = interactive.status()
    assert result["installed"] and not result["available"]
    assert interactive.ENGINE_VERSION in result["reason"]


@pytest.mark.parametrize("initial_mode", [None, "NORMAL"])
def test_interactive_kernel_settings_restore_environment_and_torch_globals(monkeypatch, initial_mode):
    threads = [32]
    backend = SimpleNamespace(benchmark=True)
    fake = SimpleNamespace(backends=SimpleNamespace(cudnn=backend), get_num_threads=lambda: threads[0], set_num_threads=lambda n: threads.__setitem__(0, n))
    monkeypatch.setitem(sys.modules, "torch", fake)
    if initial_mode is None:
        monkeypatch.delenv("MIOPEN_FIND_MODE", raising=False)
    else:
        monkeypatch.setenv("MIOPEN_FIND_MODE", initial_mode)
    with pytest.raises(RuntimeError, match="prediction"):
        with interactive._kernel_settings():
            assert interactive.os.environ["MIOPEN_FIND_MODE"] == "FAST"
            assert backend.benchmark is False and threads[0] <= 8
            raise RuntimeError("prediction failed")
    assert interactive.os.environ.get("MIOPEN_FIND_MODE") == initial_mode
    assert backend.benchmark is True and threads[0] == 32


def test_constructor_benchmark_override_is_disabled_before_model_warmup(monkeypatch):
    backend = SimpleNamespace(benchmark=False)
    fake_torch = SimpleNamespace(backends=SimpleNamespace(cudnn=backend), device=lambda name: name)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)

    def constructor(**kwargs):
        backend.benchmark = True  # Upstream nnInteractive constructor behavior.
        return SimpleNamespace(arguments=kwargs)

    monkeypatch.setitem(sys.modules, "nnInteractive.inference.inference_session", SimpleNamespace(nnInteractiveInferenceSession=constructor))
    session = interactive._new_session({"gpu_index": 0})
    assert backend.benchmark is False
    assert session.arguments["device"] == "cuda:0" and not session.arguments["use_torch_compile"]


def test_explicit_apply_groups_nearby_points_once_then_predicts_only_new_warm_batch(batch_runtime_files, tmp_path):
    fixture, runtime = batch_runtime_files, interactive.InteractiveRuntime()
    prompts = [fixture.point("add", [7, 8, 9]), fixture.point("remove", [3, 4, 5], False)]
    first = predict(runtime, fixture, tmp_path, prompts, batches=[boundary(2)])
    session = fixture.sessions[0]
    assert first["predictions_this_request"] == len(session.calls) == 1
    assert len(session.points) == 2 and len(session.calls[0]["evidence"]) == 2
    assert first["batch_statistics"][0]["prediction_regions"] == 1
    candidate = nib.load(tmp_path / "candidate.nii.gz").get_fdata()
    assert candidate[7, 8, 9] == 1 and candidate[3, 4, 5] == 0
    prompts.append(fixture.point("third", [8, 9, 10]))
    history = [boundary(2), boundary(3, autozoom=False)]
    second = predict(runtime, fixture, tmp_path, prompts, "next.nii.gz", batches=history)
    assert second["reused_prompt_prefix"] and second["predictions_this_request"] == 1
    assert len(session.calls) == 2 and session.calls[-1]["autozoom"] is False
    assert len(session.points) == 3 and session.initial_modes == [False]
    unchanged = predict(runtime, fixture, tmp_path, prompts, "unchanged.nii.gz", batches=history)
    assert unchanged["predictions_this_request"] == 0 and unchanged["reused_prompt_prefix"]
    assert len(session.calls) == 2
    np.testing.assert_array_equal(nib.load(tmp_path / "next.nii.gz").dataobj, nib.load(tmp_path / "unchanged.nii.gz").dataobj)
    runtime.release()


def test_batch_undo_and_edit_replay_original_apply_boundaries_exactly(batch_runtime_files, tmp_path):
    fixture, runtime = batch_runtime_files, interactive.InteractiveRuntime()
    first_two = [fixture.point("one", [7, 8, 9]), fixture.point("two", [3, 4, 5], False)]
    predict(runtime, fixture, tmp_path, first_two, "first.nii.gz", batches=[boundary(2)])
    three = [*first_two, fixture.point("three", [8, 9, 10])]
    predict(runtime, fixture, tmp_path, three, "third.nii.gz", batches=[boundary(2), boundary(3)])
    session = fixture.sessions[0]
    undo = predict(runtime, fixture, tmp_path, first_two, "undo.nii.gz", batches=[boundary(2)])
    assert not undo["reused_prompt_prefix"] and undo["predictions_this_request"] == 1
    np.testing.assert_array_equal(nib.load(tmp_path / "first.nii.gz").dataobj, nib.load(tmp_path / "undo.nii.gz").dataobj)
    edited = [fixture.point("one", [9, 7, 8]), first_two[1], three[2]]
    result = predict(runtime, fixture, tmp_path, edited, "edit.nii.gz", batches=[boundary(2), boundary(3)])
    assert not result["reused_prompt_prefix"] and result["predictions_this_request"] == 2
    assert len(session.calls) == 5 and session.initial_modes == [False, False, False]
    target = nib.load(tmp_path / "edit.nii.gz").get_fdata()
    assert target[7, 8, 9] == 0 and target[9, 7, 8] == target[8, 9, 10] == 1
    np.testing.assert_array_equal(nib.load(fixture.mask).dataobj, fixture.baseline)
    runtime.release()


def test_equal_end_whole_refinement_boundary_is_reused_and_next_batch_starts_after_reset(batch_runtime_files, tmp_path):
    fixture, runtime = batch_runtime_files, interactive.InteractiveRuntime()
    prompts = [fixture.point("one", [7, 8, 9]), fixture.point("two", [3, 4, 5], False)]
    predict(runtime, fixture, tmp_path, prompts, "before-full.nii.gz", batches=[boundary(2)])
    history = [boundary(2), boundary(2, autozoom=False, full=True)]
    full = predict(runtime, fixture, tmp_path, prompts, "full.nii.gz", batches=history)
    session = fixture.sessions[0]
    assert full["reused_prompt_prefix"] and full["predictions_this_request"] == 1
    assert session.full_calls == [{"autozoom": False}] and session.initial_modes == [False, True]
    assert nib.load(tmp_path / "full.nii.gz").get_fdata()[1, 1, 1] == 1
    assert full["batch_statistics"][0]["resets_prompt_channels"]
    prompts.append(fixture.point("after-full", [8, 9, 10]))
    appended = predict(runtime, fixture, tmp_path, prompts, "appended.nii.gz", batches=[*history, boundary(3)])
    assert appended["reused_prompt_prefix"] and appended["predictions_this_request"] == 1
    assert len(session.full_calls) == 1 and len(session.calls) == 2
    assert len(session.calls[-1]["evidence"]) == 1  # Previous gestures were reset by official full refinement.
    undone = predict(runtime, fixture, tmp_path, prompts[:2], "undo-full.nii.gz", batches=[boundary(2)])
    assert not undone["reused_prompt_prefix"] and undone["predictions_this_request"] == 1
    np.testing.assert_array_equal(nib.load(tmp_path / "before-full.nii.gz").dataobj, nib.load(tmp_path / "undo-full.nii.gz").dataobj)
    runtime.release()


def test_start_empty_initializes_zero_but_measures_against_saved_source_and_replays_when_changed(batch_runtime_files, tmp_path):
    fixture, runtime = batch_runtime_files, interactive.InteractiveRuntime()
    prompts = [fixture.point("one", [7, 8, 9])]
    result = predict(runtime, fixture, tmp_path, prompts, batches=[boundary(1)], options={"start_empty": True})
    session = fixture.sessions[0]
    assert not session.initials[0].any()
    assert result["before_voxel_count"] == int(fixture.baseline.sum())
    assert result["after_voxel_count"] == 1 and result["changed_voxels"] == int(fixture.baseline.sum()) + 1
    assert nib.load(tmp_path / "candidate.nii.gz").get_fdata().sum() == 1
    changed = predict(runtime, fixture, tmp_path, prompts, "saved-start.nii.gz", batches=[boundary(1)], options={"start_empty": False})
    assert not changed["reused_prompt_prefix"] and changed["predictions_this_request"] == 1
    assert changed["after_voxel_count"] == int(fixture.baseline.sum()) + 1
    assert session.initial_modes == [False, False] and len(session.images) == 1
    np.testing.assert_array_equal(nib.load(fixture.mask).dataobj, fixture.baseline)
    runtime.release()


def test_changed_persisted_autozoom_or_apply_grouping_replays_history(batch_runtime_files, tmp_path):
    fixture, runtime = batch_runtime_files, interactive.InteractiveRuntime()
    prompts = [fixture.point("one", [7, 8, 9]), fixture.point("two", [8, 9, 10])]
    predict(runtime, fixture, tmp_path, prompts, batches=[boundary(1), boundary(2)])
    changed = predict(
        runtime, fixture, tmp_path, prompts, "changed-options.nii.gz", batches=[boundary(1, autozoom=False), boundary(2)], options={"autozoom": False}
    )
    session = fixture.sessions[0]
    assert not changed["reused_prompt_prefix"] and changed["predictions_this_request"] == 2
    assert [item["autozoom"] for item in session.calls[-2:]] == [False, True]
    grouped = predict(runtime, fixture, tmp_path, prompts, "regrouped.nii.gz", batches=[boundary(2)])
    assert not grouped["reused_prompt_prefix"] and grouped["predictions_this_request"] == 1
    assert len(session.calls[-1]["evidence"]) == 2
    assert session.initial_modes == [False, False, False]
    runtime.release()


def test_cancelled_batch_invalidates_runtime_and_retry_replays_all_apply_groups(batch_runtime_files, tmp_path):
    fixture, runtime = batch_runtime_files, interactive.InteractiveRuntime()
    prompts = [fixture.point("one", [7, 8, 9]), fixture.point("two", [8, 9, 10])]
    predict(runtime, fixture, tmp_path, prompts[:1], batches=[boundary(1)])
    stopped = [False]
    fixture.sessions[0].after_batch = lambda: stopped.__setitem__(0, True)
    with pytest.raises(interactive.SegmentationCancelled):
        predict(runtime, fixture, tmp_path, prompts, "cancelled-batch.nii.gz", batches=[boundary(1), boundary(2)], cancelled=lambda: stopped[0])
    assert runtime._session is None and not (tmp_path / "cancelled-batch.nii.gz").exists()
    result = predict(runtime, fixture, tmp_path, prompts, "retry-batch.nii.gz", batches=[boundary(1), boundary(2)])
    assert not result["reused_prompt_prefix"] and result["predictions_this_request"] == 2
    assert len(fixture.sessions) == 2 and len(fixture.sessions[1].calls) == 2
    np.testing.assert_array_equal(nib.load(fixture.mask).dataobj, fixture.baseline)
    runtime.release()


def test_runtime_materializes_all_drawing_tools_into_one_apply_without_ras_axis_confusion(batch_runtime_files, tmp_path):
    fixture, runtime = batch_runtime_files, interactive.InteractiveRuntime()
    prompts = [
        fixture.point("point", [7, 8, 9]),
        {"id": "box", "kind": "bbox", "include": True, "axis": 0, "index": 6, "vertices": [[3, 4], [6, 7]]},
        {"id": "scribble", "kind": "scribble", "include": False, "axis": 1, "index": 5, "vertices": [[2, 3], [5, 7]], "radius_mm": 1.0},
        {"id": "lasso", "kind": "lasso", "include": True, "axis": 2, "index": 6, "vertices": [[2, 2], [5, 2], [5, 5], [2, 5]]},
    ]
    result = predict(runtime, fixture, tmp_path, prompts, batches=[boundary(4)])
    session = fixture.sessions[0]
    assert result["predictions_this_request"] == 1
    assert [item["kind"] for item in session.calls[0]["evidence"]] == ["point", "bbox", "scribble", "lasso"]
    for item in session.calls[0]["evidence"][2:]:
        array, bounds = item["payload"]
        assert array.shape == tuple(b - a for a, b in bounds)
        assert array.dtype == np.uint8 and array.max() == 1 and array.size < fixture.values.size
    assert result["prompts"][0]["voxel"] == [7, 8, 9]
    runtime.release()
