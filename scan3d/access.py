"""The gate in front of every route: who may call it, decided before its handler runs.

A body is parsed inside the route, and forms before any dependency, so only a gate here
can refuse before an upload spools. It decides by the route the router will pick, never
by a string prefix, and every route must have a rule: `check_policy` stops the app from
starting otherwise, and a route that somehow arrives without one is refused.
"""

from collections import OrderedDict
from dataclasses import dataclass
import threading

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers, MutableHeaders
from starlette.requests import HTTPConnection
from starlette.responses import JSONResponse
from starlette.routing import Match, Mount, Route
from starlette.staticfiles import StaticFiles

from . import identity
from .accounts import SESSION_LIFETIME
from .store import safe_id
from .uploads import upload_owner

COOKIE = "scan3d_session"
SECURE_COOKIE = "__Host-scan3d_session"
CROSS_ORIGIN = "Cross-origin mutations are not allowed"
_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")
# Polling reads a case repeatedly, and one row per session says the same thing.
_OPENED_LIMIT = 4096


@dataclass(frozen=True)
class Rule:
    kind: str
    audit: str | None = None


_REVISION = "/api/cases/{case_id}/revisions/{revision_id}"
_LICENSE = "/api/totalsegmentator/license"
# Every route without {case_id}, by method and template. A {case_id} route needs an entry for an audit action or another kind.
RULES = {
    ("GET", "/api/health"): Rule("public"),
    ("GET", "/api/session"): Rule("public"),
    ("POST", "/api/session"): Rule("public"),
    ("DELETE", "/api/session"): Rule("public"),
    ("POST", "/api/session/redeem"): Rule("public"),
    ("GET", "/"): Rule("public"),
    # FastAPI's own pages exist only with sign-in off. Any other kind is refused in password mode.
    ("GET", "/docs"): Rule("off_only"),
    ("GET", "/docs/oauth2-redirect"): Rule("off_only"),
    ("GET", "/redoc"): Rule("off_only"),
    ("GET", "/openapi.json"): Rule("off_only"),
    ("POST", "/api/session/sign-out-others"): Rule("signed_in"),
    ("GET", "/api/cases"): Rule("signed_in"),
    ("GET", "/api/agreement"): Rule("signed_in"),
    ("GET", "/api/trash"): Rule("signed_in"),
    ("GET", "/api/sizes"): Rule("signed_in"),
    ("POST", "/api/cases/demo"): Rule("signed_in", "case.import"),
    ("POST", "/api/cases/import"): Rule("signed_in", "case.import"),
    ("POST", "/api/imports"): Rule("signed_in", "case.import"),
    ("GET", "/api/segmentation-options"): Rule("signed_in"),
    ("GET", "/api/imaging-capabilities"): Rule("signed_in"),
    ("GET", "/api/interactive/status"): Rule("signed_in"),
    ("GET", "/api/orthanc/status"): Rule("archive"),
    ("GET", _LICENSE): Rule("signed_in"),
    ("POST", "/api/cases/import-local"): Rule("admin", "case.import"),
    ("GET", "/api/storage"): Rule("admin"),
    ("POST", "/api/storage/clear-caches"): Rule("admin"),
    ("PUT", _LICENSE): Rule("admin", "license.change"),
    ("DELETE", _LICENSE): Rule("admin", "license.change"),
    # Each People change writes its own audit row, with its target, inside its transaction.
    ("GET", "/api/people"): Rule("admin"),
    ("POST", "/api/people"): Rule("admin"),
    ("POST", "/api/people/{user_id}/link"): Rule("admin"),
    ("POST", "/api/people/{user_id}/role"): Rule("admin"),
    ("POST", "/api/people/{user_id}/archive"): Rule("admin"),
    ("POST", "/api/people/{user_id}/disable"): Rule("admin"),
    ("POST", "/api/people/{user_id}/enable"): Rule("admin"),
    ("POST", "/api/people/{user_id}/sign-out"): Rule("admin"),
    ("POST", "/api/orthanc/studies/query"): Rule("archive", "archive.query"),
    ("GET", "/api/orthanc/studies/{study_id}/series"): Rule("archive"),
    ("POST", "/api/orthanc/imports"): Rule("archive", "case.import"),
    ("POST", "/api/orthanc/launch"): Rule("archive", "archive.launch"),
    ("GET", "/api/archive/status"): Rule("archive"),
    ("POST", "/api/archive/studies/query"): Rule("archive", "archive.query"),
    ("POST", "/api/archive/studies/series"): Rule("archive"),
    ("POST", "/api/archive/imports"): Rule("archive", "case.import"),
    ("POST", "/api/archive/launch"): Rule("archive", "archive.launch"),
    ("GET", "/api/jobs/{job_id}"): Rule("job"),
    ("GET", "/api/jobs/{job_id}/measurements"): Rule("job"),
    ("POST", "/api/jobs/{job_id}/cancel"): Rule("job"),
    ("POST", "/api/imports/{upload_id}/parts"): Rule("upload"),
    ("POST", "/api/imports/{upload_id}/finish"): Rule("upload"),
    ("DELETE", "/api/imports/{upload_id}"): Rule("upload"),
    ("POST", "/api/trash/{case_id}/restore"): Rule("trashed"),
    ("POST", "/api/trash/{case_id}/purge"): Rule("trashed_admin"),
    ("GET", "/api/cases/{case_id}"): Rule("case", "case.open"),
    ("PATCH", "/api/cases/{case_id}"): Rule("case", "case.rename"),
    ("GET", "/api/cases/{case_id}/orthanc-series"): Rule("case_archive"),
    ("POST", "/api/cases/{case_id}/orthanc-series"): Rule("case_archive", "case.import"),
    ("GET", "/api/cases/{case_id}/archive-series"): Rule("case_archive"),
    ("POST", "/api/cases/{case_id}/archive-series"): Rule("case_archive", "case.import"),
    ("GET", f"{_REVISION}/slicer.zip"): Rule("case", "export.slicer"),
    ("GET", f"{_REVISION}/export.html"): Rule("case", "export.html"),
    ("POST", f"{_REVISION}/export.html"): Rule("case", "export.html"),
    ("POST", f"{_REVISION}/dicom-seg"): Rule("case", "export.seg"),
    ("GET", f"{_REVISION}/dicom-seg/{{export_id}}/segment.dcm"): Rule("case", "export.seg_file"),
    ("GET", f"{_REVISION}/dicom-view"): Rule("case_archive"),
    ("POST", f"{_REVISION}/dicom-view"): Rule("case_archive", "export.view"),
    ("GET", f"{_REVISION}/dicom-view-file"): Rule("case"),
    ("POST", f"{_REVISION}/dicom-view-file"): Rule("case", "export.view"),
    ("GET", f"{_REVISION}/dicom-view-file/{{export_id}}/view.dcm"): Rule("case", "export.view_file"),
}
# These exist only in some configurations: FastAPI's pages with sign-in off, the build notice with no viewer built,
# and People only with sign-in on.
_CONDITIONAL = frozenset({("GET", "/docs"), ("GET", "/docs/oauth2-redirect"), ("GET", "/redoc"), ("GET", "/openapi.json"), ("GET", "/")}) | {
    key for key in RULES if key[1].startswith("/api/people")
}
_CASE = Rule("case")
_STATIC = Rule("public")
# Each scoped kind reads one path parameter, and a route with that parameter must be of such a kind.
_SCOPED = {"case_id": ("case", "case_archive", "trashed", "trashed_admin"), "job_id": ("job",), "upload_id": ("upload",)}

_SIGN_IN = (401, "Sign in to continue")
_NO_RULE = (403, "This route has no access rule")
ARCHIVE_REFUSED = "This account cannot use the archive"
_NOT_ADMIN = (403, "Administrator access is required")
_NO_ARCHIVE = (403, ARCHIVE_REFUSED)
# Health, sign-in and viewer files touch no case and no cache, so nothing waits for them.
_UNCOUNTED_KINDS = ("public", "off_only")
_UNCOUNTED = object()


def rule_for(route, method):
    """The rule of a matched route, or None. HEAD uses the GET rule."""
    if isinstance(route, Mount):
        return _STATIC if route.path == "" and isinstance(route.app, StaticFiles) else None
    if not isinstance(route, Route):
        return None
    rule = RULES.get(("GET" if method == "HEAD" else method, route.path))
    return rule or (_CASE if "case_id" in route.param_convertors else None)


def check_policy(routes):
    """Refuse to start with a route that has no rule, or a rule that matches no route.

    Runs in both modes, so a new route without a rule fails every test that creates the app.
    """
    seen = set()
    for route in routes:
        if isinstance(route, Mount):
            if rule_for(route, "GET") is None:
                raise RuntimeError(f"No access rule covers {route!r}")
            continue
        if not isinstance(route, Route) or not route.methods:
            raise RuntimeError(f"Unknown route type: {route!r}")
        for method in route.methods:
            rule = rule_for(route, method)
            if rule is None:
                raise RuntimeError(f"No access rule for {method} {route.path}")
            if any((name in route.param_convertors) != (rule.kind in kinds) for name, kinds in _SCOPED.items()):
                raise RuntimeError(f"{method} {route.path} needs the rule for its path parameters")
            seen.add(("GET" if method == "HEAD" else method, route.path))
    stale = set(RULES) - seen - _CONDITIONAL
    if stale:
        raise RuntimeError(f"Access rules match no route: {sorted(stale)}")


def matched_route(scope):
    """The route the router will pick, with its path parameters, or (None, {}).

    The router takes the first full match, in order. A partial match is a method mismatch: it
    keeps looking, so the static mount can still take the request, and only with no full match
    anywhere does it fall back to the partial, for a 405. `matches` copies, so scope is untouched.
    """
    for route in scope["app"].router.routes:
        match, child_scope = route.matches(scope)
        if match == Match.FULL:
            return route, child_scope.get("path_params", {})
    return None, {}


def _well_formed(record_id):
    try:
        safe_id(record_id)
    except ValueError:
        return False
    return True


def session_token(cookies):
    """The (name, token) a request presents, or (None, None). The name is kept for any re-issue."""
    for name in (SECURE_COOKIE, COOKIE):
        if token := cookies.get(name):
            return name, token
    return None, None


def session_cookie(name, token, shared_device=False):
    # No Max-Age on a shared device, so the cookie goes when the browser closes.
    lifetime = () if shared_device else (f"Max-Age={int(SESSION_LIFETIME.total_seconds())}",)
    return "; ".join((f"{name}={token}", "HttpOnly", "SameSite=Strict", "Path=/", *lifetime, *(("Secure",) if name == SECURE_COOKIE else ())))


def cleared_cookie(name):
    return session_cookie(name, "", True) + "; Max-Age=0"


def _refusal_response(status, detail):
    return JSONResponse({"detail": detail}, status_code=status, headers={"Cache-Control": "no-store"})


class SessionGate:
    """A pure ASGI middleware, so a refusal costs no body read and no task of its own.

    Added first, it sits innermost of the user middleware, behind the host check and the
    same-origin check, and sees the scope the router will see.
    """

    def __init__(self, app, service):
        self.app = app
        self.accounts = service.accounts
        self.activity = service.activity
        self.uploads = service.settings.data_dir / "import-uploads"
        self.password = service.settings.sign_in_required
        self.opened = OrderedDict()
        self.opened_lock = threading.Lock()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        cookie, token = session_token(HTTPConnection(scope).cookies) if self.password else (None, None)
        if token and scope["method"] not in _SAFE_METHODS and "origin" not in Headers(scope=scope):
            await _refusal_response(403, CROSS_ORIGIN)(scope, receive, send)
            return
        route, params = matched_route(scope)
        if route is None:
            # Only the router's 404, 405 or slash redirect can follow, and none of them runs a handler.
            identity.activate(None if self.password else identity.LOCAL)
            await self.app(scope, receive, send)
            return
        rule = rule_for(route, scope["method"])
        principal, refusal, counted = await run_in_threadpool(self._admit, rule, params, token)
        identity.activate(principal)
        if refusal:
            await _refusal_response(*refusal)(scope, receive, send)
            return
        # No await comes between the hop and this try, so a cancelled request still gives its count back.
        try:
            with self.activity.request():
                no_store = principal is not None and principal.shared_device and rule.kind != "public"
                if not no_store and not (principal and principal.slid):
                    await self.app(scope, receive, send)
                    return

                async def send_with_session(message):
                    if message["type"] == "http.response.start":
                        headers = MutableHeaders(scope=message)
                        if no_store:
                            # Patient data must not stay on a computer the next person will use.
                            headers["Cache-Control"] = "no-store"
                        # Signing in or out sets its own cookie, which must not be overwritten here.
                        if principal.slid and "set-cookie" not in headers:
                            headers.append("Set-Cookie", session_cookie(cookie, token))
                    await send(message)

                await self.app(scope, receive, send_with_session)
        finally:
            if counted is not _UNCOUNTED:
                self.activity.leave(counted)

    def _admit(self, rule, params, token):
        """Session, rule, count and audit work together, on one thread hop. A refused request is never counted."""
        if self.password:
            principal = self.accounts.principal(token) if token else None
            refusal = self._refusal(rule, principal, params)
        else:
            principal, refusal = identity.LOCAL, None if rule else _NO_RULE
        if refusal or rule.kind in _UNCOUNTED_KINDS:
            return principal, refusal, _UNCOUNTED
        case_id = self._case_of(rule, params)
        # Restore and Delete forever count in the total only, or a purge would wait for itself.
        counted = None if rule.kind in ("trashed", "trashed_admin") else case_id
        # Before the Trash check: a request counted here is refused there or seen by any purge, which always follows Trash.
        self.activity.enter(counted)
        try:
            refusal = self._hidden(rule, case_id)
            # After the Trash check, so a case in Trash reads as absent here too.
            if refusal is None and rule.kind == "case_archive" and not principal.archive_access:
                refusal = _NO_ARCHIVE
            if refusal is None and rule.audit:
                self._audit(rule.audit, principal, params)
        except BaseException:
            self.activity.leave(counted)
            raise
        if refusal:
            self.activity.leave(counted)
            return principal, refusal, _UNCOUNTED
        return principal, None, counted

    def _case_of(self, rule, params):
        """The case a request acts on, or None. A job acts on its case, and so does a finished upload."""
        if rule.kind in ("case", "case_archive", "trashed", "trashed_admin"):
            return params["case_id"]
        if rule.kind == "job":
            return self.accounts.job_case(params["job_id"])
        if rule.kind == "upload":
            return upload_owner(self.uploads, params["upload_id"])[2]
        return None

    def _hidden(self, rule, case_id):
        """A case in Trash reads as missing in both modes, in the route's own words, so nothing shows it exists."""
        detail = {"case": "Case not found", "case_archive": "Case not found", "job": "Job not found", "upload": "Upload not found"}.get(rule.kind)
        return (404, detail) if detail and case_id and self.accounts.in_trash(case_id) else None

    def _refusal(self, rule, principal, params):
        """Why this principal may not use this rule, or None. Falling off the end refuses."""
        kind = rule.kind if rule else None
        if kind == "public":
            return None
        if principal is None:
            return _SIGN_IN
        if kind == "signed_in":
            return None
        if kind == "admin":
            return None if principal.is_admin else _NOT_ADMIN
        if kind == "archive":
            return None if principal.archive_access else _NO_ARCHIVE
        if kind == "trashed_admin" and not principal.is_admin:
            return _NOT_ADMIN
        if kind in ("case", "case_archive", "trashed", "trashed_admin"):
            return None if self._may_open(params["case_id"]) else (404, "Case not found")
        if kind == "job":
            job_id = params["job_id"]
            case_id = self.accounts.job_case(job_id) if _well_formed(job_id) else None
            return None if case_id and self._may_open(case_id) else (404, "Job not found")
        if kind == "upload":
            found, owner, case_id = upload_owner(self.uploads, params["upload_id"])
            # Finishing again returns the case record, so once there is a case, its access decides.
            if case_id:
                return None if self._may_open(case_id) else (404, "Upload not found")
            # Uploads from before sign-in, or from sign-in off, belong to no one, so admins may finish them.
            allowed = found and (owner == principal.id or (owner in (None, identity.LOCAL.id) and principal.is_admin))
            return None if allowed else (404, "Upload not found")
        return _NO_RULE

    def _may_open(self, case_id):
        """One shared library: anyone signed in opens any case that exists. Trash is `_hidden`'s, after this."""
        return _well_formed(case_id) and self.accounts.case_exists(case_id)

    def _audit(self, action, principal, params):
        """Record that a request was allowed to start. A failed write fails the request."""
        case_id = params.get("case_id")
        # With sign-in off nothing has checked this yet, and an audit row never holds raw input.
        if case_id is not None and not _well_formed(case_id):
            return
        key = (principal.token_hash or principal.id, case_id)
        if action == "case.open" and not self._first_open(key):
            return
        try:
            self.accounts.audit(action, principal.id, case_id)
        except BaseException:
            if action == "case.open":
                with self.opened_lock:
                    self.opened.pop(key, None)
            raise

    def _first_open(self, key):
        with self.opened_lock:
            if key in self.opened:
                return False
            self.opened[key] = None
            while len(self.opened) > _OPENED_LIMIT:
                self.opened.popitem(last=False)
        return True
