"""Strict readers for DICOM JSON (PS3.18 F.2) datasets returned by a DICOMweb archive.

Identities are strict: a malformed UID or count is an archive error, never a guess. Display
text follows ``orthanc._text``: anything malformed or oversized reads as empty, and multiple
values are joined with a backslash, so a row reads the same as one from Orthanc's REST API.
"""

import re

from .errors import ArchiveError

_UID = re.compile(r"[0-9]+(?:\.[0-9]+)*\Z")
_INTEGER = re.compile(r" *[+-]?[0-9]{1,12} *\Z")
_TEXT_LIMIT = 1024


def datasets(value, *, cap):
    """A QIDO or STOW answer: a list of at most ``cap`` datasets."""
    if not isinstance(value, list) or len(value) > cap or any(not isinstance(item, dict) for item in value):
        raise ArchiveError("The archive returned an invalid DICOM JSON response.")
    return value


def _values(dataset, tag):
    element = dataset.get(tag)
    if element is None:
        return []
    if not isinstance(element, dict):
        raise ArchiveError("The archive returned an invalid DICOM JSON attribute.")
    values = element.get("Value", [])
    if not isinstance(values, list):
        raise ArchiveError("The archive returned an invalid DICOM JSON attribute.")
    return values


def uid(dataset, tag, *, optional=False):
    values = _values(dataset, tag)
    if optional and values in ([], [None], [""]):
        return None
    if len(values) != 1 or not isinstance(values[0], str) or len(values[0]) > 64 or not _UID.fullmatch(values[0]):
        raise ArchiveError("The archive returned an invalid DICOM identity.")
    return values[0]


def _capped(value):
    return value if len(value) <= _TEXT_LIMIT else ""


def text(dataset, tag):
    try:
        values = _values(dataset, tag)
    except ArchiveError:
        return ""
    if any(not isinstance(value, str) for value in values):
        return ""
    return _capped("\\".join(values))


def integer(dataset, tag):
    """An IS or US value, which servers send as a JSON number or, for IS, a string."""
    values = _values(dataset, tag)
    if not values or values == [None]:
        return None
    value = values[0]
    if len(values) == 1 and isinstance(value, str) and _INTEGER.fullmatch(value):
        value = int(value)
    if len(values) != 1 or isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ArchiveError("The archive returned an invalid DICOM count.")
    return value


def _name(value):
    """Rebuild ``Alphabetic=Ideographic=Phonetic``, dropping trailing empty groups as DICOM does."""
    if not isinstance(value, dict):
        return None
    groups = [value.get(key) or "" for key in ("Alphabetic", "Ideographic", "Phonetic")]
    if any(not isinstance(group, str) for group in groups):
        return None
    while groups and not groups[-1]:
        groups.pop()
    return "=".join(groups)


def person_name(dataset, tag):
    try:
        names = [_name(value) for value in _values(dataset, tag)]
    except ArchiveError:
        return ""
    if any(name is None for name in names):
        return ""
    return _capped("\\".join(names))
