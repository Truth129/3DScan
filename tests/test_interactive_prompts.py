import numpy as np
import pytest

from scan3d.interactive_prompts import materialize_prompt, normalize_options, normalize_prompts


def normalized(prompt, affine=None, shape=(30, 32, 34)):
    return normalize_prompts([prompt], np.eye(4) if affine is None else affine, shape)[0]


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_native_box_has_one_slice_extent_and_native_axes(axis):
    prompt = normalized(dict(id="box", kind="bbox", include=False, axis=axis, index=4, vertices=[[3, 5], [7, 9]]))
    result = materialize_prompt(prompt, np.eye(4), (30, 32, 34))
    assert result["bbox_ijk"][axis] == [4, 5]
    assert [bounds for i, bounds in enumerate(result["bbox_ijk"]) if i != axis] == [[3, 8], [5, 10]]
    assert result["include"] is False and result["id"] == "box"


@pytest.mark.parametrize("axis", [0, 1, 2])
def test_lasso_fills_only_native_plane_including_boundary(axis):
    prompt = normalized(dict(id="loop", kind="lasso", include=True, axis=axis, index=4, vertices=[[2, 2], [6, 2], [6, 6], [2, 6]]))
    result = materialize_prompt(prompt, np.eye(4), (30, 32, 34))
    assert result["image"].dtype == np.uint8 and result["image"].shape[axis] == 1
    assert result["image"].sum() == 25
    assert list(result["image"].shape) == [b - a for a, b in result["interaction_bbox"]]


def test_physical_scribble_uses_full_affine_metric_and_cropped_binary_image():
    affine = np.eye(4)
    affine[:3, :3] = [[-2, 1, 0], [0, 3, 0], [0, 0, 4]]
    prompt = normalized(dict(id="stroke", kind="scribble", include=True, axis=2, index=10, vertices=[[5, 8], [15, 8]], radius_mm=3), affine)
    result = materialize_prompt(prompt, affine, (30, 32, 34))
    origin = np.asarray([a for a, b in result["interaction_bbox"]])
    occupied = np.argwhere(result["image"]) + origin
    world = occupied @ affine[:3, :3].T
    first = affine[:3, :3] @ [5, 8, 10]
    last = affine[:3, :3] @ [15, 8, 10]
    segment = last - first
    t = np.clip((world - first) @ segment / (segment @ segment), 0, 1)
    assert np.all(np.linalg.norm(world - first - t[:, None] * segment, axis=1) <= 3 + 1e-8)
    assert result["image"].shape[2] == 1 and result["image"].size < 30 * 32
    assert len(occupied) > 10


def test_legacy_point_normalizes_and_materializes_with_kernel_support():
    prompt = normalized(dict(id="legacy", include=True, ras=[3, 4, 5]))
    assert prompt["kind"] == "point"
    prompt["voxel"] = [3, 4, 5]  # Runtime provenance can include derived indices.
    result = materialize_prompt(prompt, np.eye(4), (30, 32, 34))
    assert result["voxel"] == [3, 4, 5]
    assert result["support_bbox_ijk"] == [[0, 8], [0, 9], [1, 10]]
    from scan3d.interactive_batch import plan_batches

    plans = plan_batches([result], (192, 192, 192), 4, (30, 32, 34))
    assert plans[0]["prompt_ids"] == ["legacy"]


@pytest.mark.parametrize("changes", [{"axis": True}, {"vertices": [[-1, 2], [4, 5]]}, {"vertices": [[2, 2], [2, 4]]}])
def test_invalid_native_box_is_rejected(changes):
    prompt = dict(id="box", kind="bbox", include=True, axis=2, index=4, vertices=[[2, 2], [6, 6]])
    with pytest.raises(ValueError):
        normalized({**prompt, **changes})


def test_vertex_budgets_options_and_no_finite_coordinate_escape():
    stroke = dict(id="s", kind="scribble", include=True, axis=2, index=4, vertices=[[2, 2], [6, 6]])
    with pytest.raises(ValueError, match="finite"):
        normalized({**stroke, "vertices": [[2, 2], [float("nan"), 3]]})
    with pytest.raises(ValueError, match="8192"):
        normalize_prompts([{**stroke, "id": str(i), "vertices": [[2, 2]] * 2048} for i in range(5)], np.eye(4), (30, 32, 34))
    assert normalize_options({"autozoom": False}) == {"autozoom": False, "start_empty": False}
    with pytest.raises(ValueError):
        normalize_options({"autozoom": 1})
