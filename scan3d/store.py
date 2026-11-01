"""SQLite records are committed only after their filesystem artifacts are complete."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3

from . import inference_paths
from .errors import NotFound
from .fileio import atomic_json
from .identifiers import identifier
from .relationships import INDEPENDENT, relationship_dirty


class ReviewConflict(ValueError):
    """The reviewer must reload state before retrying a stale mutation."""


LANES = ("gpu", "cpu")
# A reviewer is waiting on these; every other kind is queued batch work.
INTERACTIVE_KINDS = frozenset({"review_predict", "review_accept"})


def job_lane(job):
    """TotalSegmentator and nnInteractive inference share the one GPU lane.

    Direct paint/erase/scissors, relationship-only Apply, acceptance, image
    preparation, refinement, Slicer import and include-all never touch the
    GPU, so they run on the CPU lane rather than behind minutes of inference.
    """
    # Claims classify every queued row; a malformed one fails in its job, not here.
    payload = job.get("payload") or {}
    inference = job["kind"] == "review_predict" and not (payload.get("relationship_only") or payload.get("method") == "direct")
    return "gpu" if job["kind"] == "segment" or inference else "cpu"


def job_priority(job):
    """Interactive review work runs before queued batch work in each lane."""
    return 0 if job["kind"] in INTERACTIVE_KINDS else 1


def now():
    return datetime.now(timezone.utc).isoformat()


def safe_id(value):
    if not re.fullmatch(r"[a-f0-9]{32}", value):
        raise ValueError("Invalid record identifier")
    return value


class Store:
    def __init__(self, directory: Path):
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.db = directory / "scan3d.sqlite3"
        with self.connection() as connection:
            connection.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS cases(id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS revisions(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS revision_case ON revisions(case_id);
                CREATE TABLE IF NOT EXISTS jobs(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS job_status ON jobs(status);
                CREATE TABLE IF NOT EXISTS review_sessions(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS review_case ON review_sessions(case_id);
                CREATE TABLE IF NOT EXISTS image_assets(id TEXT PRIMARY KEY, case_id TEXT NOT NULL, data TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS image_asset_case ON image_assets(case_id);
            """)
            if connection.execute("PRAGMA user_version").fetchone()[0] < 3:
                connection.execute("PRAGMA user_version=3")

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def get(self, table, record_id):
        if table not in ("cases", "revisions", "jobs", "review_sessions", "image_assets"):
            raise ValueError("Unknown record type")
        safe_id(record_id)
        with self.connection() as con:
            row = con.execute(f"SELECT data FROM {table} WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound(f"{table[:-1].capitalize()} not found")
        record = json.loads(row["data"])
        return self._upgrade_review(record) if table == "review_sessions" else record

    def save(self, table, record):
        if table == "image_assets":
            return self.register_image_asset(record)
        if table == "revisions" and ("reference_image_id" in record or "image_asset" in record):
            self.resolve_reference(record)
        safe_id(record["id"])
        encoded = json.dumps(record, allow_nan=False)
        with self.connection() as con:
            if table == "cases":
                con.execute("INSERT OR REPLACE INTO cases VALUES (?,?)", (record["id"], encoded))
            elif table == "revisions":
                con.execute("INSERT OR REPLACE INTO revisions VALUES (?,?,?)", (record["id"], record["case_id"], encoded))
            elif table == "jobs":
                con.execute("INSERT OR REPLACE INTO jobs VALUES (?,?,?,?)", (record["id"], record["case_id"], record["status"], encoded))
            elif table == "review_sessions":
                con.execute("INSERT OR REPLACE INTO review_sessions VALUES (?,?,?)", (record["id"], record["case_id"], encoded))
            else:
                raise ValueError("Unknown record type")
        return record

    def list(self, table, case_id=None):
        if table not in ("cases", "revisions", "jobs", "review_sessions", "image_assets"):
            raise ValueError("Unknown record type")
        with self.connection() as con:
            if case_id:
                rows = con.execute(f"SELECT data FROM {table} WHERE case_id=? ORDER BY rowid DESC", (case_id,)).fetchall()
            else:
                rows = con.execute(f"SELECT data FROM {table} ORDER BY rowid DESC").fetchall()
        return [json.loads(row["data"]) for row in rows]

    def case_dir(self, case_id):
        path = self.directory / "cases" / safe_id(case_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def revision(self, case_id, revision_id):
        revision = self.get("revisions", revision_id)
        if revision["case_id"] != case_id:
            raise NotFound("Revision does not belong to this case")
        return revision

    def register_image_asset(self, record):
        """Register immutable source-image metadata without replacing a record."""
        from .image_assets import assert_reference_image

        assert_reference_image(record, record.get("file_path"))
        safe_id(record["id"])
        safe_id(record["case_id"])
        encoded = json.dumps(record, allow_nan=False)
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM cases WHERE id=?", (record["case_id"],)).fetchone() is None:
                raise NotFound("Case not found")
            row = con.execute("SELECT data FROM image_assets WHERE id=?", (record["id"],)).fetchone()
            if row is not None:
                existing = json.loads(row["data"])
                if {k: v for k, v in existing.items() if k != "file_path"} != {k: v for k, v in record.items() if k != "file_path"}:
                    raise ValueError("Image assets are immutable")
                # A copied source with identical bytes and provenance is the
                # same asset; retain its original immutable storage location.
                return existing
            con.execute("INSERT INTO image_assets VALUES (?,?,?)", (record["id"], record["case_id"], encoded))
        return record

    def image_asset(self, case_id, image_id):
        self.get("cases", case_id)
        try:
            record = self.get("image_assets", image_id)
        except KeyError:
            record = next((entry for entry in self.image_assets(case_id) if entry["id"] == image_id), None)
            if record is None:
                raise NotFound("Image not found") from None
        if record["case_id"] != case_id:
            raise NotFound("Image does not belong to this case")
        return record

    def image_assets(self, case_id):
        """Include legacy references in the read model without migrating rows."""
        self.get("cases", case_id)
        records = {record["id"]: record for record in self.list("image_assets", case_id)}
        for revision in self.list("revisions", case_id):
            asset = self.resolve_reference(revision)
            records.setdefault(asset["id"], asset)
        return list(records.values())

    def resolve_reference(self, revision, *, persist=False):
        from .image_assets import reference_image, public_image_asset

        asset = reference_image(revision)
        if "reference_image_id" in revision or "image_asset" in revision:
            try:
                registered = self.get("image_assets", asset["id"])
            except KeyError:
                raise ValueError("Revision references an unregistered source image") from None
            if public_image_asset(registered) != public_image_asset(asset):
                raise ValueError("Revision image metadata differs from its immutable source image")
        if persist:
            return self.register_image_asset(asset)
        return asset

    def activate(self, case_id, revision_id):
        self.revision(case_id, revision_id)
        case = self.get("cases", case_id)
        case["active_revision_id"] = revision_id
        case["status"] = "ready"
        acquisition = self.revision(case_id, revision_id).get("acquisition")
        case.update(selected_series_uid=acquisition.get("series_uid") if acquisition else None, phase=acquisition.get("phase") if acquisition else None)
        return self.save("cases", case)

    def commit_revision(self, revision, job=None, expected_active_revision_id=None, review_guard=None):
        """Revision visibility, case activation and job completion are one transaction."""
        if "reference_image_id" in revision or "image_asset" in revision:
            self.resolve_reference(revision)
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT data FROM cases WHERE id=?", (revision["case_id"],)).fetchone()
            case = json.loads(row["data"])
            if expected_active_revision_id is not None and case.get("active_revision_id") != expected_active_revision_id:
                conflict = ReviewConflict if review_guard else ValueError
                raise conflict("The active revision changed; start from the current revision")
            review = None
            if review_guard:
                row = con.execute("SELECT data FROM review_sessions WHERE id=? AND case_id=?", (review_guard["session_id"], revision["case_id"])).fetchone()
                if row is None:
                    raise NotFound("Review session not found")
                review = json.loads(row["data"])
                if (
                    not job
                    or review["generation"] != review_guard["generation"]
                    or review["status"] != "accepting"
                    or review.get("job_id") != job["id"]
                    or review["revision_id"] != revision.get("parent_id")
                    or not review.get("candidate")
                    or review["candidate"]["generation"] != review["generation"]
                ):
                    raise ReviewConflict("The correction draft changed; review the current candidate before accepting")
                if case.get("active_revision_id") != review["revision_id"]:
                    raise ReviewConflict("The active revision changed; start a review from the current revision")
            case.update(active_revision_id=revision["id"], status="ready")
            if revision.get("acquisition"):
                case.update(selected_series_uid=revision["acquisition"].get("series_uid"), phase=revision["acquisition"].get("phase"))
            con.execute("INSERT INTO revisions VALUES (?,?,?)", (revision["id"], revision["case_id"], json.dumps(revision, allow_nan=False)))
            con.execute("UPDATE cases SET data=? WHERE id=?", (json.dumps(case), case["id"]))
            if job:
                current = json.loads(con.execute("SELECT data FROM jobs WHERE id=?", (job["id"],)).fetchone()["data"])
                if current.get("cancel_requested"):
                    raise InterruptedError("Processing cancelled before activation")
                job.update(current)
                job.update(status="completed", stage="Complete", revision_id=revision["id"], completed_at=now())
                con.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job["status"], json.dumps(job), job["id"]))
            if review is not None:
                review.update(status="accepted", accepted_revision_id=revision["id"], error=None, updated_at=now())
                con.execute("UPDATE review_sessions SET data=? WHERE id=?", (json.dumps(review, allow_nan=False), review["id"]))

    @staticmethod
    def _upgrade_review(review):
        if review.get("schema_version", 1) < 2:
            review["prompts"] = [dict(prompt, kind=prompt.get("kind", "point")) for prompt in review["prompts"]]
            review["options"] = {"autozoom": True, "start_empty": False}
            review["batches"] = (
                [dict(end=i + 1, autozoom=True, full_refine=False, legacy=True) for i in range(len(review["prompts"]))] if review.get("candidate") else []
            )
            if review.get("candidate"):
                review["candidate"].setdefault("prompts_snapshot", review["prompts"])
                review["candidate"].setdefault("options", review["options"])
                review["candidate"].setdefault("batches_snapshot", review["batches"])
            review["schema_version"] = 2
        review.setdefault("options", {"autozoom": True, "start_empty": False})
        review.setdefault("batches", [])
        review.setdefault("relationship", dict(INDEPENDENT))
        review.setdefault("initial_relationship", dict(INDEPENDENT))
        review.setdefault("method", "nninteractive")
        review.setdefault("inference_path", None)
        return review

    @staticmethod
    def _review_row(con, session_id, case_id=None):
        safe_id(session_id)
        row = con.execute("SELECT data FROM review_sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            raise NotFound("Review session not found")
        review = json.loads(row["data"])
        if case_id is not None and review["case_id"] != case_id:
            raise NotFound("Review session does not belong to this case")
        return Store._upgrade_review(review)

    @staticmethod
    def _review_generation(review, generation):
        if review["generation"] != generation:
            raise ReviewConflict("The correction draft changed; reload its current prompts")
        if review["status"] in ("accepted", "discarded"):
            raise ReviewConflict("This correction session is closed")

    def open_review(self, record):
        """Store a new draft, or return the one already open for its revision, structure and method.

        A client shows one draft per target, so a second would hide the first's marks. The read and
        the insert share a write lock, because two pages making a first mark at once would both
        find none. Newest first, as in `list`, so targets that hold two drafts from before this
        check answer with the one a client shows.
        """
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            for row in con.execute("SELECT data FROM review_sessions WHERE case_id=? ORDER BY rowid DESC", (record["case_id"],)).fetchall():
                review = self._upgrade_review(json.loads(row["data"]))
                if review["status"] not in ("accepted", "discarded") and all(review[key] == record[key] for key in ("revision_id", "structure_id", "method")):
                    return review
            con.execute("INSERT INTO review_sessions VALUES (?,?,?)", (record["id"], record["case_id"], json.dumps(record, allow_nan=False)))
        return record

    def change_review(self, case_id, session_id, generation, prompts=None, discard=False, options=None, relationship=None):
        """Replace a prompt generation and invalidate work in the same transaction."""
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            review = self._review_row(con, session_id, case_id)
            self._review_generation(review, generation)
            if review["status"] == "accepting" and not discard:
                raise ReviewConflict("Wait for acceptance to finish or cancel it before editing")
            if review.get("job_id"):
                row = con.execute("SELECT data FROM jobs WHERE id=?", (review["job_id"],)).fetchone()
                if row:
                    job = json.loads(row["data"])
                    if job["status"] in ("queued", "running"):
                        job["cancel_requested"] = True
                        if job["status"] == "queued":
                            job.update(status="cancelled", stage="Cancelled", completed_at=now())
                        con.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job["status"], json.dumps(job), job["id"]))
            previous = review["prompts"]
            next_prompts = previous if prompts is None else prompts
            next_options = review["options"] if options is None else options
            next_relationship = review["relationship"] if relationship is None else relationship
            common = 0
            for first, second in zip(previous, next_prompts):
                if first != second:
                    break
                common += 1
            options_changed = next_options != review["options"]
            batches = [] if options_changed or discard else [batch for batch in review["batches"] if batch["end"] <= common]
            candidate = review.get("candidate")
            if (
                discard
                or options_changed
                or not candidate
                or candidate.get("batches_snapshot") != batches
                or next_prompts[: len(candidate.get("prompts_snapshot", []))] != candidate.get("prompts_snapshot")
            ):
                candidate = None
            elif next_prompts == candidate["prompts_snapshot"] and next_relationship == candidate.get("relationship", INDEPENDENT):
                # Undoing only unapplied marks restores the exact saved result,
                # with a fresh immutable alias and no inference work.
                candidate = {**candidate, "generation": generation + 1, "revision_id": f"review-{session_id}-{generation + 1}"}
                review.setdefault("candidates", {})[str(generation + 1)] = candidate
            if review["method"] == "direct" and candidate is None and not discard:
                # Applied paint/erase undo can restore an exact immutable
                # checkpoint immediately; no model or dense replay is needed.
                for previous_candidate in reversed(list(review.get("candidates", {}).values())):
                    if (
                        previous_candidate.get("prompts_snapshot") == next_prompts
                        and previous_candidate.get("options") == next_options
                        and previous_candidate.get("batches_snapshot") == batches
                        and previous_candidate.get("relationship", INDEPENDENT) == next_relationship
                    ):
                        candidate = {**previous_candidate, "generation": generation + 1, "revision_id": f"review-{session_id}-{generation + 1}"}
                        review.setdefault("candidates", {})[str(generation + 1)] = candidate
                        break
            if candidate and candidate["generation"] == generation + 1 and candidate.get("preview_3d_ready"):
                candidate["model_manifest_url"] = f"/api/cases/{case_id}/revisions/{candidate['revision_id']}/model-manifest"
            review.update(
                generation=generation + 1,
                candidate=candidate,
                job_id=None,
                error=None,
                prompts=next_prompts,
                options=next_options,
                batches=batches,
                # With no step left nothing here came from a card, and an older build that later
                # fills `batches` would leave this in place, so that a re-upgrade would misread it.
                inference_path=review["inference_path"] if batches else None,
                relationship=next_relationship,
                status="discarded" if discard else "ready" if candidate and candidate["generation"] == generation + 1 else "draft",
                updated_at=now(),
            )
            con.execute("UPDATE review_sessions SET data=? WHERE id=?", (json.dumps(review, allow_nan=False), session_id))
        return review

    @staticmethod
    def _review_path(review, requested, step_replay):
        """The path a model run uses, frozen as {id, label, identity}, or None for an unnamed default.

        A draft is free until its first published result, then stays on that card, so every
        step of a saved correction ran on one card. A new child is never a warm prefix, so one
        added mark on another card would silently rebuild reviewed work from the original mask.
        The lock is non-empty `batches`, which only a published result writes, so a failed
        first run never locks. Absent means the draft's card, which keeps an old tab working,
        unless another card now holds its ID: that job could never start, so absent is refused
        as a request naming the new card is. Only a replay may leave a card, because it repeats
        every step on purpose, and the binding then moves at publish, so a failed replay leaves
        the draft where it was.
        """
        bound = review["inference_path"] if review["batches"] else None
        chosen = bound if requested is None else inference_paths.binding(requested)
        left = review["batches"] and not step_replay and inference_paths.identity(chosen) != inference_paths.identity(bound)
        if left or (requested is None and inference_paths.replaced(bound)):
            label = (bound or inference_paths.binding(inference_paths.DEFAULT))["label"]
            raise ReviewConflict(f"This draft's AI results came from {label}. Re-run its steps to use another GPU.")
        return chosen

    def enqueue_review(self, case_id, session_id, generation, kind, full_refine=False, inference_path=None, replay=False):
        if kind not in ("review_predict", "review_accept"):
            raise ValueError("Unknown interactive review operation")
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            review = self._review_row(con, session_id, case_id)
            self._review_generation(review, generation)
            if review["method"] == "direct":
                if full_refine:
                    raise ValueError("Whole-object AI refinement is unavailable in direct editing")
                if any(prompt.get("kind") not in {"scribble", "scissors"} for prompt in review["prompts"]):
                    raise ValueError("Direct editing supports paint and erase strokes or 3D scissors only")
            elif any(prompt.get("kind") == "scissors" for prompt in review["prompts"]):
                raise ValueError("3D scissors are only available in direct editing")
            if review.get("job_id"):
                row = con.execute("SELECT status FROM jobs WHERE id=?", (review["job_id"],)).fetchone()
                if row and row["status"] in ("queued", "running"):
                    raise ReviewConflict("This correction session already has a queued or running job")
            chosen = None
            if kind == "review_predict":
                applied = review["batches"][-1]["end"] if review["batches"] else 0
                direct_replay = review["method"] == "direct" and bool(review["prompts"]) and not review.get("candidate")
                # Repeat the draft's exact Apply steps, usually to change card. Nothing need be
                # pending, and a full refine with no marks is a step too, which must not become two.
                step_replay = replay and review["method"] != "direct" and bool(review["batches"])
                policy_only = relationship_dirty(review) and len(review["prompts"]) <= applied and not full_refine and not step_replay
                reusable = review.get("candidate")
                if (
                    policy_only
                    and reusable
                    and (
                        reusable.get("prompts_snapshot") != review["prompts"]
                        or reusable.get("options") != review["options"]
                        or reusable.get("batches_snapshot") != review["batches"]
                    )
                ):
                    reusable = None
                if policy_only and not reusable and review["prompts"]:
                    policy_only = False
                if len(review["prompts"]) <= applied and not full_refine and not policy_only and not direct_replay and not step_replay:
                    raise ReviewConflict("Add or edit a prompt before applying again")
                if not review["prompts"] and not full_refine and not policy_only and not step_replay:
                    raise ValueError("Add a positive or negative prompt before applying")
                # Decided here because `policy_only` is. Direct and relationship-only Apply run no
                # model on any card, so they ignore the choice, and a stale client cannot break them.
                if review["method"] != "direct" and not policy_only:
                    chosen = self._review_path(review, inference_path, step_replay)
                if str(generation) in review.get("candidates", {}):
                    generation += 1
                    review["generation"] = generation
            else:
                candidate = review.get("candidate")
                if not candidate or candidate["generation"] != generation:
                    raise ReviewConflict("Generate and inspect a candidate before accepting")
                if not candidate["summary"]["after_voxels"] and not (review["method"] == "direct" and not review.get("new_structure")):
                    raise ValueError("An empty candidate cannot be accepted; revise the prompts")
                if not candidate.get("total_changed_voxels", candidate["summary"]["changed_voxels"]) and not candidate.get("relationship_changed"):
                    raise ValueError("The candidate is unchanged; no correction is needed")
                case = json.loads(con.execute("SELECT data FROM cases WHERE id=?", (case_id,)).fetchone()["data"])
                if case.get("active_revision_id") != review["revision_id"]:
                    raise ReviewConflict("The active revision changed; start a review from the current revision")
            payload = {key: review[key] for key in ("revision_id", "structure_id", "prompts", "generation", "options", "batches", "relationship", "method")}
            if kind == "review_predict":
                if policy_only:
                    payload["relationship_only"] = True
                    payload["reuse_candidate_revision_id"] = reusable["revision_id"] if reusable else None
                else:
                    payload["batches"] = (
                        review["batches"]
                        if (direct_replay or step_replay) and len(review["prompts"]) <= applied
                        else [*review["batches"], dict(end=len(review["prompts"]), autozoom=review["options"]["autozoom"], full_refine=full_refine)]
                    )
            payload["session_id"] = session_id
            if kind == "review_accept":
                payload["candidate_revision_id"] = review["candidate"]["revision_id"]
            job = dict(
                id=identifier(),
                case_id=case_id,
                kind=kind,
                status="queued",
                stage="Queued",
                error=None,
                revision_id=None,
                created_at=now(),
                payload=payload,
                cancel_requested=False,
                review_session_id=session_id,
                **({"inference_path": chosen} if chosen else {}),
            )
            con.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job["id"], case_id, job["status"], json.dumps(job, allow_nan=False)))
            review.update(status="predicting" if kind == "review_predict" else "accepting", job_id=job["id"], error=None, updated_at=now())
            con.execute("UPDATE review_sessions SET data=? WHERE id=?", (json.dumps(review, allow_nan=False), session_id))
        return job

    def publish_review_candidate(self, job, candidate):
        """A cancelled or superseded inference can never become the current draft."""
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            current = json.loads(con.execute("SELECT data FROM jobs WHERE id=?", (job["id"],)).fetchone()["data"])
            if current.get("cancel_requested") or current["status"] != "running":
                raise InterruptedError("Interactive prediction cancelled before publication")
            review = self._review_row(con, job["payload"]["session_id"], job["case_id"])
            if review["generation"] != job["payload"]["generation"] or review["status"] != "predicting" or review.get("job_id") != job["id"]:
                raise InterruptedError("Interactive prediction superseded by newer prompts")
            generation = str(review["generation"])
            if generation in review.get("candidates", {}):
                raise ValueError("A candidate already exists for this immutable generation")
            review.setdefault("candidates", {})[generation] = candidate
            review.update(candidate=candidate, batches=job["payload"]["batches"], status="ready", error=None, updated_at=now())
            # A relationship-only Apply ran nothing, so it says nothing about which card made this.
            if not job["payload"].get("relationship_only"):
                review["inference_path"] = current.get("inference_path")
            current.update(status="completed", stage="Candidate ready for review", completed_at=now(), revision_id=None)
            con.execute("UPDATE review_sessions SET data=? WHERE id=?", (json.dumps(review, allow_nan=False), review["id"]))
            con.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (current["status"], json.dumps(current), current["id"]))
        return review

    def reconcile_review(self, case_id, session_id):
        """Restore editable drafts after cancellation, failure or a worker restart."""
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            review = self._review_row(con, session_id, case_id)
            if review["status"] in ("predicting", "accepting") and review.get("job_id"):
                row = con.execute("SELECT data FROM jobs WHERE id=?", (review["job_id"],)).fetchone()
                job = json.loads(row["data"]) if row else None
                if job is None or job["status"] in ("failed", "cancelled"):
                    error = (
                        job.get("error") or "Correction job cancelled; your prompts are saved" if job else "Correction job unavailable; your prompts are saved"
                    )
                    review.update(status="ready" if review.get("candidate") else "draft", error=error, updated_at=now())
                    con.execute("UPDATE review_sessions SET data=? WHERE id=?", (json.dumps(review, allow_nan=False), session_id))
        return review

    @contextmanager
    def _revision_update(self, case_id, revision_id):
        """Edit a revision record and its revision.json inside one write transaction."""
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT data FROM revisions WHERE id=? AND case_id=?", (revision_id, case_id)).fetchone()
            if row is None:
                raise NotFound("Revision not found")
            revision = json.loads(row["data"])
            yield revision
            con.execute("UPDATE revisions SET data=? WHERE id=?", (json.dumps(revision, allow_nan=False), revision_id))
            atomic_json(Path(revision["directory"]) / "revision.json", revision)

    def record_detailed_export(self, case_id, revision_id, report, seconds):
        with self._revision_update(case_id, revision_id) as revision:
            revision.setdefault("mesh_statistics", {})["detailed"] = report
            revision.setdefault("processing_timings", {})["detailed_export_seconds"] = seconds

    def set_review_status(self, case_id, revision_id, status):
        with self._revision_update(case_id, revision_id) as revision:
            if not revision["structures"]:
                raise ValueError("This CT has no segmentation to review")
            revision["review_status"] = status
        return revision

    def attach_surface_assets(self, case_id, revision_id, assets):
        with self._revision_update(case_id, revision_id) as revision:
            revision["surface_assets"] = [{k: asset[k] for k in ("id", "key", "triangles")} for asset in assets]
            revision["model_manifest_url"] = f"/api/cases/{case_id}/revisions/{revision_id}/model-manifest"
        return revision

    def enqueue(self, case_id, kind, payload, **fields):
        self.get("cases", case_id)
        job = dict(
            id=identifier(),
            case_id=case_id,
            kind=kind,
            status="queued",
            stage="Queued",
            error=None,
            revision_id=None,
            created_at=now(),
            payload=payload,
            cancel_requested=False,
            **fields,
        )
        return self.save("jobs", job)

    def claim(self, lane=None):
        """Start a lane's most urgent job; queue order breaks priority ties.

        Without a lane, synchronous callers claim across both lanes. An idle
        lane only reads; the write lock is taken when there is a job to claim.
        """
        if lane is not None and lane not in LANES:
            raise ValueError("Unknown worker lane")

        def next_job(con):
            queued = [json.loads(row["data"]) for row in con.execute("SELECT data FROM jobs WHERE status='queued' ORDER BY rowid")]
            return min((job for job in queued if lane is None or job_lane(job) == lane), key=job_priority, default=None)

        with self.connection() as con:
            if next_job(con) is None:
                return None
            con.execute("BEGIN IMMEDIATE")
            job = next_job(con)
            if job is None:
                return None
            job.update(status="running", stage="Starting", started_at=now())
            con.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job["status"], json.dumps(job), job["id"]))
            return job

    def update_job(self, job_id, **changes):
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT data FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise NotFound("Job not found")
            job = json.loads(row["data"])
            job.update(changes)
            con.execute("UPDATE jobs SET status=?,data=? WHERE id=?", (job["status"], json.dumps(job), job_id))
        return job

    def recover(self):
        for job in self.list("jobs"):
            if job["status"] == "running":
                self.update_job(job["id"], status="failed", stage="Interrupted", error="Worker restarted. Retry to reuse completed stages.")
        for review in self.list("review_sessions"):
            self.reconcile_review(review["case_id"], review["id"])


def public_case(record, settings=None):
    """Client view; with settings, series of a modality not offered here read as unavailable."""
    result = {k: v for k, v in record.items() if k not in ("source_dir", "references")}
    result["series"] = []
    for series in record.get("series", []):
        view = {k: v for k, v in series.items() if k != "files"}
        reason = settings.modality_unavailable(series.get("modality", "CT")) if settings else None
        if reason:
            view.update(supported=False, unsupported_reason=reason)
        result["series"].append(view)
    return result


def public_revision(record):
    from .image_assets import reference_image, public_image_asset, bind_structures

    asset = reference_image(record) if all(key in record for key in ("case_id", "ct_file", "geometry")) else None
    result = {k: v for k, v in record.items() if k not in ("ct_file", "directory", "inference_manifest", "image_asset")}
    if asset is not None:
        result["reference_image_id"] = asset["id"]
        result["image_asset"] = public_image_asset(asset)
    result.setdefault("kind", "segmentation" if record["structures"] else "ct")
    result["structures"] = [
        {k: v for k, v in s.items() if k != "mask_file"}
        for s in (bind_structures(record["structures"], asset["id"]) if asset is not None else record["structures"])
    ]
    manifest = record.get("inference_manifest")
    if manifest:
        result["model_provenance"] = {
            k: manifest[k] for k in ("engine", "engine_version", "nnunet_version", "resolution", "started_at", "finished_at") if k in manifest
        }
        result["model_provenance"]["tasks"] = [t["task"] for t in manifest.get("tasks", [])]
        result["model_provenance"]["options"] = manifest.get("options")
        result["model_provenance"]["model_fingerprint_digest"] = manifest.get("model_fingerprint", {}).get("digest")
    return result


def public_job(record):
    public = {k: v for k, v in record.items() if k not in ("payload", "cancel_requested", "measurements")}
    # A send Orthanc has started runs to its end, so a client must stop offering Cancel.
    if (record.get("payload") or {}).get("delivery_state"):
        public["delivery_started"] = True
    return public
