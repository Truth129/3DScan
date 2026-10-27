"""Record IDs with a stable wire format across supported Python runtimes."""

import sys
import uuid


def identifier() -> str:
    """Use time-ordered UUID7 on Python 3.14+, retaining UUID4 on older runtimes.

    Both forms are 32 lowercase hex characters. Existing IDs remain valid;
    content-addressed image identities are generated independently.
    """
    if sys.version_info >= (3, 14):
        return uuid.uuid7().hex
    return uuid.uuid4().hex
