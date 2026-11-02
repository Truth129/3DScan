import json

import pytest

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


def test_cross_case_revision_cannot_be_activated(tmp_path):
    store = Store(tmp_path)
    a, b = case(store), case(store)
    revision = store.save("revisions", dict(id=identifier(), case_id=a["id"], structures=[]))
    with pytest.raises(KeyError):
        store.activate(b["id"], revision["id"])
    assert store.get("cases", b["id"])["active_revision_id"] is None


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
    assert restored["phase"] == "arterial"
    assert restored["selected_series_uid"] == "arterial-series"


def test_public_records_do_not_expose_local_sources():
    record = dict(id="a", references={"a": "/private"}, source_dir="/private", series=[dict(uid="series", files=["/private/a.dcm"])])
    assert "references" not in public_case(record)
    assert "files" not in public_case(record)["series"][0]
    revision = dict(
        id="b",
        ct_file="/private/ct",
        directory="/private",
        structures=[dict(id="total:liver", mask_file="/private/mask")],
        inference_manifest={"engine_version": "2.18.0", "tasks": [{"task": "total", "report": {"input": "/private/source"}}]},
    )
    public = public_revision(revision)
    assert "ct_file" not in public and "directory" not in public
    assert "mask_file" not in public["structures"][0]
    assert "/private" not in __import__("json").dumps(public)


def test_case_list_leaves_out_file_paths_and_follows_a_writer_that_skips_summaries(tmp_path):
    store = Store(tmp_path)
    older = case(store)
    record = store.save(
        "cases", dict(id=identifier(), status="imported", active_revision_id=None, source_dir="/x", references={}, series=[dict(uid="1", files=["/x/1.dcm"])])
    )
    listed = store.case_summaries()
    assert [entry["id"] for entry in listed] == [record["id"], older["id"]]
    assert listed[0]["series"] == [dict(uid="1")] and "source_dir" not in listed[0]
    with store.connection() as con:
        con.execute("INSERT OR REPLACE INTO cases VALUES (?,?)", (record["id"], json.dumps(dict(record, status="ready"))))
    listed = store.case_summaries()
    assert [entry["status"] for entry in listed if entry["id"] == record["id"]] == ["ready"]
    assert public_case(listed[0]) == public_case(store.get("cases", record["id"]))


def test_opening_an_older_database_summarizes_its_cases(tmp_path):
    store = Store(tmp_path)
    record = case(store)
    with store.connection() as con:
        con.execute("DELETE FROM case_summaries")
        con.execute("PRAGMA user_version=3")
    Store(tmp_path)
    with store.connection() as con:
        assert [row["case_id"] for row in con.execute("SELECT case_id FROM case_summaries")] == [record["id"]]
        assert con.execute("PRAGMA user_version").fetchone()[0] == 5


def test_the_write_log_stays_open_and_can_still_be_checkpointed(tmp_path):
    store = Store(tmp_path)
    case(store)
    assert store.db.with_name(f"{store.db.name}-wal").exists()
    with store.connection() as con:
        assert con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
