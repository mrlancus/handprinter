"""Online mode (Cloudflare Tunnel): the tunnel arrives from loopback, so nobody is trusted by address.

Spec: every page and API call needs the password session; the default password is 6892; a short
password is protected by per-visitor and overall attempt limits; personal data is never cached by
the CDN; state changes still need the per-start request token.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from handprinter.config import load_settings
from handprinter.web.app import create_app
from handprinter.web.security import LoginLimiter

PUBLIC = {"host": "quiet-words-here.trycloudflare.com", "x-forwarded-proto": "https"}


def online(tmp_path, **env):
    s = load_settings({"HANDPRINTER_DATA_DIR": str(tmp_path / "d"), "HANDPRINTER_ONLINE": "1", **env}, dotenv=tmp_path / "none")
    return s, TestClient(create_app(s), base_url="https://quiet-words-here.trycloudflare.com", client=("127.0.0.1", 40000))  # cloudflared connects from loopback


def visitor(ip):
    return {**PUBLIC, "cf-connecting-ip": ip}


def test_loopback_tunnel_traffic_must_sign_in_with_default_password(tmp_path):
    s, c = online(tmp_path)
    assert s.password == "6892" and s.headless_recognition  # recognition can be started remotely
    assert c.get("/api/status", headers=PUBLIC).status_code == 401
    r = c.get("/", headers=PUBLIC, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert c.get("/login", headers=PUBLIC).status_code == 200
    assert c.get("/static/css/tokens.css", headers=PUBLIC).status_code == 200
    assert c.get("/api/sources/src_0000000000000000/image", headers=PUBLIC).status_code == 401

    assert c.post("/api/login", json={"token": "1234"}, headers=visitor("203.0.113.9")).status_code == 403
    ok = c.post("/api/login", json={"token": "6892"}, headers=visitor("203.0.113.9"))
    assert ok.status_code == 200
    cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "max-age=2592000" in cookie and "samesite=lax" in cookie

    status = c.get("/api/status", headers=PUBLIC)
    assert status.status_code == 200 and status.json()["online"] is True
    assert "no-store" in status.headers["cache-control"] and status.headers["cdn-cache-control"] == "no-store"


def test_password_from_env_and_state_changes_still_need_request_token(tmp_path):
    _, c = online(tmp_path, HANDPRINTER_PASSWORD="a different one")
    assert c.post("/api/login", json={"token": "6892"}, headers=visitor("198.51.100.1")).status_code == 403
    assert c.post("/api/login", json={"token": "a different one"}, headers=visitor("198.51.100.1")).status_code == 200
    origin = {**PUBLIC, "origin": "https://quiet-words-here.trycloudflare.com"}
    assert c.post("/api/batches", json={}, headers=origin).status_code == 403  # no request token
    token = re.search(r'name="hp-token" content="([^"]+)"', c.get("/", headers=PUBLIC).text).group(1)
    assert c.post("/api/batches", json={}, headers={**origin, "x-handprinter-token": token}).status_code == 200
    evil = {**PUBLIC, "origin": "https://evil.example", "x-handprinter-token": token}
    assert c.post("/api/batches", json={}, headers=evil).status_code == 403


def test_guessing_is_limited_per_visitor_and_overall(tmp_path):
    _, c = online(tmp_path)
    for _ in range(5):
        assert c.post("/api/login", json={"token": "0000"}, headers=visitor("192.0.2.1")).status_code == 403
    locked = c.post("/api/login", json={"token": "6892"}, headers=visitor("192.0.2.1"))
    assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0  # even the right password waits
    assert c.post("/api/login", json={"token": "6892"}, headers=visitor("192.0.2.2")).status_code == 200
    # many addresses together: the overall cap closes sign-in for everyone
    for i in range(15):
        c.post("/api/login", json={"token": "1111"}, headers=visitor(f"10.0.0.{i}"))
    assert c.post("/api/login", json={"token": "6892"}, headers=visitor("10.9.9.9")).status_code == 429
    assert c.get("/api/status", headers=PUBLIC).status_code == 200  # already signed-in devices keep working


def test_limits_expire():
    now = [0.0]
    lim = LoginLimiter(clock=lambda: now[0])
    for _ in range(5):
        lim.failed("a")
    assert 0 < lim.retry_after("a") <= 15 * 60 and lim.retry_after("b") == 0
    now[0] += 15 * 60 + 1
    assert lim.retry_after("a") == 0
    for i in range(20):
        lim.failed(f"x{i}")
    assert lim.retry_after("new") > 0
    now[0] += 24 * 3600 + 1
    assert lim.retry_after("new") == 0


def test_tunnel_reads_the_quick_tunnel_address_and_cleans_up(tmp_path, monkeypatch):
    """A stand-in for cloudflared that prints its usual banner; the real binary is never run in tests."""
    import sys
    import time

    from handprinter import tunnel

    fake = tmp_path / "fake_cloudflared.py"
    fake.write_text(
        "import sys, time\n"
        "assert '--url' in sys.argv and sys.argv[-1] == 'http://127.0.0.1:8765', sys.argv\n"
        "print('2026-11-22T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...', flush=True)\n"
        "print('2026-11-22T10:00:01Z INF |  https://shy-river-paper-lamp.trycloudflare.com  |', flush=True)\n"
        "time.sleep(30)\n", encoding="utf-8")
    s, _ = online(tmp_path)
    seen = []
    t = tunnel.Tunnel(s, "cloudflared", None, None, seen.append)
    # run "python fake_cloudflared.py <the same arguments>" instead of the real binary
    orig_popen = tunnel.subprocess.Popen
    monkeypatch.setattr(tunnel.subprocess, "Popen", lambda cmd, **kw: orig_popen([sys.executable, str(fake), *cmd[1:]], **kw))
    t.start()
    for _ in range(100):
        if seen:
            break
        time.sleep(0.05)
    assert seen == ["https://shy-river-paper-lamp.trycloudflare.com"]
    assert tunnel.read_state(s) == {"url": seen[0], "kind": "quick"}
    t.stop()
    assert tunnel.read_state(s) is None and t.proc.poll() is not None


def test_named_tunnel_token_goes_through_the_environment_not_the_command_line(tmp_path, monkeypatch):
    import sys

    from handprinter import tunnel

    captured = {}

    class P:
        stdout = iter(())

        def poll(self):
            return 0

    def fake_popen(cmd, env=None, **kw):
        captured.update(cmd=cmd, env=env)
        return P()

    monkeypatch.setattr(tunnel.subprocess, "Popen", fake_popen)
    s, _ = online(tmp_path)
    seen = []
    tunnel.Tunnel(s, "cloudflared", "secret-token-value", "https://hand.example.com", seen.append).start()
    assert "secret-token-value" not in " ".join(captured["cmd"]) and captured["env"]["TUNNEL_TOKEN"] == "secret-token-value"
    assert captured["cmd"][-1] == "run" and seen == ["https://hand.example.com"]
