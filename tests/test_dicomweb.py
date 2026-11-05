"""Synthetic DICOMweb transport; no network, archive or patient data."""

import hashlib

import pytest

from scan3d.archive import dicomjson, multipart
from scan3d.archive.errors import ArchiveError

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
