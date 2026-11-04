import json
import sqlite3

import pytest

from scan3d.identity import EVERY_CASE
from scan3d.store import Store, identifier, now, public_case, public_revision


def case(store):
    return store.save("cases", dict(id=identifier(), created_at=now(), status="imported", active_revision_id=None, series=[]))


def test_revision_activation_and_completed_job_are_atomic(tmp_path):
    store = Store(tmp_path)
    record = case(store)
    queued = store.enqueue(record["id"], "refine", {})
    job = store.claim()
    revision = dict(id=identifier(), case_id=record["id"], parent_id=None, structures=[], created_at=now())
    store.update_job(queued["id"], cancel_requested=True)
    with pytest.raises(InterruptedError):
        store.commit_revision(revision, job)
    assert store.list("revisions") == []
    assert store.get("cases", record["id"])["active_revision_id"] is None
    assert store.get("jobs", job["id"])["status"] == "running"
    store.update_job(job["id"], cancel_requested=False)
    store.commit_revision(revision, job)
    assert store.get("cases", record["id"])["active_revision_id"] == revision["id"]
    assert store.get("jobs", job["id"])["status"] == "completed"


def test_a_job_claimed_on_a_trashed_case_is_flagged_and_still_running(tmp_path):
    store = Store(tmp_path)
    record = case(store)
    store.trash_case(record["id"], None)
    queued = store.enqueue(record["id"], "refine", {})
    job = store.claim()
    assert job["id"] == queued["id"] and job["status"] == "running" and job["cancel_requested"] is True
    assert store.get("jobs", queued["id"])["cancel_requested"] is True


def test_restart_preserves_completed_records_and_marks_running_interrupted(tmp_path):
    store = Store(tmp_path)
    record = case(store)
    job = store.enqueue(record["id"], "process", {})
    store.claim()
    queued = store.enqueue(record["id"], "refine", {})
    reopened = Store(tmp_path)
    reopened.recover()
    assert reopened.get("jobs", job["id"])["status"] == "failed"
    assert reopened.get("jobs", queued["id"])["status"] == "queued"
    assert reopened.claim()["id"] == queued["id"]


def test_undo_restores_the_matching_series_and_contrast_phase(tmp_path):
    store = Store(tmp_path)
    record = case(store)
    revisions = []
    for phase in ("arterial", "venous"):
        revision = dict(id=identifier(), case_id=record["id"], structures=[], acquisition={"series_uid": phase + "-series", "phase": phase})
        store.commit_revision(revision)
        revisions.append(revision)
    restored = store.activate(record["id"], revisions[0]["id"])
    assert restored["phase"] == "arterial" and restored["selected_series_uid"] == "arterial-series"
    # A revision of another case is never activated.
    other = case(store)
    with pytest.raises(KeyError):
        store.activate(other["id"], revisions[0]["id"])
    assert store.get("cases", other["id"])["active_revision_id"] is None


def test_public_records_do_not_expose_local_sources():
    record = dict(id="a", references={"a": "/private"}, source_dir="/private", series=[dict(uid="series", files=["/private/a.dcm"])])
    assert "/private" not in json.dumps(public_case(record))
    revision = dict(
        id="b",
        ct_file="/private/ct",
        directory="/private",
        structures=[dict(id="total:liver", mask_file="/private/mask")],
        inference_manifest={"engine_version": "2.18.0", "tasks": [{"task": "total", "report": {"input": "/private/source"}}]},
    )
    assert "/private" not in json.dumps(public_revision(revision))


def test_case_list_leaves_out_file_paths_and_follows_a_writer_that_skips_summaries(tmp_path):
    store = Store(tmp_path)
    older = case(store)
    record = store.save(
        "cases", dict(id=identifier(), status="imported", active_revision_id=None, source_dir="/x", references={}, series=[dict(uid="1", files=["/x/1.dcm"])])
    )
    listed = store.case_summaries(EVERY_CASE, None)
    assert [entry["id"] for entry in listed] == [record["id"], older["id"]]
    assert listed[0]["series"] == [dict(uid="1")] and "source_dir" not in listed[0]
    with store.connection() as con:
        con.execute("INSERT OR REPLACE INTO cases VALUES (?,?)", (record["id"], json.dumps(dict(record, status="ready"))))
    listed = store.case_summaries(EVERY_CASE, None)
    assert [entry["status"] for entry in listed if entry["id"] == record["id"]] == ["ready"]
    assert public_case(listed[0]) == {**public_case(store.get("cases", record["id"])), "opened_at": None, "added_by": None}


def test_opening_an_older_database_summarizes_its_cases(tmp_path):
    store = Store(tmp_path)
    record = case(store)
    with store.connection() as con:
        con.execute("DELETE FROM case_summaries")
        con.execute("PRAGMA user_version=3")
    Store(tmp_path)
    with store.connection() as con:
        assert [row["case_id"] for row in con.execute("SELECT case_id FROM case_summaries")] == [record["id"]]
        assert con.execute("PRAGMA user_version").fetchone()[0] == 8


def test_the_write_log_stays_open_and_can_still_be_checkpointed(tmp_path):
    store = Store(tmp_path)
    case(store)
    assert store.db.with_name(f"{store.db.name}-wal").exists()
    with store.connection() as con:
        assert con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0


def test_a_purged_case_refuses_every_insert(tmp_path):
    store = Store(tmp_path)
    record = case(store)
    case_id = record["id"]

    def run(sql, *values):
        with store.connection() as con:
            con.execute(sql, values)

    def summary():
        with store.connection() as con:
            con.execute("DELETE FROM case_summaries WHERE case_id=?", (case_id,))
            con.execute("INSERT INTO case_summaries VALUES (?,?)", (case_id, "{}"))

    inserts = (
        lambda: store.save("cases", record),
        lambda: store.save("revisions", dict(id=identifier(), case_id=case_id, structures=[])),
        lambda: store.save("jobs", dict(id=identifier(), case_id=case_id, status="completed")),
        lambda: store.save("review_sessions", dict(id=identifier(), case_id=case_id)),
        lambda: run("INSERT INTO image_assets VALUES (?,?,?)", identifier(), case_id, "{}"),
        summary,
        lambda: run("INSERT OR IGNORE INTO case_access(case_id,user_id,granted_at) VALUES (?,?,?)", case_id, identifier(), now()),
        lambda: run("INSERT INTO case_opened VALUES (?,?,?)", identifier(), case_id, now()),
    )
    store.trash_case(case_id, None)
    for insert in inserts:
        insert()
    for state in ("purging", "purged"):
        run("UPDATE trash SET state=? WHERE case_id=?", state, case_id)
        for insert in inserts:
            with pytest.raises(sqlite3.IntegrityError, match="^purged$"):
                insert()
