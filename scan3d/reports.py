"""Research-only measurement reports on a saved revision: which masks each needs, the CPU job and its result.

Research use only, not for diagnostic use. A report is a recipe of TotalSegmentator tasks, whose
masks are saved as a normal revision, and a `report` job that measures from that revision's masks,
so a corrected mask gives corrected numbers. The job runs `report_worker` in a child process, which
measures with TotalSegmentator's own report code, and never makes a revision. Its result lives in
`cases/<case>/reports/<report>/<key>/`: the pictures, `result.json`, then `complete.json` last. The
key covers the code, the image and the masks, so a later revision with the same masks reuses the
folder, and every read checks the folder again.

Adding a report is one `Definition` here and one function in `report_worker.WORKERS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
import hashlib
from importlib import metadata, util
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Callable

import numpy as np

from . import identity, review_volume, segmentation_options
from .config import ROOT
from .errors import NotFound
from .fileio import atomic_destination, atomic_json, atomic_text
from .identifiers import identifier
from .store import now, safe_id

log = logging.getLogger(__name__)

KIND = "report"
FORMAT = "scan3d-report-v1"
NOTICE = "Research use only. Not for diagnostic use."
_KEY = re.compile(r"[0-9a-f]{64}")
_FILE = re.compile(r"(cpr|plane-\d{1,2})\.png")
_LOG_TAIL = 4000  # bytes of a failed worker's log kept in the service log
# In the key, as coronary._CODE is: a saved result never outlives the code that measured or shaped it.
_CODE = [hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest() for name in ("reports.py", "report_worker.py", "aorta_report.py")]
# Upstream's report code measures with these; one that is absent (CI installs TotalSegmentator without
# its dependencies) reads as None.
_LIBRARIES = ("TotalSegmentator", "numpy", "scipy", "scikit-image", "nibabel", "networkx")


@dataclass(frozen=True)
class Need:
    """A mask a report reads: the structure `task:label` of the revision, never matched by name."""

    id: str
    title: str
    task: str
    label: str
    required: bool = False


@dataclass(frozen=True)
class Definition:
    id: str
    title: str
    engine: str
    module: str  # the upstream module whose presence makes the report available
    version: int  # in the key: bump when the code changes what a report measures or draws
    needs: tuple[Need, ...]
    recipe: tuple[str, ...]  # the segmentation tasks that make the needs, in the order they run
    shape: Callable[[dict], dict]  # the worker's output as the result's report fields
    summary: Callable[[dict], dict]  # a result's summary for the list of runs


@cache
def _definitions():
    from . import aorta_report

    return {definition.id: definition for definition in (aorta_report.DEFINITION,)}


def definition(report_id):
    try:
        return _definitions()[report_id]
    except KeyError:
        raise NotFound("Report not found") from None


def statuses(service, case_id, revision_id):
    return {"reports": [status(service, case_id, revision_id, report_id) for report_id in _definitions()]}


def status(service, case_id, revision_id, report_id):
    """Whether the report applies to this revision, which inputs it has, and what a segmentation would add."""
    report = definition(report_id)
    head = {"id": report.id, "title": report.title, "engine": report.engine}
    if revision_id.startswith("review-"):
        # A candidate alias is an unsaved proposal, as agreement and cinematic references say.
        return {
            **head,
            "available": False,
            "reason": f"Save the segmentation before running the {report.title.lower()} report",
            "ready": False,
            "needs": [],
            "missing_tasks": [],
            "can_segment": False,
            "segment_reason": "Save the segmentation first",
        }
    revision = service.store.revision(case_id, revision_id)
    unavailable = reason = _unavailable(service, revision, report)
    needs = _needs(revision, report)
    lost = [need for need in needs if need["required"] and need["state"] == "not_found"]
    if reason is None and lost:
        reason = f"The {' and the '.join(need['title'].lower() for need in lost)} {'were' if len(lost) > 1 else 'was'} looked for and not found"
    ready = reason is None and all(need["state"] == "present" for need in needs if need["required"])

    missing = [task for task in report.recipe if any(need["task"] == task and need["state"] == "missing" for need in needs)]
    try:
        from totalsegmentator.registry import requires_license
    except ImportError:
        # No TotalSegmentator on this install: `unavailable` says so, and no task can be offered.
        missing = []
    missing_tasks = [{"id": task, "title": segmentation_options._label(task), "license_required": requires_license(task)} for task in missing]
    case = service.store.get("cases", case_id)
    if unavailable:
        segment_reason = unavailable
    elif case.get("active_revision_id") != revision_id:
        segment_reason = "Segmentation runs only on the case's current revision"
    elif not missing:
        segment_reason = "Every task this report needs has run"
    elif lost:
        # The tasks left cannot bring back what one already looked for, so no report would follow.
        segment_reason = reason
    elif case.get("synthetic") or revision.get("synthetic"):
        # As the workspace, which offers no segmentation on the demo phantom.
        segment_reason = "The synthetic demo does not run segmentation models"
    elif any(task["license_required"] for task in missing_tasks) and not segmentation_options.license_configured():
        segment_reason = "These tasks need a TotalSegmentator license configured on this workstation"
    else:
        segment_reason = None
    return {
        **head,
        "available": unavailable is None,
        "reason": reason,
        "ready": ready,
        "needs": needs,
        "missing_tasks": missing_tasks,
        "can_segment": segment_reason is None,
        "segment_reason": segment_reason,
    }


def segment(service, case_id, revision_id, report_id, inference_path=None):
    """One segmentation job for the missing recipe tasks; the report follows on the revision it saves."""
    state = status(service, case_id, revision_id, report_id)
    if not state["can_segment"]:
        raise ValueError(state["segment_reason"])
    tasks = [task["id"] for task in state["missing_tasks"]]
    return service.segment_case(case_id, revision_id, tasks=tasks, inference_path=inference_path, report=report_id)


def start(service, case_id, revision_id, report_id, requested_by=None):
    """Queue a run, or return the queued, running or complete one for this revision and key."""
    report = definition(report_id)
    _, inputs, key = _prepare(service, case_id, revision_id, report)
    folder = _folder(service, case_id, report, key)
    with service.store.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        rows = con.execute("SELECT data FROM jobs WHERE case_id=? AND json_extract(data,'$.kind')=? ORDER BY rowid DESC", (case_id, KIND)).fetchall()
        for row in rows:
            job = json.loads(row["data"])
            if job.get("revision_id") != revision_id or job["payload"].get("report") != report.id or job["payload"].get("key") != key:
                continue
            # A run being cancelled is never handed back, and a complete job whose folder went is run
            # again rather than handed back to 404.
            if (job["status"] in ("queued", "running") and not job.get("cancel_requested")) or (
                job["status"] == "completed" and _stored(folder, key, pictures=True) is not None
            ):
                return job
        job = dict(
            id=identifier(),
            case_id=case_id,
            kind=KIND,
            status="queued",
            stage="Queued",
            error=None,
            revision_id=revision_id,
            created_at=now(),
            payload={"report": report.id, "key": key, "inputs": inputs},
            cancel_requested=False,
            requested_by=requested_by or identity.actor_id(),
        )
        con.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job["id"], case_id, job["status"], json.dumps(job, allow_nan=False)))
    return job


def analyses(service, case_id, revision_id, report_id):
    """This revision's runs of the report, newest first, with a summary once complete."""
    report = definition(report_id)
    _saved(service, case_id, revision_id, report)
    jobs = [
        job
        for job in service.store.list("jobs", case_id)
        if job["kind"] == KIND and job.get("revision_id") == revision_id and job["payload"].get("report") == report.id
    ]
    return [
        {
            "analysis_id": job["id"],
            "state": job["status"],
            "stage": job["stage"],
            "created_at": job["created_at"],
            "finished_at": job.get("completed_at"),
            "error": job.get("error"),
            "summary": (job.get("result") or {}).get("summary") if job["status"] == "completed" else None,
        }
        for job in jobs
    ]


def report(service, case_id, revision_id, report_id):
    state = status(service, case_id, revision_id, report_id)
    # A candidate alias has no runs; its status says to save first.
    return {"report": state, "analyses": [] if revision_id.startswith("review-") else analyses(service, case_id, revision_id, report_id)}


def read(service, case_id, revision_id, report_id, analysis_id):
    """A complete run's result, rechecked on disk, or NotFound."""
    result, _, _ = _complete(service, case_id, revision_id, report_id, analysis_id)
    return result


def picture(service, case_id, revision_id, report_id, analysis_id, name):
    """A picture the result lists, as bytes that still match the marker, or NotFound."""
    result, folder, marker = _complete(service, case_id, revision_id, report_id, analysis_id)
    if name not in result["files"] or not _FILE.fullmatch(name):
        raise NotFound("Report picture not found")
    try:
        body = (folder / name).read_bytes()
    except OSError:
        body = None
    if body is None or hashlib.sha256(body).hexdigest() != marker["files"].get(name):
        raise NotFound("The report result is missing or changed. Run it again.")
    return body


def run(service, job):
    """The job: measure in the worker and save, or complete at once from a matching folder."""
    service.stage(job, "Loading masks")
    report = definition(job["payload"]["report"])
    revision, inputs, key = _prepare(service, job["case_id"], job["revision_id"], report)
    if key != job["payload"]["key"]:
        # Saved masks never change, so in practice a restart onto new code or libraries did this.
        raise ValueError("The saved revision or the report software changed after this report was queued. Run it again.")
    folder = _folder(service, job["case_id"], report, key)
    result = _stored(folder, key, pictures=True)
    if result is None:
        result = _measure(service, job, report, revision, inputs, key, folder)
    service.store.update_job(
        job["id"],
        status="completed",
        stage="Complete",
        error=None,
        completed_at=now(),
        result={"analysis_id": job["id"], "key": key, "summary": result["summary"]},
    )


def _measure(service, job, report, revision, inputs, key, folder):
    work = folder.parent / f"work-{job['id']}"
    # A run the service lost mid-way leaves its scratch behind; nothing else uses it.
    for stale in folder.parent.glob("work-*"):
        if stale != work and _ended(service, stale.name.removeprefix("work-")):
            shutil.rmtree(stale, ignore_errors=True)
    structures = {structure["id"]: structure for structure in revision["structures"]}
    labels = {need.id: need for need in report.needs}
    request = {
        "report": report.id,
        "ct": revision["ct_file"],
        "masks": {need: {"path": structures[sid]["mask_file"], "task": labels[need].task, "label": labels[need].label} for need, sid in inputs.items()},
        "work": str(work / "scratch"),
        "output": str(work / "out"),
        "progress": str(work / "progress.txt"),
    }
    try:
        work.mkdir(parents=True, exist_ok=True)
        service.stage(job, "Measuring")
        raw = _launch(service, job, request, work)
        service.stage(job, "Saving")
        fields = report.shape(raw)
        files = [name for name in raw["files"] if _FILE.fullmatch(name)]
        digests = {}
        for name in files:
            body = (work / "out" / name).read_bytes()
            with atomic_destination(folder / name) as temporary:
                temporary.write_bytes(body)
            digests[name] = hashlib.sha256(body).hexdigest()
        result = {
            "format": FORMAT,
            "notice": NOTICE,
            "report": report.id,
            "analysis_id": job["id"],
            "case_id": job["case_id"],
            "revision_id": job["revision_id"],
            "key": key,
            "created_at": now(),
            "engine": {"name": "TotalSegmentator", "version": raw["engine_version"], "report_version": raw["report_version"]},
            "inputs": [{"need": need, "structure_id": sid} for need, sid in inputs.items()],
            **fields,
            "files": files,
        }
        result["summary"] = report.summary(result)
        body = json.dumps(result, allow_nan=False, separators=(",", ":"))
        atomic_text(folder / "result.json", body)
        atomic_json(folder / "complete.json", {"format": FORMAT, "key": key, "result_sha256": hashlib.sha256(body.encode()).hexdigest(), "files": digests})
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _launch(service, job, request, work):
    """Run the worker as a child process, stopping it on cancel, and return its output."""
    from .segmentation import _stop

    request_path = work / "request.json"
    atomic_json(request_path, request)
    environment = {**os.environ, "MPLBACKEND": "Agg", "PYTHONPATH": os.pathsep.join(filter(None, (str(ROOT), os.environ.get("PYTHONPATH"))))}
    command = [sys.executable, "-P", "-m", "scan3d.report_worker", str(request_path)]
    progress, shown = Path(request["progress"]), None
    log_path = work / "worker.log"
    with log_path.open("wb") as output:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT, env=environment, start_new_session=True)
        try:
            while process.poll() is None:
                if progress.is_file() and (current := progress.read_text()) != shown:
                    shown = current
                    service.stage(job, current)
                else:
                    service.check_cancel(job)
                time.sleep(0.2)
        except BaseException:
            _stop(process)
            raise
    out = Path(request["output"])
    if process.returncode:
        if (failure := out / "error.json").is_file():
            raise ValueError(json.loads(failure.read_text())["error"])
        with log_path.open("rb") as stream:
            stream.seek(max(0, log_path.stat().st_size - _LOG_TAIL))
            log.error("Report worker for job %s failed (exit %s):\n%s", job["id"], process.returncode, stream.read().decode(errors="replace"))
        raise RuntimeError(f"The report stopped (exit {process.returncode}); inspect the service log")
    return json.loads((out / "worker.json").read_text())


def _ended(service, job_id):
    try:
        return service.store.get("jobs", job_id)["status"] not in ("queued", "running")
    except (KeyError, ValueError):
        return True


def _saved(service, case_id, revision_id, report):
    if revision_id.startswith("review-"):
        raise NotFound(f"Save the segmentation before reading the {report.title.lower()} report")
    return service.store.revision(case_id, revision_id)


def _complete(service, case_id, revision_id, report_id, analysis_id):
    report = definition(report_id)
    _saved(service, case_id, revision_id, report)
    job = service.store.get("jobs", analysis_id)
    if job["case_id"] != case_id or job["kind"] != KIND or job.get("revision_id") != revision_id or job["payload"].get("report") != report.id:
        raise NotFound("Report not found")
    if job["status"] != "completed":
        raise NotFound("The report has not finished")
    key = job["payload"]["key"]
    folder = _folder(service, case_id, report, key)
    result = _stored(folder, key)
    if result is None:
        raise NotFound("The report result is missing or changed. Run it again.")
    # The folder may come from another revision's run with the same masks.
    result.update(analysis_id=job["id"], case_id=case_id, revision_id=revision_id)
    return result, folder, json.loads((folder / "complete.json").read_text())


def _unavailable(service, revision, report):
    """Why the report cannot apply to this revision at all, or None."""
    if service.store.resolve_reference(revision)["modality"] != "CT":
        return f"The {report.title.lower()} report needs a CT"
    # Upstream measures with each axis's voxel spacing, which a sheared grid does not have.
    matrix = np.asarray(revision["geometry"]["affine_ras"], dtype=float)[:3, :3]
    direction = matrix / np.linalg.norm(matrix, axis=0)
    if np.abs(direction.T @ direction - np.eye(3)).max() > 1e-4:
        return f"The {report.title.lower()} report cannot measure a scan with sheared slices"
    try:
        found = util.find_spec(report.module) is not None
    except (ImportError, ValueError):
        found = False
    if not found:
        return f"This server has no {report.engine}"
    return None


def _needs(revision, report):
    """Each need's state on this revision: present, missing (its task never ran on this line, or a later
    run of another task replaced its mask) or not_found."""
    from .segmentation_merge import _identity

    structures = {structure["id"]: structure for structure in revision["structures"]}
    # The tasks run on this revision's line of segmentations; a structure kept from a task shows it ran
    # on revisions from before runs were recorded.
    ran = {task for run in revision.get("segmentation_runs") or () for task in run.get("tasks") or ()}
    ran |= {structure.get("task") for structure in revision["structures"]}
    # A later run of another task takes a same-named structure (segmentation_merge), which is not this
    # task finding nothing: running the task again brings the structure back. A hand-drawn one is not.
    taken = set()
    for structure in revision["structures"]:
        if structure.get("task") not in (None, "custom"):
            try:
                taken.add(_identity(structure))
            except ValueError:
                pass
    needs = []
    for need in report.needs:
        key = f"{need.task}:{need.label}"
        structure = structures.get(key)
        if structure is not None and structure.get("task") != need.task:
            structure = None
        if structure is not None and structure.get("voxel_count", 1):
            state = "present"
        elif structure is None and _identity({"id": key, "task": need.task}) in taken:
            state = "missing"
        else:
            state = "not_found" if structure is not None or need.task in ran else "missing"
        needs.append(
            {
                "id": need.id,
                "title": need.title,
                "task": need.task,
                "task_title": segmentation_options._label(need.task),
                "required": need.required,
                "state": state,
                "structure_id": structure["id"] if state == "present" else None,
            }
        )
    return needs


def _prepare(service, case_id, revision_id, report):
    """The revision, its inputs by need and the key, refusing anything that cannot run."""
    if revision_id.startswith("review-"):
        raise ValueError(f"Save the segmentation before running the {report.title.lower()} report")
    revision = service.store.revision(case_id, revision_id)
    if reason := _unavailable(service, revision, report):
        raise ValueError(reason)
    needs = _needs(revision, report)
    lost = [need["title"].lower() for need in needs if need["required"] and need["state"] != "present"]
    if lost:
        raise ValueError(f"The {' and the '.join(lost)} {'are' if len(lost) > 1 else 'is'} not in this revision. Run the missing segmentation first.")
    inputs = {need["id"]: need["structure_id"] for need in needs if need["state"] == "present"}
    structures = {structure["id"]: structure for structure in revision["structures"]}
    try:
        masks = {need: review_volume._file_digest(structures[sid]["mask_file"]) for need, sid in inputs.items()}
    except OSError:
        raise ValueError("A mask of this revision cannot be read") from None
    asset = service.store.resolve_reference(revision)
    geometry = revision["geometry"]
    basis = {
        "format": "report-v1",
        "report": report.id,
        "version": report.version,
        "image": asset["content_sha256"],
        "shape": list(geometry["shape"]),
        "affine": geometry["affine_ras"],
        "masks": masks,
        "code": _CODE,
        "libraries": {name: _version(name) for name in _LIBRARIES},
    }
    key = hashlib.sha256(json.dumps(basis, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return revision, inputs, key


def _folder(service, case_id, report, key):
    # Never Store.case_dir, which would make the folder of a case being deleted.
    if not _KEY.fullmatch(key):
        raise ValueError("Invalid report key")
    return service.settings.data_dir / "cases" / safe_id(case_id) / "reports" / report.id / key


def _version(name):
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return None


def _stored(folder, key, pictures=False):
    """The saved result when the folder is complete and its bytes match the marker, else None.

    A read checks a picture's bytes as it is served. With `pictures`, asked where a run is reused,
    every picture is checked here, so a damaged one is measured again."""
    try:
        marker = json.loads((folder / "complete.json").read_text())
        body = (folder / "result.json").read_bytes()
        if not isinstance(marker, dict) or marker.get("format") != FORMAT or marker.get("key") != key:
            return None
        if marker.get("result_sha256") != hashlib.sha256(body).hexdigest():
            return None
        result = json.loads(body)
        for name, digest in (marker.get("files") or {}).items():
            path = folder / name
            if not path.is_file() or (pictures and hashlib.sha256(path.read_bytes()).hexdigest() != digest):
                return None
    except (OSError, ValueError):
        return None
    return result if isinstance(result, dict) and result.get("format") == FORMAT and result.get("key") == key else None
