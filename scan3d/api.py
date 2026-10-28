"""HTTP application: transport middleware, error mapping and the resource routes."""

from contextlib import asynccontextmanager
import re
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__, api_cases, api_imports, api_reviews, api_system, api_viewer
from .config import Settings
from .errors import NotFound
from .orthanc import OrthancChanged, OrthancError
from .service import Service
from .store import ReviewConflict
from .uploads import ImportConflict


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

    Pre-gzipped CT chunks and review frames declare Content-Encoding and pass
    through unchanged. Slicer ZIPs are already compressed, and HTML exports keep
    Content-Length so browsers can show download progress.
    Level 5 is within 0.5% of level 9's size on the viewer bundles at half the CPU.
    """

    _skip = re.compile(r"/(?:slicer\.zip|export\.html)$")

    def __init__(self, app):
        self.app = app
        self.gzip = GZipMiddleware(app, minimum_size=1024, compresslevel=5)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and not self._skip.search(scope["path"]):
            await self.gzip(scope, receive, send)
        else:
            await self.app(scope, receive, send)


def _error_response(status, headers=None):
    async def handler(request, exc):
        return JSONResponse(status_code=status, content={"detail": str(exc)}, headers=headers)

    return handler


def create_app(settings: Settings | None = None):
    service = Service(settings)

    @asynccontextmanager
    async def lifespan(app):
        service.start()
        yield
        service.stop()

    app = FastAPI(title="3DScan", version=__version__, lifespan=lifespan)
    app.state.service = service
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
        return await request_validation_exception_handler(request, exc)

    # Only explicit NotFound means 404; any other KeyError is a defect (500).
    no_store = {"Cache-Control": "no-store"}
    for error, status, headers in (
        (ValueError, 400, None),
        (NotFound, 404, None),
        (ReviewConflict, 409, None),
        (ImportConflict, 409, None),
        (OrthancChanged, 409, no_store),
        (OrthancError, 502, no_store),
    ):
        app.add_exception_handler(error, _error_response(status, headers))

    @app.middleware("http")
    async def same_origin(request: Request, call_next):
        # Local filesystem endpoints must not be callable by an unrelated browser origin.
        private = request.url.path == "/api/totalsegmentator/license" or request.url.path.startswith("/api/orthanc/")
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD", "OPTIONS") and origin:
            if urlsplit(origin).netloc != request.headers.get("host"):
                headers = {"Cache-Control": "no-store"} if private else None
                return JSONResponse(status_code=403, content={"detail": "Cross-origin mutations are not allowed"}, headers=headers)
        response = await call_next(request)
        if private:
            # Archive queries and validation failures can contain study identity.
            response.headers["Cache-Control"] = "no-store"
        return response

    for module in (api_system, api_imports, api_cases, api_reviews, api_viewer):
        module.add_routes(app, service)

    if service.settings.web_dir.is_dir():
        app.mount("/", ViewerStaticFiles(directory=service.settings.web_dir, html=True), name="web")
    else:

        @app.get("/", response_class=HTMLResponse)
        def build_notice():
            return '<h1>3DScan API is running</h1><p>Build the interface with <code>cd web &amp;&amp; npm install &amp;&amp; npm run build</code>.</p><a href="/docs">API documentation</a>'

    return app
