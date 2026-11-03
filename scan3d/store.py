"""SQLite records are committed only after their filesystem artifacts are complete."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
import weakref

from . import identity, inference_paths
from .accounts import Accounts
from .errors import Conflict, NotFound
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
                CREATE TABLE IF NOT EXISTS case_summaries(case_id TEXT PRIMARY KEY, data TEXT NOT NULL);
                -- Any write drops the summary, so a writer that skips it, such as an older build
                -- after a rollback, leaves the list reading the full record and never a stale one.
                CREATE TRIGGER IF NOT EXISTS case_summary_insert AFTER INSERT ON cases BEGIN DELETE FROM case_summaries WHERE case_id=NEW.id; END;
                CREATE TRIGGER IF NOT EXISTS case_summary_update AFTER UPDATE ON cases BEGIN DELETE FROM case_summaries WHERE case_id=NEW.id; END;
                CREATE TRIGGER IF NOT EXISTS case_summary_delete AFTER DELETE ON cases BEGIN DELETE FROM case_summaries WHERE case_id=OLD.id; END;
                -- Ownership stays beside the case records, with no column on `cases` and no foreign
                -- keys, so that older code still runs against this file after a rollback.
                CREATE TABLE IF NOT EXISTS users(
                  id TEXT PRIMARY KEY,
                  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
                  display_name TEXT NOT NULL,
                  password_hash TEXT,
                  role TEXT NOT NULL DEFAULT 'member' CHECK(role IN ('admin','member')),
                  archive_access INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, disabled_at TEXT);
                CREATE TABLE IF NOT EXISTS sessions(
                  token_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL,
                  shared_device INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL, last_seen_at TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS session_user ON sessions(user_id);
                CREATE TABLE IF NOT EXISTS case_access(
                  case_id TEXT NOT NULL, user_id TEXT NOT NULL,
                  role TEXT NOT NULL DEFAULT 'owner' CHECK(role IN ('owner','editor','viewer')),
                  granted_by TEXT, granted_at TEXT NOT NULL,
                  PRIMARY KEY(case_id, user_id)) WITHOUT ROWID;
                CREATE INDEX IF NOT EXISTS access_user ON case_access(user_id, case_id);
                CREATE TABLE IF NOT EXISTS audit_events(
                  seq INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                  actor_id TEXT, action TEXT NOT NULL, case_id TEXT, target TEXT,
                  detail TEXT NOT NULL DEFAULT '{}');
                CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_events
                  BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
                CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_events
                  BEGIN SELECT RAISE(ABORT,'audit is append-only'); END;
                CREATE TABLE IF NOT EXISTS trash(
                  id TEXT PRIMARY KEY,
                  kind TEXT NOT NULL CHECK(kind IN ('case','revisions')),
                  case_id TEXT NOT NULL,
                  revision_ids TEXT NOT NULL DEFAULT '[]',
                  image_ids TEXT NOT NULL DEFAULT '[]',
                  prior_active_revision TEXT,
                  state TEXT NOT NULL CHECK(state IN ('trashed','purging','purged')),
                  trashed_by TEXT, trashed_at TEXT NOT NULL);
                CREATE UNIQUE INDEX IF NOT EXISTS trash_case ON trash(case_id) WHERE kind='case';
                CREATE TABLE IF NOT EXISTS case_opened(
                  user_id TEXT NOT NULL, case_id TEXT NOT NULL, opened_at TEXT NOT NULL,
                  PRIMARY KEY(user_id, case_id)) WITHOUT ROWID;
                -- A link's code is kept only as its SHA-256. A link works once, for a day.
                CREATE TABLE IF NOT EXISTS one_time_codes(
                  token_hash TEXT PRIMARY KEY,
                  purpose TEXT NOT NULL CHECK(purpose IN ('invite','reset')),
                  user_id TEXT NOT NULL, created_by TEXT NOT NULL, expires_at TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS one_time_code_user ON one_time_codes(user_id);
                -- When each person last used 3DScan. Sessions go at sign-out, so they cannot say.
                CREATE TABLE IF NOT EXISTS last_seen(user_id TEXT PRIMARY KEY, seen_at TEXT NOT NULL) WITHOUT ROWID;
                -- A purged case stays gone: any insert for its ID is refused, at every call site and in older builds.
                CREATE TRIGGER IF NOT EXISTS cases_stay_purged BEFORE INSERT ON cases
                  WHEN EXISTS(SELECT 1 FROM trash WHERE case_id=NEW.id AND kind='case' AND state IN ('purging','purged'))
                  BEGIN SELECT RAISE(ABORT,'purged'); END;
                CREATE TRIGGER IF NOT EXISTS case_summaries_stay_purged BEFORE INSERT ON case_summaries
                  WHEN EXISTS(SELECT 1 FROM trash WHERE case_id=NEW.case_id AND kind='case' AND state IN ('purging','purged'))
                  BEGIN SELECT RAISE(ABORT,'purged'); END;
                CREATE TRIGGER IF NOT EXISTS revisions_stay_purged BEFORE INSERT ON revisions
                  WHEN EXISTS(SELECT 1 FROM trash WHERE case_id=NEW.case_id AND kind='case' AND state IN ('purging','purged'))
                  BEGIN SELECT RAISE(ABORT,'purged'); END;
                CREATE TRIGGER IF NOT EXISTS jobs_stay_purged BEFORE INSERT ON jobs
                  WHEN EXISTS(SELECT 1 FROM trash WHERE case_id=NEW.case_id AND kind='case' AND state IN ('purging','purged'))
                  BEGIN SELECT RAISE(ABORT,'purged'); END;
                CREATE TRIGGER IF NOT EXISTS review_sessions_stay_purged BEFORE INSERT ON review_sessions
                  WHEN EXISTS(SELECT 1 FROM trash WHERE case_id=NEW.case_id AND kind='case' AND state IN ('purging','purged'))
                  BEGIN SELECT RAISE(ABORT,'purged'); END;
                CREATE TRIGGER IF NOT EXISTS image_assets_stay_purged BEFORE INSERT ON image_assets
                  WHEN EXISTS(SELECT 1 FROM trash WHERE case_id=NEW.case_id AND kind='case' AND state IN ('purging','purged'))
                  BEGIN SELECT RAISE(ABORT,'purged'); END;
                CREATE TRIGGER IF NOT EXISTS case_access_stay_purged BEFORE INSERT ON case_access
                  WHEN EXISTS(SELECT 1 FROM trash WHERE case_id=NEW.case_id AND kind='case' AND state IN ('purging','purged'))
                  BEGIN SELECT RAISE(ABORT,'purged'); END;
                CREATE TRIGGER IF NOT EXISTS case_opened_stay_purged BEFORE INSERT ON case_opened
                  WHEN EXISTS(SELECT 1 FROM trash WHERE case_id=NEW.case_id AND kind='case' AND state IN ('purging','purged'))
                  BEGIN SELECT RAISE(ABORT,'purged'); END;
            """)
            if connection.execute("PRAGMA user_version").fetchone()[0] < 4:
                # Under the write lock, because a summary written from an older read would hide a newer record.
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute("PRAGMA user_version").fetchone()[0] < 4:
                    for row in connection.execute("SELECT data FROM cases").fetchall():
                        self.summarize_case(connection, json.loads(row["data"]))
                    connection.execute("PRAGMA user_version=4")
            if connection.execute("PRAGMA user_version").fetchone()[0] < 5:
                connection.execute("PRAGMA user_version=5")
            if connection.execute("PRAGMA user_version").fetchone()[0] < 6:
                connection.execute("PRAGMA user_version=6")
            if connection.execute("PRAGMA user_version").fetchone()[0] < 7:
                connection.execute("PRAGMA user_version=7")
            if connection.execute("PRAGMA user_version").fetchone()[0] < 8:
                connection.execute("PRAGMA user_version=8")
        # Closing the last connection to a WAL database checkpoints it and removes the log, which
        # would otherwise add milliseconds to every write. This one stays idle: its read joins
        # the database and finishes, so it holds no transaction and checkpoints still run.
        keeper = sqlite3.connect(self.db, check_same_thread=False)
        keeper.execute("SELECT 1 FROM cases LIMIT 1").fetchall()
        self._release_keeper = weakref.finalize(self, keeper.close)

    def close(self):
        """Empty the write log on a clean stop. A service stopped by signal runs no exit handlers."""
        self._release_keeper()

    @contextmanager
    def connection(self):
        con = sqlite3.connect(self.db, timeout=30)
        con.row_factory = sqlite3.Row
        # Removed records are overwritten rather than left in free pages. Builds differ on the default.
        con.execute("PRAGMA secure_delete=ON").fetchall()
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
                self.summarize_case(con, record)
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
        # Every case list goes through a scope, so private cases can return, and a new global reader must fail
        # loudly here. `storage_rows` and `existing_case_ids` are the only global readers, and the second gives IDs only.
        if table == "cases":
            raise ValueError("Read cases through visible_cases or case_summaries, with a scope")
        if table not in ("revisions", "jobs", "review_sessions", "image_assets"):
            raise ValueError("Unknown record type")
        with self.connection() as con:
            if case_id:
                rows = con.execute(f"SELECT data FROM {table} WHERE case_id=? ORDER BY rowid DESC", (case_id,)).fetchall()
            else:
                rows = con.execute(f"SELECT data FROM {table} ORDER BY rowid DESC").fetchall()
        return [json.loads(row["data"]) for row in rows]

    @staticmethod
    def summarize_case(con, case):
        """Call after writing a case, so the library need not parse every series' file paths."""
        con.execute("INSERT OR REPLACE INTO case_summaries VALUES (?,?)", (case["id"], json.dumps(public_case(case))))

    def create_case(self, record):
        """A case and its owner are one write, so a crash can never leave a new case with neither."""
        safe_id(record["id"])
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("INSERT INTO cases VALUES (?,?)", (record["id"], json.dumps(record, allow_nan=False)))
            self.summarize_case(con, record)
            grant_owner(con, record["id"])
        return record

    @staticmethod
    def _scope_join(scope):
        """A scope is EVERY_CASE or a user id; anything else is a defect, never an open filter."""
        if scope is identity.EVERY_CASE:
            return "", ()
        if isinstance(scope, str) and scope:
            return " JOIN case_access a ON a.case_id=c.id AND a.user_id=?", (scope,)
        raise TypeError("A case scope is EVERY_CASE or a user id")

    def _scoped_cases(self, columns, scope, opener, joins=""):
        """Rows of a scope, the opener's last opened first, then newest. A case in Trash reads as absent."""
        access, values = self._scope_join(scope)
        with self.connection() as con:
            return con.execute(
                f"SELECT {columns} FROM cases c{access}{joins} LEFT JOIN case_opened o ON o.user_id=? AND o.case_id=c.id "
                "WHERE NOT EXISTS(SELECT 1 FROM trash t WHERE t.case_id=c.id AND t.kind='case') "
                "ORDER BY o.opened_at IS NULL, o.opened_at DESC, c.rowid DESC",
                (*values, opener),
            ).fetchall()

    def case_summaries(self, scope, opener):
        """The cases in a scope, for `public_case`, in the opener's order. One with no summary reads in full."""
        rows = self._scoped_cases(
            "COALESCE(s.data, c.data) AS data, o.opened_at AS opened_at, w.user_id AS adder_id, u.display_name AS adder",
            scope,
            opener,
            " LEFT JOIN case_summaries s ON s.case_id=c.id" + _ADDER,
        )
        return [{**json.loads(row["data"]), "opened_at": row["opened_at"], "added_by": _added_by(row, opener)} for row in rows]

    def visible_cases(self, scope):
        """Full records of the cases in a scope, in no one's order. Prefer `case_summaries` for a list."""
        return [json.loads(row["data"]) for row in self._scoped_cases("c.data AS data", scope, None)]

    def case_ids(self, scope):
        """Every case in a scope, including any in Trash, for measuring."""
        access, values = self._scope_join(scope)
        with self.connection() as con:
            return [row["id"] for row in con.execute(f"SELECT c.id AS id FROM cases c{access}", values).fetchall()]

    def trashed_cases(self, scope, viewer):
        """The cases in a scope that are in Trash, last trashed first, for `public_case`."""
        access, values = self._scope_join(scope)
        with self.connection() as con:
            rows = con.execute(
                "SELECT COALESCE(s.data, c.data) AS data, t.trashed_at AS trashed_at, w.user_id AS adder_id, u.display_name AS adder "
                f"FROM trash t JOIN cases c ON c.id=t.case_id{access} LEFT JOIN case_summaries s ON s.case_id=c.id{_ADDER} "
                "WHERE t.kind='case' AND t.state='trashed' ORDER BY t.trashed_at DESC",
                values,
            ).fetchall()
        return [{**json.loads(row["data"]), "trashed_at": row["trashed_at"], "added_by": _added_by(row, viewer)} for row in rows]

    def storage_rows(self, scope):
        """Every case with its owner. A label reads only for its owner, because labels can hold patient text."""
        self._scope_join(scope)  # A bad scope must raise here too, or it would show every label.
        viewer = None if scope is identity.EVERY_CASE else scope
        with self.connection() as con:
            rows = con.execute(
                "SELECT COALESCE(s.data, c.data) AS data, a.user_id AS owner_id, u.display_name AS owner, t.trashed_at AS trashed_at FROM cases c "
                "LEFT JOIN case_summaries s ON s.case_id=c.id LEFT JOIN case_access a ON a.case_id=c.id AND a.role='owner' "
                "LEFT JOIN users u ON u.id=a.user_id LEFT JOIN trash t ON t.case_id=c.id AND t.kind='case' ORDER BY c.rowid DESC"
            ).fetchall()
        result = []
        for row in rows:
            case = json.loads(row["data"])
            label = case.get("label") if viewer is None or row["owner_id"] == viewer else None
            result.append(dict(id=case["id"], label=label, owner=row["owner"], created_at=case.get("created_at"), trashed_at=row["trashed_at"]))
        return result

    def mark_opened(self, case_id, user_id):
        safe_id(case_id)
        with self.connection() as con:
            changed = con.execute(
                "INSERT INTO case_opened(user_id,case_id,opened_at) SELECT ?,?,? WHERE EXISTS(SELECT 1 FROM cases WHERE id=?) "
                "ON CONFLICT(user_id,case_id) DO UPDATE SET opened_at=excluded.opened_at",
                (user_id, case_id, now(), case_id),
            ).rowcount
        if not changed:
            raise NotFound("Case not found")

    def trash_case(self, case_id, actor_id):
        """Nothing on disk moves, so paths and any late write stay valid. A separate row hides the case."""
        safe_id(case_id)
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            if con.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is None:
                raise NotFound("Case not found")
            # Two requests can pass the gate together. A plain insert would make the second a 500.
            added = con.execute(
                "INSERT INTO trash(id,kind,case_id,state,trashed_by,trashed_at) VALUES (?,'case',?,'trashed',?,?) "
                "ON CONFLICT(case_id) WHERE kind='case' DO NOTHING",
                (identifier(), case_id, actor_id, now()),
            ).rowcount
            if added:
                Accounts._write_audit(con, "case.trash", actor_id, case_id)
                # In this transaction, so no job can publish between hiding the case and stopping its work.
                con.execute(
                    "UPDATE jobs SET data=json_set(data,'$.cancel_requested',json('true')) WHERE case_id=? AND status IN ('queued','running')", (case_id,)
                )

    def restore_case(self, case_id, actor_id):
        safe_id(case_id)
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            if not con.execute("DELETE FROM trash WHERE case_id=? AND kind='case' AND state='trashed'", (case_id,)).rowcount:
                raise NotFound("Case not found")
            Accounts._write_audit(con, "case.restore", actor_id, case_id)

    @staticmethod
    def _purge_refusal(con, case_id, busy):
        """Why this case cannot be deleted forever now, raised. Not an admin is the gate's 403, before this."""
        if con.execute("SELECT 1 FROM trash WHERE case_id=? AND kind='case' AND state='trashed'", (case_id,)).fetchone() is None:
            raise NotFound("Case not found")
        # Trash flagged it to stop, and a job that never started holds no file, so only a running one waits.
        running = con.execute("SELECT json_extract(data,'$.kind') AS kind FROM jobs WHERE case_id=? AND status='running' LIMIT 1", (case_id,)).fetchone()
        if running and running["kind"] in ("orthanc_export", "orthanc_view_export"):
            # A send Orthanc has started finishes on purpose, which can take its upload and two more minutes.
            raise Conflict("A send to Orthanc is still finishing. Try again in a few minutes.")
        if running:
            raise Conflict("Work on this case is still stopping. Try again in a minute.")
        if busy():
            raise Conflict("This case is still in use. Try again in a minute.")

    def check_purge(self, case_id, busy):
        """The refusals of `purge_case`, with no write, so a case that cannot go yet costs no password hash."""
        safe_id(case_id)
        with self.connection() as con:
            self._purge_refusal(con, case_id, busy)

    def purge_case(self, case_id, actor_id, busy):
        """Step 1 of Delete forever: every row of the case goes in one transaction, and its trash row stays as the tombstone."""
        safe_id(case_id)
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            # The gate decided before the password check, which can wait, and a demotion meanwhile must stop the purge.
            Accounts._require_admin(con, actor_id)
            self._purge_refusal(con, case_id, busy)
            counts = {
                table: con.execute(f"DELETE FROM {table} WHERE case_id=?", (case_id,)).rowcount
                for table in ("revisions", "jobs", "review_sessions", "image_assets")
            }
            for table in ("case_access", "case_opened", "case_summaries"):
                con.execute(f"DELETE FROM {table} WHERE case_id=?", (case_id,))
            con.execute("DELETE FROM trash WHERE case_id=? AND kind='revisions'", (case_id,))
            con.execute("DELETE FROM cases WHERE id=?", (case_id,))
            con.execute("UPDATE trash SET state='purging' WHERE case_id=? AND kind='case'", (case_id,))
            Accounts._write_audit(con, "case.purge", actor_id, case_id, detail=counts)

    def trash_case_ids(self, state):
        with self.connection() as con:
            return [row["case_id"] for row in con.execute("SELECT case_id FROM trash WHERE kind='case' AND state=? ORDER BY trashed_at", (state,))]

    def existing_case_ids(self):
        """Every case ID, for the janitor's sweep only. IDs, never a list to show anyone."""
        with self.connection() as con:
            return {row["id"] for row in con.execute("SELECT id FROM cases")}

    def running_job(self):
        with self.connection() as con:
            return con.execute("SELECT 1 FROM jobs WHERE status='running' LIMIT 1").fetchone() is not None

    def live_surface_keys(self):
        with self.connection() as con:
            return live_surface_keys(con)

    def checkpoint(self):
        """Fold the write log into the database and empty it, so removed rows leave no copy there. False while a reader holds it."""
        with self.connection() as con:
            return con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0

    def finish_purge(self, case_ids):
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.executemany("UPDATE trash SET state='purged' WHERE case_id=? AND kind='case' AND state='purging'", [(case_id,) for case_id in case_ids])

    def rename_case(self, case_id, label):
        from .uploads import ImportConflict

        safe_id(case_id)
        label = label.strip()
        if not label or len(label) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in label):
            raise ValueError("A case name must contain 1 to 128 printable characters")
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT data FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                raise NotFound("Case not found")
            case = json.loads(row["data"])
            # An import holds its record in memory and saves it whole at the end, which would undo a new name.
            if case.get("status") == "importing":
                raise ImportConflict("This case is still importing. Rename it when the import finishes.")
            case["label"] = label
            con.execute("UPDATE cases SET data=? WHERE id=?", (json.dumps(case, allow_nan=False), case_id))
            self.summarize_case(con, case)
        return case

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

    def activate(self, case_id, revision_id, expected=None):
        acquisition = self.revision(case_id, revision_id).get("acquisition")
        with self.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT data FROM cases WHERE id=?", (case_id,)).fetchone()
            if row is None:
                raise NotFound("Case not found")
            case = json.loads(row["data"])
            # A page behind someone else's save picks from a History that lacks it, so it must not replace it unseen.
            if expected is not None and case.get("active_revision_id") != expected:
                raise Conflict("Someone else or another tab saved a newer version of this case. Show it, then try again.")
            case.update(
                active_revision_id=revision_id,
                status="ready",
                selected_series_uid=acquisition.get("series_uid") if acquisition else None,
                phase=acquisition.get("phase") if acquisition else None,
            )
            con.execute("UPDATE cases SET data=? WHERE id=?", (json.dumps(case, allow_nan=False), case_id))
            self.summarize_case(con, case)
        return case

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
            self.summarize_case(con, case)
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
            # Accept moves no generation, so this stops a tab that has not seen the Save, Discard included.
            if review["status"] == "accepting":
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
                # Publishing moves no generation, so this is what a tab that has not seen the run
                # is refused on. Without it, that tab's next save would cancel the run or replace
                # its result. Accept cannot move one, because its candidate must stay current.
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
                requested_by=identity.actor_id(),
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
            requested_by=identity.actor_id(),
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
            # Some requests enqueue after the Trash cancel has run, and queued rows outlive a restart.
            if con.execute("SELECT 1 FROM trash WHERE case_id=? AND kind='case'", (job["case_id"],)).fetchone():
                job["cancel_requested"] = True
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


def grant_owner(con, case_id):
    """Own a case that has no owner yet, inside the transaction that writes it.

    Nobody is signed in on the command line or with sign-in off, so that work goes to the oldest
    active admin. With no admin there is no owner yet, and `adopt_orphans` settles it later. A
    retried Orthanc import reuses its case and keeps the owner it has: the rule is in this
    statement, so no caller can add a second one.
    """
    actor = identity.actor_id()
    owner = Accounts.oldest_admin(con) if actor in (None, identity.LOCAL.id) else actor
    if owner:
        con.execute(
            "INSERT OR IGNORE INTO case_access(case_id,user_id,role,granted_by,granted_at) "
            "SELECT ?,?,'owner',?,? WHERE NOT EXISTS(SELECT 1 FROM case_access WHERE case_id=?)",
            (case_id, owner, actor, now(), case_id),
        )


# Who added a case: its owner row, which `grant_owner` writes once and nothing moves.
_ADDER = " LEFT JOIN case_access w ON w.case_id=c.id AND w.role='owner' LEFT JOIN users u ON u.id=w.user_id"


def _added_by(row, viewer):
    """The adder's display name for someone else. Your own cases, ownerless ones and sign-in off name nobody."""
    return None if viewer in (None, identity.LOCAL.id) or row["adder_id"] in (None, viewer) else row["adder"]


def live_surface_keys(con):
    """Every surface asset key a revision or a review draft names, wherever its record keeps the list."""
    keys = set()

    def walk(value):
        if isinstance(value, dict):
            for name, item in value.items():
                if name == "surface_assets" and isinstance(item, list):
                    keys.update(asset["key"] for asset in item if isinstance(asset, dict) and isinstance(asset.get("key"), str))
                else:
                    walk(item)
        elif isinstance(value, list):
            for item in value:
                walk(item)

    for table in ("revisions", "review_sessions"):
        for row in con.execute(f"SELECT data FROM {table}"):
            walk(json.loads(row[0]))
    return keys


def public_case(record, settings=None):
    """Client view; with settings, series of a modality not offered here read as unavailable."""
    result = {k: v for k, v in record.items() if k not in ("source_dir", "references", "added_sources")}
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
    public = {k: v for k, v in record.items() if k not in ("payload", "cancel_requested", "measurements", "requested_by")}
    # A send Orthanc has started runs to its end, so a client must stop offering Cancel.
    if (record.get("payload") or {}).get("delivery_state"):
        public["delivery_started"] = True
    return public
