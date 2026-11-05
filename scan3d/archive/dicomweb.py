"""DICOMweb transport (QIDO-RS search, WADO-RS retrieve, STOW-RS store) for a standard archive.

Request hygiene follows ``OrthancClient``: no ambient proxies or .netrc, no redirects, verified
TLS, split timeouts and bounded answers. Every message is fixed text, because query values are
patient names and URLs can carry them; nothing here logs.

Searches turn off fuzzy matching and refuse wildcards unless the archive is configured for
them, so a search means the same on every server. Paged listings stop at an empty page, not a
short one, because servers may cap a page below the size asked for.

Orthanc's DICOMweb plugin answers a transfer syntax it cannot convert to with 500 rather than
406, so the Explicit VR Little Endian fallback reads either as a refusal.
"""

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlsplit, urlunsplit

from pydicom.filereader import read_file_meta_info
import requests

from ..errors import NotFound
from ..fileio import hash_file
from . import dicomjson
from .errors import ArchiveChanged, ArchiveError
from .multipart import StowBody, save_parts
from .settings import AUTH_MODES, _loopback

_UID = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_ARCHIVE_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_QUERY_TAGS = {"PatientID", "PatientName", "AccessionNumber", "StudyDate"}
# StudyDescription and IssuerOfPatientID are not returned by default; the counts and modalities
# are, but naming them costs nothing on servers that already send them.
_STUDY_PARAMS = [("fuzzymatching", "false"), ("includefield", "00081030,00100021,00080061,00201206")]
_SERIES_FIELDS = "0008103E,00201209"
_INSTANCE_FIELDS = "00200052"
_STUDY_UID, _SERIES_UID, _SOP_UID, _SOP_CLASS, _FRAME = "0020000D", "0020000E", "00080018", "00080016", "00200052"
_EXPLICIT_LITTLE = "1.2.840.10008.1.2.1"
# HTJ2K and JPEG XL: the import tools here cannot decode them, so these are fetched once more as
# Explicit VR Little Endian, which every archive must be able to send.
_UNDECODABLE = frozenset(f"1.2.840.10008.1.2.4.{suffix}" for suffix in (201, 202, 203, 110, 111, 112))
# STOW failure reasons for a SOP the archive already holds: DICOM's duplicate SOP Instance, and
# Azure's own code. Duplicates are not proof of identical content; the caller reads back.
_DUPLICATE_REASONS = {0x0111, 45070}
_JSON_LIMIT = 16 * 1024**2
_TOKEN_LIMIT = 16 * 1024
_BLOCK = 1024 * 1024
_PAGE = 200
_MAX_SERIES = 10_000
_MAX_INSTANCES = 100_000
_READ_BACK_SECONDS = 10


@dataclass(frozen=True)
class Retrieved:
    sha256: str
    size: int
    transfer_syntax_uid: str


@dataclass(frozen=True)
class Stored:
    state: str  # "stored", or "duplicate" when the archive already held this SOP
    sop_instance_uid: str
    warnings: tuple[str, ...] = ()  # STOW warning reasons as hex codes, such as "B000"


def _uid(value):
    """A UID the caller passed in; it is interpolated into request paths."""
    if not isinstance(value, str) or len(value) > 64 or not _UID.fullmatch(value):
        raise ValueError("Invalid DICOM UID")
    return value


def _meta_uid(meta, keyword):
    value = meta.get(keyword)
    value = str(value) if value is not None else ""
    if len(value) > 64 or not _UID.fullmatch(value):
        raise ValueError(keyword)
    return value


def client_for(settings):
    """The client for this workstation's DICOMweb settings."""
    return DicomWebClient(
        settings.url,
        auth=settings.auth,
        username=settings.username,
        password=settings.password,
        token_file=settings.token_file or None,
        ca_bundle=settings.ca_bundle or None,
        timeout=settings.timeout,
        wildcards=settings.wildcards,
        archive_id=settings.archive_id or None,
    )


def _study(row):
    return {
        "study_uid": dicomjson.uid(row, _STUDY_UID),
        "description": dicomjson.text(row, "00081030"),
        "study_date": dicomjson.text(row, "00080020"),
        "patient_id": dicomjson.text(row, "00100020"),
        "issuer_of_patient_id": dicomjson.text(row, "00100021"),
        "patient_name": dicomjson.person_name(row, "00100010"),
        "accession_number": dicomjson.text(row, "00080050"),
        "series_count": dicomjson.integer(row, "00201206"),
        "modalities": dicomjson.text(row, "00080061"),
    }


class DicomWebClient:
    def __init__(
        self,
        base_url,
        *,
        auth="none",
        username="",
        password="",
        token_file=None,
        ca_bundle=None,
        timeout=30,
        wildcards=False,
        archive_id=None,
        session=None,
    ):
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except (TypeError, ValueError, AttributeError):
            raise ValueError("Invalid configured DICOMweb URL") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise ValueError("Configure an HTTP(S) DICOMweb URL without credentials, query or fragment")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("DICOMweb timeout must be between 0 and 300 seconds")
        if auth not in AUTH_MODES:
            raise ValueError("DICOMweb authentication must be none, basic or bearer")
        if (auth == "basic") != bool(username) or auth != "basic" and password or (auth == "bearer") != bool(token_file):
            raise ValueError("DICOMweb credentials do not match the authentication mode")
        hostname = parsed.hostname.lower()
        if auth != "none" and parsed.scheme == "http" and not _loopback(hostname):
            raise ValueError("DICOMweb credentials need an https URL unless the archive is on this machine")
        if archive_id not in (None, "") and (not isinstance(archive_id, str) or not _ARCHIVE_ID.fullmatch(archive_id)):
            raise ValueError("Invalid configured DICOMweb archive identifier")
        if not isinstance(wildcards, bool):
            raise ValueError("DICOMweb wildcard setting must be true or false")
        # requests trusts its bundled public CAs, not the system store, so a private CA needs its own file.
        if ca_bundle and not Path(ca_bundle).is_file():
            raise ArchiveError("The archive CA bundle cannot be read. Check the configured connection.")
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None and not (parsed.scheme == "http" and port == 80 or parsed.scheme == "https" and port == 443):
            host += f":{port}"
        self.base_url = urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
        # A configured identity survives a host move; otherwise the URL is the archive.
        identity = f"dicomweb-id:{archive_id}" if archive_id else f"dicomweb:{self.base_url}"
        self.source_key = hashlib.sha256(identity.encode()).hexdigest()
        self.timeout = (min(timeout, 10), timeout)
        self.verify = str(ca_bundle) if ca_bundle else True
        self.wildcards = wildcards
        self.session = session or requests.Session()
        # Never send archive credentials through ambient proxies or .netrc.
        self.session.trust_env = False
        self.auth = (username, password) if auth == "basic" else None
        self._token_file = Path(token_file) if auth == "bearer" else None
        self._token = self._token_mtime = None
        self._sleep, self._clock = time.sleep, time.monotonic

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _read_token(self, *, force=False):
        """The bearer token, read again whenever the file changes, so a rotated token is used at once."""
        try:
            mtime = self._token_file.stat().st_mtime_ns
            if force or self._token is None or mtime != self._token_mtime:
                with self._token_file.open("rb") as stream:
                    raw = stream.read(_TOKEN_LIMIT + 1).strip()
                # Visible ASCII only, so a token can never inject a header line.
                if not 0 < len(raw) <= _TOKEN_LIMIT or any(not 0x21 <= byte <= 0x7E for byte in raw):
                    raise ArchiveError("The archive token file is empty or invalid.")
                self._token, self._token_mtime = raw.decode("ascii"), mtime
        except OSError:
            raise ArchiveError("The archive token file cannot be read.") from None
        return self._token

    def _send(self, method, path, headers, token, **kwargs):
        if token is not None:
            headers = {**headers, "Authorization": f"Bearer {token}"}
        try:
            return self.session.request(
                method,
                self.base_url + path,
                headers=headers,
                auth=self.auth,
                timeout=self.timeout,
                allow_redirects=False,
                verify=self.verify,
                stream=True,
                **kwargs,
            )
        except requests.RequestException:
            raise ArchiveError("The archive could not be reached. Check the configured connection.") from None

    def _request(self, method, path, *, accept=(200,), headers=None, **kwargs):
        headers = dict(headers or {})
        token = self._read_token() if self._token_file else None
        response = self._send(method, path, headers, token, **kwargs)
        if response.status_code == 401 and token is not None:
            # A token replaced within the same mtime tick is caught here, once.
            fresh = self._read_token(force=True)
            if fresh != token:
                response.close()
                response = self._send(method, path, headers, fresh, **kwargs)
        status = response.status_code
        if status in accept:
            return response
        response.close()
        if status == 404:
            raise NotFound("The selected archive resource is no longer available")
        if status in {401, 403}:
            raise ArchiveError("Archive authentication failed.")
        raise ArchiveError("The archive rejected the request. Check the configured connection.")

    def _body(self, response):
        try:
            raw = bytearray()
            for block in response.iter_content(chunk_size=64 * 1024):
                if len(raw) + len(block) > _JSON_LIMIT:
                    raise ArchiveError("The archive returned too much metadata. Narrow the search or select a smaller series.")
                raw.extend(block)
            return bytes(raw)
        except requests.RequestException:
            raise ArchiveError("The archive metadata transfer was interrupted. Retry the request.") from None
        finally:
            response.close()

    def _qido(self, path, params):
        response = self._request("GET", path, accept=(200, 204), headers={"Accept": "application/dicom+json"}, params=params)
        if response.status_code == 204:
            response.close()
            return []
        try:
            value = json.loads(self._body(response))
        except (ValueError, UnicodeError):
            raise ArchiveError("The archive returned invalid metadata.") from None
        return dicomjson.datasets(value, cap=_MAX_INSTANCES)

    def _paged(self, path, params, *, read, cap):
        """Every row of a listing, de-duplicated by identity, as rows shift when items arrive mid-listing."""
        rows, offset = {}, 0
        while True:
            page = self._qido(path, [*params, ("limit", str(_PAGE)), ("offset", str(offset))])
            if not page:
                return list(rows.values())
            added = 0
            for row in page:
                identity, value = read(row)
                if identity in rows:
                    if rows[identity] != value:
                        raise ArchiveChanged("The archive listing changed while it was read. Refresh it before importing.")
                    continue
                rows[identity] = value
                added += 1
                if len(rows) > cap:
                    raise ArchiveError("The archive listing is too large to import.")
            if not added:
                raise ArchiveError("The archive repeated a page of results. It may not support paged searches.")
            offset += len(page)

    def status(self):
        self._qido("/studies", [("limit", "1")])
        return {"name": "DICOMweb", "version": "", "api_version": None}

    def find_studies(self, query=None, *, limit=50, offset=0):
        """At most ``limit`` studies, even from a server that ignores it; ask for one more to see if more exist."""
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("DICOMweb query limit must be between 1 and 1000")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("Invalid DICOMweb query offset")
        query = {} if query is None else query
        if not isinstance(query, dict) or set(query) - _QUERY_TAGS or any(not isinstance(value, str) or len(value) > 256 for value in query.values()):
            raise ValueError("Use patient ID/name, accession number or study date to query the archive")
        query = {key: value for key, value in query.items() if value}
        if not self.wildcards and any("*" in value or "?" in value for value in query.values()):
            raise ValueError("This archive is set up for exact searches. Remove * and ? from the search.")
        rows = self._qido("/studies", [*query.items(), *_STUDY_PARAMS, ("limit", str(limit)), ("offset", str(offset))])
        return [_study(row) for row in rows[:limit]]

    def study_page(self, *, limit, offset=0):
        """The archive's own page of studies, untrimmed, as Study Instance UIDs: whether it honours limit and offset."""
        return [dicomjson.uid(row, _STUDY_UID) for row in self._qido("/studies", [("limit", str(limit)), ("offset", str(offset))])]

    def study(self, study_uid):
        study_uid = _uid(study_uid)
        rows = [_study(row) for row in self._qido("/studies", [("StudyInstanceUID", study_uid), *_STUDY_PARAMS, ("limit", "2")])]
        if any(row["study_uid"] != study_uid for row in rows):
            raise ArchiveError("The archive returned a different study than requested.")
        if not rows:
            raise NotFound("The selected archive study is no longer available")
        if len(rows) > 1:
            # For example, two patients filed under one Study Instance UID.
            raise ArchiveChanged("The archive holds more than one study with this identity. Check the archive before importing.")
        return rows[0]

    def study_series(self, study_uid):
        summary = self.study(study_uid)

        def read(row):
            if dicomjson.uid(row, _STUDY_UID, optional=True) not in (None, summary["study_uid"]):
                raise ArchiveChanged("The archive study changed. Refresh its series before importing.")
            series_uid = dicomjson.uid(row, _SERIES_UID)
            return series_uid, {
                "series_uid": series_uid,
                "description": dicomjson.text(row, "0008103E"),
                "modality": dicomjson.text(row, "00080060"),
                # The archive's own count, which may be stale; 0 means it gave none.
                "reported_instances": dicomjson.integer(row, "00201209") or None,
            }

        # Paged too: some servers apply a default QIDO limit and would silently drop series.
        series = self._paged(f"/studies/{summary['study_uid']}/series", [("includefield", _SERIES_FIELDS)], read=read, cap=_MAX_SERIES)
        return {"study": summary, "series": series}

    def instances(self, study_uid, series_uid):
        """Sorted ``(sop_instance_uid, sop_class_uid, frame_of_reference_uid or None)`` for one series."""
        study_uid, series_uid = _uid(study_uid), _uid(series_uid)

        def read(row):
            if dicomjson.uid(row, _SERIES_UID, optional=True) not in (None, series_uid):
                raise ArchiveChanged("The archive series changed. Refresh it before importing.")
            sop = dicomjson.uid(row, _SOP_UID)
            return sop, (sop, dicomjson.uid(row, _SOP_CLASS), dicomjson.uid(row, _FRAME, optional=True))

        rows = self._paged(f"/studies/{study_uid}/series/{series_uid}/instances", [("includefield", _INSTANCE_FIELDS)], read=read, cap=_MAX_INSTANCES)
        return tuple(sorted(rows))

    def _fetch(self, path, resource, sop_uid, transfer_syntax, max_bytes, cancelled):
        """One WADO-RS instance into a new file, and its transfer syntax. A failure leaves no file."""
        accept = f'multipart/related; type="application/dicom"; transfer-syntax={transfer_syntax}'
        # A conversion the archive cannot make is 406, or 500 from Orthanc ("Cannot transcode image").
        refusals = {406} if transfer_syntax == "*" else {406, *range(500, 600)}
        # Identity encoding: the bytes on disk are the bytes the archive sent.
        response = self._request("GET", resource, accept={200, *refusals}, headers={"Accept": accept, "Accept-Encoding": "identity"})
        try:
            if response.status_code != 200:
                if transfer_syntax == "*":
                    raise ArchiveError("The archive refused to send the instance as stored.")
                raise ArchiveError(f"The archive could not send the instance as Explicit VR Little Endian ({transfer_syntax}).")
            chunks = response.iter_content(chunk_size=_BLOCK)
            parts = save_parts(chunks, response.headers.get("Content-Type"), lambda index: path, max_parts=1, max_bytes=max_bytes, cancelled=cancelled)
        except requests.RequestException:
            raise ArchiveError("The archive DICOM transfer was interrupted. Retry the import.") from None
        finally:
            response.close()
        if not parts:
            raise ArchiveError("The archive returned no DICOM file.")
        (part,) = parts
        try:
            if part.content_type != "application/dicom":
                raise ArchiveError("The archive returned an unexpected file response.")
            try:
                meta = read_file_meta_info(path)
                syntax, sop = _meta_uid(meta, "TransferSyntaxUID"), _meta_uid(meta, "MediaStorageSOPInstanceUID")
            except Exception:
                raise ArchiveError("The archive returned an invalid DICOM file.") from None
            if sop != sop_uid:
                raise ArchiveChanged("The archive returned a different instance than requested. Refresh the series before importing.")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return syntax, part

    def retrieve(self, study_uid, series_uid, sop_uid, path, *, max_bytes, cancelled=None) -> Retrieved:
        """Write one instance to ``path``, which must not exist, as stored where it can be decoded here."""
        resource = f"/studies/{_uid(study_uid)}/series/{_uid(series_uid)}/instances/{_uid(sop_uid)}"
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("Invalid archive import size limit")
        path = Path(path)
        syntax, part = self._fetch(path, resource, sop_uid, "*", max_bytes, cancelled)
        if syntax in _UNDECODABLE:
            path.unlink()
            syntax, part = self._fetch(path, resource, sop_uid, _EXPLICIT_LITTLE, max_bytes, cancelled)
            if syntax in _UNDECODABLE:
                path.unlink()
                raise ArchiveError(f"The archive did not convert the instance to Explicit VR Little Endian ({_EXPLICIT_LITTLE}).")
        return Retrieved(sha256=part.sha256, size=part.size, transfer_syntax_uid=syntax)

    def read_back(self, study_uid, series_uid, sop_uid, path, *, max_bytes) -> Retrieved:
        """``retrieve`` that waits out an archive's indexing lag after a store."""
        deadline = self._clock() + _READ_BACK_SECONDS
        while True:
            try:
                return self.retrieve(study_uid, series_uid, sop_uid, path, max_bytes=max_bytes)
            except NotFound:
                remaining = deadline - self._clock()
                if remaining <= 0:
                    raise
                self._sleep(min(1.0, remaining))

    def store(self, path, *, expected_sha256, max_bytes=512 * 1024**2) -> Stored:
        """STOW-RS one immutable file. A duplicate is reported, not overwritten; the caller reads back.

        "stored" does not prove which bytes the archive holds: Orthanc answers a second file
        under a known SOP Instance UID as stored and keeps the first. Only a read-back shows it.
        """
        path = Path(path)
        if path.is_symlink() or not path.is_file():
            raise ValueError("The DICOM file to send is unavailable")
        size = path.stat().st_size
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 0 < size <= max_bytes:
            raise ValueError("The DICOM file exceeds the archive upload limit")
        if hash_file(path) != expected_sha256:
            raise ValueError("The saved DICOM file changed; create a new export")
        try:
            sop = _meta_uid(read_file_meta_info(path), "MediaStorageSOPInstanceUID")
        except Exception:
            raise ValueError("The file to send is not a DICOM file with a valid identity") from None
        body = StowBody(path, expected_sha256=expected_sha256)
        headers = {"Accept": "application/dicom+json", "Content-Type": body.content_type}
        response = self._request("POST", "/studies", accept=(200, 202, 409), headers=headers, data=body)
        status, raw = response.status_code, self._body(response)
        try:
            value = json.loads(raw) if raw.strip() else None
        except (ValueError, UnicodeError):
            value = None
        # The response is one dataset; some servers wrap it in a list.
        if isinstance(value, list) and len(value) == 1:
            value = value[0]
        dataset = value if isinstance(value, dict) else {}
        failed = [item for item in dicomjson.sequence(dataset, "00081198") if dicomjson.uid(item, "00081155", optional=True) == sop]
        if failed:
            reasons = [dicomjson.integer(item, "00081197") for item in failed]
            if _DUPLICATE_REASONS.intersection(reasons):
                return Stored(state="duplicate", sop_instance_uid=sop)
            known = [reason for reason in reasons if reason is not None]
            raise ArchiveError(f"The archive refused the DICOM file (failure reason {known[0]:04X})." if known else "The archive refused the DICOM file.")
        # Some servers answer a duplicate with a plain 409 error rather than a DICOM dataset.
        if status == 409 and b"already exists" in raw.lower():
            return Stored(state="duplicate", sop_instance_uid=sop)
        referenced = [item for item in dicomjson.sequence(dataset, "00081199") if dicomjson.uid(item, "00081155", optional=True) == sop]
        if status == 409 or not referenced:
            raise ArchiveError("The archive did not confirm storage of the DICOM file.")
        codes = [dicomjson.integer(item, "00081196") for item in (dataset, *referenced)]
        return Stored(state="stored", sop_instance_uid=sop, warnings=tuple(sorted({f"{code:04X}" for code in codes if code is not None})))
