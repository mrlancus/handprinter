"""Cloudflare Tunnel for online mode: publishes the local app at an https address.

Everything still runs on this PC (files, database, worker, Claude Code, GPU); Cloudflare only
carries the encrypted traffic. Two kinds:

- Quick tunnel (no account): `cloudflared tunnel --url http://127.0.0.1:<port>`. A random
  https://<words>.trycloudflare.com address that changes every time it starts.
- Named tunnel (free Cloudflare account + a domain): set HANDPRINTER_TUNNEL_TOKEN (and
  HANDPRINTER_PUBLIC_URL for display). Same address every time.
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


class Tunnel:
    def __init__(self, settings: Settings, binary: str, token: str | None, public_url: str | None,
                 on_url: Callable[[str], None]):
        self.settings, self.binary, self.token, self.public_url, self.on_url = settings, binary, token, public_url, on_url
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
