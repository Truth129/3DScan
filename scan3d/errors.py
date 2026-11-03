"""Application errors with an explicit HTTP meaning."""


class NotFound(KeyError):
    """A requested record is absent or belongs to another case.

    The API maps only this to 404. Any other KeyError is a defect and surfaces
    as a logged 500 instead of a misleading "not found" naming a dict key.
    Subclassing KeyError keeps existing ``except KeyError`` lookups working.
    """

    def __str__(self):
        return str(self.args[0]) if self.args else "Not found"


class Conflict(ValueError):
    """The request is sound, but something under way must finish first. The API maps this to 409."""
