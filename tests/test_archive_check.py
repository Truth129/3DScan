"""`scan3d archive-check` against a fake archive: what it reports, and that it reports no patient data."""

from io import BytesIO

import pydicom
from pydicom.config import disable_value_validation
from pydicom.filereader import read_file_meta_info
import pytest

from scan3d.archive import check
from scan3d.archive.errors import ArchiveTooLarge
from scan3d.errors import NotFound
from test_archive_exports import ArchiveStore
from test_orthanc_imports import SERIES_UID, STUDY_UID, instance_bytes

URL = "https://archive.example.test/dicom-web"


class CheckArchive(ArchiveStore):
    """Four studies, listed whatever limit is asked for, as some archives do."""

    def study_page(self, *, limit, offset=0):
        return [STUDY_UID, "1.2.3.2", "1.2.3.3", "1.2.3.4"][offset:]


class LegacyArchive(CheckArchive):
    """Its only image has a SOP UID with a leading-zero component, which pydicom warns about."""

    UID = "1.2.826.20261105.073.9"

    def __init__(self):
        super().__init__()
        with disable_value_validation():
            ds = pydicom.dcmread(BytesIO(instance_bytes(0)))
            ds.SOPInstanceUID = ds.file_meta.MediaStorageSOPInstanceUID = self.UID
            output = BytesIO()
            ds.save_as(output, enforce_file_format=True)
        self.series = {SERIES_UID: [output.getvalue()]}

    def retrieve(self, study_uid, series_uid, sop_uid, path, **kwargs):
        got = super().retrieve(study_uid, series_uid, sop_uid, path, **kwargs)
        # As the real client checks each file's identity.
        read_file_meta_info(path).get("MediaStorageSOPInstanceUID")
        return got


def checked(monkeypatch, capsys, *, archive=None, write_synthetic=False, **environment):
    """The exit status, each check's level by name, everything printed (both streams), and the fake archive."""
    archive = archive or CheckArchive()
    monkeypatch.setattr(check, "client_for", lambda settings: archive)
    for name in ("SCAN3D_ORTHANC_URL", "SCAN3D_DICOMWEB_ID", "SCAN3D_DICOMWEB_TOKEN_FILE", "SCAN3D_DICOMWEB_AUTH"):
        monkeypatch.delenv(name, raising=False)
    for name, value in {"SCAN3D_DICOMWEB_URL": URL, **environment}.items():
        monkeypatch.setenv(name, value)
    status = check.main(write_synthetic=write_synthetic)
    printed = capsys.readouterr()
    lines = printed.out.splitlines()
    return status, {line.split(":")[0].split(maxsplit=1)[1]: line.split()[0] for line in lines if ":" in line}, printed.out + printed.err, archive


def test_the_check_reports_what_the_archive_does_and_no_patient_data(monkeypatch, capsys):
    status, levels, output, archive = checked(monkeypatch, capsys)
    assert status == 0
    assert levels == {
        "Connection": "ok",
        "Archive key": "warn",
        "Search limit": "warn",
        "Search offset": "ok",
        "Series": "ok",
        "Images": "ok",
        "Retrieval": "ok",
        "Storing": "ok",
    }
    assert archive.source_key in output and "Not tried" in output and not archive.stores
    assert not any(text in output for text in (STUDY_UID, SERIES_UID, archive.patient_id, "Synthetic"))


def test_write_synthetic_stores_one_synthetic_image_and_reads_it_back(monkeypatch, capsys):
    status, levels, output, archive = checked(monkeypatch, capsys, write_synthetic=True, SCAN3D_DICOMWEB_ID="synthetic")
    assert status == 0 and levels["Storing"] == "ok" and levels["Archive key"] == "ok"
    assert "read back the same bytes" in output
    (stored,) = archive.held.values()
    assert pydicom.dcmread(BytesIO(stored)).PatientID == "SCAN3D-ARCHIVE-CHECK"


def test_a_non_conformant_uid_is_never_printed_logged_or_warned(monkeypatch, capsys, caplog, recwarn):
    archive = LegacyArchive()
    recwarn.clear()
    status, levels, output, _archive = checked(monkeypatch, capsys, archive=archive)
    assert status == 0 and levels["Retrieval"] == "ok"
    assert LegacyArchive.UID not in output + caplog.text
    assert not [warning for warning in recwarn if LegacyArchive.UID in str(warning.message)]


@pytest.mark.parametrize(
    ("method", "error", "name", "level", "text"),
    [
        ("status", NotFound("The selected archive resource is no longer available"), "Connection", "fail", "Check the path in SCAN3D_DICOMWEB_URL."),
        ("retrieve", ArchiveTooLarge("The archive file exceeds the import size limit."), "Retrieval", "warn", "retrieval was not proven"),
    ],
)
def test_a_wrong_path_and_a_large_first_image_are_explained(monkeypatch, capsys, method, error, name, level, text):
    def refuse(self, *args, **kwargs):
        raise error

    monkeypatch.setattr(CheckArchive, method, refuse)
    status, levels, output, _archive = checked(monkeypatch, capsys)
    assert levels[name] == level and text in output and status == (1 if level == "fail" else 0)


def test_an_unconfigured_or_unreadable_archive_says_so(monkeypatch, capsys, tmp_path):
    monkeypatch.delenv("SCAN3D_DICOMWEB_URL", raising=False)
    assert check.main() == 2 and "No DICOMweb archive is configured" in capsys.readouterr().out
    token = str(tmp_path / "missing.token")
    status, levels, output, _archive = checked(monkeypatch, capsys, SCAN3D_DICOMWEB_AUTH="bearer", SCAN3D_DICOMWEB_TOKEN_FILE=token)
    assert status == 1 and levels == {"Settings": "fail"} and "SCAN3D_DICOMWEB_TOKEN_FILE cannot be read." in output and token not in output
