"""HTTP application: transport middleware, error mapping and the resource routes."""

from contextlib import asynccontextmanager
import re
import sqlite3
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, api_cases, api_imports, api_people, api_reviews, api_session, api_system, api_viewer
from .access import SessionGate, check_policy
from .config import Settings
from .errors import Conflict, Forbidden, NotFound
from .orthanc import OrthancChanged, OrthancError
from .service import Service
from .store import ReviewConflict
from .uploads import ImportConflict


_MULTIPART_FRAMING = 1024**2
_SIGN_IN_LIMIT = 4 * 1024


class ViewerStaticFiles(StaticFiles):
    """Revalidate viewer entry points while retaining immutable build assets."""

    _hashed_asset = re.compile(r"^assets/(?:[^/]+/)*[^/]+-[A-Za-z0-9_-]{8}\.(?:js|css)$")

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        if response.status_code in (200, 206, 304):
            resource = path.replace("\\", "/").strip("/")
            if resource in ("", ".", "index.html", "offline-viewer.js", "offline-viewer.css"):
                # Include conditional 304 responses: clients must revalidate
                # mutable entry points after every deployment.
                response.headers["Cache-Control"] = "no-cache"
            elif self._hashed_asset.fullmatch(resource):
                response.headers["Cache-Control"] = "private, max-age=31536000, immutable"
        return response


class SelectiveGZip:
    """Compress JSON and viewer assets for remote clients; leave downloads alone.

    GLB routes negotiate cached gzip files themselves. Pre-gzipped CT chunks and
    review frames declare Content-Encoding and pass through unchanged.
    Slicer ZIPs are already compressed, and HTML and SEG exports keep
    Content-Length so browsers can show download progress.
    Level 5 is within 0.5% of level 9's size on the viewer bundles at half the CPU.
    """

    _skip = re.compile(r"/(?:slicer\.zip|export\.html|segment\.dcm|view\.dcm)$")
    _glb = re.compile(r"^/api/cases/[^/]+/revisions/[^/]+/(?:model\.glb|surfaces/[^/]+\.glb)$")

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not (self._skip.search(scope["path"]) or self._glb.fullmatch(scope["path"])):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def _error_response(status, headers=None):
    async def handler(request, exc):
        return JSONResponse(status_code=status, content={"detail": str(exc)}, headers=headers)

    return handler


def _length_refusal(request, limit, undeclared, oversized):
    """Refuse a body too large to accept before anything reads it.

    A declared length is the only thing that can be checked first, so a chunked
    body is refused rather than counted. h11 also lets chunked framing override
    a Content-Length, so the presence of one proves nothing on its own.
    """
    length = request.headers.get("content-length")
    if "transfer-encoding" in request.headers or length is None or not (length.isascii() and length.isdigit()):
        return JSONResponse(status_code=411, content={"detail": undeclared}, headers={"Cache-Control": "no-store"})
    # Digit strings past Python's int conversion limit would raise here.
    if len(length) > 18 or int(length) > limit:
        return JSONResponse(status_code=413, content={"detail": oversized}, headers={"Cache-Control": "no-store"})
    return None


def _view_upload_refusal(request, limit):
    # Multipart parsing would otherwise spool the whole capture.
    return _length_refusal(
        request, limit + _MULTIPART_FRAMING, "A captured view must declare its Content-Length.", "The captured view exceeds the upload limit."
    )


def _sign_in_refusal(request):
    # This route is public, so it must not let anyone make the server hold a large body.
    return _length_refusal(request, _SIGN_IN_LIMIT, "A sign-in request must declare its Content-Length.", "The sign-in request is too large.")


def _is_session_path(request):
    # A route's pattern ends in $, which also matches before a final newline, so an encoded one
    # reaches the same handler. request.url.path is no help: a crafted Host header can move it.
    return request.scope["path"].removesuffix("\n") == "/api/session"


def _is_redeem_path(request):
    # As for sign-in: scope["path"], and a final newline reaches the same handler.
    return request.scope["path"].removesuffix("\n") == "/api/session/redeem"


_PURGE = re.compile(r"/api/trash/[^/]+/purge")


def _is_purge_path(request):
    # As for sign-in: scope["path"], and a final newline reaches the same handler.
    return _PURGE.fullmatch(request.scope["path"].removesuffix("\n")) is not None


def create_app(settings: Settings | None = None):
    service = Service(settings)
    # Checked here rather than with the other settings, so a backup never stops for a missing token.
    if service.settings.dicomweb and (missing := service.settings.dicomweb.missing_files()):
        raise ValueError(f"{' and '.join(missing)} cannot be read")

    @asynccontextmanager
    async def lifespan(app):
        if service.settings.sign_in_required:
            service.accounts.adopt_orphans()
        service.start(janitor=True)
        yield
        service.stop()
        service.store.close()

    pages = dict(docs_url=None, redoc_url=None, openapi_url=None) if service.settings.sign_in_required else {}
    app = FastAPI(title="3DScan", version=__version__, lifespan=lifespan, **pages)
    app.state.service = service
    # First, so it sits innermost: the host and origin checks run before it reads a session.
    app.add_middleware(SessionGate, service=service)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(service.settings.allowed_hosts))
    app.add_middleware(SelectiveGZip)

    @app.exception_handler(RequestValidationError)
    async def safe_validation_error(request, exc):
        # Pydantic's ordinary error details include the submitted input, even for
        # malformed JSON, incorrect types and forbidden extra fields.
        if request.url.path == "/api/totalsegmentator/license":
            return JSONResponse(
                status_code=422, content={"detail": "Invalid license request. Enter a license key only."}, headers={"Cache-Control": "no-store"}
            )
        if _is_session_path(request):
            return JSONResponse(status_code=422, content={"detail": "Invalid sign-in request."}, headers={"Cache-Control": "no-store"})
        if _is_redeem_path(request):
            # The body holds a link's code and a password.
            return JSONResponse(status_code=422, content={"detail": "Invalid set-password request."}, headers={"Cache-Control": "no-store"})
        if _is_purge_path(request):
            return JSONResponse(status_code=422, content={"detail": "Invalid delete request."}, headers={"Cache-Control": "no-store"})
        return await request_validation_exception_handler(request, exc)

    # Only explicit NotFound means 404; any other KeyError is a defect (500).
    no_store = {"Cache-Control": "no-store"}
    for error, status, headers in (
        (ValueError, 400, None),
        (Forbidden, 403, None),
        (NotFound, 404, None),
        (ReviewConflict, 409, None),
        (ImportConflict, 409, None),
        (Conflict, 409, None),
        (OrthancChanged, 409, no_store),
        (OrthancError, 502, no_store),
    ):
        app.add_exception_handler(error, _error_response(status, headers))

    @app.exception_handler(sqlite3.IntegrityError)
    async def purged_case(request, exc):
        # Only the stay-purged triggers raise this message. Any other integrity error is a defect, a logged 500.
        if str(exc) != "purged":
            raise exc
        return JSONResponse(status_code=404, content={"detail": "Case not found"})

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        # Local filesystem endpoints must not be callable by an unrelated browser origin.
        # scope["path"] is what routing matches. request.url.path would stop at
        # an encoded "?" in an earlier segment, and miss the upload check below.
        # A final newline reaches the same handler, because a route's $ also matches before one.
        view_export = request.scope["path"].removesuffix("\n").endswith(("/dicom-view", "/dicom-view-file"))
        session = _is_session_path(request)
        redeem = _is_redeem_path(request)
        private = (
            session
            or redeem
            or request.url.path == "/api/totalsegmentator/license"
            or request.url.path.startswith("/api/orthanc/")
            or request.url.path.endswith(("/dicom-seg", "/segment.dcm", "/view.dcm"))
            or view_export
            or _is_purge_path(request)
            # A People answer can carry a link's code.
            or request.scope["path"].startswith("/api/people")
        )
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if urlsplit(origin).netloc != request.headers.get("host"):
                headers = {"Cache-Control": "no-store"} if private else None
                return JSONResponse(status_code=403, content={"detail": "Cross-origin mutations are not allowed"}, headers=headers)
        if view_export and request.method == "POST":
            refused = _view_upload_refusal(request, service.settings.view_upload_limit)
            if refused:
                return refused
        # Redeem is public too.
        if (session or redeem) and request.method == "POST":
            refused = _sign_in_refusal(request)
            if refused:
                return refused
        response = await call_next(request)
        if private:
            # Archive queries and validation failures can contain study identity.
            response.headers["Cache-Control"] = "no-store"
        elif request.scope["path"].startswith("/api/"):
            # A browser keeps whatever is not marked, and a kept case list outlives the session that read it.
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    for module in (api_system, api_session, api_people, api_imports, api_cases, api_reviews, api_viewer):
        module.add_routes(app, service)

    if service.settings.web_dir.is_dir():
        app.mount("/", ViewerStaticFiles(directory=service.settings.web_dir, html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        def build_notice():
            return '<h1>3DScan API is running</h1><p>Build the interface with <code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code>.</p><a href="/docs">API documentation</a>'

    check_policy(app.routes)
    return app
