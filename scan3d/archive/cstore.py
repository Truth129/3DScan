"""Send one DICOM instance to a configured node with C-STORE, and C-ECHO it.

pynetdicom is imported inside these functions, so a workstation without it still starts and
does everything else. Its logger is held at WARNING, as its INFO lines name what is sent.

Outcomes, which a delivery records:
- success, or a warning status (B000, B006, B007), which counts as stored;
- `SendRefused`: the node does not accept this kind of object, so nothing was sent;
- `SendFailed`: the node refused or could not be reached before anything was sent;
- `SendUnknown`: the connection ended after sending began, so the instance may have arrived.
"""

from dataclasses import dataclass
import logging
import ssl

from .errors import ArchiveError

_WARNINGS = {0xB000, 0xB006, 0xB007}
_EXPLICIT, _IMPLICIT = "1.2.840.10008.1.2.1", "1.2.840.10008.1.2"
_VERIFICATION = "1.2.840.10008.1.1"


class SendFailed(ArchiveError):
    """Nothing reached the destination's storage."""


class SendRefused(SendFailed):
    """The destination does not accept this kind of DICOM object."""


class SendUnknown(ArchiveError):
    """Sending began and did not finish, so the instance may already be there."""


@dataclass(frozen=True)
class Sent:
    status: int
    transfer_syntax_uid: str

    @property
    def warning(self):
        return self.status != 0


def _application(destination, calling_ae):
    try:
        from pynetdicom import AE
    except ImportError:
        raise SendFailed("Direct DICOM sends need pynetdicom, which this installation lacks. Nothing was sent.") from None

    logging.getLogger("pynetdicom").setLevel(logging.WARNING)
    ae = AE(ae_title=destination.calling_ae or calling_ae)
    ae.acse_timeout = ae.dimse_timeout = ae.network_timeout = destination.timeout
    ae.connection_timeout = min(destination.timeout, 10)
    return ae


def _associate(ae, destination):
    tls = None
    if destination.tls:
        try:
            context = ssl.create_default_context(ssl.Purpose.SERVER_AUTH, cafile=destination.ca_bundle or None)
        except OSError:  # ssl.SSLError included
            raise SendFailed(f"{destination.name}'s ca_bundle cannot be loaded. Nothing was sent.") from None
        tls = (context, destination.host)
    try:
        association = ae.associate(destination.host, destination.port, ae_title=destination.ae_title, tls_args=tls)
    except (OSError, ValueError, RuntimeError):
        raise SendFailed(f"{destination.name} could not be reached. Nothing was sent.") from None
    if association.is_rejected:
        raise SendFailed(f"{destination.name} refused the connection; check its AE titles. Nothing was sent.")
    if not association.is_established and association.rejected_contexts:
        # pynetdicom ends an association in which the node accepted nothing that was offered.
        raise SendRefused(f"{destination.name} does not accept this kind of DICOM object. Nothing was sent.")
    if not association.is_established:
        raise SendFailed(f"{destination.name} could not be reached. Nothing was sent.")
    return association


def send(destination, calling_ae, dataset):
    """C-STORE one dataset, encoded as Explicit VR Little Endian, or Implicit if that is all the node takes."""
    ae = _application(destination, calling_ae)
    sop_class = str(dataset.SOPClassUID)
    ae.add_requested_context(sop_class, [_EXPLICIT, _IMPLICIT])
    association = _associate(ae, destination)
    try:
        accepted = [context for context in association.accepted_contexts if context.abstract_syntax == sop_class]
        if not accepted:
            raise SendRefused(f"{destination.name} does not accept this kind of DICOM object. Nothing was sent.")
        syntax = str(accepted[0].transfer_syntax[0])
        try:
            answer = association.send_c_store(dataset)
        except (OSError, ValueError, RuntimeError, AttributeError):
            raise SendUnknown(f"The send to {destination.name} stopped part way, so it may already have arrived.") from None
        if not answer or "Status" not in answer:
            raise SendUnknown(f"{destination.name} did not confirm the send, so it may already have arrived.")
        status = int(answer.Status)
        if status != 0 and status not in _WARNINGS:
            raise SendFailed(f"{destination.name} refused the instance (status {status:04X}).")
        return Sent(status, syntax)
    finally:
        if association.is_established:
            association.release()


def echo(destination, calling_ae):
    """Whether the destination answers C-ECHO; raises SendFailed when it cannot."""
    ae = _application(destination, calling_ae)
    ae.add_requested_context(_VERIFICATION)
    association = _associate(ae, destination)
    try:
        answer = association.send_c_echo()
        if not answer or int(answer.get("Status", -1)) != 0:
            raise SendFailed(f"{destination.name} did not answer C-ECHO.")
    finally:
        if association.is_established:
            association.release()
