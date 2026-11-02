"""Who a request acts as, and how a password is kept.

This module imports nothing from `store`, which reads the current person when it
creates a case, a job or a review.
"""

from contextvars import ContextVar
from dataclasses import dataclass, field
import hashlib
import hmac
import re
import secrets
import unicodedata

PASSWORD_LENGTHS = (10, 1024)
_USERNAME = re.compile(r"[A-Za-z0-9._@-]{1,64}")
# Parameters travel in each hash, so raising these later leaves stored hashes readable.
_SCRYPT = dict(n=2**17, r=8, p=1)
_DIGEST_BYTES = 32
_SALT_BYTES = 16
# Fixed, so an unknown name costs the same work with no stored dummy hash to precompute.
_BURN_SALT = b"scan3d-no-such-account"


@dataclass(frozen=True)
class Principal:
    id: str
    username: str
    display_name: str
    role: str
    archive_access: bool
    token_hash: str | None = field(default=None, repr=False)
    shared_device: bool = False
    # This request moved the session's expiry on, so its cookie needs the same.
    slid: bool = False

    @property
    def is_admin(self):
        return self.role == "admin"


LOCAL = Principal("local", "local", "Local user", "admin", True)
EVERY_CASE = object()

_principal = ContextVar("scan3d_principal", default=None)


def current():
    return _principal.get()


def activate(principal):
    _principal.set(principal)


def actor_id():
    principal = current()
    return principal.id if principal else None


def case_scope(principal):
    """A scope is EVERY_CASE or a user id, never None, so nobody can read as everything by default."""
    if principal is None:
        raise RuntimeError("A case scope needs a principal")
    return EVERY_CASE if principal is LOCAL else principal.id


def valid_username(username):
    return _USERNAME.fullmatch(username) is not None


def check_password(password):
    shortest, longest = PASSWORD_LENGTHS
    if not shortest <= len(password) <= longest:
        raise ValueError(f"A password needs {shortest:,} to {longest:,} characters")


def _derive(password, salt, n, r, p):
    # NFKC, so one typed character cannot hash differently on two keyboards. Surrogatepass,
    # because a lone surrogate would otherwise raise an error that quotes the password.
    encoded = unicodedata.normalize("NFKC", password).encode("utf-8", "surrogatepass")
    # hashlib's default maxmem of 32 MiB refuses these parameters, which need 128 MiB.
    return hashlib.scrypt(encoded, salt=salt, n=n, r=r, p=p, dklen=_DIGEST_BYTES, maxmem=2 * 128 * n * r)


def hash_password(password):
    check_password(password)
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = _derive(password, salt, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password, stored):
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        expected = bytes.fromhex(digest)
        derived = _derive(password, bytes.fromhex(salt), int(n), int(r), int(p))
    except (AttributeError, MemoryError, OverflowError, ValueError):
        return False
    return hmac.compare_digest(derived, expected)


def burn_password(password):
    """Spend what checking a real password costs, for an account that cannot sign in."""
    _derive(password, _BURN_SALT, **_SCRYPT)
