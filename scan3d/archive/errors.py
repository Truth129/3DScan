"""Archive errors whose text is safe to show.

Messages are fixed strings. They never carry credentials, URLs, query values or patient data.
"""


class ArchiveError(RuntimeError):
    """An archive or network failure. The API answers 502."""


class ArchiveTooLarge(ArchiveError):
    """A file the archive sends is over the size this transfer allows."""


class ArchiveChanged(RuntimeError):
    """The archive changed after a snapshot was selected. The API answers 409."""
