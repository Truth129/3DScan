"""Persistent, explicitly accepted nnInteractive corrections in patient RAS.

A prediction is a draft mask, never an active segmentation revision. Immutable
candidate aliases reuse the CT review transport without adding temporary masks
to case history or rebuilding surfaces while the reviewer is still clicking.
"""

from __future__ import annotations

import hashlib
import fcntl
import json
from pathlib import Path
import re
import shutil

import numpy as np

from . import __version__, imaging
from .interactive_prompts import normalize_options, normalize_prompts
from .relationships import (INDEPENDENT, ancestors, effective_relationship, normalize_relationship,
                            reciprocal_updates, relationship_dirty, relationship_suggestions)
from .store import ReviewConflict, identifier, now, safe_id


_ALIAS = re.compile(r"review-([a-f0-9]{32})-(0|[1-9][0-9]{0,9})")
_MEASUREMENTS = {"volume_ml", "voxel_count", "bounds_ras", "centroid_ras", "qc"}


def _digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def _prompt_digest(prompts):
    return hashlib.sha256(json.dumps(prompts, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def _summary(before_count, after_count, added, removed, voxel_volume_ml):
    return dict(before_voxels=before_count, after_voxels=after_count,
                changed_voxels=added + removed, added_voxels=added, removed_voxels=removed,
                voxel_volume_ml=voxel_volume_ml, before_volume_ml=before_count * voxel_volume_ml,
                after_volume_ml=after_count * voxel_volume_ml,
                volume_delta_ml=(after_count - before_count) * voxel_volume_ml)


def _candidate_changes(candidate, selected):
    return {entry["id"]: entry for entry in candidate.get("affected_structures", [])} or {
        selected["id"]: dict(id=selected["id"], mask_file=candidate["mask_file"], summary=candidate["summary"],
                              relationship=candidate.get("relationship", selected.get("relationship")))}


def _changed_structure(structure, change):
    entry = dict(structure)
    if not change.get("metadata_only"):
        entry = {key: value for key, value in entry.items() if key not in _MEASUREMENTS}
        entry.update(mask_file=change["mask_file"], volume_ml=change["summary"]["after_volume_ml"],
                     voxel_count=change["summary"]["after_voxels"], bounds_ras=None, centroid_ras=None, qc=[])
    if change.get("relationship") is not None:
        entry["relationship"] = change["relationship"]
    return entry


def public_review(record):
    """Return review state without source paths, checkpoint paths or mask paths."""
    result = {key: record.get(key) for key in (
        "id", "case_id", "revision_id", "structure_id", "status", "generation", "prompts",
        "job_id", "error", "accepted_revision_id", "created_at", "updated_at",
        "options", "batches", "relationship", "relationship_suggestions",
    )}
    candidate = record.get("candidate")
    result["candidate"] = ({key: candidate[key] for key in (
        "generation", "revision_id", "summary", "created_at", "relationship", "overlaps", "warnings", "total_changed_voxels", "relationship_changed",
    ) if key in candidate} if candidate else None)
    if candidate:
        result["candidate"]["affected_structures"] = [
            {key: entry[key] for key in ("id", "name", "role", "summary", "metadata_only", "relationship") if key in entry}
            for entry in candidate.get("affected_structures", [])]
    applied = record.get("batches", [])[-1]["end"] if record.get("batches") else 0
    result.update(applied_prompt_count=applied, pending_count=max(0, len(record["prompts"]) - applied),
                  dirty=not candidate or candidate["generation"] != record["generation"])
    result["relationship_dirty"] = relationship_dirty(record)
    result["new_structure"] = bool(record.get("new_structure"))
    if record.get("new_structure"):
        result["target"] = {key: value for key, value in record["target"].items() if key != "mask_file"}
        result["preview_revision_id"] = f"review-{record['id']}-0"
    return result


def _generation(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < 2**31:
        raise ValueError("A valid correction generation is required")
    return value


class ReviewManager:
    def __init__(self, service):
        self.service = service
        self.store = service.store

    def _base(self, review):
        base = self.store.revision(review["case_id"], review["revision_id"])
        selected = next((s for s in base["structures"] if s["id"] == review["structure_id"]), None)
        if selected is None and review.get("new_structure"):
            selected = review.get("target")
            if (not selected or selected["id"] != review["structure_id"] or selected.get("task") != "custom"
                    or not re.fullmatch(r"custom:[a-f0-9]{32}", selected["id"])):
                raise ValueError("The provisional structure identity is invalid")
        if selected is None:
            raise ValueError("The selected structure is unavailable in the source revision")
        return base, selected

    def create(self, case_id, revision_id, structure_id, relationship=None):
        safe_id(case_id)
        case = self.store.get("cases", case_id)
        base = self.store.revision(case_id, revision_id)
        if case.get("active_revision_id") != revision_id:
            raise ReviewConflict("Start a correction from the active revision")
        if structure_id not in {s["id"] for s in base["structures"]}:
            raise ValueError("Structure does not belong to the selected revision")
        initial = effective_relationship(base["structures"], structure_id)
        chosen = initial if relationship is None else normalize_relationship(relationship, base["structures"], structure_id)
        record = dict(id=identifier(), case_id=case_id, revision_id=revision_id, structure_id=structure_id,
                      status="draft", generation=0, prompts=[], job_id=None, candidate=None, candidates={},
                      options=normalize_options(), batches=[], error=None, accepted_revision_id=None,
                      relationship=chosen, initial_relationship=initial,
                      relationship_suggestions=relationship_suggestions(base["structures"], structure_id),
                      created_at=now(), updated_at=now(), schema_version=2)
        return self.store.save("review_sessions", record)

    def create_structure(self, case_id, revision_id, name, group="Custom", color="#e3a35d", relationship=None):
        """Create an empty draft target, without changing the segmentation history."""
        def label(value, maximum, field):
            if not isinstance(value, str):
                raise ValueError(f"The segment {field} must be text")
            result = value.strip()
            if not result or len(result) > maximum or any(ord(char) < 32 or ord(char) == 127 for char in result):
                raise ValueError(f"The segment {field} must contain 1 to {maximum} printable characters")
            return result

        name, group = label(name, 128, "name"), label(group, 64, "group")
        if not isinstance(color, str) or not re.fullmatch(r"#[a-fA-F0-9]{6}", color):
            raise ValueError("The segment color must be a six-digit hex color")
        case = self.store.get("cases", case_id)
        base = self.store.revision(case_id, revision_id)
        if case.get("active_revision_id") != revision_id:
            raise ReviewConflict("Start a new segment from the active revision")
        session_id, structure_id = identifier(), f"custom:{identifier()}"
        chosen = normalize_relationship(relationship, base["structures"], structure_id)
        directory = self.store.case_dir(case_id) / "reviews" / session_id
        image, _ = imaging._image(Path(base["ct_file"]))
        baseline = directory / "baseline.nii.gz"
        # Remote reviewers can create targets concurrently. Bound dense empty
        # CT-sized allocations across API threads/processes, independently of
        # the inference queue; no GPU work is required to create a draft.
        with (self.service.settings.data_dir / "empty-mask-generation.lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            imaging._save_mask(np.zeros(image.shape, dtype=np.uint8), image, baseline)
        target = dict(id=structure_id, name=name, task="custom", group=group, color=color.lower(),
                      mask_file=str(baseline), volume_ml=0., voxel_count=0, centroid_ras=None, bounds_ras=None, qc=[], relationship=chosen)
        record = dict(id=session_id, case_id=case_id, revision_id=revision_id, structure_id=structure_id,
                      new_structure=True, target=target, status="draft", generation=0, prompts=[], job_id=None,
                      candidate=None, candidates={}, options=normalize_options({"start_empty": True}), batches=[],
                      relationship=chosen, initial_relationship=dict(INDEPENDENT), relationship_suggestions=[],
                      error=None, accepted_revision_id=None, created_at=now(), updated_at=now(), schema_version=2)
        return self.store.save("review_sessions", record)

    def get(self, case_id, session_id):
        safe_id(case_id)
        return self.store.reconcile_review(case_id, session_id)

    def list(self, case_id):
        self.store.get("cases", case_id)
        return [self.get(case_id, record["id"]) for record in self.store.list("review_sessions", case_id)]

    def _prompts(self, review, prompts):
        base, _ = self._base(review)
        image, affine = imaging._image(Path(base["ct_file"]))
        return normalize_prompts(prompts, affine, image.shape)

    def update(self, case_id, session_id, expected_generation, prompts, options=None, relationship=None):
        review = self.get(case_id, session_id)
        _generation(expected_generation)
        base, selected = self._base(review)
        chosen = normalize_relationship(relationship, base["structures"], selected["id"]) if relationship is not None else None
        return self.store.change_review(case_id, session_id, expected_generation, self._prompts(review, prompts),
                                        options=normalize_options(options) if options is not None else None, relationship=chosen)

    def predict(self, case_id, session_id, generation, full_refine=False):
        self.get(case_id, session_id)
        if not isinstance(full_refine, bool):
            raise ValueError("Whole-object refinement must be a boolean")
        return self.store.enqueue_review(case_id, session_id, _generation(generation), "review_predict", full_refine=full_refine)

    def accept(self, case_id, session_id, generation):
        self.get(case_id, session_id)
        return self.store.enqueue_review(case_id, session_id, _generation(generation), "review_accept")

    def discard(self, case_id, session_id, expected_generation=None):
        review = self.get(case_id, session_id)
        generation = review["generation"] if expected_generation is None else _generation(expected_generation)
        return self.store.change_review(case_id, session_id, generation, discard=True)

    def candidate_revision(self, case_id, alias):
        match = _ALIAS.fullmatch(alias)
        if match is None:
            raise KeyError("Correction candidate not found")
        review = self.get(case_id, match[1])
        if review["status"] == "discarded":
            raise KeyError("Correction candidate was discarded")
        base, selected = self._base(review)
        candidate = review.get("candidates", {}).get(match[2])
        if candidate is None and review.get("new_structure") and match[2] == "0":
            candidate = dict(revision_id=alias, mask_file=selected["mask_file"],
                             summary={"after_volume_ml": 0., "after_voxels": 0})
        if not candidate or candidate["revision_id"] != alias or not Path(candidate["mask_file"]).is_file():
            raise KeyError("Correction candidate not found")
        changes = _candidate_changes(candidate, selected)
        structures = [_changed_structure(structure, changes[structure["id"]]) if structure["id"] in changes else dict(structure)
                      for structure in base["structures"]]
        if review.get("new_structure") and selected["id"] not in {entry["id"] for entry in structures}:
            structures.append(_changed_structure(selected, changes[selected["id"]]))
        parent = f"review-{review['id']}-0" if review.get("new_structure") and match[2] != "0" else base["id"]
        return {**base, "id": alias, "parent_id": parent, "structures": structures,
                "source": "nninteractive_candidate", "review_status": "unreviewed"}

    def _running(self, job, status):
        self.service.check_cancel(job)
        review = self.store.get("review_sessions", job["payload"]["session_id"])
        if (review["case_id"] != job["case_id"] or review["generation"] != job["payload"]["generation"]
                or review["status"] != status or review.get("job_id") != job["id"]):
            raise InterruptedError("Correction job superseded by another draft")
        return review

    def _overlaps(self, job, base, selected, after, image, affine, directory, target, summary):
        """Prefilter spatially, then hold only the target and one peer mask at a time."""
        relationship = normalize_relationship(job["payload"]["relationship"], base["structures"], selected["id"])
        updates = reciprocal_updates(base["structures"], selected, relationship)
        voxel_ml = summary["voxel_volume_ml"]
        changed_relationship = (selected.get("relationship") or INDEPENDENT) != relationship
        affected = [dict(id=selected["id"], name=selected["name"], role="target", summary=summary,
                         mask_file=str(target) if summary["changed_voxels"] else selected["mask_file"],
                         metadata_only=not bool(summary["changed_voxels"]), relationship=relationship)]
        overlaps, warnings, checked = [], [], {}
        bounds = imaging._foreground_bounds(after)
        target_bounds = None
        if bounds is not None:
            corners = imaging._world(affine, imaging._corners(bounds[0] - .5, bounds[1] + .5))
            target_bounds = np.stack((corners.min(axis=0), corners.max(axis=0)))
        parents = {entry["id"]: (entry.get("relationship") or {}).get("parent_id") for entry in base["structures"]}
        parents[selected["id"]] = relationship.get("parent_id")
        protected = ancestors(selected["id"], parents)
        protected.update(entry["id"] for entry in base["structures"] if selected["id"] in ancestors(entry["id"], parents))
        exclusive = set(relationship["exclusive_with"])
        target_flat = after.reshape(-1)
        self.service.stage(job, "Checking explicit sibling relationships and preserved overlaps")
        for entry in base["structures"]:
            self.service.check_cancel(job)
            peer_id = entry["id"]
            if peer_id == selected["id"]:
                continue
            next_relationship = updates.get(peer_id)
            metadata_changed = next_relationship is not None and (entry.get("relationship") or INDEPENDENT) != next_relationship
            changed_relationship |= metadata_changed
            peer_bounds = entry.get("bounds_ras")
            disjoint = target_bounds is None or entry.get("voxel_count") == 0
            if peer_bounds is not None:
                box = np.asarray(peer_bounds, dtype=float)
                if box.shape == (2, 3) and np.isfinite(box).all() and target_bounds is not None:
                    disjoint |= bool(np.any(target_bounds[1] < box[0] - 1e-4) or np.any(box[1] < target_bounds[0] - 1e-4))
            overlap = 0
            peer_count = entry.get("voxel_count", 0)
            result_path = entry["mask_file"]
            if not disjoint:
                digest = _digest(entry["mask_file"])
                peer, _, _ = imaging._binary(Path(entry["mask_file"]), image, affine)
                peer_flat = peer.reshape(-1)
                peer_count = 0
                for start in range(0, target_flat.size, 4 * 1024 * 1024):
                    self.service.check_cancel(job)
                    a, b = target_flat[start:start + 4 * 1024 * 1024], peer_flat[start:start + 4 * 1024 * 1024]
                    peer_count += int(np.count_nonzero(b))
                    overlap += int(np.count_nonzero(a & b))
                    if peer_id in exclusive:
                        b[a] = False
                if overlap:
                    checked[peer_id] = dict(id=peer_id, mask_file=entry["mask_file"], sha256=digest)
                    removed = peer_id in exclusive
                    reason = "explicit_sibling" if removed else "parent_or_child" if peer_id in protected else relationship["kind"] if relationship["kind"] == "independent" else "unselected"
                    overlaps.append(dict(id=peer_id, name=entry["name"], voxel_count=overlap, volume_ml=overlap * voxel_ml,
                                         policy="removed" if removed else "preserved", reason=reason))
                    if removed:
                        result_path = str(directory / f"peer-{hashlib.sha256(peer_id.encode()).hexdigest()}.nii.gz")
                        imaging._save_mask(peer, image, Path(result_path))
                        if overlap == peer_count:
                            warnings.append(f"Replacing overlap leaves {entry['name']} empty; inspect this subdivision")
                    elif peer_id not in protected:
                        warnings.append(f"Overlap with {entry['name']} is preserved; no exclusive sibling relationship was selected")
                if _digest(entry["mask_file"]) != digest:
                    raise ValueError("An overlapping source mask changed during candidate preparation")
                del peer, peer_flat
            removed = overlap if peer_id in exclusive else 0
            if removed or metadata_changed:
                checked.setdefault(peer_id, dict(id=peer_id, mask_file=entry["mask_file"], sha256=_digest(entry["mask_file"])))
                affected.append(dict(id=peer_id, name=entry["name"], role="exclusive_sibling",
                                     summary=_summary(peer_count, peer_count - removed, 0, removed, voxel_ml),
                                     mask_file=result_path, metadata_only=not bool(removed),
                                     relationship=next_relationship or entry.get("relationship") or dict(INDEPENDENT)))
        for entry in affected:
            entry["mask_sha256"] = _digest(entry["mask_file"])
        for entry in checked.values():
            self.service.check_cancel(job)
            if _digest(entry["mask_file"]) != entry["sha256"]:
                raise ValueError("An overlapping source mask changed during candidate preparation")
        return dict(relationship=relationship, affected_structures=affected, overlaps=overlaps, warnings=warnings,
                    checked_sources=list(checked.values()), relationship_changed=bool(changed_relationship),
                    total_changed_voxels=sum(entry["summary"]["changed_voxels"] for entry in affected))

    def run_predict(self, job, runtime):
        review = self._running(job, "predicting")
        base, selected = self._base(review)
        directory = self.store.case_dir(job["case_id"]) / "reviews" / review["id"] / "candidates" / job["id"]
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / "mask.nii.gz"
        ct = Path(base["ct_file"])
        mask_path = Path(selected["mask_file"])
        ct_digest, before_digest = _digest(ct), _digest(mask_path)
        if job["payload"].get("relationship_only"):
            alias = job["payload"].get("reuse_candidate_revision_id")
            previous = next((entry for entry in review.get("candidates", {}).values() if entry["revision_id"] == alias), None)
            if alias:
                if (not previous or previous["ct_sha256"] != ct_digest or previous["before_mask_sha256"] != before_digest
                        or previous.get("prompts_snapshot") != job["payload"]["prompts"]
                        or previous.get("options") != job["payload"]["options"]
                        or previous.get("batches_snapshot") != job["payload"]["batches"]
                        or _digest(previous["mask_file"]) != previous["mask_sha256"]):
                    raise ValueError("The prediction source changed; apply a new prediction")
                shutil.copyfile(previous["mask_file"], target)
                provenance = {**previous.get("provenance", {}), "reused_prediction": True, "source_candidate": alias}
            else:
                if job["payload"]["prompts"]:
                    raise ValueError("Unapplied prompts require a neural prediction")
                if job["payload"]["options"]["start_empty"]:
                    reference, _ = imaging._image(ct)
                    imaging._save_mask(np.zeros(reference.shape, dtype=np.uint8), reference, target)
                else:
                    shutil.copyfile(mask_path, target)
                provenance = dict(engine="mask-relationship-processing", version=__version__, reused_prediction=False, source="base_mask")
        else:
            provenance = runtime.predict(ct, mask_path, job["payload"]["prompts"], target,
                progress=lambda message: self.service.stage(job, message), cancelled=lambda: self.service.cancelled(job),
                session_key=f"{review['id']}:{review['revision_id']}:{before_digest}",
                options=job["payload"]["options"], batches=job["payload"]["batches"])
        self._running(job, "predicting")
        self.service.stage(job, "Checking the candidate on the reference CT grid")
        image, affine = imaging._image(ct)
        before, _, _ = imaging._binary(mask_path, image, affine)
        after, _, _ = imaging._binary(target, image, affine)
        # Count in bounded chunks; avoid allocating several full CT-sized XOR,
        # addition and removal workspaces beside the model's resident tensors.
        before_flat, after_flat = before.reshape(-1), after.reshape(-1)
        before_count = after_count = added = removed = 0
        for start in range(0, before_flat.size, 4 * 1024 * 1024):
            self.service.check_cancel(job)
            a, b = before_flat[start:start + 4 * 1024 * 1024], after_flat[start:start + 4 * 1024 * 1024]
            before_count += int(np.count_nonzero(a))
            after_count += int(np.count_nonzero(b))
            added += int(np.count_nonzero(b & ~a))
            removed += int(np.count_nonzero(a & ~b))
        voxel_volume_ml = abs(float(np.linalg.det(affine[:3, :3]))) / 1000
        summary = _summary(before_count, after_count, added, removed, voxel_volume_ml)
        del before, before_flat, after_flat, a, b
        overlap = self._overlaps(job, base, selected, after, image, affine, directory, target, summary)
        del after
        if _digest(ct) != ct_digest or _digest(mask_path) != before_digest:
            raise ValueError("The source CT or mask changed during interactive prediction")
        candidate = dict(generation=review["generation"], revision_id=f"review-{review['id']}-{review['generation']}",
                         mask_file=str(target), summary=summary, created_at=now(), provenance=provenance,
                         prompts_snapshot=job["payload"]["prompts"], options=job["payload"]["options"],
                         batches_snapshot=job["payload"]["batches"],
                         prompts_sha256=_prompt_digest(job["payload"]["prompts"]),
                         ct_sha256=ct_digest, before_mask_sha256=before_digest, mask_sha256=_digest(target), **overlap)
        from .service import atomic_json
        atomic_json(directory / "candidate.json", candidate)
        self.service.check_cancel(job)
        return self.store.publish_review_candidate(job, candidate)

    def run_accept(self, job):
        review = self._running(job, "accepting")
        if self.store.get("cases", job["case_id"]).get("active_revision_id") != review["revision_id"]:
            raise ValueError("The active revision changed; start a review from the current revision")
        candidate = review.get("candidate")
        if (not candidate or candidate["generation"] != review["generation"]
                or candidate["revision_id"] != job["payload"].get("candidate_revision_id")):
            raise ValueError("The candidate changed; inspect the current preview before accepting")
        if (not candidate["summary"]["after_voxels"] or not (
                candidate.get("total_changed_voxels", candidate["summary"]["changed_voxels"]) or candidate.get("relationship_changed"))):
            raise ValueError("Only a nonempty, changed candidate can be accepted")
        base, selected = self._base(review)
        ct = Path(base["ct_file"])
        def verify_sources():
            self._running(job, "accepting")
            if (_digest(ct) != candidate["ct_sha256"] or _digest(selected["mask_file"]) != candidate["before_mask_sha256"]
                    or _digest(candidate["mask_file"]) != candidate["mask_sha256"]):
                raise ValueError("A correction source or candidate changed; generate a new candidate")
            records = {entry["id"]: entry for entry in base["structures"]}
            for entry in candidate.get("checked_sources", []):
                self.service.check_cancel(job)
                if (entry["id"] not in records or records[entry["id"]]["mask_file"] != entry["mask_file"]
                        or _digest(entry["mask_file"]) != entry["sha256"]):
                    raise ValueError("An overlapping source mask changed; generate a new candidate")
            for entry in candidate.get("affected_structures", []):
                self.service.check_cancel(job)
                if _digest(entry["mask_file"]) != entry["mask_sha256"]:
                    raise ValueError("An affected candidate mask changed; generate a new candidate")
        verify_sources()
        changes = _candidate_changes(candidate, selected)
        if candidate.get("relationship", INDEPENDENT) != job["payload"].get("relationship", INDEPENDENT):
            raise ReviewConflict("The segment relationship changed; inspect its current candidate")
        structures = []
        for structure in base["structures"]:
            entry = dict(structure)
            if entry["id"] in changes:
                change = changes[entry["id"]]
                if not change.get("metadata_only"):
                    entry = {key: value for key, value in entry.items() if key not in _MEASUREMENTS}
                    entry["mask_file"] = change["mask_file"]
                if change.get("relationship") is not None:
                    entry["relationship"] = change["relationship"]
            structures.append(entry)
        is_new = bool(review.get("new_structure"))
        if is_new:
            if selected["id"] in {structure["id"] for structure in base["structures"]}:
                raise ReviewConflict("This new structure already belongs to the base revision")
            entry = {key: value for key, value in selected.items() if key not in _MEASUREMENTS}
            entry["mask_file"] = candidate["mask_file"]
            entry["relationship"] = candidate.get("relationship", INDEPENDENT)
            structures.append(entry)
        affected_ledger = public_review(review)["candidate"].get("affected_structures", [])
        source_hashes = {entry["id"]: entry["sha256"] for entry in candidate.get("checked_sources", [])}
        source_hashes[selected["id"]] = candidate["before_mask_sha256"]
        for entry in affected_ledger:
            entry["before_mask_sha256"] = source_hashes[entry["id"]]
            entry["after_mask_sha256"] = changes[entry["id"]]["mask_sha256"]
        from .service import atomic_json, code_fingerprint
        operation = dict(type="nninteractive_new_structure" if is_new else "nninteractive_correction",
                         session_id=review["id"], generation=review["generation"],
                         structure_id=review["structure_id"], input_revision_id=base["id"],
                         prompts=job["payload"]["prompts"], prompts_sha256=candidate["prompts_sha256"],
                         options=candidate.get("options"), batches=candidate.get("batches_snapshot"),
                         summary=candidate["summary"], runtime=candidate.get("provenance"),
                         relationship=candidate.get("relationship", INDEPENDENT),
                         affected_structures=affected_ledger,
                         overlaps=candidate.get("overlaps", []), warnings=candidate.get("warnings", []),
                         total_changed_voxels=candidate.get("total_changed_voxels", candidate["summary"]["changed_voxels"]),
                         relationship_changed=candidate.get("relationship_changed", False),
                         ct_sha256=candidate["ct_sha256"], before_mask_sha256=candidate["before_mask_sha256"],
                         after_mask_sha256=candidate["mask_sha256"], code_version=__version__, code_sha256=code_fingerprint())
        if is_new:
            operation["new_structure"] = {key: value for key, value in selected.items() if key != "mask_file"}
            operation["new_structure"]["relationship"] = candidate.get("relationship", INDEPENDENT)
        self.service.stage(job, "Preparing the accepted correction and updating its surface")
        revision = self.service.build_revision(job["case_id"], ct, structures,
            source="nninteractive_new_structure" if is_new else "nninteractive_correction",
            parent=base["id"], operation=operation, acquisition=base.get("acquisition"), job=job,
            extra_qc=sorted(set(self.service._inherited_qc(base)) | set(candidate.get("warnings", []))),
            inference_manifest=base.get("inference_manifest"))
        verify_sources()
        revision["review_status"] = "corrected"
        atomic_json(Path(revision["directory"]) / "revision.json", revision)
        return revision
