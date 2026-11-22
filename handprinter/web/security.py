"""Loopback/LAN guard, Host and Origin checks, and the per-start request token."""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from handprinter.config import LOOPBACK_HOSTS, Settings

TOKEN_HEADER = "x-handprinter-token"
SESSION_COOKIE = "hp_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def session_value(access_token: str) -> str:
    return hmac.new(access_token.encode(), b"handprinter-session", hashlib.sha256).hexdigest()


def allowed_hosts(settings: Settings, extra: set[str]) -> set[str]:
    names = set(LOOPBACK_HOSTS) | extra
    if settings.lan_mode:
        names.add(settings.host)
    hosts = set()
    for name in names:
        bare = f"[{name}]" if ":" in name and not name.startswith("[") else name
        hosts |= {bare, f"{bare}:{settings.port}"}
    return hosts


def make_guard(settings: Settings, request_token: str, extra_hosts: set[str]) -> Callable[[Request, Callable[[Request], Awaitable[Response]]], Awaitable[Response]]:
    hosts = allowed_hosts(settings, extra_hosts)
    # Bound to all interfaces: the LAN address is not known in advance, so any Host is accepted
    # and protection comes from the access-token session cookie required for non-loopback clients.
    any_host = settings.lan_mode and settings.host in ("0.0.0.0", "::")

    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        host = request.headers.get("host", "")
        client = request.client.host if request.client else ""
        remote = client not in LOOPBACK_HOSTS
        # Loopback clients skip the sign-in, so their Host must always be a known name
        # (blocks DNS rebinding); remote clients in wildcard LAN mode must sign in instead.
        if host not in hosts and not (any_host and remote):
            return JSONResponse({"detail": "host not allowed"}, status_code=400)
        origin = request.headers.get("origin")
        if origin and urlsplit(origin).netloc != host:
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        public = request.url.path in ("/login", "/api/login") or request.url.path.startswith("/static/")
        if settings.lan_mode and remote and not public:
            cookie = request.cookies.get(SESSION_COOKIE, "")
            if not hmac.compare_digest(cookie, session_value(settings.access_token)):
                if request.url.path.startswith("/api/"):
                    return JSONResponse({"detail": "sign in required"}, status_code=401)
                return Response(status_code=303, headers={"location": "/login"})
        if request.method not in SAFE_METHODS and request.url.path != "/api/login":
            if not hmac.compare_digest(request.headers.get(TOKEN_HEADER, ""), request_token):
                return JSONResponse({"detail": "missing or invalid request token (reload the page)"}, status_code=403)
        response = await call_next(request)
        response.headers.setdefault("x-content-type-options", "nosniff")
        response.headers.setdefault("referrer-policy", "no-referrer")
        response.headers.setdefault("x-frame-options", "DENY")
        response.headers.setdefault(
            "content-security-policy",
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        return response

    return guard
