"""Direct DICOM sends: a SEG or 3D view of a folder case, C-STOREd to a local pynetdicom node."""

from io import BytesIO
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pydicom
from pynetdicom import AE, evt
import pytest

from scan3d.api import create_app
from scan3d.archive import cstore
from scan3d.archive.sends import DirectSegExports
from scan3d.archive.settings import DicomSendSettings
from scan3d.config import Settings
from test_dicom_seg import fixture  # noqa: F401
from test_dicom_seg_file_export import execute, import_folder, save_masks
from test_orthanc_view_exports import PNG

SEG, SECONDARY_CAPTURE, VERIFICATION = "1.2.840.10008.5.1.4.1.1.66.4", "1.2.840.10008.5.1.4.1.1.7", "1.2.840.10008.1.1"
EXPLICIT, IMPLICIT = "1.2.840.10008.1.2.1", "1.2.840.10008.1.2"


class Node:
    """A receiver on a free local port that records what it stores, answering with `status`."""

    def __init__(self, sop_classes=(SEG, SECONDARY_CAPTURE), syntaxes=(EXPLICIT, IMPLICIT)):
        self.received, self.status, self.abort = [], 0x0000, False
        ae = AE(ae_title="RECEIVER")
        for sop_class in sop_classes:
            ae.add_supported_context(sop_class, list(syntaxes))
        ae.add_supported_context(VERIFICATION)
        self.server = ae.start_server(("127.0.0.1", 0), block=False, evt_handlers=[(evt.EVT_C_STORE, self._store)])
        self.port = self.server.server_address[1]

    def _store(self, event):
        if self.abort:
            event.assoc.abort()
            return 0x0000
        self.received.append((str(event.dataset.SOPInstanceUID), str(event.context.transfer_syntax)))
        return self.status


@pytest.fixture
def rig(tmp_path, fixture):  # noqa: F811
    listed = tmp_path / "destinations.json"

    def use(**kwargs):
        """A new node under the same alias, which moves the destination to its port."""
        if getattr(rig, "node", None):
            rig.node.server.shutdown()
        rig.node = Node(**kwargs)
        listed.write_text(json.dumps([{"id": "pacs", "name": "Test PACS", "ae_title": "RECEIVER", "host": "127.0.0.1", "port": rig.node.port, "timeout": 5}]))

    rig = SimpleNamespace(use=use, node=None)
    use()
    app = create_app(
        Settings(tmp_path / "data", (tmp_path,), web_dir=tmp_path / "dist", modalities=("CT",), dicom_send=DicomSendSettings(destinations_file=str(listed)))
    )
    rig.service = app.state.service
    rig.case, ct = import_folder(rig.service, fixture)
    rig.saved = save_masks(rig.service, ct, fixture)
    rig.http = TestClient(app, base_url="http://127.0.0.1")
    revision = f"/api/cases/{rig.case['id']}/revisions/{rig.saved['id']}"
    rig.seg, rig.view = f"{revision}/dicom-seg", f"{revision}/dicom-view"
    yield rig
    rig.http.close()
    rig.node.server.shutdown()


def send_seg(rig):
    response = rig.http.post(rig.seg, json={"delivery": "direct", "destination": "pacs"})
    assert response.status_code == 200, response.text
    return response.json()


def delivery(rig, job):
    """The one destination's record in the export's receipt."""
    export_id = rig.service.store.get("jobs", job["id"])["payload"]["export_id"]
    receipt = json.loads((rig.service.store.case_dir(rig.case["id"]) / "dicom-exports" / export_id / "delivery-v2.json").read_text())
    (recorded,) = receipt["destinations"].values()
    return recorded


def test_a_folder_case_sends_its_seg_and_its_view_to_a_configured_node(rig):
    (listed,) = rig.http.get(rig.seg).json()["direct"]["destinations"]
    assert (listed["id"], listed["name"], listed["aet"]) == ("pacs", "Test PACS", "RECEIVER")
    job = send_seg(rig)
    assert job["kind"] == "direct_seg_export" and send_seg(rig)["id"] == job["id"]
    finished = execute(rig, job)
    assert (finished["status"], finished["result"]["dicom_seg"]["sent"]) == ("completed", True)
    assert delivery(rig, finished)["state"] == "success" and finished["result"]["dicom_seg"]["destination_key"] == listed["key"]
    # A SEG file of the same masks is the very instance that was sent.
    made = execute(rig, rig.http.post(rig.seg, json={"delivery": "file"}).json())
    saved = pydicom.dcmread(BytesIO(rig.http.get(f"{rig.seg}/{made['result']['dicom_seg']['export_id']}/segment.dcm").content))
    assert rig.node.received == [(saved.SOPInstanceUID, EXPLICIT)]

    assert rig.http.get(rig.view).json()["direct"]["available"] is True
    data = {"reference_image_id": rig.saved["reference_image_id"], "destination": "pacs", "delivery": "direct"}
    view = rig.http.post(rig.view, files={"image": ("view.png", PNG, "image/png")}, data=data).json()
    assert view["kind"] == "direct_view_export" and execute(rig, view)["status"] == "completed"
    assert len(rig.node.received) == 2


def test_a_node_that_takes_only_implicit_vr_is_sent_implicit_vr(rig):
    rig.use(syntaxes=(IMPLICIT,))
    finished = execute(rig, send_seg(rig))
    assert finished["status"] == "completed" and rig.node.received[0][1] == IMPLICIT
    assert [item["transfer_syntax_uid"] for item in delivery(rig, finished)["sent"].values()] == [IMPLICIT]


def test_a_node_that_takes_no_seg_is_refused_and_sent_nothing(rig):
    rig.use(sop_classes=(SECONDARY_CAPTURE,))
    finished = execute(rig, send_seg(rig))
    assert finished["status"] == "failed" and "does not accept this kind of DICOM object. Nothing was sent." in finished["error"]
    assert delivery(rig, finished)["state"] == "refused" and rig.node.received == []


def test_a_refusing_status_fails_the_send(rig):
    rig.node.status = 0xA700
    finished = execute(rig, send_seg(rig))
    assert finished["status"] == "failed" and "refused the instance (status A700)" in finished["error"]
    assert delivery(rig, finished)["state"] == "failure"


def test_a_send_cut_off_may_have_arrived_and_a_retry_sends_the_same_instance(rig):
    rig.node.abort = True
    first = execute(rig, send_seg(rig))
    assert (first["status"], first["stage"]) == ("failed", "Delivery not confirmed") and "may already have arrived" in first["error"]
    assert delivery(rig, first)["state"] == "unknown"
    # A retry the node refuses sent nothing itself, but the first attempt may still be there.
    rig.node.abort, rig.node.status = False, 0xA700
    refused = execute(rig, send_seg(rig))
    assert (refused["stage"], delivery(rig, refused)["state"]) == ("Delivery not confirmed", "unknown")
    assert "An earlier attempt may already have arrived." in refused["error"]
    rig.node.status = 0x0000
    retry = execute(rig, send_seg(rig))
    assert retry["status"] == "completed" and delivery(rig, retry)["state"] == "success"
    assert retry["payload"]["export_id"] == first["payload"]["export_id"] and len({sop for sop, _syntax in rig.node.received}) == 1


def test_a_restart_during_a_send_records_that_it_may_have_arrived(rig, monkeypatch):
    def stopped(*args):
        raise SystemExit

    monkeypatch.setattr(cstore, "send", stopped)
    job = send_seg(rig)
    with pytest.raises(SystemExit):
        execute(rig, job)
    rig.service.store.recover()
    rig.service.direct_seg_exports.recover()
    recovered = rig.service.store.get("jobs", job["id"])
    assert (recovered["stage"], recovered["error"]) == ("Delivery not confirmed", DirectSegExports.unconfirmed_error)
    assert delivery(rig, job)["state"] == "unknown"


def test_a_destination_moved_after_queueing_is_never_sent_to(rig):
    job = send_seg(rig)
    before = rig.node
    rig.use()
    finished = execute(rig, job)
    assert finished["status"] == "failed" and "address changed after this send was queued" in finished["error"]
    assert before.received == rig.node.received == []
    moved = rig.service.store.get("jobs", send_seg(rig)["id"])
    assert moved["payload"]["export_key"] != finished["payload"]["export_key"]
    # The dialog tells destinations apart by address, so the old send never reads as sent to the new one.
    (listed,) = rig.http.get(rig.seg).json()["direct"]["destinations"]
    assert listed["key"] == moved["result"]["dicom_seg"]["destination_key"] != finished["result"]["dicom_seg"]["destination_key"]


def test_trash_during_a_send_says_the_send_will_finish(rig, monkeypatch):
    sending, trashed = cstore.send, {}

    def send(*args):
        trashed.update(rig.service.trash_case(rig.case["id"]))
        return sending(*args)

    monkeypatch.setattr(cstore, "send", send)
    finished = execute(rig, send_seg(rig))
    assert (trashed["jobs_stopped"], trashed["sends_continuing"], finished["status"]) == (0, 1, "completed")


def test_a_ca_bundle_that_does_not_load_stops_the_server_and_the_send(rig, tmp_path):
    bundle = tmp_path / "not-pem.crt"
    bundle.write_bytes(b"\x30\x82 not a PEM certificate")
    listed = [{"id": "pacs", "ae_title": "RECEIVER", "host": "127.0.0.1", "port": rig.node.port, "tls": True, "ca_bundle": str(bundle)}]
    (tmp_path / "destinations.json").write_text(json.dumps(listed))
    settings = DicomSendSettings(destinations_file=str(tmp_path / "destinations.json"))
    assert settings.missing_files() == ["The ca_bundle of destination pacs"]
    finished = execute(rig, send_seg(rig))
    assert (finished["stage"], delivery(rig, finished)["state"]) == ("Send failed", "failure")
    assert finished["error"] == "pacs's ca_bundle cannot be loaded. Nothing was sent."
