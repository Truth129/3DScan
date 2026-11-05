"""Streaming ``multipart/related`` (RFC 2046) for DICOMweb: WADO-RS answers in, STOW-RS bodies out.

The reader never holds a whole part in memory. It keeps back just enough bytes to find a
boundary that a chunk split in two, and caps parts, bytes, preamble and headers, so a server
cannot fill the disk or memory with an answer nobody asked for.
"""

from dataclasses import dataclass
import email.message
import hashlib
from pathlib import Path
import re
import secrets

from .errors import ArchiveError

# RFC 2046 bchars: up to 70 characters, and the last may not be a space.
_BOUNDARY = re.compile(r"[0-9A-Za-z'()+_,\-./:=? ]{0,69}[0-9A-Za-z'()+_,\-./:=?]\Z")
_PREAMBLE_LIMIT = 64 * 1024
_HEADER_LIMIT = 16 * 1024
_PADDING_LIMIT = 256
_BLOCK = 1024 * 1024


def media_type(content_type):
    """The lower-case ``type/subtype`` of a Content-Type value; RFC 2045's text/plain when absent."""
    message = email.message.Message()
    message["Content-Type"] = content_type if isinstance(content_type, str) else ""
    return message.get_content_type()


def boundary(content_type):
    message = email.message.Message()
    message["Content-Type"] = content_type if isinstance(content_type, str) else ""
    value = message.get_param("boundary")
    if message.get_content_type() != "multipart/related" or not isinstance(value, str) or not _BOUNDARY.fullmatch(value):
        raise ArchiveError("The archive returned an unexpected file response.")
    return value


@dataclass(frozen=True)
class Part:
    path: Path
    content_type: str
    size: int
    sha256: str


def _part_type(block):
    content_type = ""
    for line in bytes(block).split(b"\r\n"):
        name, colon, value = line.partition(b":")
        if not colon:
            raise ArchiveError("The archive returned a malformed file response.")
        if name.strip().lower() == b"content-type":
            content_type = value.strip().decode("latin-1")
    return media_type(content_type)


def save_parts(chunks, content_type, target, *, max_parts, max_bytes, cancelled=None):
    """Write each part of a multipart/related stream to ``target(index)``, opened ``xb``.

    Bytes before the first boundary and after the last are ignored, as RFC 2046 requires. On
    any failure, the files this call created are removed, so a retry can use the same paths.
    """
    separator = b"\r\n--" + boundary(content_type).encode("ascii")
    keep = len(separator) - 1
    # The first boundary may open the stream with no line break before it.
    buffer = bytearray(b"\r\n")
    state, skipped, received = "preamble", 0, 0
    created, parts = [], []
    output = digest = path = part_type = None
    size = 0

    def write(data):
        nonlocal size, received
        size += len(data)
        received += len(data)
        if received > max_bytes:
            raise ArchiveError("The archive file exceeds the import size limit.")
        output.write(data)
        digest.update(data)

    def step():
        """Advance one state if the buffer allows; False means more bytes are needed."""
        nonlocal state, skipped, output, digest, path, part_type, size
        if state == "preamble":
            index = buffer.find(separator)
            if index < 0:
                drop = max(0, len(buffer) - keep)
                skipped += drop
                del buffer[:drop]
                if skipped > _PREAMBLE_LIMIT:
                    raise ArchiveError("The archive returned an unexpected file response.")
                return False
            del buffer[: index + len(separator)]
            state = "delimiter"
            return True
        if state == "delimiter":
            if len(buffer) < 2:
                return False
            if buffer.startswith(b"--"):
                state = "end"
                return False
            index = buffer.find(b"\r\n")
            if index < 0 or buffer[:index].strip(b" \t"):
                if index >= 0 or len(buffer) > _PADDING_LIMIT:
                    raise ArchiveError("The archive returned a malformed file response.")
                return False
            del buffer[: index + 2]
            if len(parts) >= max_parts:
                raise ArchiveError("The archive returned more files than requested.")
            state = "headers"
            return True
        if state == "headers":
            if buffer.startswith(b"\r\n"):
                block, end = b"", 2
            else:
                index = buffer.find(b"\r\n\r\n")
                if index < 0:
                    if len(buffer) > _HEADER_LIMIT:
                        raise ArchiveError("The archive returned a malformed file response.")
                    return False
                block, end = buffer[:index], index + 4
            if end > _HEADER_LIMIT:
                raise ArchiveError("The archive returned a malformed file response.")
            part_type = _part_type(block) if block else media_type("")
            del buffer[:end]
            path = Path(target(len(parts)))
            output = path.open("xb")
            created.append(path)
            digest, size = hashlib.sha256(), 0
            state = "body"
            return True
        index = buffer.find(separator)
        if index < 0:
            flush = len(buffer) - keep
            if flush > 0:
                write(buffer[:flush])
                del buffer[:flush]
            return False
        write(buffer[:index])
        del buffer[: index + len(separator)]
        output.close()
        output = None
        parts.append(Part(path=path, content_type=part_type, size=size, sha256=digest.hexdigest()))
        state = "delimiter"
        return True

    try:
        for chunk in chunks:
            if cancelled and cancelled():
                raise InterruptedError("Archive import cancelled")
            buffer.extend(chunk)
            while step():
                pass
            if state == "end":
                break
        if state != "end":
            raise ArchiveError("The archive file transfer ended early. Retry the import.")
        return tuple(parts)
    except BaseException:
        if output is not None:
            output.close()
        for item in created:
            item.unlink(missing_ok=True)
        raise


class StowBody:
    """A one-instance STOW-RS body of known length, so requests sends Content-Length, not chunks.

    Some archives refuse chunked uploads. Each iteration reopens the file, so a request can be
    sent again. The closing boundary goes out only once the bytes sent match the expected size
    and hash: a file that changes mid-send never forms a complete part the archive could store.
    """

    def __init__(self, path, *, expected_sha256):
        self.path = Path(path)
        self.size = self.path.stat().st_size
        self.expected_sha256 = expected_sha256
        self.boundary = secrets.token_hex(16)
        self.content_type = f'multipart/related; type="application/dicom"; boundary={self.boundary}'
        self._head = f"--{self.boundary}\r\nContent-Type: application/dicom\r\n\r\n".encode("ascii")
        self._tail = f"\r\n--{self.boundary}--\r\n".encode("ascii")

    def __len__(self):
        return len(self._head) + self.size + len(self._tail)

    def __iter__(self):
        yield self._head
        digest, sent = hashlib.sha256(), 0
        with self.path.open("rb") as stream:
            # One byte past the expected size shows a file that grew, without sending it.
            while block := stream.read(min(_BLOCK, self.size - sent + 1)):
                sent += len(block)
                if sent > self.size:
                    break
                digest.update(block)
                yield block
        if sent != self.size or digest.hexdigest() != self.expected_sha256:
            raise ValueError("The saved DICOM file changed; create a new export")
        yield self._tail
