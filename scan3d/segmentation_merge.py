"""Merge task outputs by exact anatomical identity without modifying mask data.

Inference IDs retain their task prefix for provenance. Matching ignores only
case, underscores and whitespace, so similarly named structures and left/right
counterparts remain separate. Previous revisions and their metadata are inputs,
never mutable working state.
"""

from __future__ import annotations

from copy import deepcopy

from .relationships import INDEPENDENT, normalize_relationship


REPLACEMENT_POLICY = "same_task_or_exact_name_v1"


def _anatomical_name(entry):
    task, structure_id = entry.get("task"), entry["id"]
    # Native IDs contain the canonical model label; custom IDs are random and
    # their editable name is the anatomical identity supplied by the reviewer.
    prefix = f"{task}:" if task and task != "custom" else None
    if prefix and structure_id.startswith(prefix):
        name = structure_id[len(prefix):]
    else:
        name = entry.get("name", "")
    if not isinstance(name, str):
        raise ValueError("Segments must have an anatomical name")
    name = " ".join(name.replace("_", " ").casefold().split())
    if not name:
        raise ValueError("Segments must have an anatomical name")
    return name


def _ids(entries):
    values = [entry.get("id") for entry in entries]
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("Segments must have a nonempty structure ID")
    if len(values) != len(set(values)):
        raise ValueError("Segmentation replacement requires unique structure IDs")
    return set(values)


def _remap_relationship(value, replacements):
    if not isinstance(value, dict):
        raise ValueError("Invalid segment relationship")
    result = deepcopy(value)
    parent = result.get("parent_id")
    if parent is not None:
        if not isinstance(parent, str):
            raise ValueError("Invalid segment relationship parent")
        result["parent_id"] = replacements.get(parent, parent)
    peers = result.get("exclusive_with", [])
    if (not isinstance(peers, list) or any(not isinstance(peer, str) for peer in peers)
            or len(peers) != len(set(peers))):
        raise ValueError("Invalid segment relationship exclusive siblings")
    if "exclusive_with" in result:
        # Several old aliases can resolve to the same replacement. That creates
        # one direct peer link, rather than duplicate references to that peer.
        result["exclusive_with"] = sorted({replacements.get(peer, peer) for peer in peers})
    return result


def merge_task_outputs(previous, incoming, selected_tasks, *, reference_image_id=None):
    """Return independent merged metadata and an auditable replacement record.

    A selected task replaces its prior outputs wholesale, including newly empty
    labels. Other tasks lose only labels matching a nonempty incoming structure.
    Explicit relationships follow the replacement IDs; conflicting or invalid
    graphs fail before a revision is built. No overlap-based voxel edits occur.
    """
    from .image_assets import bind_structures

    scopes = {entry["reference_image_id"] for entry in previous + incoming
              if entry.get("reference_image_id") is not None}
    if reference_image_id is not None:
        scopes.add(reference_image_id)
    if len(scopes) > 1:
        raise ValueError("Segmentation replacement cannot combine different reference images")
    reference_image_id = next(iter(scopes), None)
    if reference_image_id is not None:
        previous = bind_structures(previous, reference_image_id)
        incoming = bind_structures(incoming, reference_image_id)
    _ids(previous)
    incoming_ids = _ids(incoming)
    by_id = {entry["id"]: entry for entry in incoming}
    tasks = set(selected_tasks)
    by_name = {}
    for entry in incoming:
        key = _anatomical_name(entry)
        if key in by_name:
            raise ValueError(f"Incoming segmentation has ambiguous duplicate anatomical name: {key}")
        by_name[key] = entry

    replacements, removed, candidates = {}, [], {}
    for entry in incoming:
        value = entry.get("relationship")
        candidates[entry["id"]] = [] if value is None else [value]
    preserved = []
    for entry in previous:
        target = by_name.get(_anatomical_name(entry))
        if entry["id"] in incoming_ids:
            # An exact ID remains the same logical structure on a task rerun.
            target = by_id[entry["id"]]
        replace = entry.get("task") in tasks or target is not None
        if replace:
            removed.append(entry["id"])
            if target is not None:
                replacements[entry["id"]] = target["id"]
                if entry.get("relationship") is not None:
                    candidates[target["id"]].append(entry["relationship"])
        else:
            preserved.append(deepcopy(entry))

    outputs = deepcopy(incoming)
    merged = preserved + outputs
    _ids(merged)
    for entry in preserved:
        if entry.get("relationship") is not None:
            entry["relationship"] = _remap_relationship(entry["relationship"], replacements)
    for entry in outputs:
        choices = [_remap_relationship(value, replacements) for value in candidates[entry["id"]]]
        candidates[entry["id"]] = choices
        if choices:
            entry["relationship"] = choices[0]

    # Populate every candidate first, so cycle/ancestry validation sees the full
    # inherited graph rather than an order-dependent partially rebuilt graph.
    graph = [{**entry, "relationship": entry.get("relationship") or INDEPENDENT} for entry in merged]
    normalized = {}
    for entry in merged:
        try:
            choices = candidates.get(entry["id"]) or [entry.get("relationship")]
            values = [normalize_relationship(value, graph, entry["id"]) for value in choices]
            if any(value != values[0] for value in values[1:]):
                raise ValueError("Matching segments have conflicting explicit relationships")
            if entry.get("relationship") is not None:
                normalized[entry["id"]] = values[0]
        except ValueError as exc:
            raise ValueError(
                f"Segmentation replacement would invalidate the relationship for {entry.get('name', entry['id'])}: "
                f"{exc}. The prior revision was kept; adjust the relationship before retrying."
            ) from exc
    for entry in merged:
        if entry["id"] in normalized:
            entry["relationship"] = normalized[entry["id"]]

    operation = {
        "replacement_policy": REPLACEMENT_POLICY,
        "replaced_structure_ids": removed,
        "replacement_id_map": replacements,
    }
    if reference_image_id is not None:
        operation["reference_image_id"] = reference_image_id
    return merged, operation
