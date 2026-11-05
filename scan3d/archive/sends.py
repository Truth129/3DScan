"""Send SEG and 3D-view exports straight to a configured DICOM node with C-STORE.

A direct send binds to the case's own copy of the CT, as a file does, so a folder, ZIP or
DICOMweb case can use it. An Orthanc case keeps sending through Orthanc. The artifact is the
exporters' own, so a SEG file, an archived SEG and a sent SEG of the same masks are one instance.

The receipt (`delivery-v2.json`) says "starting" before connecting. A send that then stops
without an answer is "unknown": a retry sends the same instance again and says it may already
have arrived, because nothing here can ask the node what it holds.
"""

import hashlib
import logging
from types import SimpleNamespace

import pydicom

from ..orthanc_exports import ARCHIVE_OFF, ExportUnavailable, OrthancExports
from ..orthanc_view_exports import OrthancViewExports
from ..store import now
from . import cstore
from .errors import ArchiveError
from .exports import DirectDelivery, SentInstance, load_receipt, save_receipt

log = logging.getLogger(__name__)
RECEIPT = "delivery-v2.json"


def _key(identity):
    return hashlib.sha256(identity.encode()).hexdigest()


class _DirectDelivery:
    """C-STORE delivery, for an Orthanc exporter subclass that keeps its artifact."""

    retry_hint = "Check the destination; a retry sends the same instance again."
    unconfirmed_error = f"The send was not confirmed, so it may already have arrived. {retry_hint}"

    def _send_settings(self):
        settings = self.service.settings.dicom_send
        if settings is None:
            raise ExportUnavailable("No DICOM destination is configured on this workstation.")
        return settings

    def _configured(self, destination_id):
        try:
            destinations = {item.id: item for item in self._send_settings().destinations()}
        except ValueError as exc:
            raise ExportUnavailable(str(exc)) from None
        if destination_id not in destinations:
            raise ExportUnavailable(f"Choose a configured DICOM destination for {self.label}.")
        return destinations[destination_id]

    def _destination(self, destination, resend):
        if destination is None:
            raise ExportUnavailable(f"Choose a DICOM destination for {self.label}.")
        return self._configured(destination).name

    def _export_key(self, artifact_key, destination, file_delivery):
        """One artifact to one node, named by where it goes rather than by its alias."""
        return hashlib.sha256(f"{artifact_key}:dimse:{_key(self._configured(destination).identity)}".encode()).hexdigest()

    def _enqueue(self, case_id, revision_id, fields, extras, destination, display_name, resend, summarize, **kwargs):
        # The address it was queued for, so a destination moved meanwhile is refused, never sent to.
        identity = self._configured(destination).identity
        extras = {**extras, "delivery": "direct", "destination_identity": identity}

        def keyed(payload):
            return {**summarize(payload), "destination_key": _key(identity)}

        return super()._enqueue(case_id, revision_id, fields, extras, destination, display_name, resend, keyed, **kwargs)

    def direct_availability(self, case_id, revision_id, *, archive=True):
        """The destinations this case's revision can be sent to, for the export dialogs."""
        try:
            if not archive:
                raise ExportUnavailable(ARCHIVE_OFF)
            destinations = self._send_settings().destinations()
            self._local_inputs(case_id, revision_id)
        except (ExportUnavailable, ValueError) as exc:
            return {"available": False, "reason": str(exc), "destinations": []}
        # `key` names the address, as a send's result does, so a moved destination never reads as already sent.
        listed = [{"id": item.id, "name": item.name, "aet": item.ae_title, "key": _key(item.identity)} for item in destinations]
        return {"available": True, "destinations": listed}

    def _sent_result(self, artifact, payload, state):
        destination = payload["destination"]
        receipt = SimpleNamespace(uploaded={}, destinations={destination: SimpleNamespace(state=state)})
        result = self._result(artifact, receipt, destination, payload["destination_name"])
        result[self.result_key]["destination_key"] = _key(payload["destination_identity"])
        return result

    def recover(self):
        """A send cut off by a restart may have arrived: say so, and record it as unknown."""
        super().recover()
        for job in self.service.store.list("jobs"):
            if job["kind"] != self.kind or job["status"] != "failed" or job.get("stage") != "Interrupted" or job["payload"].get("delivery_state") != "starting":
                continue
            self.service.store.update_job(job["id"], error=self.unconfirmed_error, stage="Delivery not confirmed")
            path = self._directory(job["case_id"], job["payload"]["export_id"]) / RECEIPT
            try:
                receipt = load_receipt(path, job, self.label)
                key = _key(job["payload"]["destination_identity"])
                if receipt.destinations.get(key) and receipt.destinations[key].state == "starting":
                    receipt.destinations[key] = _with_state(receipt.destinations[key], "unknown")
                    save_receipt(path, receipt)
            except (ExportUnavailable, OSError, KeyError):
                pass  # The job's text already says the send may have arrived.

    def run(self, job):
        payload = job["payload"]
        artifact, delivery, receipt, path, key, sending, uncertain = None, None, None, None, None, False, False
        destination_id = payload["destination"]
        try:
            directory = self._prepare_directory(job)
            artifact = self._artifact(job, directory)
            destination = self._configured(destination_id)
            if destination.identity != payload["destination_identity"]:
                raise ExportUnavailable(f"{destination.name}'s address changed after this send was queued. Start a new send.")
            path = directory / RECEIPT
            receipt = load_receipt(path, job, self.label)
            key = _key(destination.identity)
            previous = receipt.destinations.get(key)
            # Whatever this attempt does, an earlier one that ended unanswered may have arrived.
            uncertain = bool(previous and previous.state in ("starting", "unknown"))
            if not (previous and previous.state == "success" and previous.attempt_id == payload["delivery_attempt_id"]):
                self.service.check_cancel(job)
                # Write-ahead: from here a lost answer is "unknown", never "not sent".
                delivery = DirectDelivery(destination.name, destination.identity, payload["delivery_attempt_id"], "starting", {})
                receipt.destinations[key] = delivery
                save_receipt(path, receipt)
                sending = True
                payload["delivery_state"] = "starting"
                self.service.store.update_job(job["id"], payload=payload, stage=f"Sending {self.noun} to {destination.name}")
                for item in artifact.files:
                    sent = cstore.send(destination, self._send_settings().ae_title, pydicom.dcmread(directory / "dicom" / item.path))
                    delivery.sent[item.path] = SentInstance(item.sop_instance_uid, f"{sent.status:04X}", sent.transfer_syntax_uid)
                    save_receipt(path, receipt)
                receipt.destinations[key] = delivery = _with_state(delivery, "success")
                save_receipt(path, receipt)
                payload["delivery_state"] = "success"
                self.service.store.update_job(job["id"], payload=payload)
            self.service.store.update_job(
                job["id"],
                status="completed",
                stage=f"{self.noun} sent",
                error=None,
                completed_at=now(),
                result=self._sent_result(artifact, payload, "success"),
            )
        except Exception as exc:
            state = (
                "refused"
                if isinstance(exc, cstore.SendRefused)
                else "failure"
                if isinstance(exc, cstore.SendFailed) and not (delivery and delivery.sent)
                else "unknown"
                if sending
                else None
            )
            if uncertain and state in ("failure", "refused"):
                state = "unknown"
            if state and delivery:
                try:
                    receipt.destinations[key] = _with_state(delivery, state)
                    save_receipt(path, receipt)
                except OSError:
                    pass  # The write-ahead "starting" already keeps a retry from claiming nothing was sent.
            cancelled = isinstance(exc, InterruptedError) and not sending
            if isinstance(exc, cstore.SendUnknown):
                error = f"{exc} {self.retry_hint}"
            elif uncertain and isinstance(exc, cstore.SendFailed):
                error = f"{exc} An earlier attempt may already have arrived. {self.retry_hint}"
            elif isinstance(exc, (ExportUnavailable, ArchiveError)):
                error = str(exc)
            else:
                error = self.unconfirmed_error if state == "unknown" else self.failed_error
            if not cancelled:
                log.warning("%s direct send %s failed (%s)", self.label, job["id"], type(exc).__name__)
            changes = dict(
                status="cancelled" if cancelled else "failed",
                stage="Cancelled before sending" if cancelled else "Delivery not confirmed" if state == "unknown" else "Send failed",
                error=None if cancelled else error,
                completed_at=now(),
            )
            if artifact:
                changes["result"] = self._sent_result(artifact, payload, state or "failure")
            self.service.store.update_job(job["id"], **changes)


def _with_state(delivery, state):
    return DirectDelivery(delivery.name, delivery.identity, delivery.attempt_id, state, dict(delivery.sent))


class DirectSegExports(_DirectDelivery, OrthancExports):
    kind = "direct_seg_export"
    # A SEG file or an archived SEG of the same masks is this same instance.
    shared_kinds = ("orthanc_export", "archive_seg_export")
    interrupted_error = "Export interrupted before sending. Retry to send its exact SEG."

    def _local_inputs(self, case_id, revision_id):
        return self._inputs(case_id, revision_id, delivery="direct")


class DirectViewExports(_DirectDelivery, OrthancViewExports):
    kind = "direct_view_export"
    # Only the dialog that holds the image can retry, so these state the outcome alone.
    interrupted_error = "Export interrupted before sending."
    archive_bound = False

    def _local_inputs(self, case_id, revision_id):
        return self._inputs(case_id, revision_id)

    def _payload_fields(self, inputs):
        return {}

    def _check_study(self, inputs, path):
        """A node gives no way to check its study before a send."""
