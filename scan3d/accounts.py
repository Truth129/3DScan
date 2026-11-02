"""Accounts, sessions and the audit log.

All SQL on the sign-in tables lives here, except the case lists and the grant in
`store`, which are part of writing and reading a case.
"""

from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time

from . import identity
from .errors import NotFound
from .identifiers import identifier

SESSION_LIFETIME = timedelta(days=30)
SHARED_DEVICE_LIFETIME = timedelta(hours=12)
# A write per request would cost more than the session is worth; this bounds how stale it gets.
SLIDE_INTERVAL = timedelta(minutes=10)
_FREE_FAILURES = 3
_WAITS = (1, 2, 4, 8, 16)
_LONGEST_WAIT = 30
_THROTTLE_KEYS = 4096
_DISPLAY_NAME_LENGTH = 128


class WrongPassword(Exception):
    """One answer for every way a sign-in fails, so it never tells who has an account."""


class TooManyAttempts(Exception):
    def __init__(self, seconds):
        super().__init__(seconds)
        self.seconds = seconds


def _utcnow():
    return datetime.now(timezone.utc)


def _stamp(moment):
    # Fixed width, because expired rows are found by comparing these as text.
    return moment.isoformat(timespec="microseconds")


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class _Throttle:
    """Waits after repeated failures, per case-folded username, held in memory.

    A wait refuses without hashing and ends on its own, so it never locks an
    account. Unknown names arrive here too, which is why the table is bounded.
    """

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.entries = OrderedDict()
        self.lock = threading.Lock()

    def remaining(self, key):
        with self.lock:
            entry = self.entries.get(key)
            return max(0, math.ceil(entry["until"] - self.clock())) if entry else 0

    def failed(self, key):
        with self.lock:
            count = self.entries.pop(key, {"count": 0})["count"] + 1
            beyond = count - _FREE_FAILURES
            wait = 0 if beyond <= 0 else _WAITS[beyond - 1] if beyond <= len(_WAITS) else _LONGEST_WAIT
            self.entries[key] = {"count": count, "until": self.clock() + wait}
            while len(self.entries) > _THROTTLE_KEYS:
                self.entries.popitem(last=False)

    def clear(self, key):
        with self.lock:
            self.entries.pop(key, None)


class Accounts:
    def __init__(self, store):
        self.store = store
        self.throttle = _Throttle()
        # Each hash takes 128 MiB and a core, so a burst of attempts must not hold many.
        self.hashing = threading.BoundedSemaphore(2)

    def add_user(self, username, password, display_name=None):
        """Every account is an administrator with the archive until members exist."""
        if not identity.valid_username(username):
            raise ValueError("A username needs 1 to 64 letters, digits or . _ @ -")
        name = (username if display_name is None else display_name).strip()
        if not name or len(name) > _DISPLAY_NAME_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise ValueError(f"A display name needs 1 to {_DISPLAY_NAME_LENGTH} printable characters")
        encoded = identity.hash_password(password)
        user_id = identifier()
        try:
            with self.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                con.execute(
                    "INSERT INTO users(id,username,display_name,password_hash,role,archive_access,created_at) VALUES (?,?,?,?,'admin',1,?)",
                    (user_id, username, name, encoded, _stamp(_utcnow())),
                )
                self._write_audit(con, "user.add", target=user_id)
        except sqlite3.IntegrityError:
            raise ValueError("That username is already in use") from None
        return user_id

    def set_password(self, username, password):
        """A new password ends every session, so it also recovers a lost or shared device."""
        encoded = identity.hash_password(password)
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if row is None:
                raise NotFound("User not found")
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, row["id"]))
            con.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
            self._write_audit(con, "user.password", target=row["id"])
        return row["id"]

    def users(self):
        with self.store.connection() as con:
            rows = con.execute("SELECT id,username,display_name,role,archive_access,created_at,disabled_at FROM users ORDER BY created_at,id").fetchall()
        return [dict(row) for row in rows]

    def has_users(self):
        with self.store.connection() as con:
            return con.execute("SELECT 1 FROM users LIMIT 1").fetchone() is not None

    @staticmethod
    def oldest_admin(con):
        row = con.execute("SELECT id FROM users WHERE role='admin' AND disabled_at IS NULL ORDER BY created_at,id LIMIT 1").fetchone()
        return row[0] if row else None

    def adopt_orphans(self):
        """Give every case with no access row to the oldest active admin, and return how many."""
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            admin = self.oldest_admin(con)
            if admin is None:
                return 0
            adopted = con.execute(
                "INSERT OR IGNORE INTO case_access(case_id,user_id,role,granted_at) SELECT c.id,?,'owner',? FROM cases c "
                "WHERE NOT EXISTS(SELECT 1 FROM case_access a WHERE a.case_id=c.id)",
                (admin, _stamp(_utcnow())),
            ).rowcount
            if adopted:
                self._write_audit(con, "case.adopt", detail={"count": adopted})
        return adopted

    def _refuse_during_wait(self, key):
        if seconds := self.throttle.remaining(key):
            raise TooManyAttempts(seconds)

    def sign_in(self, username, password, shared_device=False):
        """Return (token, principal), or raise WrongPassword or TooManyAttempts."""
        key = username.casefold()
        self._refuse_during_wait(key)
        if not self.hashing.acquire(timeout=1):
            raise TooManyAttempts(1)
        try:
            # Attempts queue for a slot, and one that failed while this one queued sets a wait.
            self._refuse_during_wait(key)
            account = None
            if identity.valid_username(username):
                with self.store.connection() as con:
                    account = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
            if account is None or account["disabled_at"] is not None or account["password_hash"] is None:
                identity.burn_password(password)
                accepted = False
            else:
                accepted = identity.verify_password(password, account["password_hash"])
        finally:
            self.hashing.release()
        if not accepted:
            self.throttle.failed(key)
            self.audit("session.sign_in_failed", target=account["id"] if account else None)
            raise WrongPassword
        self.throttle.clear(key)
        token, moment = secrets.token_urlsafe(32), _utcnow()
        hashed = token_hash(token)
        lifetime = SHARED_DEVICE_LIFETIME if shared_device else SESSION_LIFETIME
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            con.execute("DELETE FROM sessions WHERE expires_at<=?", (_stamp(moment),))
            con.execute(
                "INSERT INTO sessions(token_hash,user_id,shared_device,created_at,last_seen_at,expires_at) VALUES (?,?,?,?,?,?)",
                (hashed, account["id"], int(shared_device), _stamp(moment), _stamp(moment), _stamp(moment + lifetime)),
            )
            self._write_audit(con, "session.sign_in", actor_id=account["id"])
        return token, self._principal(account, hashed, shared_device)

    @staticmethod
    def _principal(user, hashed, shared_device, slid=False):
        return identity.Principal(
            user["id"], user["username"], user["display_name"], user["role"], bool(user["archive_access"]), hashed, bool(shared_device), slid
        )

    def principal(self, token):
        """The person a session token belongs to, or None. A session past its slide interval moves on."""
        hashed, moment = token_hash(token), _utcnow()
        with self.store.connection() as con:
            row = con.execute(
                "SELECT s.shared_device,s.last_seen_at,s.expires_at,u.id,u.username,u.display_name,u.role,u.archive_access,u.disabled_at "
                "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                (hashed,),
            ).fetchone()
            if row is None or row["disabled_at"] is not None or datetime.fromisoformat(row["expires_at"]) <= moment:
                return None
            slid = False
            # A shared device has a hard cap, so it never moves.
            if not row["shared_device"] and moment - datetime.fromisoformat(row["last_seen_at"]) >= SLIDE_INTERVAL:
                slid = self._slide(con, hashed, row["last_seen_at"], moment)
        return self._principal(row, hashed, row["shared_device"], slid)

    @staticmethod
    def _slide(con, hashed, seen, moment):
        """Move a session on, if nothing else did and the write lock is free.

        A plain read must never wait on the lock to keep a session fresh. Losing costs nothing,
        because a later request slides it instead, and only the first of a burst commits.
        """
        con.execute("PRAGMA busy_timeout=0").fetchall()
        try:
            changed = con.execute(
                "UPDATE sessions SET last_seen_at=?,expires_at=? WHERE token_hash=? AND last_seen_at=?",
                (_stamp(moment), _stamp(moment + SESSION_LIFETIME), hashed, seen),
            ).rowcount
        except sqlite3.OperationalError:
            return False
        return bool(changed)

    def end_session(self, hashed):
        with self.store.connection() as con:
            return con.execute("DELETE FROM sessions WHERE token_hash=?", (hashed,)).rowcount

    def end_other_sessions(self, user_id, keep_token_hash):
        with self.store.connection() as con:
            return con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, keep_token_hash)).rowcount

    def can_open(self, case_id, user_id):
        with self.store.connection() as con:
            return con.execute("SELECT 1 FROM case_access WHERE case_id=? AND user_id=?", (case_id, user_id)).fetchone() is not None

    def job_case(self, job_id):
        with self.store.connection() as con:
            row = con.execute("SELECT case_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["case_id"] if row else None

    def audit(self, action, actor_id=None, case_id=None, target=None, detail=None):
        """Record an action. IDs and counts only, never a label, search term or typed username.

        A failed write raises, so nothing that should leave a trace can proceed without one.
        """
        with self.store.connection() as con:
            self._write_audit(con, action, actor_id, case_id, target, detail)

    @staticmethod
    def _write_audit(con, action, actor_id=None, case_id=None, target=None, detail=None):
        con.execute(
            "INSERT INTO audit_events(at,actor_id,action,case_id,target,detail) VALUES (?,?,?,?,?,?)",
            (_stamp(_utcnow()), actor_id, action, case_id, target, json.dumps(detail or {})),
        )
