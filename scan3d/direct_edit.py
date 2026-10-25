"""Deterministic native-slice paint/erase, independent of model inference."""

from __future__ import annotations

from pathlib import Path
import time

import numpy as np

from . import __version__, imaging
from .interactive_prompts import materialize_prompt, normalize_options, normalize_prompts


def apply_strokes(ct_path, mask_path, prompts, output_path, *, options=None,
                  progress=lambda _: None, cancelled=lambda: False):
    """Replay exact ordered strokes from the immutable source mask.

    A physical-radius stroke changes only voxel centres on its one native CT
    slice. The shared rasterizer handles anisotropy, reflection, obliquity and
    shear. Each stroke allocates its cropped plane, never another full volume.
    Return the validated arrays so candidate summaries need no second decode.
    """
    def check_cancel():
        if cancelled():
            raise InterruptedError("Direct editing cancelled")

    check_cancel()
    ct_path, mask_path, output_path = map(Path, (ct_path, mask_path, output_path))
    if output_path.resolve() in (ct_path.resolve(), mask_path.resolve()):
        raise ValueError("Direct edits require a new mask output")
    image, affine = imaging._image(ct_path)
    strokes = normalize_prompts(prompts, affine, image.shape)
    if not strokes or any(stroke["kind"] not in {"scribble", "scissors"} for stroke in strokes):
        raise ValueError("Direct editing supports paint and erase strokes or 3D scissors only")
    settings = normalize_options(options)
    started = time.monotonic()
    before, _, _ = imaging._binary(mask_path, image, affine)
    after = np.zeros_like(before) if settings["start_empty"] else before.copy()
    operations = []
    for index, stroke in enumerate(strokes, 1):
        check_cancel()
        if stroke["kind"] == "scissors":
            from .scissors import erase_projected
            removed = erase_projected(after, affine, stroke, cancelled=cancelled,
                progress=lambda fraction: progress(f"Applying 3D {stroke['shape']} cut {index}/{len(strokes)}: {round(fraction * 100)}%"))
            operations.append(dict(id=stroke["id"], kind="scissors", shape=stroke["shape"],
                                   operation=stroke["operation"], removed_voxels=removed, unlimited_depth=True))
            continue
        progress(f"Applying {'paint' if stroke['include'] else 'erase'} stroke {index}/{len(strokes)}")
        materialized = materialize_prompt(stroke, affine, image.shape)
        bounds = tuple(slice(low, high) for low, high in materialized["interaction_bbox"])
        plane = after[bounds]
        # The cropped interaction image has one voxel of depth along its axis.
        # Setting selected centres directly never smooths or fills a neighbour.
        plane[materialized["image"] != 0] = stroke["include"]
    check_cancel()
    imaging._save_mask(after, image, output_path)
    check_cancel()
    cuts = sum(stroke["kind"] == "scissors" for stroke in strokes)
    provenance = dict(engine="direct-edit" if cuts else "direct-paint", version=__version__, device="cpu",
                      stroke_count=len(strokes) - cuts, cut_count=cuts, operation_count=len(strokes), operations=operations,
                      replay="ordered_from_immutable_source",
                      scope="native_brushes_and_projected_3d_cuts" if cuts else "native_slice_voxel_centres", radius_units="mm",
                      smoothing=False, cavity_filling=False,
                      runtime_seconds=round(time.monotonic() - started, 6))
    return provenance, (before, after, image, affine)
