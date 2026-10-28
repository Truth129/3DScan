"""Read-only Orthanc transport with immutable, bounded series snapshots.

Orthanc stability means an inactivity interval, not proof that an acquisition is
complete. A selected snapshot is checked again after transfer; accepted source
identity still comes from the downloaded DICOM and its content hashes.
"""

import hashlib
import json
import math
from pathlib import Path
import re
from urllib.parse import urlsplit, urlunsplit

import requests

from .errors import NotFound

_ID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{8}){4}\Z")
_UID = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_QUERY_TAGS = {"PatientID", "PatientName", "AccessionNumber", "StudyDate"}
_STUDY_TAGS = ["PatientID", "PatientName", "AccessionNumber", "StudyDate", "StudyDescription", "StudyInstanceUID", "ModalitiesInStudy"]
_INSTANCE_TAGS = "SOPInstanceUID;SOPClassUID;FrameOfReferenceUID;StudyInstanceUID;SeriesInstanceUID"
_BLOCK = 1024 * 1024
_JSON_LIMIT = 16 * 1024**2
_MAX_INSTANCES = 100_000


class OrthancError(RuntimeError):
    """An upstream failure safe to return without server credentials or PHI."""


class OrthancChanged(RuntimeError):
    """The archive changed after the user selected a particular snapshot."""


def orthanc_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError("Invalid Orthanc resource identifier")
    return value


def _uid(value, *, optional=False):
    if optional and value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 64 or not _UID.fullmatch(value):
        raise OrthancError("Orthanc returned an invalid DICOM identity.")
    return value


def _text(value):
    return value if isinstance(value, str) and len(value) <= 1024 else ""


def _tags(record):
    result = {}
    for field in ("PatientMainDicomTags", "MainDicomTags", "RequestedTags"):
        if isinstance(record.get(field), dict):
            result.update(record[field])
    return result


def _record(value):
    if not isinstance(value, dict):
        raise OrthancError("Orthanc returned an invalid resource response.")
    return value


def _number(value, *, optional=False):
    if optional and value in (None, 0):
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise OrthancError("Orthanc returned an invalid instance count or size.")
    return value


def snapshot_fingerprint(snapshot):
    try:
        fields = {
            key: snapshot[key]
            for key in ("source_key", "study_id", "study_uid", "series_id", "series_uid", "frame_of_reference_uid", "instances", "expected_instances", "status")
        }
    except (KeyError, TypeError):
        raise OrthancChanged("The selected Orthanc snapshot is no longer valid. Refresh it before importing.") from None
    return hashlib.sha256(json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class OrthancClient:
    def __init__(self, base_url, username="", password="", *, timeout=30, session=None):
        try:
            parsed = urlsplit(base_url)
            port = parsed.port
        except (TypeError, ValueError):
            raise ValueError("Invalid configured Orthanc URL") from None
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise ValueError("Configure an HTTP(S) Orthanc URL without credentials, query or fragment")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 300:
            raise ValueError("Orthanc timeout must be between 0 and 300 seconds")
        hostname = parsed.hostname.lower()
        host = f"[{hostname}]" if ":" in hostname else hostname
        if port is not None and not (parsed.scheme == "http" and port == 80 or parsed.scheme == "https" and port == 443):
            host += f":{port}"
        self.base_url = urlunsplit((parsed.scheme, host, parsed.path.rstrip("/"), "", ""))
        self.source_key = hashlib.sha256(self.base_url.encode()).hexdigest()
        self.timeout = (min(timeout, 10), timeout)
        self.session = session or requests.Session()
        # Never send archive credentials through ambient proxies or .netrc.
        self.session.trust_env = False
        self.auth = (username, password) if username or password else None

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def _request(self, method, path, **kwargs):
        try:
            response = self.session.request(
                method,
                self.base_url + path,
                auth=self.auth,
                timeout=self.timeout,
                allow_redirects=False,
                verify=True,
                stream=True,
                **kwargs,
            )
        except requests.RequestException:
            raise OrthancError("Orthanc could not be reached. Check the configured connection.") from None
        if response.status_code == 404:
            response.close()
            raise NotFound("The selected Orthanc resource is no longer available")
        if response.status_code != 200:
            status = response.status_code
            response.close()
            message = "Orthanc authentication failed." if status in {401, 403} else "Orthanc rejected the request. Check the configured connection."
            raise OrthancError(message)
        return response

    def _json(self, method, path, **kwargs):
        response = self._request(method, path, headers={"Accept": "application/json"}, **kwargs)
        try:
            raw = bytearray()
            for block in response.iter_content(chunk_size=64 * 1024):
                if len(raw) + len(block) > _JSON_LIMIT:
                    raise OrthancError("Orthanc returned too much metadata. Narrow the search or select a smaller series.")
                raw.extend(block)
            try:
                return json.loads(raw)
            except (ValueError, UnicodeError):
                raise OrthancError("Orthanc returned invalid metadata.") from None
        except requests.RequestException:
            raise OrthancError("The Orthanc metadata transfer was interrupted. Retry the request.") from None
        finally:
            response.close()

    def status(self):
        value = _record(self._json("GET", "/system"))
        return {"name": _text(value.get("Name")) or "Orthanc", "version": _text(value.get("Version")), "api_version": value.get("ApiVersion")}

    def _study(self, value):
        value = _record(value)
        tags = _tags(value)
        try:
            resource_id = orthanc_id(value.get("ID"))
        except ValueError:
            raise OrthancError("Orthanc returned an invalid study identifier.") from None
        series = value.get("Series", [])
        if not isinstance(series, list) or len(series) > _MAX_INSTANCES:
            raise OrthancError("Orthanc returned an invalid series list.")
        return {
            "id": resource_id,
            "study_uid": _uid(tags.get("StudyInstanceUID")),
            "description": _text(tags.get("StudyDescription")),
            "study_date": _text(tags.get("StudyDate")),
            "patient_id": _text(tags.get("PatientID")),
            "patient_name": _text(tags.get("PatientName")),
            "accession_number": _text(tags.get("AccessionNumber")),
            "series_count": len(series),
            "modalities": _text(tags.get("ModalitiesInStudy")),
            "stable": value.get("IsStable") is True,
        }

    def find_studies(self, query=None, *, limit=50, offset=0):
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("Orthanc query limit must be between 1 and 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or not 0 <= offset <= 1_000_000:
            raise ValueError("Invalid Orthanc query offset")
        query = {} if query is None else query
        if not isinstance(query, dict) or set(query) - _QUERY_TAGS or any(not isinstance(value, str) or len(value) > 256 for value in query.values()):
            raise ValueError("Use patient ID/name, accession number or study date to query Orthanc")
        result = self._json(
            "POST", "/tools/find", json={"Level": "Study", "Expand": True, "Query": query, "Limit": limit, "Since": offset, "RequestedTags": _STUDY_TAGS}
        )
        if not isinstance(result, list) or len(result) > limit:
            raise OrthancError("Orthanc returned an invalid study query response.")
        return [self._study(value) for value in result]

    def study_series(self, study_id):
        study_id = orthanc_id(study_id)
        study = _record(self._json("GET", f"/studies/{study_id}"))
        summary = self._study(study)
        if summary["id"] != study_id:
            raise OrthancError("Orthanc returned a different study than requested.")
        series = []
        for resource_id in study.get("Series", []):
            snap = self.snapshot(resource_id)
            if snap["study_id"] != study_id or snap["study_uid"] != summary["study_uid"]:
                raise OrthancChanged("The Orthanc study changed. Refresh its series before importing.")
            series.append(
                {
                    key: snap[key]
                    for key in (
                        "id",
                        "description",
                        "modality",
                        "series_uid",
                        "instance_count",
                        "expected_instances",
                        "stable",
                        "completeness",
                        "snapshot",
                        "warnings",
                    )
                }
            )
        return {"study": summary, "series": series}

    def snapshot(self, series_id):
        series_id = orthanc_id(series_id)
        raw = _record(self._json("GET", f"/series/{series_id}", params={"requested-tags": "FrameOfReferenceUID"}))
        try:
            parent = orthanc_id(raw.get("ParentStudy"))
            if orthanc_id(raw.get("ID")) != series_id:
                raise ValueError()
            listed = [orthanc_id(value) for value in raw["Instances"]]
        except (ValueError, TypeError, KeyError):
            raise OrthancError("Orthanc returned invalid series membership.") from None
        if not listed or len(listed) > _MAX_INSTANCES or len(listed) != len(set(listed)):
            raise OrthancError("Orthanc returned empty, duplicated or excessive series membership.")
        study = _record(self._json("GET", f"/studies/{parent}"))
        study_summary = self._study(study)
        if study_summary["id"] != parent or series_id not in study.get("Series", []):
            raise OrthancChanged("The Orthanc study changed. Refresh its series before importing.")
        expanded = self._json("GET", f"/series/{series_id}/instances", params={"expand": "true", "requested-tags": _INSTANCE_TAGS})
        if not isinstance(expanded, list) or len(expanded) != len(listed):
            raise OrthancChanged("The Orthanc series changed. Refresh it before importing.")
        instances = []
        for item in expanded:
            item = _record(item)
            tags = _tags(item)
            try:
                item_id = orthanc_id(item.get("ID"))
            except ValueError:
                raise OrthancError("Orthanc returned an invalid instance identifier.") from None
            if item.get("ParentSeries") != series_id:
                raise OrthancChanged("The Orthanc series changed. Refresh it before importing.")
            file_uuid = item.get("FileUuid")
            if not isinstance(file_uuid, str) or not file_uuid or len(file_uuid) > 128:
                raise OrthancError("Orthanc did not provide immutable DICOM file identities.")
            instances.append(
                {
                    "id": item_id,
                    "sop_instance_uid": _uid(tags.get("SOPInstanceUID")),
                    "sop_class_uid": _uid(tags.get("SOPClassUID")),
                    "file_uuid": file_uuid,
                    "size": _number(item.get("FileSize")),
                }
            )
        if {item["id"] for item in instances} != set(listed) or len({item["sop_instance_uid"] for item in instances}) != len(listed):
            raise OrthancChanged("The Orthanc series contains changed or duplicate instances. Refresh it before importing.")
        tags = _tags(raw)
        expected = _number(raw.get("ExpectedNumberOfInstances"), optional=True)
        stable = raw.get("IsStable") is True
        status = raw.get("Status", "Unknown")
        if status not in {"Complete", "Unknown", "Missing", "Inconsistent"}:
            raise OrthancError("Orthanc returned an invalid series completeness status.")
        completeness = (
            "receiving"
            if not stable
            else "incomplete"
            if status in {"Missing", "Inconsistent"} or expected is not None and expected != len(instances)
            else "complete"
            if expected is not None and status == "Complete"
            else "unknown"
        )
        warnings = [] if completeness == "complete" else ["An unchanged snapshot is not proof that the scanner sent the full acquisition."]
        result = {
            "id": series_id,
            "source_key": self.source_key,
            "series_id": series_id,
            "series_uid": _uid(tags.get("SeriesInstanceUID")),
            "study_id": parent,
            "study_uid": study_summary["study_uid"],
            "frame_of_reference_uid": _uid(tags.get("FrameOfReferenceUID"), optional=True),
            "description": _text(tags.get("SeriesDescription")),
            "modality": _text(tags.get("Modality")),
            "instance_count": len(instances),
            "expected_instances": expected,
            "stable": stable,
            "status": status,
            "completeness": completeness,
            "warnings": warnings,
            "instances": sorted(instances, key=lambda item: item["id"]),
        }
        result["snapshot"] = snapshot_fingerprint(result)
        return result

    def download(self, snapshot, destination, *, max_bytes, progress=None, cancelled=None):
        if snapshot.get("source_key") != self.source_key or snapshot.get("snapshot") != snapshot_fingerprint(snapshot):
            raise OrthancChanged("The selected Orthanc snapshot is no longer valid. Refresh it before importing.")
        if snapshot.get("completeness") not in {"complete", "unknown"} or snapshot.get("stable") is not True:
            raise OrthancChanged("The Orthanc series is still receiving or incomplete. Refresh it when the acquisition is finished.")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
            raise ValueError("Invalid Orthanc import size limit")
        if sum(item["size"] for item in snapshot["instances"]) > max_bytes:
            raise ValueError("Orthanc import exceeds size limit")
        if cancelled and cancelled():
            raise InterruptedError("Orthanc import cancelled")
        current = self.snapshot(snapshot["series_id"])
        if current["snapshot"] != snapshot["snapshot"] or current["stable"] is not True:
            raise OrthancChanged("The Orthanc series changed before transfer. Refresh it before importing.")
        destination = Path(destination)
        destination.mkdir(parents=True, exist_ok=True)
        if any(destination.iterdir()):
            raise ValueError("Orthanc imports require an empty staging directory")
        total = 0
        files = []
        for index, item in enumerate(snapshot["instances"]):
            if cancelled and cancelled():
                raise InterruptedError("Orthanc import cancelled")
            resource_id = orthanc_id(item["id"])
            response = self._request("GET", f"/instances/{resource_id}/file", headers={"Accept": "application/dicom"})
            path = destination / f"{index:06d}.dcm"
            digest = hashlib.sha256()
            size = 0
            try:
                with path.open("xb") as output:
                    for block in response.iter_content(chunk_size=_BLOCK):
                        if cancelled and cancelled():
                            raise InterruptedError("Orthanc import cancelled")
                        size += len(block)
                        total += len(block)
                        if size > item["size"] or total > max_bytes:
                            raise OrthancChanged("The Orthanc file changed size during transfer. Refresh before importing.")
                        output.write(block)
                        digest.update(block)
                if size != item["size"]:
                    raise OrthancChanged("The Orthanc file transfer ended early. Retry the import.")
            except requests.RequestException:
                raise OrthancError("The Orthanc DICOM transfer was interrupted. Retry the import.") from None
            finally:
                response.close()
            files.append(
                {"instance_id": resource_id, "sop_instance_uid": item["sop_instance_uid"], "sha256": digest.hexdigest(), "size": size, "path": path.name}
            )
            if progress:
                progress(index + 1, len(snapshot["instances"]), total)
        if cancelled and cancelled():
            raise InterruptedError("Orthanc import cancelled")
        current = self.snapshot(snapshot["series_id"])
        if current["snapshot"] != snapshot["snapshot"] or current["stable"] is not True:
            raise OrthancChanged("The Orthanc series changed during transfer. Refresh it before importing.")
        content = [{key: item[key] for key in ("sop_instance_uid", "sha256", "size")} for item in sorted(files, key=lambda item: item["sop_instance_uid"])]
        return {
            "snapshot": snapshot,
            "files": files,
            "bytes": total,
            "content_digest": hashlib.sha256(json.dumps(content, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        }
