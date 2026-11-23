"""Loopback/LAN guard, Host and Origin checks, and the per-start request token."""

from __future__ import annotations

import hashlib
import hmac
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from handprinter.config import LOOPBACK_HOSTS, Settings

TOKEN_HEADER = "x-handprinter-token"
SESSION_COOKIE = "hp_session"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def session_value(secret: str) -> str:
    # Derived from the password, so changing the password signs every device out.
    return hmac.new(secret.encode(), b"handprinter-session", hashlib.sha256).hexdigest()


SESSION_MAX_AGE = 30 * 24 * 3600  # stay signed in on the iPad for 30 days


class LoginLimiter:
    """Brute-force brake for sign-in, sized for a short (even 4-digit) password.

    - Per visitor: after 5 wrong tries within 15 minutes, that visitor waits out the 15 minutes.
    - Overall: after 20 wrong tries within 24 hours from anyone, sign-in is closed until the
      oldest of them is a day old. Already signed-in devices keep working.
    With a 4-digit password that is at most 20 guesses a day: guessing it would take months.
    """

    PER_CLIENT, CLIENT_WINDOW = 5, 15 * 60
    GLOBAL, GLOBAL_WINDOW = 20, 24 * 3600

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.by_client: dict[str, deque[float]] = {}
        self.all: deque[float] = deque()

    def _trim(self, q: deque[float], window: float, now: float) -> None:
        while q and q[0] <= now - window:
            q.popleft()

    def retry_after(self, client: str) -> int:
        """Seconds until this client may try again (0 = allowed now)."""
        with self.lock:
            now = self.clock()
            self._trim(self.all, self.GLOBAL_WINDOW, now)
            q = self.by_client.get(client, deque())
            self._trim(q, self.CLIENT_WINDOW, now)
            waits = [0.0]
            if len(q) >= self.PER_CLIENT:
                waits.append(q[0] + self.CLIENT_WINDOW - now)
            if len(self.all) >= self.GLOBAL:
                waits.append(self.all[0] + self.GLOBAL_WINDOW - now)
            return int(max(waits) + 0.999)

    def failed(self, client: str) -> None:
        with self.lock:
            now = self.clock()
            self.by_client.setdefault(client, deque()).append(now)
            self.all.append(now)


def client_key(request: Request, relay_secret: str = "") -> str:
    """Visitor identity for rate limiting. Behind Cloudflare the real address is in
    CF-Connecting-IP (Cloudflare overwrites it, so it cannot be forged through the tunnel).
    Through the workers.dev relay that header holds the relay's address, so the relay passes the
    visitor's in X-Relay-Client-IP, trusted only together with the relay's secret."""
    if relay_secret and hmac.compare_digest(request.headers.get("x-relay-key", ""), relay_secret):
        relayed = request.headers.get("x-relay-client-ip", "")
        if relayed:
            return relayed
    return request.headers.get("cf-connecting-ip") or (request.client.host if request.client else "?")


def is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


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
    expected = session_value(settings.secret) if settings.login_required else ""

    async def guard(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        host = request.headers.get("host", "")
        client = request.client.host if request.client else ""
        remote = client not in LOOPBACK_HOSTS
        # Online mode: the Cloudflare tunnel connects from loopback, so nobody is trusted by
        # address; every request needs the session cookie and the public hostname is accepted.
        must_sign_in = settings.online or (settings.lan_mode and remote)
        # Loopback clients skip the sign-in in local/LAN mode, so their Host must always be a
        # known name (blocks DNS rebinding); remote clients in wildcard LAN mode must sign in instead.
        if host not in hosts and not (any_host and remote) and not settings.online:
            return JSONResponse({"detail": "host not allowed"}, status_code=400)
        origin = request.headers.get("origin")
        # Behind the tunnel the public name may arrive as X-Forwarded-Host. (CSRF is stopped by the
        # per-start request token in any case; this check is defence in depth.)
        forwarded_host = request.headers.get("x-forwarded-host", "") if settings.online else ""
        if origin and urlsplit(origin).netloc not in (host, forwarded_host):
            return JSONResponse({"detail": "cross-origin request refused"}, status_code=403)
        public = request.url.path in ("/login", "/api/login", "/manifest.webmanifest") or request.url.path.startswith("/static/")
        if must_sign_in and not public:
            cookie = request.cookies.get(SESSION_COOKIE, "")
            if not hmac.compare_digest(cookie, expected):
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
        if settings.online and not request.url.path.startswith("/static/"):
            # Personal data: never stored by Cloudflare or other shared caches.
            response.headers["cache-control"] = response.headers.get("cache-control", "no-store").replace("public", "private")
            response.headers.setdefault("cdn-cache-control", "no-store")
        response.headers.setdefault(
            "content-security-policy",
            "default-src 'self'; img-src 'self' blob: data:; style-src 'self'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'",
        )
        return response

    return guard
