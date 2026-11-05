"""`scan3d archive-check`: what this workstation's DICOMweb archive does, read-only by default.

It prints counts, the archive key and transfer syntaxes, never a patient name, ID, UID or
search term, so its output can go into a support ticket. `--write-synthetic` also stores one
synthetic image in a new synthetic study and reads it back. Use it on scratch archives only:
DICOMweb has no way to delete it.
"""

from dataclasses import dataclass
import hashlib
import logging
from pathlib import Path
import tempfile
from typing import Literal
import warnings

import numpy as np
from pydicom.config import disable_value_validation
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, SecondaryCaptureImageStorage, generate_uid

from ..errors import NotFound
from .dicomweb import client_for
from .errors import ArchiveChanged, ArchiveError, ArchiveTooLarge
from .exports import _same_content, _Unverifiable

_FAILURES = (ArchiveError, ArchiveChanged, NotFound, ValueError, OSError)
_RETRIEVE_LIMIT = 64 * 1024**2


@dataclass(frozen=True)
class Result:
    level: Literal["ok", "warn", "fail"]
    check: str
    detail: str


def _reason(exc):
    # The client's messages are fixed text; anything else is named by its kind alone.
    return str(exc) if isinstance(exc, (ArchiveError, ArchiveChanged, NotFound, ValueError)) else f"The request failed ({type(exc).__name__})."


def synthetic_image(path):
    """One small synthetic image in a new synthetic study, named so nobody mistakes it for a patient's."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID, meta.MediaStorageSOPInstanceUID, meta.TransferSyntaxUID = SecondaryCaptureImageStorage, generate_uid(), ExplicitVRLittleEndian
    ds = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID, ds.SOPInstanceUID = meta.MediaStorageSOPClassUID, meta.MediaStorageSOPInstanceUID
    ds.StudyInstanceUID, ds.SeriesInstanceUID = generate_uid(), generate_uid()
    ds.PatientName, ds.PatientID = "SCAN3D^ARCHIVE CHECK", "SCAN3D-ARCHIVE-CHECK"
    ds.StudyDescription = ds.SeriesDescription = "3DScan archive check (synthetic)"
    ds.Modality, ds.ConversionType, ds.SeriesNumber, ds.InstanceNumber = "OT", "WSD", 1, 1
    ds.Rows = ds.Columns = 8
    ds.SamplesPerPixel, ds.PhotometricInterpretation = 1, "MONOCHROME2"
    ds.BitsAllocated = ds.BitsStored = 8
    ds.HighBit, ds.PixelRepresentation = 7, 0
    ds.PixelData = np.arange(64, dtype=np.uint8).tobytes()
    ds.save_as(path, enforce_file_format=True)
    return ds


def _store(client, directory):
    sent = directory / "synthetic.dcm"
    ds = synthetic_image(sent)
    answer = client.store(sent, expected_sha256=hashlib.sha256(sent.read_bytes()).hexdigest())
    held = directory / "read-back.dcm"
    got = client.read_back(ds.StudyInstanceUID, ds.SeriesInstanceUID, ds.SOPInstanceUID, held, max_bytes=16 * 1024**2)
    warnings = f" STOW warnings: {', '.join(answer.warnings)}." if answer.warnings else ""
    if got.sha256 == hashlib.sha256(sent.read_bytes()).hexdigest():
        return Result("ok", "Storing", f"Stored a synthetic image and read back the same bytes.{warnings}")
    try:
        same = _same_content(sent, held)
    except _Unverifiable:
        return Result("fail", "Storing", f"Stored a synthetic image, but its copy could not be read back here ({got.transfer_syntax_uid}).")
    if same:
        return Result("ok", "Storing", f"Stored a synthetic image and read back the same image, re-encoded as {got.transfer_syntax_uid}.{warnings}")
    return Result("fail", "Storing", "Stored a synthetic image, but the archive returned different contents for it.")


def run_checks(settings, *, write_synthetic=False):
    """Each check's outcome, stopping where a failure leaves nothing further to try."""
    results = []
    if missing := settings.missing_files():
        return [Result("fail", "Settings", f"{name} cannot be read.") for name in missing]
    try:
        client = client_for(settings)
    except _FAILURES as exc:
        return [Result("fail", "Settings", _reason(exc))]
    with client:
        try:
            client.status()
        except NotFound:
            # The commonest setup mistake: a URL without the server's own DICOMweb root.
            return [Result("fail", "Connection", "The server has no DICOMweb search at this URL. Check the path in SCAN3D_DICOMWEB_URL.")]
        except _FAILURES as exc:
            return [Result("fail", "Connection", _reason(exc))]
        results.append(Result("ok", "Connection", "The archive answers searches."))
        if settings.archive_id:
            results.append(Result("ok", "Archive key", f"{client.source_key}, from SCAN3D_DICOMWEB_ID."))
        else:
            results.append(
                Result(
                    "warn",
                    "Archive key",
                    f"{client.source_key}, from the URL. Set SCAN3D_DICOMWEB_ID before the first import, so a moved archive keeps its cases.",
                )
            )
        try:
            first = client.study_page(limit=3)
            if len(first) > 3:
                results.append(Result("warn", "Search limit", f"The archive ignores limit ({len(first)} studies for 3), so 3DScan trims its answers."))
            else:
                results.append(Result("ok", "Search limit", f"Honoured: {len(first)} of at most 3 studies."))
            if not first:
                results.append(Result("warn", "Studies", "The archive lists no studies, so series, images and retrieval were not checked."))
            else:
                if len(first) > 1:
                    second = client.study_page(limit=1, offset=1)
                    if second and second[0] == first[0]:
                        results.append(Result("warn", "Search offset", "The archive ignores offset, so only a search's first page can be trusted."))
                    else:
                        results.append(Result("ok", "Search offset", "Honoured."))
                results.extend(_images(client, first[0]))
        except _FAILURES as exc:
            results.append(Result("fail", "Searches", _reason(exc)))
        if not write_synthetic:
            results.append(Result("ok", "Storing", "Not tried. Run with --write-synthetic on a scratch archive to check it."))
        elif not settings.store:
            results.append(Result("warn", "Storing", "Not tried: SCAN3D_DICOMWEB_STORE=false keeps this archive read-only."))
        else:
            with tempfile.TemporaryDirectory(prefix="scan3d-archive-check-") as temporary:
                try:
                    results.append(_store(client, Path(temporary)))
                except _FAILURES as exc:
                    results.append(Result("fail", "Storing", _reason(exc)))
    return results


def _images(client, study_uid):
    """The first study's series, its first series' images, and one image retrieved and then deleted."""
    try:
        series = client.study_series(study_uid)["series"]
    except _FAILURES as exc:
        return [Result("fail", "Series", _reason(exc))]
    results = [Result("ok", "Series", f"{len(series)} series in the first study.")]
    for row in series:
        try:
            listed = client.instances(study_uid, row["series_uid"])
        except _FAILURES as exc:
            return [*results, Result("fail", "Images", _reason(exc))]
        if not listed:
            continue
        frames = sum(1 for *_ids, frame in listed if frame)
        results.append(Result("ok", "Images", f"{len(listed)} images in a series; {frames} name their Frame of Reference in the listing."))
        try:
            with tempfile.TemporaryDirectory(prefix="scan3d-archive-check-") as temporary:
                got = client.retrieve(study_uid, row["series_uid"], listed[0][0], Path(temporary) / "image.dcm", max_bytes=_RETRIEVE_LIMIT)
        except ArchiveTooLarge:
            # Imports allow far more; this only keeps a check from writing a large file.
            return [*results, Result("warn", "Retrieval", "The first image is over 64 MiB, more than this check reads, so retrieval was not proven.")]
        except _FAILURES as exc:
            return [*results, Result("fail", "Retrieval", _reason(exc))]
        return [*results, Result("ok", "Retrieval", f"Retrieved one image as {got.transfer_syntax_uid}, {got.size} bytes, and deleted it.")]
    return [*results, Result("warn", "Images", "No series of the first study lists any images.")]


def main(*, write_synthetic=False):
    """Print each check, and return the exit status: 1 if any failed, 2 if no archive is configured."""
    from ..config import Settings

    try:
        settings = Settings.from_env().dicomweb
    except ValueError as exc:
        print(f"fail  Settings: {exc}")
        return 2
    if settings is None:
        print("No DICOMweb archive is configured. Set SCAN3D_DICOMWEB_URL first.")
        return 2
    # pydicom warns about a non-conformant value by printing it, and a UID or name must never reach this output.
    pydicom_log = logging.getLogger("pydicom")
    level = pydicom_log.level
    pydicom_log.setLevel(logging.ERROR)
    try:
        with disable_value_validation(), warnings.catch_warnings():
            warnings.simplefilter("ignore")
            results = run_checks(settings, write_synthetic=write_synthetic)
    finally:
        pydicom_log.setLevel(level)
    for result in results:
        print(f"{result.level:<5} {result.check}: {result.detail}")
    return 1 if any(result.level == "fail" for result in results) else 0
