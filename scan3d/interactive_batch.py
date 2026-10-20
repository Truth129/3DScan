"""Coverage-preserving batched prompts for the pinned nnInteractive 2.6 API.

Upstream queues prompt centers but predicts only the last queued center. This
adapter registers all prompt evidence first, then supplies one explicitly
planned center at a time. Nearby prompts share a native patch; distant prompts
retain separate patches, and oversized supports are tiled rather than lost at
the model's maximum AutoZoom factor. Batch boundaries belong in replay history:
batch inference is deliberately distinct from predicting after every gesture.
"""

from __future__ import annotations

from importlib import metadata
from itertools import product
import math

import numpy as np

from .segmentation import SegmentationCancelled


ADAPTER_VERSION = 1
SUPPORTED_ENGINE_VERSION = "2.6.0"


def _integers(values, name, length=3):
    array = np.asarray(values)
    if (array.shape != (length,) or array.dtype.kind not in "iuf"
            or not np.isfinite(array).all() or not np.equal(array, np.floor(array)).all()):
        raise ValueError(f"{name} must contain {length} finite integers")
    return array.astype(np.int64)


def _bbox(value, shape, name="Prompt support"):
    array = np.asarray(value)
    if (array.shape != (3, 2) or array.dtype.kind not in "iuf"
            or not np.isfinite(array).all() or not np.equal(array, np.floor(array)).all()):
        raise ValueError(f"{name} must contain three integer half-open intervals")
    array = array.astype(np.int64)
    if np.any(array[:, 0] < 0) or np.any(array[:, 1] > shape) or np.any(array[:, 1] <= array[:, 0]):
        raise ValueError(f"{name} lies outside the reference CT or has an empty interval")
    return array


def _union(boxes):
    return np.stack((np.min([b[:, 0] for b in boxes], axis=0),
                     np.max([b[:, 1] for b in boxes], axis=0)), axis=1)


def _context(box, margin, shape):
    return np.stack((np.maximum(0, box[:, 0] - margin),
                     np.minimum(shape, box[:, 1] + margin)), axis=1)


def _support(prompt, shape):
    kind = prompt.get("kind")
    if kind == "point":
        voxel = _integers(prompt.get("voxel"), "Point voxel")
        if np.any(voxel < 0) or np.any(voxel >= shape):
            raise ValueError("Point voxel lies outside the reference CT")
        # The pinned official model's point kernel has radius four voxels.
        actual = np.stack((np.maximum(0, voxel - 4), np.minimum(shape, voxel + 5)), axis=1)
    elif kind == "bbox":
        actual = _bbox(prompt.get("bbox_ijk"), shape, "Box prompt")
        if not np.any(actual[:, 1] - actual[:, 0] == 1):
            raise ValueError("The pinned nnInteractive model supports only 2D box prompts")
    elif kind in {"scribble", "lasso"}:
        crop_box = _bbox(prompt.get("interaction_bbox"), shape, "Drawing crop")
        image = np.asarray(prompt.get("image"))
        if tuple(image.shape) != tuple(crop_box[:, 1] - crop_box[:, 0]):
            raise ValueError("Drawing crop shape does not match its half-open intervals")
        if image.dtype.kind not in "bui" or image.min() < 0 or image.max() > 1:
            raise ValueError("Drawing crops must contain binary values")
        if not np.any(crop_box[:, 1] - crop_box[:, 0] == 1):
            raise ValueError("Drawing prompts must lie on one native CT slice")
        occupied = [np.flatnonzero(np.any(image, axis=tuple(j for j in range(3) if j != axis)))
                    for axis in range(3)]
        if any(not len(indices) for indices in occupied):
            raise ValueError("Drawing prompt is empty")
        actual = np.array([[indices[0], indices[-1] + 1] for indices in occupied])
        actual += crop_box[:, 0, None]
    else:
        raise ValueError("Unknown nnInteractive prompt kind")
    if "support_bbox_ijk" in prompt:
        supplied = _bbox(prompt["support_bbox_ijk"], shape)
        if np.any(supplied[:, 0] > actual[:, 0]) or np.any(supplied[:, 1] < actual[:, 1]):
            raise ValueError("Prompt support does not cover all of its annotation voxels")
        return supplied
    return actual


def _plan(box, prompt_ids, patch, max_zoom, shape, margin):
    context = _context(box, margin, shape)
    desired = context[:, 1] - context[:, 0]
    zoom = min(float(max_zoom), max(1.0, float(np.max(desired / patch))))
    scaled = np.array([round(int(size) * zoom) for size in patch], dtype=np.int64)
    if np.any(scaled < box[:, 1] - box[:, 0]):
        raise ValueError("Planned patch cannot cover its prompt support")
    # Clamping the center to this admissible interval guarantees exact support
    # coverage even where image-edge context or the zoom cap is asymmetric.
    center = (context[:, 0] + context[:, 1]) // 2
    center_low = box[:, 1] - (scaled - scaled // 2)
    center_high = box[:, 0] + scaled // 2
    center = np.minimum(np.maximum(center, center_low), center_high)
    input_box = np.stack((center - scaled // 2, center + scaled // 2 + scaled % 2), axis=1)
    return {"prompt_ids": list(prompt_ids), "support_bbox_ijk": box.tolist(),
            "context_bbox_ijk": context.tolist(), "input_bbox_ijk": input_box.tolist(),
            "center_ijk": center.tolist(), "zoom": zoom, "scaled_patch_size": scaled.tolist(),
            "context_limited_by_zoom_cap": bool(np.any(input_box[:, 0] > context[:, 0])
                                                 or np.any(input_box[:, 1] < context[:, 1]))}


def plan_batches(normalized_prompts, patch_size, max_zoom, image_shape):
    """Return deterministic patches whose union covers every prompt support.

    Merge nearby supports only when their contextual union fits one native
    patch. An already large drawing can use a larger coarse patch. Supports
    exceeding the maximum coarse patch receive overlapping contextual tiles.
    No annotation is dropped merely because it is far from the final gesture.
    """
    patch = _integers(patch_size, "Model patch size")
    shape = _integers(image_shape, "Reference image shape")
    if np.any(patch < 1) or np.any(shape < 1):
        raise ValueError("Image and model patch dimensions must be positive")
    if not isinstance(max_zoom, (int, float)) or not math.isfinite(max_zoom) or max_zoom < 1:
        raise ValueError("Maximum zoom must be finite and at least one")
    maximum = np.array([round(int(size) * max_zoom) for size in patch], dtype=np.int64)
    margin = np.maximum(0, patch // 6)
    capacity = np.maximum(1, maximum - 2 * margin)
    if not isinstance(normalized_prompts, list):
        raise ValueError("Normalized prompts must be a list")
    clusters, large, seen = [], [], set()
    for ordinal, prompt in enumerate(normalized_prompts):
        if not isinstance(prompt, dict) or not isinstance(prompt.get("id"), str) or not prompt["id"] or prompt["id"] in seen:
            raise ValueError("Prompt IDs must be unique nonempty strings")
        seen.add(prompt["id"])
        box = _support(prompt, shape)
        if np.all(_context(box, margin, shape)[:, 1] - _context(box, margin, shape)[:, 0] <= patch):
            candidates = []
            for index, cluster in enumerate(clusters):
                merged = _union([cluster["box"], box])
                contextual = _context(merged, margin, shape)
                size = contextual[:, 1] - contextual[:, 0]
                if np.all(size <= patch):
                    candidates.append((int(np.prod(size)), index, merged))
            if candidates:
                _, index, merged = min(candidates, key=lambda candidate: candidate[:2])
                clusters[index]["box"] = merged
                clusters[index]["ids"].append(prompt["id"])
            else:
                clusters.append({"box": box, "ids": [prompt["id"]], "ordinal": ordinal})
        else:
            large.append({"box": box, "ids": [prompt["id"]], "ordinal": ordinal})
    pieces = list(clusters)
    for cluster in large:
        box = cluster["box"]
        if np.all(box[:, 1] - box[:, 0] <= maximum):
            pieces.append(cluster)
            continue
        intervals = []
        for axis in range(3):
            intervals.append([(start, min(int(box[axis, 1]), start + int(capacity[axis])))
                              for start in range(int(box[axis, 0]), int(box[axis, 1]), int(capacity[axis]))])
        for tile in product(*intervals):
            pieces.append({**cluster, "box": np.asarray(tile, dtype=np.int64)})
    if len(pieces) > 4096:
        raise ValueError("Prompt geometry requires too many inference regions")
    pieces.sort(key=lambda piece: (piece["ordinal"], tuple(piece["box"].flatten())))
    return [_plan(piece["box"], piece["ids"], patch, max_zoom, shape, margin) for piece in pieces]


def _check_cancelled(cancelled):
    if cancelled():
        raise SegmentationCancelled("Batched nnInteractive correction was cancelled")


def _register(session, prompt):
    kind, include = prompt["kind"], prompt.get("include")
    if not isinstance(include, bool):
        raise ValueError("Prompt polarity must be an explicit boolean")
    capability = {"point": "points", "bbox": "bbox2d", "scribble": "scribble", "lasso": "lasso"}[kind]
    if not session.supported_interactions.get(capability):
        raise ValueError(f"The loaded checkpoint does not support {kind} prompts")
    if kind == "point":
        session.add_point_interaction(prompt["voxel"], include_interaction=include, run_prediction=False)
    elif kind == "bbox":
        session.add_bbox_interaction(prompt["bbox_ijk"], include_interaction=include, run_prediction=False)
    else:
        method = session.add_scribble_interaction if kind == "scribble" else session.add_lasso_interaction
        method(prompt["image"], include_interaction=include, run_prediction=False,
               interaction_bbox=prompt["interaction_bbox"])


def register_and_predict(session, prompts, progress, cancelled, *, autozoom=True):
    """Register evidence once and run each explicitly covering region once.

    Uses private queue/prediction hooks only for the exactly pinned engine.
    Callers invalidate their runtime on cancellation or error; partial results
    must never be published. The returned count measures prediction calls, not
    the internal network forwards used by AutoZoom and detailed refinement.
    """
    if metadata.version("nnInteractive") != SUPPORTED_ENGINE_VERSION:
        raise RuntimeError("Batched inference requires the pinned nnInteractive 2.6.0 adapter")
    if not isinstance(autozoom, bool):
        raise ValueError("AutoZoom must be an explicit boolean")
    plans = plan_batches(prompts, session.configuration_manager.patch_size,
                         session.MAX_AUTOZOOM_FACTOR, session.original_image_shape[1:])
    _check_cancelled(cancelled)
    previous_autozoom = session.do_autozoom
    prediction_calls, changed = 0, []
    try:
        session.set_do_autozoom(autozoom)
        for prompt in prompts:
            _check_cancelled(cancelled)
            _register(session, prompt)
            _check_cancelled(cancelled)
        # Every prompt is encoded above. Discard upstream's last-center-only
        # queue and submit exactly one known covering center per model call.
        session.new_interaction_centers = []
        session.new_interaction_zoom_out_factors = []
        for index, plan in enumerate(plans):
            _check_cancelled(cancelled)
            progress(f"Applying correction region {index + 1} of {len(plans)}")
            session.new_interaction_centers = [plan["center_ijk"]]
            session.new_interaction_zoom_out_factors = [plan["zoom"]]
            box = session._predict()
            prediction_calls += 1
            _check_cancelled(cancelled)
            if box is not None:
                changed.append(np.asarray(box, dtype=np.int64))
        return {"adapter_version": ADAPTER_VERSION, "clusters": len(plans),
                "prediction_calls": prediction_calls, "prediction_regions": prediction_calls, "plans": plans,
                "changed_bbox_ijk": _union(changed).tolist() if changed else None}
    finally:
        session.new_interaction_centers = []
        session.new_interaction_zoom_out_factors = []
        session.set_do_autozoom(previous_autozoom)


def refine_whole_mask(session, progress, cancelled, *, autozoom=True):
    """Explicit official whole-label refinement, resetting prompt channels.

    Copying is essential: the official API clears its target during reset, so
    passing its own target buffer as the initial label would erase that label.
    Replay history must record this reset boundary separately from gesture
    batches. Callers invalidate the warm runtime if the operation fails.
    """
    if metadata.version("nnInteractive") != SUPPORTED_ENGINE_VERSION:
        raise RuntimeError("Whole-mask refinement requires the pinned nnInteractive 2.6.0 adapter")
    if not isinstance(autozoom, bool):
        raise ValueError("AutoZoom must be an explicit boolean")
    if not (getattr(session, "supports_initial_label", False)
            and getattr(session, "supports_zero_shot_label_refinement", False)):
        raise ValueError("The loaded checkpoint does not support whole-mask refinement")
    _check_cancelled(cancelled)
    current = np.asarray(session.target_buffer)
    if (current.shape != tuple(session.original_image_shape[1:]) or current.dtype.kind not in "bui"
            or current.min() < 0 or current.max() > 1):
        raise ValueError("Whole-mask refinement requires a binary mask on the reference CT grid")
    if not np.any(current):
        raise ValueError("Whole-mask refinement requires a nonempty candidate mask")
    initial = current.astype(np.uint8, copy=True)
    previous_autozoom = session.do_autozoom
    try:
        session.set_do_autozoom(autozoom)
        progress("Refining the whole candidate mask")
        _check_cancelled(cancelled)
        box = session.add_initial_seg_interaction(initial, run_prediction=True)
        _check_cancelled(cancelled)
        return {"adapter_version": ADAPTER_VERSION, "prediction_regions": 1, "prediction_calls": 1,
                "mode": "whole_mask", "resets_prompt_channels": True,
                "changed_bbox_ijk": np.asarray(box, dtype=np.int64).tolist() if box is not None else None}
    finally:
        session.new_interaction_centers = []
        session.new_interaction_zoom_out_factors = []
        session.set_do_autozoom(previous_autozoom)
