"""Source-bound SEG uploads and CPU revision publication, independent of Orthanc."""

import hashlib
import json
from pathlib import Path
import re

import pydicom
from pydicom.errors import InvalidDicomError

from .fileio import atomic_json, code_fingerprint, hash_file
from .image_assets import assert_reference_image
from .store import ReviewConflict, identifier, now


MAX_FILES = 64
MAX_UPLOAD_BYTES = 512 * 1024**2


def _owned_file(path, directory):
    path, directory = Path(path), Path(directory)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(directory.resolve()):
        raise ValueError("A SEG import input is unavailable or outside this case")
    return path


def _stat_identity(stat):
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _fingerprint(path, aggregate=None):
    path = Path(path)
    try:
        before = path.stat()
        if aggregate is None:
            digest = hash_file(path)
        else:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for block in iter(lambda: stream.read(4 * 1024**2), b""):
                    digest.update(block)
                    aggregate.update(block)
            digest = digest.hexdigest()
        after = path.stat()
    except OSError:
        raise ValueError("A SEG import input cannot be read") from None
    if _stat_identity(before) != _stat_identity(after):
        raise ValueError("A SEG import input changed while it was being checked")
    return {"path": str(path), "sha256": digest, "size": after.st_size}


def series_files(service, case_id, case, source):
    """The retained files of a reference's original CT series, proven to lie inside this case."""
    series = next((entry for entry in case["series"] if entry["uid"] == source.get("series_uid")), None)
    if not series or series.get("modality") != "CT" or not series.get("files") or len(series["files"]) > 100_000:
        raise ValueError("The prepared CT has no complete original DICOM series for SEG matching")
    for key in ("study_uid", "frame_of_reference_uid"):
        if not source.get(key) or source[key] != series.get(key):
            raise ValueError("The prepared CT has no verified DICOM study and coordinate frame; prepare its original series again")
    directory = Path(case.get("source_dir", ""))
    if directory.is_symlink() or not directory.resolve().is_relative_to(service.store.case_dir(case_id).resolve()):
        raise ValueError("The original CT source is unavailable")
    paths = sorted((_owned_file(path, directory) for path in series["files"]), key=str)
    if len({path.resolve() for path in paths}) != len(paths):
        raise ValueError("The original CT contains repeated source files")
    return paths


def prepared_digest(service, case_id, ct_file):
    """The complete sorted DICOM byte stream names a normally prepared reference."""
    converted = Path(ct_file).parent
    if converted.parent == service.store.case_dir(case_id) / "converted" and re.fullmatch(r"[a-f0-9]{64}", converted.name):
        return converted.name
    return None


def _inputs(service, case_id, revision_id):
    parent = service.store.revision(case_id, revision_id)
    case = service.store.get("cases", case_id)
    if case.get("active_revision_id") != revision_id:
        raise ReviewConflict("The active revision changed; import SEG from the current CT")
    reference = service.store.resolve_reference(parent)
    if reference["modality"] != "CT":
        raise ValueError("DICOM SEG import currently requires a prepared classic CT source")
    if reason := service.settings.modality_unavailable(reference["modality"]):
        raise ValueError(reason)
    _owned_file(parent["ct_file"], service.store.case_dir(case_id))
    assert_reference_image(reference, Path(parent["ct_file"]), parent["geometry"])
    source = reference["source"]
    paths = series_files(service, case_id, case, source)
    for path in paths:
        try:
            header = pydicom.dcmread(path, stop_before_pixels=True, specific_tags=["StudyInstanceUID", "SeriesInstanceUID", "FrameOfReferenceUID"])
        except (OSError, InvalidDicomError, ValueError):
            raise ValueError("An original CT header cannot be checked for SEG import") from None
        if any(
            str(header.get(tag, "")) != source[key]
            for key, tag in (("study_uid", "StudyInstanceUID"), ("series_uid", "SeriesInstanceUID"), ("frame_of_reference_uid", "FrameOfReferenceUID"))
        ):
            raise ValueError("The original CT identities differ from the prepared source image")
    return parent, reference, paths


def stage_import(service, case_id, revision_id, uploads):
    """Retain uploaded bytes privately and freeze inputs before queueing CPU work."""
    parent, reference, source_paths = _inputs(service, case_id, revision_id)
    if not 1 <= len(uploads) <= MAX_FILES:
        raise ValueError("Select between 1 and 64 DICOM SEG files")
    directory = service.store.case_dir(case_id) / "seg-imports" / identifier()
    directory.mkdir(parents=True, mode=0o700)
    files, total = [], 0
    for index, uploaded in enumerate(uploads):
        path = directory / f"input-{index:03d}.dcm"
        with path.open("xb") as output:
            path.chmod(0o600)
            while block := uploaded.file.read(1024 * 1024):
                total += len(block)
                if total > min(MAX_UPLOAD_BYTES, service.settings.upload_limit):
                    raise ValueError("DICOM SEG uploads exceed the 512 MiB or configured upload limit")
                output.write(block)
        if not path.stat().st_size:
            raise ValueError("DICOM SEG files cannot be empty")
        files.append(_fingerprint(path))
    if len({entry["sha256"] for entry in files}) != len(files):
        raise ValueError("The same DICOM SEG file was selected more than once")
    aggregate = hashlib.sha256()
    sources = [_fingerprint(path, aggregate) for path in source_paths]
    # Check the preparation cache's binding even for edits made before enqueue.
    if prepared_digest(service, case_id, parent["ct_file"]) not in (None, aggregate.hexdigest()):
        raise ValueError("The original CT bytes no longer match the prepared source image")
    masks = [_fingerprint(Path(entry["mask_file"])) for entry in parent["structures"]]
    payload = {
        "revision_id": revision_id,
        "reference_image_id": reference["id"],
        "reference_sha256": reference["content_sha256"],
        "files": files,
        "sources": sources,
        "parent_masks": masks,
        "directory": str(directory),
    }
    payload["import_key"] = hashlib.sha256(
        json.dumps([revision_id, reference["id"], sorted(entry["sha256"] for entry in files)], separators=(",", ":")).encode()
    ).hexdigest()
    atomic_json(directory / "inputs.json", payload)
    with service.store.connection() as con:
        con.execute("BEGIN IMMEDIATE")
        row = con.execute("SELECT data FROM cases WHERE id=?", (case_id,)).fetchone()
        if json.loads(row["data"]).get("active_revision_id") != parent["id"]:
            raise ReviewConflict("The active revision changed during upload; select the current CT and retry")
        row = con.execute(
            "SELECT data FROM jobs WHERE case_id=? AND json_extract(data,'$.kind')='dicom_seg_import' "
            "AND json_extract(data,'$.payload.import_key')=? AND status IN ('queued','running') ORDER BY rowid DESC LIMIT 1",
            (case_id, payload["import_key"]),
        ).fetchone()
        if row:
            return json.loads(row["data"])
        job = dict(
            id=identifier(),
            case_id=case_id,
            kind="dicom_seg_import",
            status="queued",
            stage="Queued",
            error=None,
            revision_id=None,
            source_revision_id=revision_id,
            created_at=now(),
            payload=payload,
            cancel_requested=False,
        )
        con.execute("INSERT INTO jobs VALUES (?,?,?,?)", (job["id"], case_id, job["status"], json.dumps(job, allow_nan=False)))
    return job


def _verify(service, job):
    payload = job["payload"]
    parent, reference, source_paths = _inputs(service, job["case_id"], payload["revision_id"])
    if reference["id"] != payload["reference_image_id"] or reference["content_sha256"] != payload["reference_sha256"]:
        raise ValueError("The prepared CT changed after SEG import was queued")
    if [str(path) for path in source_paths] != [entry["path"] for entry in payload["sources"]]:
        raise ValueError("The original CT series changed after SEG import was queued")
    upload_root = service.store.case_dir(job["case_id"]) / "seg-imports"
    for entry in [*payload["files"], *payload["sources"]]:
        service.check_cancel(job)
        path = _owned_file(entry["path"], upload_root if entry in payload["files"] else service.store.case_dir(job["case_id"]))
        if _fingerprint(path) != entry:
            raise ValueError("A CT or SEG input changed after import was queued")
    for entry in payload["parent_masks"]:
        service.check_cancel(job)
        if _fingerprint(Path(entry["path"])) != entry:
            raise ValueError("An existing segmentation mask changed after import was queued")
    return parent


def run_import(service, job):
    from .dicom_seg_import import import_segmentation

    service.stage(job, "Checking DICOM SEG source identity and exact CT geometry")
    parent = _verify(service, job)
    payload = job["payload"]
    imported = import_segmentation(
        [Path(entry["path"]) for entry in payload["sources"]],
        Path(parent["ct_file"]),
        [Path(entry["path"]) for entry in payload["files"]],
        Path(payload["directory"]) / "masks",
        cancelled=lambda: service.cancelled(job),
    )
    existing = {entry["id"] for entry in parent["structures"]}
    if any(entry["id"] in existing for entry in imported):
        raise ValueError("These DICOM SEG segments are already present in this revision")
    service.stage(job, "Preparing imported segmentation measurements and surfaces")
    result = service.build_revision(
        job["case_id"],
        Path(parent["ct_file"]),
        [*parent["structures"], *imported],
        source="dicom_seg_import",
        parent=parent["id"],
        acquisition=parent.get("acquisition"),
        job=job,
        operation={"type": "dicom_seg_import", "code_sha256": code_fingerprint(), "input_sha256": sorted(entry["sha256"] for entry in payload["files"])},
        extra_qc=service._inherited_qc(parent),
    )
    _verify(service, job)
    return result
