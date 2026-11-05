"""Accounts, sessions and the audit log.

All SQL on the sign-in tables lives here, except the case lists and the grant in
`store`, which are part of writing and reading a case.
"""

import asyncio
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import secrets
import sqlite3
import threading
import time

from starlette.concurrency import run_in_threadpool

from . import identity
from .errors import Conflict, Forbidden, NotFound
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
LINK_LIFETIME = timedelta(hours=24)
# A link is a People change finished later, so it works only while its maker is still an active admin.
# Losing those rights deletes the maker's links, so regaining them revives none; this join is a second guard.
_MAKER = "JOIN users a ON a.id=c.created_by AND a.role='admin' AND a.disabled_at IS NULL"
# Never a password hash or a code: a person's state and their link's expiry are all People needs.
_PEOPLE = (
    "SELECT u.id,u.username,u.display_name,u.role,u.archive_access,u.created_at,u.disabled_at,u.password_hash IS NULL AS invited,"
    f"s.seen_at AS last_seen_at,(SELECT MAX(c.expires_at) FROM one_time_codes c {_MAKER} WHERE c.user_id=u.id AND c.expires_at>?) AS link_expires_at "
    "FROM users u LEFT JOIN last_seen s ON s.user_id=u.id"
)


class LinkUsed(Exception):
    """One answer for a link that is unknown, expired, used, replaced, for a disabled account, or whose maker is no longer an admin."""


def _display_name(username, display_name):
    name = (username if display_name is None else display_name).strip()
    if not name or len(name) > _DISPLAY_NAME_LENGTH or any(ord(char) < 32 or ord(char) == 127 for char in name):
        raise ValueError(f"A display name needs 1 to {_DISPLAY_NAME_LENGTH} printable characters")
    return name


def _not_yourself(actor_id, user_id):
    if actor_id == user_id:
        raise ValueError("You cannot change your own role or disable your own account")


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
        self.hashing = asyncio.Semaphore(2)

    def add_user(self, username, password, display_name=None):
        """The command line adds administrators only; a member comes from People with a link, so nobody types their password."""
        identity.check_username(username)
        name = _display_name(username, display_name)
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
        """A new password ends every session and every link for or by this person, so it also recovers a lost or shared device."""
        encoded = identity.hash_password(password)
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
            if row is None:
                raise NotFound("User not found")
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, row["id"]))
            con.execute("DELETE FROM sessions WHERE user_id=?", (row["id"],))
            con.execute("DELETE FROM one_time_codes WHERE user_id=? OR created_by=?", (row["id"], row["id"]))
            self._write_audit(con, "user.password", target=row["id"])
        return row["id"]

    def require_user(self, username):
        with self.store.connection() as con:
            row = con.execute("SELECT id FROM users WHERE username=?", (username,)).fetchone()
        if row is None:
            raise NotFound("User not found")
        return row["id"]

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

    @staticmethod
    def _seen(con, user_id, moment):
        con.execute(
            "INSERT INTO last_seen(user_id,seen_at) VALUES (?,?) ON CONFLICT(user_id) DO UPDATE SET seen_at=excluded.seen_at",
            (user_id, _stamp(moment)),
        )

    @staticmethod
    def _require_admin(con, actor_id):
        """The gate decided at admission. This decides again under the write lock, so a request admitted
        just before a demotion cannot finish. The command line and sign-in off have no one to check."""
        if actor_id in (None, identity.LOCAL.id):
            return
        if con.execute("SELECT 1 FROM users WHERE id=? AND role='admin' AND disabled_at IS NULL", (actor_id,)).fetchone() is None:
            raise Forbidden("Administrator access is required")

    @staticmethod
    def _keep_an_admin(con, user_id):
        """Someone invited but not yet in cannot sign in, so they do not count."""
        others = con.execute(
            "SELECT COUNT(*) FROM users WHERE role='admin' AND disabled_at IS NULL AND password_hash IS NOT NULL AND id<>?", (user_id,)
        ).fetchone()[0]
        if not others:
            raise Conflict("Keep at least one administrator who can sign in")

    @staticmethod
    def _account(con, user_id):
        row = con.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if row is None:
            raise NotFound("Person not found")
        return row

    @staticmethod
    def _shown(row):
        state = "disabled" if row["disabled_at"] else "invited" if row["invited"] else "active"
        person = {key: row[key] for key in ("id", "username", "display_name", "role", "created_at", "last_seen_at", "link_expires_at")}
        return {**person, "archive_access": bool(row["archive_access"]), "state": state}

    @staticmethod
    def _person(con, user_id):
        return Accounts._shown(con.execute(_PEOPLE + " WHERE u.id=?", (_stamp(_utcnow()), user_id)).fetchone())

    @staticmethod
    def _new_code(con, actor_id, user_id, purpose, moment):
        """A fresh link that ends the person's earlier ones, so a link sent to the wrong place can be replaced."""
        code = secrets.token_urlsafe(32)
        con.execute("DELETE FROM one_time_codes WHERE user_id=? OR expires_at<=?", (user_id, _stamp(moment)))
        expires = _stamp(moment + LINK_LIFETIME)
        con.execute(
            "INSERT INTO one_time_codes(token_hash,purpose,user_id,created_by,expires_at) VALUES (?,?,?,?,?)",
            (token_hash(code), purpose, user_id, actor_id, expires),
        )
        return {"code": code, "purpose": purpose, "expires_at": expires}

    def people(self):
        with self.store.connection() as con:
            return [self._shown(row) for row in con.execute(_PEOPLE + " ORDER BY u.created_at,u.id", (_stamp(_utcnow()),))]

    def invite(self, actor_id, username, display_name=None, role="member"):
        """A person with no password yet, and a link to set one. Archive access starts on."""
        identity.check_username(username)
        name, user_id, moment = _display_name(username, display_name), identifier(), _utcnow()
        try:
            with self.store.connection() as con:
                con.execute("BEGIN IMMEDIATE")
                self._require_admin(con, actor_id)
                con.execute(
                    "INSERT INTO users(id,username,display_name,role,archive_access,created_at) VALUES (?,?,?,?,1,?)",
                    (user_id, username, name, role, _stamp(moment)),
                )
                link = self._new_code(con, actor_id, user_id, "invite", moment)
                self._write_audit(con, "user.invite", actor_id, target=user_id)
                return {"person": self._person(con, user_id), "link": link}
        except sqlite3.IntegrityError:
            raise ValueError("That username is already in use") from None

    def new_link(self, actor_id, user_id):
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_admin(con, actor_id)
            account = self._account(con, user_id)
            if account["disabled_at"]:
                raise Conflict("Enable this account before making a link")
            purpose = "invite" if account["password_hash"] is None else "reset"
            link = self._new_code(con, actor_id, user_id, purpose, _utcnow())
            self._write_audit(con, f"user.{purpose}_link", actor_id, target=user_id)
        return link

    def set_role(self, actor_id, user_id, role):
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_admin(con, actor_id)
            account = self._account(con, user_id)
            if account["role"] != role:
                _not_yourself(actor_id, user_id)
                if account["role"] == "admin":
                    self._keep_an_admin(con, user_id)
                    con.execute("DELETE FROM one_time_codes WHERE created_by=?", (user_id,))
                con.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
                self._write_audit(con, f"user.make_{role}", actor_id, target=user_id)
            return self._person(con, user_id)

    def set_archive(self, actor_id, user_id, allowed):
        """Work already queued or running finishes: like every rule here, this is decided when a request starts."""
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_admin(con, actor_id)
            if bool(self._account(con, user_id)["archive_access"]) != allowed:
                con.execute("UPDATE users SET archive_access=? WHERE id=?", (int(allowed), user_id))
                self._write_audit(con, "user.archive_on" if allowed else "user.archive_off", actor_id, target=user_id)
            return self._person(con, user_id)

    def disable(self, actor_id, user_id):
        """No sign-in, no session, no link for them and none they made. Enable brings back none of them."""
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_admin(con, actor_id)
            account = self._account(con, user_id)
            if account["disabled_at"] is None:
                _not_yourself(actor_id, user_id)
                if account["role"] == "admin":
                    self._keep_an_admin(con, user_id)
                con.execute("UPDATE users SET disabled_at=? WHERE id=?", (_stamp(_utcnow()), user_id))
                ended = con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount
                con.execute("DELETE FROM one_time_codes WHERE user_id=? OR created_by=?", (user_id, user_id))
                self._write_audit(con, "user.disable", actor_id, target=user_id, detail={"count": ended})
            return self._person(con, user_id)

    def enable(self, actor_id, user_id):
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_admin(con, actor_id)
            if self._account(con, user_id)["disabled_at"] is not None:
                con.execute("UPDATE users SET disabled_at=NULL WHERE id=?", (user_id,))
                self._write_audit(con, "user.enable", actor_id, target=user_id)
            return self._person(con, user_id)

    def sign_out_everywhere(self, actor_id, user_id):
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            self._require_admin(con, actor_id)
            self._account(con, user_id)
            ended = con.execute("DELETE FROM sessions WHERE user_id=?", (user_id,)).rowcount
            self._write_audit(con, "user.sign_out_everywhere", actor_id, target=user_id, detail={"count": ended})
        return {"signed_out": ended}

    @staticmethod
    def _live_link(con, hashed):
        return con.execute(
            f"SELECT u.* FROM one_time_codes c JOIN users u ON u.id=c.user_id {_MAKER} WHERE c.token_hash=? AND c.expires_at>? AND u.disabled_at IS NULL",
            (hashed, _stamp(_utcnow())),
        ).fetchone()

    def check_link(self, code):
        """Refuse a dead link before any hashing."""
        with self.store.connection() as con:
            if self._live_link(con, token_hash(code)) is None:
                raise LinkUsed

    async def queued_redeem(self, code, password, shared_device=False):
        # Keyed by the link, so no guesses at the person's name delay it.
        return await self._in_hashing_slot("link:" + token_hash(code), self.redeem, code, password, shared_device)

    def redeem(self, code, password, shared_device=False):
        """Set the password a link was made for, end the person's sessions and start one here. Return (token, principal)."""
        encoded = identity.hash_password(password)
        token, moment = secrets.token_urlsafe(32), _utcnow()
        hashed = token_hash(token)
        lifetime = SHARED_DEVICE_LIFETIME if shared_device else SESSION_LIFETIME
        with self.store.connection() as con:
            con.execute("BEGIN IMMEDIATE")
            # Under the write lock, so of two uses at once only the first finds the row.
            account = self._live_link(con, token_hash(code))
            if account is None:
                raise LinkUsed
            # As with set_password, so an exposed admin recovered by a reset link also ends what was made with that account.
            con.execute("DELETE FROM one_time_codes WHERE user_id=? OR created_by=?", (account["id"], account["id"]))
            con.execute("UPDATE users SET password_hash=? WHERE id=?", (encoded, account["id"]))
            con.execute("DELETE FROM sessions WHERE user_id=? OR expires_at<=?", (account["id"], _stamp(moment)))
            con.execute(
                "INSERT INTO sessions(token_hash,user_id,shared_device,created_at,last_seen_at,expires_at) VALUES (?,?,?,?,?,?)",
                (hashed, account["id"], int(shared_device), _stamp(moment), _stamp(moment), _stamp(moment + lifetime)),
            )
            self._seen(con, account["id"], moment)
            self._write_audit(con, "user.link_used", account["id"], target=account["id"])
            self._write_audit(con, "session.sign_in", actor_id=account["id"])
        self.throttle.clear(account["username"].casefold())
        return token, self._principal(account, hashed, shared_device)

    def _refuse_during_wait(self, key):
        if seconds := self.throttle.remaining(key):
            raise TooManyAttempts(seconds)

    async def _in_hashing_slot(self, key, work, *args):
        """Run `work` in a thread once a hashing slot is free, waiting up to a second on the event loop.

        The wait is on the event loop, so a flood holds no worker thread. It has to be a wait: if a
        busy slot refused at once, a refused attacker could retry at once and keep both slots busy.
        """
        self._refuse_during_wait(key)
        try:
            async with asyncio.timeout(1):
                await self.hashing.acquire()
        except TimeoutError:
            raise TooManyAttempts(1) from None
        try:
            return await run_in_threadpool(work, *args)
        finally:
            self.hashing.release()

    async def queued_sign_in(self, username, password, shared_device=False):
        return await self._in_hashing_slot(username.casefold(), self.sign_in, username, password, shared_device)

    async def queued_recheck(self, principal, password, case_id):
        return await self._in_hashing_slot(principal.username.casefold(), self.recheck, principal, password, case_id)

    def recheck(self, principal, password, case_id):
        """A signed-in person's own password, checked again before Delete forever.

        The wait is sign-in's, under the same name, so guesses here and at sign-in add up.
        """
        key = principal.username.casefold()
        self._refuse_during_wait(key)
        with self.store.connection() as con:
            row = con.execute("SELECT password_hash FROM users WHERE id=? AND disabled_at IS NULL", (principal.id,)).fetchone()
        if row is None or row["password_hash"] is None or not identity.verify_password(password, row["password_hash"]):
            self.throttle.failed(key)
            self.audit("case.purge_password_failed", principal.id, case_id)
            raise WrongPassword
        self.throttle.clear(key)

    def sign_in(self, username, password, shared_device=False):
        """Return (token, principal), or raise WrongPassword or TooManyAttempts."""
        key = username.casefold()
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
            self._seen(con, account["id"], moment)
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
                slid = self._slide(con, hashed, row["last_seen_at"], moment, row["id"])
        return self._principal(row, hashed, row["shared_device"], slid)

    @staticmethod
    def _slide(con, hashed, seen, moment, user_id):
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
            if changed:
                Accounts._seen(con, user_id, moment)
        except sqlite3.OperationalError:
            return False
        return bool(changed)

    def end_session(self, hashed):
        with self.store.connection() as con:
            return con.execute("DELETE FROM sessions WHERE token_hash=?", (hashed,)).rowcount

    def end_other_sessions(self, user_id, keep_token_hash):
        with self.store.connection() as con:
            return con.execute("DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (user_id, keep_token_hash)).rowcount

    def case_exists(self, case_id):
        """A case route needs a case, so a handler never makes a folder for an ID that names none."""
        with self.store.connection() as con:
            return con.execute("SELECT 1 FROM cases WHERE id=?", (case_id,)).fetchone() is not None

    def in_trash(self, case_id):
        with self.store.connection() as con:
            return con.execute("SELECT 1 FROM trash WHERE case_id=? AND kind='case'", (case_id,)).fetchone() is not None

    def job_case(self, job_id):
        with self.store.connection() as con:
            row = con.execute("SELECT case_id FROM jobs WHERE id=?", (job_id,)).fetchone()
        return row["case_id"] if row else None

    def others(self, user_ids):
        """Display names of the people behind these IDs, leaving out the one asking. With sign-in off it names nobody."""
        viewer = identity.actor_id()
        wanted = sorted({user_id for user_id in user_ids if user_id} - {viewer, identity.LOCAL.id})
        if viewer in (None, identity.LOCAL.id) or not wanted:
            return {}
        with self.store.connection() as con:
            rows = con.execute(f"SELECT id,display_name FROM users WHERE id IN ({','.join('?' * len(wanted))})", wanted).fetchall()
        return {row["id"]: row["display_name"] for row in rows}

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
