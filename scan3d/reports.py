"""Research-only measurement reports on a saved revision: which masks each needs, the CPU job and its result.

Research use only, not for diagnostic use. A report is a recipe of TotalSegmentator tasks, whose
masks are saved as a normal revision, and a `report` job that measures from that revision's masks,
so a corrected mask gives corrected numbers. The job runs `report_worker` in a child process, which
measures with TotalSegmentator's own report code, and never makes a revision. Its result lives in
`cases/<case>/reports/<report>/<key>/`: the pictures, `result.json`, then `complete.json` last. The
key covers the code, the image and the masks, so a later revision with the same masks reuses the
folder, and every read checks the folder again.

Adding a report is one `Definition` in its own module, registered here, and one function in
`report_worker.WORKERS`.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache
import hashlib
from importlib import metadata, util
import inspect
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
_FILE = re.compile(r"(cpr|spine|plane-\d{1,2})\.png")
_LOG_TAIL = 4000  # bytes of a failed worker's log kept in the service log
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
    any_of: tuple[str, ...] = ()  # need ids of which at least one must be present; all are required until one is
    any_of_reason: str = ""  # said when the task ran and found none of `any_of`
    any_of_title: str = ""  # when set, `status` shows the `any_of` needs as one row with this title


@cache
def _definitions():
    from . import aorta_report, pulmonary_report, spine_report

    return {definition.id: definition for definition in (aorta_report.DEFINITION, pulmonary_report.DEFINITION, spine_report.DEFINITION)}


@cache
def _code(report_id):
    """In the key, as coronary._CODE is: a saved result never outlives the code that measured or shaped it,
    which is this module, the worker and the report's own definition module."""
    files = [Path(__file__).with_name(name) for name in ("reports.py", "report_worker.py")]
    files.append(Path(inspect.getfile(definition(report_id).shape)))
    return [hashlib.sha256(path.read_bytes()).hexdigest() for path in files]


def mm(cm):
    # Upstream rounds centimetres to one decimal, so millimetres are whole: no precision is added.
    return None if cm is None else float(round(cm * 10))


def area(cm2):
    return None if cm2 is None else round(float(cm2), 1)


def point(values):
    return None if values is None else [round(float(value), 2) for value in values]


def landmark(number, identifier, name, raw, vessel):
    """One landmark of a result from the worker's output: upstream's numbers in mm and its geometry in RAS mm,
    or why it is not measured. `vessel` names what a missing cross-section is of."""
    upstream = raw["upstream"]["landmarks"][str(number)]
    geometry = raw["geometry"].get(str(number))
    entry = {"number": number, "id": identifier, "name": name, "available": False, "unavailable_reason": None}
    entry.update(dict.fromkeys(("centre", "normal", "diameter_mm", "perpendicular_mm", "area_cm2", "ends", "perpendicular_ends", "outline", "picture")))
    if geometry is None:
        missing = raw["missing"].get(str(number)) or []
        entry["unavailable_reason"] = f"Not measured: {' and '.join(missing)}" if missing else "Could not be placed on the centreline"
    elif upstream.get("start") is None or not upstream.get("diameter"):
        entry["unavailable_reason"] = f"No cross-section of the {vessel} was found here"
    else:
        entry.update(
            available=True,
            centre=point(geometry["centre"]),
            normal=[round(float(value), 4) for value in geometry["normal"]],
            diameter_mm=mm(upstream["diameter"]),
            perpendicular_mm=mm(upstream["diameter_pd"]),
            area_cm2=area(upstream["area"]),
            ends=[point(upstream["start"]), point(upstream["end"])],
            perpendicular_ends=[point(upstream["start_pd"]), point(upstream["end_pd"])] if upstream.get("start_pd") else None,
            outline=[point(value) for value in geometry["outline"]],
            picture=geometry.get("picture"),
        )
    return entry


def landmark_summary(result, total):
    measured = [entry for entry in result["landmarks"] if entry["available"]]
    widest = max(measured, key=lambda entry: entry["diameter_mm"], default=None)
    return {
        "max_diameter_mm": widest and widest["diameter_mm"],
        "max_landmark": widest and widest["number"],
        "landmarks_available": len(measured),
        "landmarks_total": total,
    }


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
    # A need of `any_of` is lost only once none of them can still come.
    gone = all(need["state"] == "not_found" for need in needs if need["id"] in report.any_of)
    lost = [need for need in needs if need["required"] and need["state"] == "not_found" and (need["id"] not in report.any_of or gone)]
    if reason is None and lost:
        named = [need for need in lost if need["id"] not in report.any_of]
        parts = [report.any_of_reason] if len(named) < len(lost) else []
        if named:
            parts.insert(0, f"The {' and the '.join(need['title'].lower() for need in named)} {'were' if len(named) > 1 else 'was'} looked for and not found")
        reason = ". ".join(parts)
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
        "needs": _shown(report, needs),
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


def spare(report_id, previous, incoming, tasks):
    """The incoming structures a report's segmentation merges, and the ids of those it leaves out.

    A recipe task can also mark a structure the report does not read, say heartchambers_highres's
    aorta, and segmentation_merge would let it take the same-named segment of another task, which
    another report may read and someone may have corrected. Such a structure is left out; one the
    report reads still replaces as in any segmentation, and so does one in a relationship or one
    whose id the previous revision holds, a re-run's own earlier output, which the merge removes."""
    from .segmentation_merge import _identity

    def name(entry):
        try:
            return _identity(entry)
        except ValueError:
            return None

    read = {f"{need.task}:{need.label}" for need in definition(report_id).needs}
    kept = {name(entry) for entry in previous if entry.get("task") not in tasks} - {None}
    # Ids that replace as in any segmentation: those the report reads, a re-run's own earlier outputs
    # (left out, the merge would still remove the old one) and the peers of a relationship.
    replaces = read | {entry.get("id") for entry in previous}
    for entry in incoming:
        value = entry.get("relationship")
        if isinstance(value, dict):
            peers = value.get("exclusive_with")
            replaces.update(peer for peer in [value.get("parent_id"), *(peers if isinstance(peers, list) else ())] if isinstance(peer, str))
    left = {entry["id"] for entry in incoming if entry["id"] not in replaces and entry.get("relationship") is None and name(entry) in kept}
    return [entry for entry in incoming if entry["id"] not in left], sorted(left)


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
    """Each need's state on this revision: present, missing (its task never ran on this line, or a run of
    another task after its last run may have replaced its mask) or not_found."""
    from .nnunet_models import MODELS
    from .segmentation_merge import REPLACEMENT_POLICY, _anatomical_name, _family

    def _identity(structure):
        # The merge's identity without its vertebral-body family: on revision lines from before that family,
        # a body and the whole vertebra of its level took each other's mask.
        return structure.get("task") if structure.get("task") in MODELS else None, _anatomical_name(structure)

    structures = {structure["id"]: structure for structure in revision["structures"]}
    # The tasks run on this revision's line of segmentations, with the index of each one's last run; a
    # structure kept from a task shows it ran on revisions from before runs were recorded.
    # `legacy` is the same for runs not recorded as merged under today's rule.
    last, legacy = {}, {}
    for index, run in enumerate(revision.get("segmentation_runs") or ()):
        for task in run.get("tasks") or ():
            last[task] = index
            if run.get("replacement_policy") != REPLACEMENT_POLICY:
                legacy[task] = index
    ran = set(last) | {structure.get("task") for structure in revision["structures"]}
    # A later run of another task takes a same-named structure (segmentation_merge), which is not this
    # task finding nothing: running the task again brings the structure back. A hand-drawn one is not.
    taken = {}
    for structure in revision["structures"]:
        if structure.get("task") not in (None, "custom"):
            try:
                taken.setdefault(_identity(structure), set()).add(structure["task"])
            except ValueError:
                pass

    def replaced(task, holders):
        # Only a holder that ran after the need's task can have taken its mask. When the need's task ran
        # last, or in the same run, it found nothing itself, and offering it again would find nothing
        # again. Without a recorded order (revisions from before runs were recorded) it may have been.
        # A holder of another merge family (a whole vertebra for a body) can have taken it only through a
        # run merged before body masks had their own family; under today's rule it holds its own mask.
        def took(holder):
            if _family(holder) == _family(task):
                return holder not in last or task not in last or last[holder] > last[task]
            return holder not in last or (holder in legacy and (task not in last or legacy[holder] > last[task]))

        return any(took(holder) for holder in holders)

    needs = []
    for need in report.needs:
        key = f"{need.task}:{need.label}"
        structure = structures.get(key)
        if structure is not None and structure.get("task") != need.task:
            structure = None
        if structure is not None and structure.get("voxel_count", 1):
            state = "present"
        elif structure is None and replaced(need.task, taken.get(_identity({"id": key, "task": need.task}), ())):
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
    # Until one of `any_of` is present, each is required: the report cannot run without one.
    if report.any_of and not any(need["state"] == "present" for need in needs if need["id"] in report.any_of):
        for need in needs:
            if need["id"] in report.any_of:
                need["required"] = True
    return needs


def _shown(report, needs):
    """The needs as `status` returns them: with `any_of_title`, the `any_of` needs are one row, where the
    first of them was, present once any is and not found once all are."""
    group = [need for need in needs if need["id"] in report.any_of]
    if not report.any_of_title or not group:
        return needs
    states = {need["state"] for need in group}
    row = {
        "id": "any_of",
        "title": report.any_of_title,
        "task": group[0]["task"],
        "task_title": group[0]["task_title"],
        "required": True,
        "state": "present" if "present" in states else "not_found" if states == {"not_found"} else "missing",
        "structure_id": None,
    }
    first = needs.index(group[0])
    return [*needs[:first], row, *(need for need in needs[first:] if need["id"] not in report.any_of)]


def _prepare(service, case_id, revision_id, report):
    """The revision, its inputs by need and the key, refusing anything that cannot run."""
    if revision_id.startswith("review-"):
        raise ValueError(f"Save the segmentation before running the {report.title.lower()} report")
    revision = service.store.revision(case_id, revision_id)
    if reason := _unavailable(service, revision, report):
        raise ValueError(reason)
    needs = _needs(revision, report)
    lost = [need for need in needs if need["required"] and need["state"] != "present"]
    if report.any_of and all(need["state"] == "not_found" for need in needs if need["id"] in report.any_of):
        raise ValueError(report.any_of_reason)
    # A titled `any_of` group is named once, and is plural.
    named = list(dict.fromkeys((report.any_of_title if need["id"] in report.any_of and report.any_of_title else need["title"]).lower() for need in lost))
    if lost:
        plural = len(named) > 1 or len(named) < len(lost)
        raise ValueError(f"The {' and the '.join(named)} {'are' if plural else 'is'} not in this revision. Run the missing segmentation first.")
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
        "code": _code(report.id),
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
