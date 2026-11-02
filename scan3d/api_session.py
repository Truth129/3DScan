"""Signing in and out. Only sign_in is async, to queue on the event loop; scrypt stays off it."""

import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from . import identity
from .access import COOKIE, SECURE_COOKIE, cleared_cookie, session_cookie
from .accounts import TooManyAttempts, WrongPassword
from .api_models import SignIn
from .config import LOOPBACK_HOSTS


def _host_name(host):
    return re.match(r"\[[^\]]*\]|[^:]*", host).group()


def add_routes(app, service):
    accounts = service.accounts
    password_mode = service.settings.sign_in_required

    def view(principal):
        user = None if principal is None else {key: getattr(principal, key) for key in ("id", "username", "display_name", "role")}
        return {"mode": "password" if password_mode else "off", "user": user, "setup_required": password_mode and not accounts.has_users()}

    @app.get("/api/session")
    def session():
        return view(identity.current())

    @app.post("/api/session")
    async def sign_in(body: SignIn, request: Request):
        if not password_mode:
            raise ValueError("Sign-in is off on this workstation")
        https = urlsplit(request.headers.get("origin", "")).scheme == "https"
        # A password must not cross a network in clear text.
        if not https and _host_name(request.headers.get("host", "")) not in LOOPBACK_HOSTS:
            raise HTTPException(403, "Sign in through the HTTPS address")
        try:
            token, principal = await accounts.queued_sign_in(body.username, body.password.get_secret_value(), body.shared_device)
        except TooManyAttempts as exc:
            detail = f"Too many attempts. Try again in {exc.seconds} seconds."
            raise HTTPException(429, detail, headers={"Retry-After": str(exc.seconds)}) from None
        except WrongPassword:
            raise HTTPException(401, "Wrong username or password") from None
        cookie = session_cookie(SECURE_COOKIE if https else COOKIE, token, body.shared_device)
        return JSONResponse(await run_in_threadpool(view, principal), headers={"Set-Cookie": cookie})

    @app.delete("/api/session")
    def sign_out(request: Request):
        principal = identity.current()
        if principal is not None and principal.token_hash:
            accounts.end_session(principal.token_hash)
            accounts.audit("session.sign_out", principal.id)
        response = JSONResponse(view(identity.LOCAL if not password_mode else None), headers={"Clear-Site-Data": '"cache"'})
        for name in (COOKIE, SECURE_COOKIE):
            if name in request.cookies:
                response.headers.append("Set-Cookie", cleared_cookie(name))
        return response

    @app.post("/api/session/sign-out-others")
    def sign_out_others():
        principal = identity.current()
        signed_out = accounts.end_other_sessions(principal.id, principal.token_hash)
        accounts.audit("session.sign_out_others", principal.id, detail={"count": signed_out})
        return {"signed_out": signed_out}
