"""Synthetic DICOMweb transport; no network, archive or patient data."""

import hashlib
from io import BytesIO
import json
import logging
import os
from urllib.parse import urlsplit

from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, HTJ2KLossless
import pytest
import requests
from requests.structures import CaseInsensitiveDict

from scan3d.archive import dicomjson, multipart
from scan3d.archive.dicomweb import DicomWebClient, Stored
from scan3d.archive.errors import ArchiveChanged, ArchiveError
from scan3d.errors import NotFound

ROOT = "1.2.826.0.1.3680043.10.543.20261105"
STUDY_UID, SERIES_UID, FRAME_UID, SOP_UID = f"{ROOT}.1", f"{ROOT}.2", f"{ROOT}.3", f"{ROOT}.4"
INSTANCES = f"/studies/{STUDY_UID}/series/{SERIES_UID}/instances"

BOUNDARY = "synthetic-boundary"
RELATED = f'multipart/related; type="application/dicom"; boundary="{BOUNDARY}"'


def related(*payloads, boundary=BOUNDARY):
    """A multipart/related body with a preamble and an epilogue, which readers must skip."""
    body = b"synthetic preamble\r\n"
    for payload in payloads:
        body += f"--{boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode() + payload + b"\r\n"
    return body + f"--{boundary}--\r\nsynthetic epilogue".encode()


def pieces(body, size):
    return [body[offset : offset + size] for offset in range(0, len(body), size)]


def element(vr, *values):
    return {"vr": vr, "Value": list(values)} if values else {"vr": vr}


def study_row(index, patient="SYNTHETIC"):
    return {
        "0020000D": element("UI", f"{ROOT}.10.{index}"),
        "00100010": element("PN", {"Alphabetic": "SYNTHETIC^PATIENT"}),
        "00100020": element("LO", patient),
        "00080061": element("CS", "CT", "SEG"),
        "00201206": element("IS", 2),
    }


def instance_row(index):
    return {"00080018": element("UI", f"{SERIES_UID}.{index}"), "00080016": element("UI", CTImageStorage), "00200052": element("UI", FRAME_UID)}


def instance_bytes(transfer_syntax, sop=SOP_UID):
    """A tiny synthetic instance; only its file meta matters to the transport."""
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID, meta.MediaStorageSOPInstanceUID, meta.TransferSyntaxUID = CTImageStorage, sop, transfer_syntax
    dataset = FileDataset(None, {}, file_meta=meta, preamble=b"\0" * 128)
    dataset.SOPClassUID, dataset.SOPInstanceUID = CTImageStorage, sop
    dataset.StudyInstanceUID, dataset.SeriesInstanceUID, dataset.PatientID = STUDY_UID, SERIES_UID, "SYNTHETIC"
    stream = BytesIO()
    dataset.save_as(stream, enforce_file_format=True)
    return stream.getvalue()


class Response:
    def __init__(self, content=b"", status=200, headers=None):
        self.status_code = status
        self.content = content
        self.headers = CaseInsensitiveDict(headers or {})
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size <= 1024**2
        for offset in range(0, len(self.content), 2):
            yield self.content[offset : offset + 2]

    def close(self):
        self.closed = True


def answer(value, status=200):
    return Response(json.dumps(value).encode(), status, {"Content-Type": "application/dicom+json"})


class Archive:
    """A DICOMweb server in memory: ``routes[(method, path)](request)`` gives each response."""

    def __init__(self):
        self.calls, self.responses, self.routes = [], [], {}
        self.trust_env, self.closed, self.failure = True, False, None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        route = self.routes.get((method, urlsplit(url).path.removeprefix("/dicom-web")))
        response = route(kwargs) if route else Response(b"synthetic server detail", 404)
        self.responses.append(response)
        return response

    def close(self):
        self.closed = True


@pytest.fixture
def archive():
    upstream = Archive()
    client = DicomWebClient(
        "https://Archive.Example.test:443/dicom-web/", auth="basic", username="synthetic-user", password="synthetic-password", session=upstream
    )
    return client, upstream


def test_person_name_rebuilds_dicom_groups():
    def name(*values):
        return dicomjson.person_name({"00100010": {"vr": "PN", "Value": list(values)}}, "00100010")

    assert name({"Alphabetic": "Yamada^Tarou", "Ideographic": "山田^太郎", "Phonetic": "やまだ^たろう"}) == "Yamada^Tarou=山田^太郎=やまだ^たろう"
    assert name({"Alphabetic": "Yamada^Tarou", "Ideographic": "山田^太郎", "Phonetic": ""}) == "Yamada^Tarou=山田^太郎"
    assert name({"Ideographic": "山田^太郎"}) == "=山田^太郎"
    assert name({"Alphabetic": "Synthetic^One"}, {"Alphabetic": "Synthetic^Two"}) == "Synthetic^One\\Synthetic^Two"
    assert name("Not^AnObject") == "" and dicomjson.person_name({}, "00100010") == ""


def test_multipart_reader_handles_split_boundaries_and_caps(tmp_path):
    # Near misses of the boundary inside the payload must stay payload, wherever chunks split.
    payload = b"DICM\r\n--synthetic-boundar\r\n-" * 3 + bytes(range(256))
    body = related(payload)
    for size in (1, 7, len(body)):
        (part,) = multipart.save_parts(pieces(body, size), RELATED, lambda index: tmp_path / f"{size}-{index}.dcm", max_parts=1, max_bytes=len(payload))
        assert part.path.read_bytes() == payload and part.size == len(payload)
        assert part.sha256 == hashlib.sha256(payload).hexdigest() and part.content_type == "application/dicom"
    truncated = body[: body.rindex(b"\r\n--")]
    for refused, limit, reason in (
        (related(payload, payload), len(payload) * 2, "more files"),
        (body, len(payload) - 1, "size limit"),
        (truncated, len(payload), "ended early"),
    ):
        with pytest.raises(ArchiveError, match=reason):
            multipart.save_parts(pieces(refused, 5), RELATED, lambda index: tmp_path / f"refused-{index}.dcm", max_parts=1, max_bytes=limit)
        assert not list(tmp_path.glob("refused-*"))


def test_find_studies_truncates_to_limit_and_treats_204_as_empty(archive):
    client, upstream = archive
    # This server ignores limit, so the client must cut the answer itself.
    upstream.routes[("GET", "/studies")] = lambda request: answer([study_row(index) for index in range(13)])
    rows = client.find_studies({"PatientName": "SYNTHETIC^PATIENT", "AccessionNumber": ""}, limit=12, offset=24)
    assert len(rows) == 12
    assert rows[0] == {
        "study_uid": f"{ROOT}.10.0",
        "description": "",
        "study_date": "",
        "patient_id": "SYNTHETIC",
        "issuer_of_patient_id": "",
        "patient_name": "SYNTHETIC^PATIENT",
        "accession_number": "",
        "series_count": 2,
        "modalities": "CT\\SEG",
    }
    method, url, request = upstream.calls[-1]
    assert (method, url) == ("GET", "https://archive.example.test/dicom-web/studies")
    assert dict(request["params"]) == {
        "PatientName": "SYNTHETIC^PATIENT",
        "fuzzymatching": "false",
        "includefield": "00081030,00100021,00080061,00201206",
        "limit": "12",
        "offset": "24",
    }
    assert request["headers"]["Accept"] == "application/dicom+json" and request["auth"] == ("synthetic-user", "synthetic-password")
    assert request["allow_redirects"] is False and request["verify"] is True and request["stream"] is True and request["timeout"] == (10, 30)
    assert upstream.trust_env is False
    upstream.routes[("GET", "/studies")] = lambda request: Response(status=204)
    assert client.find_studies({"PatientID": "SYNTHETIC"}) == []
    with pytest.raises(ValueError, match="exact searches"):
        client.find_studies({"PatientID": "SYNTH*"})
    with pytest.raises(NotFound):
        client.study(f"{ROOT}.10.0")
    # One Study Instance UID filed under two patients is not one study.
    upstream.routes[("GET", "/studies")] = lambda request: answer([study_row(0), study_row(0, patient="SYNTHETIC-2")])
    with pytest.raises(ArchiveChanged):
        client.study(f"{ROOT}.10.0")
    assert all(response.closed for response in upstream.responses)
    with client:
        pass
    assert upstream.closed


def test_instances_page_by_200_and_deduplicate(archive):
    client, upstream = archive
    rows = [instance_row(index) for index in range(450)]

    def shifted(request):
        params = dict(request["params"])
        # An instance arriving between pages shifts later pages back by one, repeating a row.
        start = max(0, int(params["offset"]) - 1)
        return answer(rows[start : start + int(params["limit"])]) if start < len(rows) else Response(status=204)

    upstream.routes[("GET", INSTANCES)] = shifted
    expected = tuple(sorted((f"{SERIES_UID}.{index}", CTImageStorage, FRAME_UID) for index in range(450)))
    assert client.instances(STUDY_UID, SERIES_UID) == expected
    assert [dict(call[2]["params"])["limit"] for call in upstream.calls] == ["200"] * 4
    upstream.routes[("GET", INSTANCES)] = lambda request: answer(rows[:200])
    with pytest.raises(ArchiveError, match="repeated a page"):
        client.instances(STUDY_UID, SERIES_UID)


def test_retrieve_refetches_htj2k_as_explicit_little_endian(archive, tmp_path):
    client, upstream = archive
    stored, converted = instance_bytes(HTJ2KLossless), instance_bytes(ExplicitVRLittleEndian)
    refusal = None

    def wado(request):
        syntax = request["headers"]["Accept"].rpartition("transfer-syntax=")[2]
        if syntax != "*" and refusal:
            return Response(b"", refusal)
        return Response(related(stored if syntax == "*" else converted), 200, {"Content-Type": RELATED})

    upstream.routes[("GET", f"{INSTANCES}/{SOP_UID}")] = wado
    result = client.retrieve(STUDY_UID, SERIES_UID, SOP_UID, tmp_path / "000000.dcm", max_bytes=10**6)
    assert result.transfer_syntax_uid == ExplicitVRLittleEndian and result.sha256 == hashlib.sha256(converted).hexdigest()
    assert (tmp_path / "000000.dcm").read_bytes() == converted
    assert [call[2]["headers"]["Accept"] for call in upstream.calls] == [
        'multipart/related; type="application/dicom"; transfer-syntax=*',
        'multipart/related; type="application/dicom"; transfer-syntax=1.2.840.10008.1.2.1',
    ]
    assert all(call[2]["headers"]["Accept-Encoding"] == "identity" for call in upstream.calls)
    # The standard refusal, and Orthanc's answer when it cannot transcode.
    for refusal in (406, 500):
        with pytest.raises(ArchiveError, match=r"1\.2\.840\.10008\.1\.2\.1"):
            client.retrieve(STUDY_UID, SERIES_UID, SOP_UID, tmp_path / "000001.dcm", max_bytes=10**6)
        assert not (tmp_path / "000001.dcm").exists()


def test_store_reads_stow_outcomes(archive, tmp_path):
    client, upstream = archive
    data = instance_bytes(ExplicitVRLittleEndian)
    path = tmp_path / "seg.dcm"
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()

    def outcome(tag, **extra):
        item = {"00081150": element("UI", CTImageStorage), "00081155": element("UI", SOP_UID), **extra}
        return {tag: {"vr": "SQ", "Value": [item]}}

    def referenced(*warning):
        return outcome("00081199", **({"00081196": element("US", *warning)} if warning else {}))

    def failed(reason):
        return outcome("00081198", **{"00081197": element("US", reason)})

    for response, expected in (
        (answer(referenced()), Stored("stored", SOP_UID)),
        (answer(referenced(0xB000), 202), Stored("stored", SOP_UID, ("B000",))),
        (answer(failed(0x0111), 409), Stored("duplicate", SOP_UID)),
        (Response(b'{"error": {"code": 409, "message": "Instance already exists"}}', 409), Stored("duplicate", SOP_UID)),
    ):
        upstream.routes[("POST", "/studies")] = lambda request, response=response: response
        assert client.store(path, expected_sha256=digest) == expected
    upstream.routes[("POST", "/studies")] = lambda request: answer(failed(0xA700), 409)
    with pytest.raises(ArchiveError, match="A700"):
        client.store(path, expected_sha256=digest)
    # The body has a known length, so requests sends Content-Length, and it carries the file.
    request = upstream.calls[-1][2]
    sent = b"".join(request["data"])
    prepared = requests.Request("POST", "https://archive.example.test/studies", data=request["data"], headers=request["headers"]).prepare()
    assert prepared.headers["Content-Length"] == str(len(sent)) and "Transfer-Encoding" not in prepared.headers
    (part,) = multipart.save_parts(pieces(sent, 3), request["headers"]["Content-Type"], lambda index: tmp_path / "sent.dcm", max_parts=1, max_bytes=len(data))
    assert part.path.read_bytes() == data and part.content_type == "application/dicom"
    with pytest.raises(ValueError, match="changed"):
        client.store(path, expected_sha256="0" * 64)


def test_bearer_token_is_reread_after_401_and_on_change(tmp_path):
    token = tmp_path / "token"
    token.write_text("synthetic-token-1\n")
    upstream = Archive()
    accepted = ["synthetic-token-1"]
    upstream.routes[("GET", "/studies")] = lambda request: (
        answer([]) if request["headers"]["Authorization"] == f"Bearer {accepted[0]}" else Response(status=401)
    )
    client = DicomWebClient("https://archive.example.test/dicom-web", auth="bearer", token_file=token, session=upstream)

    def sent():
        headers = [call[2]["headers"]["Authorization"].removeprefix("Bearer ") for call in upstream.calls]
        upstream.calls.clear()
        return headers

    client.status()
    assert sent() == ["synthetic-token-1"] and upstream.responses[-1].status_code == 200
    # A rotated token with a new mtime is used at once.
    accepted[0] = "synthetic-token-2"
    token.write_text("synthetic-token-2\n")
    moved = token.stat().st_mtime_ns + 1_000_000_000
    os.utime(token, ns=(moved, moved))
    client.status()
    assert sent() == ["synthetic-token-2"]
    # One replaced within the same mtime is only seen after the archive refuses the old one.
    accepted[0] = "synthetic-token-3"
    token.write_text("synthetic-token-3\n")
    os.utime(token, ns=(moved, moved))
    client.status()
    assert sent() == ["synthetic-token-2", "synthetic-token-3"]
    assert all(call is None for call in [request.get("auth") for _, _, request in upstream.calls])
    accepted[0] = "a token this client does not have"
    with pytest.raises(ArchiveError, match="authentication failed"):
        client.status()
    assert sent() == ["synthetic-token-3"]


def test_errors_and_logs_carry_no_query_values_or_credentials(archive, caplog):
    client, upstream = archive
    caplog.set_level(logging.DEBUG)
    hidden = ("SECRET^PATIENT", "synthetic-password", "archive.example.test")
    upstream.failure = requests.ConnectionError("https://synthetic-user:synthetic-password@archive.example.test/dicom-web/studies?PatientName=SECRET^PATIENT")
    errors = []
    with pytest.raises(ArchiveError) as caught:
        client.find_studies({"PatientName": "SECRET^PATIENT"})
    errors.append(caught.value)
    upstream.failure = None
    upstream.routes[("GET", "/studies")] = lambda request: Response(b"No such patient SECRET^PATIENT at https://archive.example.test", 500)
    with pytest.raises(ArchiveError) as caught:
        client.find_studies({"PatientName": "SECRET^PATIENT"})
    errors.append(caught.value)
    for error in errors:
        # No chained exception, which would print the transport's own message, reaches a traceback.
        assert error.__cause__ is None and (error.__context__ is None or error.__suppress_context__)
        assert not any(value in f"{error} {error!r}" for value in hidden)
    assert not any(value in caplog.text for value in hidden)
    # Credentials never cross a network in clear text; a loopback archive is on this machine.
    with pytest.raises(ValueError, match="https"):
        DicomWebClient("http://archive.example.test/dicom-web", auth="basic", username="synthetic-user", password="synthetic-password")
    local = DicomWebClient("http://127.0.0.1:8042/dicom-web/", auth="basic", username="synthetic-user", session=Archive())
    assert local.source_key == hashlib.sha256(b"dicomweb:http://127.0.0.1:8042/dicom-web").hexdigest()
    named = DicomWebClient("http://127.0.0.1:8042/dicom-web/", archive_id="synthetic-site", session=Archive())
    assert named.source_key == hashlib.sha256(b"dicomweb-id:synthetic-site").hexdigest()
