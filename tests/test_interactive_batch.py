from types import SimpleNamespace

import numpy as np
import pytest

from scan3d import interactive_batch as batch


def point(identifier, voxel, include=True):
    return {"id": identifier, "kind": "point", "voxel": voxel, "include": include}


def assert_covered(prompts, plans, shape):
    shape = np.asarray(shape)
    for prompt in prompts:
        support = batch._support(prompt, shape)
        size = support[:, 1] - support[:, 0]
        covered = np.zeros(tuple(size), dtype=bool)
        for plan in plans:
            if prompt["id"] not in plan["prompt_ids"]:
                continue
            box = np.asarray(plan["input_bbox_ijk"])
            low = np.maximum(support[:, 0], box[:, 0])
            high = np.minimum(support[:, 1], box[:, 1])
            if np.all(high > low):
                covered[tuple(slice(a, b) for a, b in zip(low - support[:, 0], high - support[:, 0]))] = True
        assert covered.all(), f"Uncovered annotation voxels for {prompt['id']}"


class FakeSession:
    MAX_AUTOZOOM_FACTOR = 4

    def __init__(self, shape=(1000, 100, 3), patch=(64, 64, 64)):
        self.original_image_shape = (1, *shape)
        self.configuration_manager = SimpleNamespace(patch_size=patch)
        self.target = np.zeros(shape, dtype=np.uint8)
        self.target_buffer = self.target
        self.supports_initial_label = self.supports_zero_shot_label_refinement = True
        self.supported_interactions = {"points": True, "bbox2d": True, "scribble": True, "lasso": True}
        self.new_interaction_centers = []
        self.new_interaction_zoom_out_factors = []
        self.do_autozoom = True
        self.events = []
        self.registered = []
        self.calls = []
        self.after_prediction = None

    def set_do_autozoom(self, value):
        self.do_autozoom = value

    def _record(self, kind, geometry, include, run_prediction):
        assert run_prediction is False
        self.registered.append((kind, geometry, include))
        self.new_interaction_centers.append([0, 0, 0])
        self.new_interaction_zoom_out_factors.append(1)
        self.events.append("register")

    def add_point_interaction(self, voxel, include_interaction, run_prediction):
        self._record("point", voxel, include_interaction, run_prediction)

    def add_bbox_interaction(self, box, include_interaction, run_prediction):
        self._record("bbox", box, include_interaction, run_prediction)

    def add_scribble_interaction(self, image, include_interaction, run_prediction, interaction_bbox):
        self._record("scribble", (image, interaction_bbox), include_interaction, run_prediction)

    def add_lasso_interaction(self, image, include_interaction, run_prediction, interaction_bbox):
        self._record("lasso", (image, interaction_bbox), include_interaction, run_prediction)

    def add_initial_seg_interaction(self, mask, run_prediction):
        # Match upstream's reset-before-copy semantics to catch alias erasure.
        self.target.fill(0)
        self.target[:] = mask
        assert run_prediction is True
        self.events.append("full_refine")
        self.registered.clear()
        return [[0, int(size)] for size in self.target.shape]

    def _predict(self):
        # Reproduce the upstream last-center limitation, so tests catch a naive
        # queue-everything implementation dropping distant annotation coverage.
        assert len(self.new_interaction_centers) == len(self.new_interaction_zoom_out_factors) == 1
        center = np.asarray(self.new_interaction_centers[-1])
        scaled = np.rint(np.asarray(self.configuration_manager.patch_size) * self.new_interaction_zoom_out_factors[-1]).astype(int)
        box = np.stack((center - scaled // 2, center + scaled // 2 + scaled % 2), axis=1)
        self.calls.append({"box": box, "autozoom": self.do_autozoom})
        self.events.append("predict")
        for kind, geometry, include in self.registered:
            if kind == "point":
                voxel = np.asarray(geometry)
                if np.all(voxel >= box[:, 0]) and np.all(voxel < box[:, 1]):
                    self.target[tuple(voxel)] = int(include)
        self.new_interaction_centers = self.new_interaction_zoom_out_factors = []
        if self.after_prediction:
            self.after_prediction()
        return np.stack((np.maximum(0, box[:, 0]), np.minimum(self.target.shape, box[:, 1])), axis=1).tolist()


@pytest.fixture(autouse=True)
def engine_version(monkeypatch):
    monkeypatch.setattr(batch.metadata, "version", lambda name: "2.6.0")


def test_nearby_points_share_one_native_patch_and_distant_points_are_not_dropped():
    prompts = [point("one", [20, 25, 1]), point("two", [30, 30, 1]), point("far", [950, 25, 1])]
    session = FakeSession()
    result = batch.register_and_predict(session, prompts, lambda message: None, lambda: False, autozoom=False)
    assert result["clusters"] == result["prediction_calls"] == 2
    assert session.events == ["register"] * 3 + ["predict"] * 2
    assert all(session.target[tuple(item["voxel"])] for item in prompts)
    assert all(not call["autozoom"] for call in session.calls)
    assert session.do_autozoom is True
    assert not session.new_interaction_centers and not session.new_interaction_zoom_out_factors
    assert_covered(prompts, result["plans"], session.target.shape)
    assert result["plans"][0]["zoom"] == 1


@pytest.mark.parametrize("patch,shape,voxel", [
    ([1, 1, 1], [1, 1, 1], [0, 0, 0]),
    ([63, 65, 67], [120, 130, 140], [0, 129, 139]),
    ([64, 64, 64], [3, 4, 5], [2, 0, 4]),
    ([192, 192, 192], [512, 512, 729], [511, 511, 728]),
])
def test_small_fov_edges_and_odd_patch_sizes_preserve_entire_support(patch, shape, voxel):
    prompts = [point("edge", voxel)]
    plans = batch.plan_batches(prompts, patch, 4, shape)
    assert len(plans) == 1
    assert_covered(prompts, plans, shape)
    assert 1 <= plans[0]["zoom"] <= 4


def test_large_drawing_uses_zoom_but_is_tiled_when_support_exceeds_cap():
    shape, patch = [1200, 130, 1], [64, 64, 64]
    prompts = [{"id": "wide", "kind": "bbox", "include": True,
                "bbox_ijk": [[0, 1199], [4, 124], [0, 1]]}]
    plans = batch.plan_batches(prompts, patch, 4, shape)
    assert len(plans) > 1
    assert all(1 <= plan["zoom"] <= 4 for plan in plans)
    assert all(np.all(np.asarray(plan["scaled_patch_size"]) <= np.asarray(patch) * 4) for plan in plans)
    assert_covered(prompts, plans, shape)
    medium = [{"id": "medium", "kind": "bbox", "include": True,
               "bbox_ijk": [[20, 220], [10, 100], [0, 1]]}]
    medium_plans = batch.plan_batches(medium, patch, 4, shape)
    assert len(medium_plans) == 1 and medium_plans[0]["zoom"] > 1
    assert_covered(medium, medium_plans, shape)


def test_zoom_cap_can_reduce_context_without_losing_annotation_voxels():
    prompts = [{"id": "almost-max", "kind": "bbox", "include": True,
                "bbox_ijk": [[0, 250], [0, 1], [2, 3]]}]
    plans = batch.plan_batches(prompts, [64, 64, 64], 4, [1000, 10, 10])
    assert len(plans) == 1 and plans[0]["context_limited_by_zoom_cap"]
    assert_covered(prompts, plans, [1000, 10, 10])


def test_drawing_crops_are_registered_without_full_volume_buffers():
    scribble = np.zeros((10, 12, 1), np.uint8)
    scribble[2:8, 4, 0] = 1
    lasso = np.ones((12, 8, 1), np.uint8)
    prompts = [
        {"id": "scribble", "kind": "scribble", "include": True, "image": scribble,
         "interaction_bbox": [[10, 20], [20, 32], [1, 2]]},
        {"id": "lasso", "kind": "lasso", "include": False, "image": lasso,
         "interaction_bbox": [[22, 34], [22, 30], [1, 2]]},
    ]
    session = FakeSession()
    result = batch.register_and_predict(session, prompts, lambda m: None, lambda: False)
    assert result["prediction_calls"] == 1
    assert session.registered[0][1][0] is scribble
    assert session.registered[1][1][0] is lasso
    assert_covered(prompts, result["plans"], session.target.shape)


def test_polarities_registration_order_and_empty_batch():
    session = FakeSession()
    session.target[20, 20, 1] = 1
    result = batch.register_and_predict(session, [point("remove", [20, 20, 1], False),
                                                 point("add", [25, 20, 1])], lambda m: None, lambda: False)
    assert result["prediction_calls"] == 1
    assert session.target[20, 20, 1] == 0 and session.target[25, 20, 1] == 1
    result = batch.register_and_predict(session, [], lambda m: None, lambda: False)
    assert result["prediction_calls"] == 0 and result["changed_bbox_ijk"] is None


def test_cancellation_never_leaves_last_center_queue_or_changed_autozoom():
    session = FakeSession()
    stopped = [False]
    session.after_prediction = lambda: stopped.__setitem__(0, True)
    prompts = [point("one", [20, 20, 1]), point("far", [900, 20, 1])]
    with pytest.raises(batch.SegmentationCancelled):
        batch.register_and_predict(session, prompts, lambda m: None, lambda: stopped[0], autozoom=False)
    assert len(session.calls) == 1 and session.do_autozoom is True
    assert not session.new_interaction_centers and not session.new_interaction_zoom_out_factors


def test_version_and_model_capability_guards(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(batch.metadata, "version", lambda name: "2.7.0")
    with pytest.raises(RuntimeError, match="pinned"):
        batch.register_and_predict(session, [point("one", [20, 20, 1])], lambda m: None, lambda: False)
    monkeypatch.setattr(batch.metadata, "version", lambda name: "2.6.0")
    session.supported_interactions["points"] = False
    with pytest.raises(ValueError, match="does not support"):
        batch.register_and_predict(session, [point("one", [20, 20, 1])], lambda m: None, lambda: False)
    with pytest.raises(ValueError, match="2D box"):
        batch.plan_batches([{"id": "3d", "kind": "bbox", "include": True,
                             "bbox_ijk": [[1, 10], [1, 10], [1, 10]]}], [64] * 3, 4, [100] * 3)


def test_support_understatement_nonbinary_and_out_of_bounds_inputs_are_rejected():
    prompt = point("one", [10, 10, 10])
    prompt["support_bbox_ijk"] = [[10, 11], [10, 11], [10, 11]]
    with pytest.raises(ValueError, match="does not cover"):
        batch.plan_batches([prompt], [64] * 3, 4, [100] * 3)
    with pytest.raises(ValueError, match="outside"):
        batch.plan_batches([point("bad", [-1, 0, 0])], [64] * 3, 4, [100] * 3)
    with pytest.raises(ValueError, match="binary"):
        batch.plan_batches([{"id": "bad", "kind": "scribble", "include": True,
                             "interaction_bbox": [[0, 2], [0, 2], [0, 1]],
                             "image": np.full((2, 2, 1), 2, np.uint8)}], [64] * 3, 4, [100] * 3)


def test_random_edge_and_tiling_coverage_is_deterministic():
    rng = np.random.default_rng(841)
    for _ in range(30):
        shape = np.array([int(rng.integers(20, 450)), int(rng.integers(20, 85)), 1])
        prompts = [point(str(index), [int(rng.integers(shape[0])), int(rng.integers(shape[1])), 0])
                   for index in range(8)]
        low = [int(rng.integers(max(1, int(size) // 2))) for size in shape[:2]]
        high = [int(rng.integers(start + 1, int(size) + 1)) for start, size in zip(low, shape[:2])]
        prompts.append({"id": "drawing", "kind": "bbox", "include": True,
                        "bbox_ijk": [[low[0], high[0]], [low[1], high[1]], [0, 1]]})
        plans = batch.plan_batches(prompts, [31, 33, 35], 2.5, shape)
        assert plans == batch.plan_batches(prompts, [31, 33, 35], 2.5, shape)
        assert_covered(prompts, plans, shape)


def test_whole_mask_refinement_copies_target_before_official_reset_and_reports_boundary():
    session = FakeSession()
    session.target[20:30, 20:30, 1] = 1
    before = session.target.copy()
    result = batch.refine_whole_mask(session, lambda m: None, lambda: False, autozoom=False)
    np.testing.assert_array_equal(session.target, before)
    assert result["prediction_regions"] == 1 and result["resets_prompt_channels"]
    assert session.events == ["full_refine"] and session.do_autozoom is True


def test_whole_refinement_requires_supported_nonempty_binary_candidate():
    session = FakeSession()
    with pytest.raises(ValueError, match="nonempty"):
        batch.refine_whole_mask(session, lambda m: None, lambda: False)
    session.target[20, 20, 1] = 2
    with pytest.raises(ValueError, match="binary"):
        batch.refine_whole_mask(session, lambda m: None, lambda: False)
    session.supports_zero_shot_label_refinement = False
    with pytest.raises(ValueError, match="does not support"):
        batch.refine_whole_mask(session, lambda m: None, lambda: False)
