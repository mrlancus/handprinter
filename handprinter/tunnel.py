"""Cloudflare Tunnel for online mode: publishes the local app at an https address.

Everything still runs on this PC (files, database, worker, Claude Code, GPU); Cloudflare only
carries the encrypted traffic. Two kinds:

- Quick tunnel (no account): `cloudflared tunnel --url http://127.0.0.1:<port>`. A random
  https://<words>.trycloudflare.com address that changes every time it starts.
- Named tunnel (free Cloudflare account + a domain): set HANDPRINTER_TUNNEL_TOKEN (and
  HANDPRINTER_PUBLIC_URL for display). Same address every time.
- Relay (free Cloudflare account, no domain): a small Worker at https://<name>.<you>.workers.dev
  (relay/worker.js) forwards to whatever quick-tunnel address this PC registered on start.
  Set HANDPRINTER_RELAY_URL and HANDPRINTER_RELAY_SECRET. Same address every time.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable

from handprinter.config import Settings

QUICK_URL = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
WINDOWS_PATHS = (r"C:\Program Files (x86)\cloudflared\cloudflared.exe", r"C:\Program Files\cloudflared\cloudflared.exe")


def find_cloudflared(env: dict[str, str] | None = None) -> str | None:
    env = os.environ if env is None else env
    explicit = env.get("HANDPRINTER_CLOUDFLARED")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found = shutil.which("cloudflared")
    if found:
        return found
    if sys.platform == "win32":
        return next((p for p in WINDOWS_PATHS if Path(p).is_file()), None)
    return None


def state_path(settings: Settings) -> Path:
    return settings.data_dir / "online.json"


def read_state(settings: Settings) -> dict | None:
    try:
        return json.loads(state_path(settings).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_state(settings: Settings, data: dict | None) -> None:
    path = state_path(settings)
    if data is None:
        path.unlink(missing_ok=True)
    else:
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data), encoding="utf-8")


def register_relay(relay_url: str, secret: str, tunnel_url: str, attempts: int = 6) -> str | None:
    """Tell the workers.dev relay this PC's current tunnel address. Returns None on success, else why not."""
    import time
    import urllib.error
    import urllib.request

    body = json.dumps({"url": tunnel_url}).encode()
    why = "no attempt made"
    for attempt in range(attempts):
        req = urllib.request.Request(relay_url.rstrip("/") + "/__relay/register", data=body, method="POST",
                                     headers={"authorization": f"Bearer {secret}", "content-type": "application/json",
                                              "user-agent": "handprinter-relay-register"})
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                if res.status == 200:
                    return None
                why = f"relay answered {res.status}"
        except urllib.error.HTTPError as exc:
            why = {403: "the relay rejected the secret: HANDPRINTER_RELAY_SECRET must match RELAY_SECRET on the Worker",
                   400: "the relay refused the address"}.get(exc.code, f"relay answered {exc.code}")
            if exc.code in (400, 403):
                return why
        except (urllib.error.URLError, OSError) as exc:
            why = f"could not reach the relay ({exc.__class__.__name__})"
        time.sleep(min(30, 2 ** attempt))
    return why


class Tunnel:
    def __init__(self, settings: Settings, binary: str, token: str | None, public_url: str | None,
                 on_url: Callable[[str], None], relay_url: str | None = None, relay_secret: str | None = None):
        self.settings, self.binary, self.token, self.public_url, self.on_url = settings, binary, token, public_url, on_url
        self.relay_url, self.relay_secret = (relay_url or "").rstrip("/") or None, relay_secret or None
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        env = dict(os.environ)
        if self.token:
            env["TUNNEL_TOKEN"] = self.token  # via the environment, never on the command line
            cmd = [self.binary, "tunnel", "--no-autoupdate", "run"]
        else:
            cmd = [self.binary, "tunnel", "--no-autoupdate", "--url", f"http://127.0.0.1:{self.settings.port}"]
        log = self.settings.sub("logs") / "cloudflared.log"
        self.proc = subprocess.Popen(cmd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace")
        if self.public_url:
            self._announce(self.public_url, "named")
        threading.Thread(target=self._pump, args=(log,), daemon=True, name="cloudflared-log").start()

    def _announce(self, url: str, kind: str) -> None:
        if kind == "quick" and self.relay_url and self.relay_secret:
            # the fixed workers.dev address is the one to use; the quick address changes every start
            _write_state(self.settings, {"url": self.relay_url, "kind": "relay", "tunnel_url": url, "relay": "registering"})
            problem = register_relay(self.relay_url, self.relay_secret, url)
            _write_state(self.settings, {"url": self.relay_url if not problem else url, "kind": "relay" if not problem else "quick",
                                         "tunnel_url": url, "relay": "ok" if not problem else problem})
            if problem:
                print(f"Relay not updated: {problem}. Use this address for now instead:", flush=True)
                self.on_url(url)
            else:
                self.on_url(self.relay_url)
            return
        _write_state(self.settings, {"url": url, "kind": kind})
        self.on_url(url)

    def _pump(self, log: Path) -> None:
        assert self.proc and self.proc.stdout
        announced = bool(self.public_url)
        with log.open("a", encoding="utf-8") as fh:
            for line in self.proc.stdout:
                fh.write(line)
                fh.flush()
                if not announced and not self.token:
                    m = QUICK_URL.search(line)
                    if m:
                        announced = True
                        self._announce(m.group(0), "quick")

    def stop(self) -> None:
        _write_state(self.settings, None)
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
