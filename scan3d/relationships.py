"""Explicit label relationships. Display groups and names never imply exclusivity."""

from __future__ import annotations


INDEPENDENT = {"kind": "independent", "exclusive_with": []}
# Exact IDs emitted by the pinned TotalSegmentator `total` adapter. These are
# suggestions requiring the reviewer's explicit selection, never automatic edits.
LUNG_LOBES = frozenset(f"total:{name}" for name in (
    "lung_upper_lobe_left", "lung_lower_lobe_left", "lung_upper_lobe_right",
    "lung_middle_lobe_right", "lung_lower_lobe_right",
))


def _parents(structures, target_id, relationship):
    parents = {entry["id"]: entry.get("relationship", {}).get("parent_id") for entry in structures}
    parents[target_id] = relationship.get("parent_id")
    return parents


def ancestors(structure_id, parents):
    result = set()
    parent = parents.get(structure_id)
    while parent:
        if parent == structure_id or parent in result:
            raise ValueError("Segment parent relationships must not contain a cycle")
        result.add(parent)
        parent = parents.get(parent)
    return result


def normalize_relationship(value, structures, target_id):
    value = INDEPENDENT if value is None else value
    if not isinstance(value, dict) or set(value) - {"kind", "parent_id", "exclusive_with"}:
        raise ValueError("Invalid segment relationship")
    kind, parent, peers = value.get("kind", "independent"), value.get("parent_id"), value.get("exclusive_with", [])
    if kind not in ("independent", "subregion", "subdivision"):
        raise ValueError("Unknown segment relationship kind")
    ids = {entry["id"] for entry in structures}
    if parent is not None and (not isinstance(parent, str) or parent not in ids or parent == target_id):
        raise ValueError("The parent must be another segment in this revision")
    if (not isinstance(peers, list) or len(peers) > 128 or any(not isinstance(peer, str) for peer in peers)
            or len(peers) != len(set(peers)) or any(peer not in ids or peer == target_id for peer in peers)):
        raise ValueError("Exclusive siblings must be unique existing segment IDs, excluding the target")
    if kind == "independent" and (parent or peers):
        raise ValueError("Independent segments have neither a parent nor exclusive siblings")
    if kind == "subregion" and (not parent or peers):
        raise ValueError("A subregion requires a parent and preserves all overlapping segments")
    if kind == "subdivision" and not peers:
        raise ValueError("A subdivision requires at least one explicitly selected exclusive sibling")
    result = {"kind": kind, "exclusive_with": sorted(peers)}
    if parent:
        result["parent_id"] = parent
    parents = _parents(structures, target_id, result)
    target_ancestors = ancestors(target_id, parents)
    for peer in peers:
        if peer in target_ancestors or target_id in ancestors(peer, parents):
            raise ValueError("Parent and child segments cannot replace each other's overlap")
        if parent and parents.get(peer) and parents[peer] != parent:
            raise ValueError("Exclusive siblings with declared parents must share the same parent")
    return result


def effective_relationship(structures, target_id):
    """An accepted A–B link is reciprocal; no unchecked transitive peers are added."""
    selected = next((entry for entry in structures if entry["id"] == target_id), {})
    own = selected.get("relationship") or INDEPENDENT
    peers = set(own.get("exclusive_with", []))
    peers.update(entry["id"] for entry in structures if entry["id"] != target_id
                 and entry.get("relationship", {}).get("kind") == "subdivision"
                 and target_id in entry["relationship"].get("exclusive_with", []))
    value = {**own, "exclusive_with": sorted(peers)}
    if peers:
        value["kind"] = "subdivision"
    return normalize_relationship(value, structures, target_id)


def relationship_suggestions(structures, target_id):
    if target_id not in LUNG_LOBES:
        return []
    peers = sorted(LUNG_LOBES.intersection(entry["id"] for entry in structures) - {target_id})
    return ([{"id": "totalsegmentator-lung-lobes-v1", "name": "Lung lobes", "exclusive_with": peers}]
            if peers else [])


def relationship_dirty(review):
    candidate = review.get("candidate")
    applied = candidate.get("relationship", INDEPENDENT) if candidate else review.get("initial_relationship", INDEPENDENT)
    return review.get("relationship", INDEPENDENT) != applied


def reciprocal_updates(structures, target, relationship):
    """Update only direct chosen/former pairs; a later Independent choice removes both directions."""
    graph = [dict(entry) for entry in structures if entry["id"] != target["id"]]
    graph.append({**target, "relationship": relationship})
    result = {target["id"]: relationship}
    chosen = set(relationship["exclusive_with"])
    for entry in structures:
        if entry["id"] == target["id"]:
            continue
        old = entry.get("relationship") or INDEPENDENT
        peers = set(old.get("exclusive_with", []))
        if target["id"] not in peers and entry["id"] not in chosen:
            continue
        peers.discard(target["id"])
        if entry["id"] in chosen:
            peers.add(target["id"])
        value = {"kind": "subdivision" if peers else "subregion" if old.get("parent_id") else "independent",
                 "exclusive_with": sorted(peers)}
        if old.get("parent_id"):
            value["parent_id"] = old["parent_id"]
        result[entry["id"]] = normalize_relationship(value, graph, entry["id"])
    return result
