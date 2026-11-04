"""A rendered 3D view saved as a DICOM file, with no archive.

The view exporter owns staging, queuing, the encoder and its checks. A file
differs only in what it binds to and where it goes: any CT prepared from DICOM,
a folder or ZIP import included, and no study check, because nothing is stored
anywhere. Its own job kind keeps a file request off the archive path, even on
code that predates it.
"""

from .orthanc_exports import ExportUnavailable
from .orthanc_view_exports import OrthancViewExports
from .store import public_job


class ViewFileExports(OrthancViewExports):
    kind = "view_file_export"
    file_delivery = True
    # Only the dialog that holds the image can retry, so these state the outcome alone.
    interrupted_error = "Export interrupted."
    file_interrupted_error = "Export interrupted."

    def _payload_fields(self, inputs):
        return {}

    def _destination(self, destination, resend):
        if destination is not None or resend:
            raise ExportUnavailable("A DICOM view file has no destination.")
        return None

    def _check_study(self, inputs, path):
        """A file joins no archive study, so there is nothing to compare."""

    def availability(self, case_id, revision_id):
        self.service.store.revision(case_id, revision_id)
        exports = [public_job(job) for job in self.service.store.list("jobs", case_id) if job["kind"] == self.kind and job.get("revision_id") == revision_id]
        result = {"available": True, "exports": exports, "encoding_version": self.encoding_version}
        try:
            self._inputs(case_id, revision_id)
        except ExportUnavailable as exc:
            result.update(available=False, reason=str(exc))
        return result
