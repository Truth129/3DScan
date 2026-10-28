"""Synthetic archive transport; no network, workstation library or GPU access."""

import copy
from dataclasses import replace
import hashlib
import json
from urllib.parse import urlsplit

import pytest
import requests

from scan3d.errors import NotFound
from scan3d.orthanc import OrthancChanged, OrthancClient, OrthancError


def oid(value):
    return "-".join([f"{value:08x}"] * 5)


STUDY, SERIES, A, B = [oid(value) for value in range(1, 5)]


class Response:
    def __init__(self, content=b"", status=200, *, fault=None):
        self.status_code = status
        self.content = content
        self.fault = fault
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size <= 1024**2
        for offset in range(0, len(self.content), 2):
            yield self.content[offset : offset + 2]
        if self.fault:
            raise self.fault

    def close(self):
        self.closed = True


class Archive:
    def __init__(self):
        self.calls = []
        self.responses = []
        self.closed = False
        self.trust_env = True
        self.study = {
            "ID": STUDY,
            "IsStable": True,
            "Series": [SERIES],
            "MainDicomTags": {"StudyInstanceUID": "1.2.3", "StudyDate": "20261028", "StudyDescription": "Synthetic CT"},
            "PatientMainDicomTags": {"PatientID": "SYNTHETIC", "PatientName": "Synthetic^Patient"},
        }
        self.series = {
            "ID": SERIES,
            "ParentStudy": STUDY,
            "Instances": [B, A],
            "ExpectedNumberOfInstances": 2,
            "Status": "Complete",
            "IsStable": True,
            "MainDicomTags": {"SeriesInstanceUID": "1.2.3.4", "SeriesDescription": "Synthetic axial CT", "Modality": "CT"},
            "RequestedTags": {"FrameOfReferenceUID": "1.2.3.9"},
        }
        self.files = {A: b"synthetic-1", B: b"synthetic-2"}
        self.instances = [
            {
                "ID": resource,
                "ParentSeries": SERIES,
                "FileUuid": f"synthetic-file-{index}",
                "FileSize": len(self.files[resource]),
                "MainDicomTags": {"SOPInstanceUID": f"1.2.3.4.{index}"},
                "RequestedTags": {"SOPClassUID": "1.2.840.10008.5.1.4.1.1.2"},
            }
            for index, resource in enumerate((B, A), 1)
        ]
        self.after_file = None
        self.failure = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if self.failure:
            raise self.failure
        path = urlsplit(url).path.removeprefix("/orthanc")
        if path == "/system":
            payload = {"Name": "Synthetic archive", "Version": "1.12.10", "ApiVersion": 27}
        elif path == "/tools/find":
            assert method == "POST"
            payload = [self.study]
        elif path == f"/studies/{STUDY}":
            payload = self.study
        elif path == f"/series/{SERIES}":
            payload = self.series
        elif path == f"/series/{SERIES}/instances":
            assert kwargs["params"]["expand"] == "true"
            payload = self.instances
        elif path.startswith("/instances/") and path.endswith("/file"):
            resource = path.split("/")[2]
            response = Response(self.files[resource])
            if self.after_file:
                self.after_file(resource)
            self.responses.append(response)
            return response
        else:
            response = Response(b"sensitive server detail", 404)
            self.responses.append(response)
            return response
        response = Response(json.dumps(copy.deepcopy(payload)).encode())
        self.responses.append(response)
        return response

    def close(self):
        self.closed = True


@pytest.fixture
def archive():
    session = Archive()
    return OrthancClient("https://archive.example.test/orthanc/", "synthetic-user", "synthetic-password", session=session), session


def test_queries_are_bounded_and_read_only_with_no_ambient_auth_or_redirects(archive):
    client, upstream = archive
    assert client.status() == {"name": "Synthetic archive", "version": "1.12.10", "api_version": 27}
    assert client.find_studies({"PatientID": "SYNTHETIC*"}, limit=12, offset=24)[0]["patient_name"] == "Synthetic^Patient"
    request = upstream.calls[-1][2]
    assert request["json"]["Query"] == {"PatientID": "SYNTHETIC*"}
    assert request["json"]["Limit"] == 12 and request["json"]["Since"] == 24
    assert request["json"]["Level"] == "Study" and request["json"]["Expand"] is True
    assert upstream.trust_env is False
    assert all(call[2]["allow_redirects"] is False and call[2]["verify"] is True and call[2]["stream"] is True for call in upstream.calls)
    assert all(call[2]["auth"] == ("synthetic-user", "synthetic-password") for call in upstream.calls)
    assert all(call[2]["timeout"] == (10, 30) for call in upstream.calls)
    assert all(response.closed for response in upstream.responses)
    with client:
        pass
    assert upstream.closed


def test_snapshot_binds_source_study_series_frame_sop_and_file_version(archive):
    client, upstream = archive
    snapshot = client.snapshot(SERIES)
    assert snapshot.study_uid == "1.2.3" and snapshot.series_uid == "1.2.3.4"
    assert snapshot.frame_of_reference_uid == "1.2.3.9"
    assert snapshot.completeness == "complete" and snapshot.warnings == ()
    assert [item.id for item in snapshot.instances] == [A, B]
    upstream.instances.reverse()
    upstream.series["Instances"].reverse()
    assert client.snapshot(SERIES).snapshot == snapshot.snapshot
    upstream.instances[0]["FileUuid"] = "replacement-with-same-SOP-UID"
    assert client.snapshot(SERIES).snapshot != snapshot.snapshot
    other = OrthancClient("https://another.example.test", session=upstream)
    assert other.snapshot(SERIES).snapshot != snapshot.snapshot


def test_study_series_summaries_keep_the_selected_snapshot(archive):
    client, upstream = archive
    result = client.study_series(STUDY)
    assert result["study"]["id"] == STUDY
    summary = result["series"][0]
    assert summary["id"] == SERIES and summary["modality"] == "CT"
    assert summary["instance_count"] == summary["expected_instances"] == 2
    assert summary["snapshot"] == client.snapshot(SERIES).snapshot
    upstream.study["Series"] = []
    with pytest.raises(OrthancChanged, match="study changed"):
        client.snapshot(SERIES)


def test_download_streams_selected_instances_and_binds_exact_content(archive, tmp_path):
    client, upstream = archive
    snapshot = client.snapshot(SERIES)
    progress = []
    result = client.download(snapshot, tmp_path / "staging", max_bytes=100, progress=lambda *value: progress.append(value))
    assert progress == [(1, 2, 11), (2, 2, 22)]
    assert result.bytes == 22 and result.snapshot == snapshot
    for item in result.files:
        actual = (tmp_path / "staging" / item.path).read_bytes()
        assert actual == upstream.files[item.instance_id]
        assert item.sha256 == hashlib.sha256(actual).hexdigest()
    assert len(result.content_digest) == 64
    assert not any("archive" in urlsplit(call[1]).path for call in upstream.calls)
    assert all(call[0] == "GET" for call in upstream.calls)
    assert all(response.closed for response in upstream.responses)


def test_same_membership_same_sop_but_replaced_archive_file_rejects_transfer(archive, tmp_path):
    client, upstream = archive
    snapshot = client.snapshot(SERIES)

    def replace(_resource):
        upstream.instances[0]["FileUuid"] = "new-file-same-instance"

    upstream.after_file = replace
    with pytest.raises(OrthancChanged, match="changed during transfer"):
        client.download(snapshot, tmp_path, max_bytes=100)
    assert len(list(tmp_path.glob("*.dcm"))) == 2


def test_changed_selection_is_rejected_before_copying_any_instance(archive, tmp_path):
    client, upstream = archive
    snapshot = client.snapshot(SERIES)
    upstream.instances[0]["FileUuid"] = "new-file-before-job-start"
    with pytest.raises(OrthancChanged, match="changed before transfer"):
        client.download(snapshot, tmp_path / "changed", max_bytes=100)
    assert not (tmp_path / "changed").exists()
    assert not any(url.endswith("/file") for _, url, _ in upstream.calls)


def test_late_arrival_or_deleted_member_rejects_before_publishing(archive, tmp_path):
    client, upstream = archive
    snapshot = client.snapshot(SERIES)
    upstream.after_file = lambda _: upstream.series["Instances"].append(oid(99)) if oid(99) not in upstream.series["Instances"] else None
    with pytest.raises(OrthancChanged, match="series changed"):
        client.download(snapshot, tmp_path, max_bytes=100)


def test_inactivity_alone_never_claims_complete_acquisition(archive, tmp_path):
    client, upstream = archive
    upstream.series.update(ExpectedNumberOfInstances=None, Status="Unknown", IsStable=True)
    snapshot = client.snapshot(SERIES)
    assert snapshot.completeness == "unknown"
    assert "not proof" in snapshot.warnings[0]
    assert client.download(snapshot, tmp_path / "explicit-snapshot", max_bytes=100).bytes == 22
    upstream.series["IsStable"] = False
    receiving = client.snapshot(SERIES)
    assert receiving.completeness == "receiving"
    with pytest.raises(OrthancChanged, match="still receiving or incomplete"):
        client.download(receiving, tmp_path / "receiving", max_bytes=100)
    assert not (tmp_path / "receiving").exists()


@pytest.mark.parametrize("status,expected", [("Missing", 3), ("Inconsistent", 2), ("Complete", 3)])
def test_known_incomplete_series_cannot_be_imported(archive, tmp_path, status, expected):
    client, upstream = archive
    upstream.series.update(Status=status, ExpectedNumberOfInstances=expected)
    snapshot = client.snapshot(SERIES)
    assert snapshot.completeness == "incomplete"
    with pytest.raises(OrthancChanged):
        client.download(snapshot, tmp_path, max_bytes=100)


def test_limits_cancel_and_truncated_files_stop_before_a_success_result(archive, tmp_path):
    client, upstream = archive
    snapshot = client.snapshot(SERIES)
    with pytest.raises(ValueError, match="size limit"):
        client.download(snapshot, tmp_path / "large", max_bytes=21)
    assert not (tmp_path / "large").exists()
    with pytest.raises(InterruptedError):
        client.download(snapshot, tmp_path / "cancelled", max_bytes=100, cancelled=lambda: True)
    assert not (tmp_path / "cancelled").exists()
    upstream.files[A] = b"short"
    with pytest.raises(OrthancChanged, match="ended early"):
        client.download(snapshot, tmp_path / "truncated", max_bytes=100)
    assert upstream.responses[-1].closed


def test_stream_growth_and_mutated_snapshot_never_bypass_the_size_bound(archive, tmp_path):
    client, upstream = archive
    snapshot = client.snapshot(SERIES)
    changed = replace(snapshot, instances=(replace(snapshot.instances[0], size=1), *snapshot.instances[1:]))
    with pytest.raises(OrthancChanged, match="no longer valid"):
        client.download(changed, tmp_path / "tampered", max_bytes=100)
    upstream.files[A] *= 2
    with pytest.raises(OrthancChanged, match="changed size"):
        client.download(snapshot, tmp_path / "growing", max_bytes=100)
    assert upstream.responses[-1].closed


def test_upstream_failure_details_and_secrets_do_not_escape(archive, monkeypatch):
    client, upstream = archive
    upstream.failure = requests.ConnectionError("https://synthetic-user:synthetic-password@archive/ contains Synthetic^Patient")
    with pytest.raises(OrthancError) as caught:
        client.status()
    assert "synthetic-password" not in str(caught.value) and "Synthetic^Patient" not in str(caught.value)
    assert caught.value.__suppress_context__
    upstream.failure = None
    for code in (301, 302, 401, 403, 500):
        response = Response(b"synthetic-password / Synthetic^Patient", code)
        monkeypatch.setattr(upstream, "request", lambda *_args, **_kwargs: response)
        with pytest.raises(OrthancError) as caught:
            client.status()
        assert "synthetic-password" not in str(caught.value) and response.closed
    monkeypatch.setattr(upstream, "request", lambda *_args, **_kwargs: Response(b"private details", 404))
    with pytest.raises(NotFound, match="no longer available"):
        client.status()


def test_malformed_or_interrupted_metadata_stays_sanitized(archive, monkeypatch):
    client, upstream = archive
    monkeypatch.setattr(upstream, "request", lambda *_args, **_kwargs: Response(b"not JSON private details"))
    with pytest.raises(OrthancError, match="invalid metadata"):
        client.status()
    response = Response(b"{", fault=requests.exceptions.ChunkedEncodingError("private server details"))
    monkeypatch.setattr(upstream, "request", lambda *_args, **_kwargs: response)
    with pytest.raises(OrthancError, match="metadata transfer was interrupted"):
        client.status()
    assert response.closed


def test_metadata_allocation_is_bounded_before_json_decode(archive, monkeypatch):
    client, upstream = archive
    response = Response(b"metadata" * 20)
    monkeypatch.setattr("scan3d.orthanc._JSON_LIMIT", 64)
    monkeypatch.setattr(upstream, "request", lambda *_args, **_kwargs: response)
    with pytest.raises(OrthancError, match="too much metadata"):
        client.status()
    assert response.closed


def test_invalid_identifiers_urls_and_queries_never_make_a_request(archive):
    client, upstream = archive
    for resource in ("../system", SERIES + "?redirect=1", "https://example.test", "short", ""):
        with pytest.raises(ValueError, match="identifier"):
            client.snapshot(resource)
    for query in ({"UnknownTag": "x"}, {"PatientName": 1}, {"PatientID": "x" * 257}):
        with pytest.raises(ValueError):
            client.find_studies(query)
    for kwargs in ({"limit": 0}, {"limit": 101}, {"offset": -1}, {"limit": True}):
        with pytest.raises(ValueError):
            client.find_studies(**kwargs)
    for url in (
        "file:///etc/passwd",
        "https://u:p@example.test",
        "https://example.test/?secret=value",
        "https://example.test/#fragment",
        "https://a.test/../x",
    ):
        with pytest.raises(ValueError):
            OrthancClient(url)
    assert upstream.calls == []


TRANSFER_JOB = "12345678-1234-1234-1234-123456789abc"


class ExportArchive(Archive):
    def __init__(self):
        super().__init__()
        self.destination_rows = {"osirix": {"AET": "OsiriX-Mac", "Host": "private-host.local", "Port": 11112}, "unlisted": {"AET": "OTHER"}}
        self.upload_status = "Success"
        self.retained = None
        self.store_failure = None
        self.store_response = {"ID": TRANSFER_JOB}
        self.job_document = {"State": "Success", "Content": {"InstancesCount": 2, "FailedInstancesCount": 0}}
        self.upload_stream = None

    def request(self, method, url, **kwargs):
        route = urlsplit(url).path.removeprefix("/orthanc")
        if route == "/modalities":
            assert method == "GET" and kwargs["params"] == {"expand": "true"}
            value = self.destination_rows
        elif route == "/instances":
            assert method == "POST" and kwargs["headers"]["Content-Type"] == "application/dicom"
            self.upload_stream = kwargs["data"]
            data = self.upload_stream.read()
            if self.retained is None:
                self.retained = data
            self.files[A] = self.retained
            value = {"ID": A, "Status": self.upload_status}
        elif route == "/modalities/osirix/store":
            self.calls.append((method, url, kwargs))
            if self.store_failure:
                raise self.store_failure
            response = Response(json.dumps(self.store_response).encode())
            self.responses.append(response)
            return response
        elif route == f"/jobs/{TRANSFER_JOB}":
            value = self.job_document
        else:
            return super().request(method, url, **kwargs)
        self.calls.append((method, url, kwargs))
        response = Response(json.dumps(value).encode())
        self.responses.append(response)
        return response


@pytest.fixture
def export_archive():
    upstream = ExportArchive()
    return OrthancClient("https://archive.example.test/orthanc", "user", "password", session=upstream), upstream


def test_export_destinations_are_server_allowlisted_and_hide_network_details(export_archive):
    client, upstream = export_archive
    assert client.destinations(()) == [] and upstream.calls == []
    assert client.destinations(("osirix", "missing", "osirix")) == [{"id": "osirix", "name": "osirix", "aet": "OsiriX-Mac"}]
    for bad in ("../instances", "osirix?x=y", "https://elsewhere.test", ""):
        before = len(upstream.calls)
        with pytest.raises(ValueError, match="destination"):
            client.destinations((bad,))
        assert len(upstream.calls) == before
    assert all(response.closed for response in upstream.responses)


@pytest.mark.parametrize("status", ["Success", "AlreadyStored"])
def test_seg_upload_streams_file_and_verifies_exact_retained_bytes(export_archive, tmp_path, status):
    client, upstream = export_archive
    source = tmp_path / "synthetic-seg.dcm"
    source.write_bytes(b"synthetic segmentation bits")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    upstream.upload_status = status
    result = client.upload_instance(source, expected_sha256=digest)
    assert result == {"id": A, "status": status, "sha256": digest, "size": source.stat().st_size}
    assert upstream.upload_stream.closed
    assert upstream.calls[0][2]["data"] is upstream.upload_stream
    assert upstream.calls[1][0] == "GET" and upstream.calls[1][1].endswith(f"/instances/{A}/file")
    assert all(response.closed for response in upstream.responses)


def test_seg_upload_rejects_same_sop_with_different_archive_content(export_archive, tmp_path):
    client, upstream = export_archive
    source = tmp_path / "synthetic-seg.dcm"
    source.write_bytes(b"expected bits")
    upstream.upload_status, upstream.retained = "AlreadyStored", b"different bits"
    with pytest.raises(OrthancError, match="archive copy differs"):
        client.upload_instance(source)
    assert not any(url.endswith("/store") for _, url, _ in upstream.calls)
    assert all(response.closed for response in upstream.responses)


def test_seg_upload_limits_and_digest_are_checked_before_network(export_archive, tmp_path):
    client, upstream = export_archive
    source = tmp_path / "synthetic-seg.dcm"
    source.write_bytes(b"synthetic")
    with pytest.raises(ValueError, match="upload limit"):
        client.upload_instance(source, max_bytes=1)
    with pytest.raises(ValueError, match="changed"):
        client.upload_instance(source, expected_sha256="0" * 64)
    linked = tmp_path / "linked.dcm"
    linked.symlink_to(source)
    with pytest.raises(ValueError, match="unavailable"):
        client.upload_instance(linked)
    assert upstream.calls == []


def test_store_submits_exact_instance_ids_and_polls_only_that_job(export_archive):
    client, upstream = export_archive
    assert client.start_store([A, B, A], "osirix") == TRANSFER_JOB
    assert upstream.calls[-1][2]["json"] == {"Resources": [A, B], "Synchronous": False, "StorageCommitment": False}
    assert client.store_job(TRANSFER_JOB) == {"id": TRANSFER_JOB, "state": "Success", "instances_count": 2, "failed_instances_count": 0}
    upstream.job_document = {"State": "Running", "Content": {}}
    assert client.store_job(TRANSFER_JOB)["instances_count"] is None
    assert all(response.closed for response in upstream.responses)


@pytest.mark.parametrize("failure", [requests.Timeout("private credentials"), requests.ConnectionError("private patient")])
def test_uncertain_store_start_never_blindly_retries_or_leaks_upstream_text(export_archive, failure):
    from scan3d.orthanc import OrthancDeliveryUnknown

    client, upstream = export_archive
    upstream.store_failure = failure
    with pytest.raises(OrthancDeliveryUnknown, match="whether the send started") as caught:
        client.start_store([A], "osirix")
    assert "private" not in str(caught.value) and len(upstream.calls) == 1


def test_invalid_store_response_retains_delivery_uncertainty(export_archive):
    from scan3d.orthanc import OrthancDeliveryUnknown

    client, upstream = export_archive
    upstream.store_response = {"ID": "../other-job"}
    with pytest.raises(OrthancDeliveryUnknown):
        client.start_store([A], "osirix")
    assert len(upstream.calls) == 1
    before = len(upstream.calls)
    for invalid in ("../other-job", A):
        with pytest.raises(ValueError):
            client.store_job(invalid)
    with pytest.raises(ValueError):
        client.start_store([A], "../osirix")
    with pytest.raises(ValueError):
        client.start_store([], "osirix")
    assert len(upstream.calls) == before


def test_transfer_failure_uses_safe_error_and_rejects_invalid_counts(export_archive):
    client, upstream = export_archive
    upstream.job_document = {"State": "Failure", "ErrorDescription": "secret patient path", "Content": {"InstancesCount": 2, "FailedInstancesCount": 1}}
    result = client.store_job(TRANSFER_JOB)
    assert result["state"] == "Failure" and "secret" not in result["error"]
    for count in (True, -1, "2", None):
        upstream.job_document["Content"]["InstancesCount"] = count
        with pytest.raises(OrthancError, match="counts"):
            client.store_job(TRANSFER_JOB)
