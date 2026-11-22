"""Common adapter interface: status, capabilities, dataset conversion, preflight, inference, training."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class Capabilities:
    levels: list[str]
    charset: str | None          # None = not documented upstream
    charset_note: str
    max_chars: int | None
    needs_style_references: int  # number of style images per request (0 = none)
    can_train_in_app: bool
    train_note: str


@dataclass
class AdapterStatus:
    name: str
    title: str
    state: str                   # "ready" | "blocked"
    checks: dict[str, bool]
    blockers: list[str] = field(default_factory=list)
    next_step: str = ""
    upstream: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class InferRequest:
    texts: list[str]
    level: str
    style_images: list[Path]
    seeds: list[int]
    out_dir: Path
    options: dict[str, Any] = field(default_factory=dict)


class AdapterUnavailable(RuntimeError):
    """The adapter cannot do this here; the message names the blocker and next step."""


class ModelAdapter:
    name = "base"
    title = "Base"

    def __init__(self, env: dict[str, str]):
        self.env = env

    def status(self) -> AdapterStatus:
        raise NotImplementedError

    def capabilities(self) -> Capabilities:
        raise NotImplementedError

    def unsupported(self, text: str, level: str) -> list[str]:
        """Reasons this text cannot be generated faithfully (empty list = supported)."""
        caps = self.capabilities()
        reasons = []
        if level not in caps.levels:
            reasons.append(f"{self.title} does not generate {level}s")
        if caps.charset is not None:
            bad = sorted({ch for ch in text if ch not in caps.charset})
            if bad:
                reasons.append(f"unsupported characters: {' '.join(repr(c) for c in bad)}")
        elif any(ord(ch) > 127 for ch in text):
            reasons.append("non-ASCII characters: support is undocumented upstream")
        if caps.max_chars is not None and len(text) > caps.max_chars:
            reasons.append(f"longer than {caps.max_chars} characters")
        return reasons

    def require_ready(self) -> AdapterStatus:
        status = self.status()
        if status.state != "ready":
            raise AdapterUnavailable("; ".join(status.blockers) + (f". Next step: {status.next_step}" if status.next_step else ""))
        return status

    def preflight(self, out_dir: Path, on_event: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        raise AdapterUnavailable(f"{self.title}: preflight not available")

    def infer(self, request: InferRequest, on_event: Callable[[dict[str, Any]], None], cancelled: Callable[[], bool]) -> dict[str, Any]:
        raise AdapterUnavailable(f"{self.title}: inference not available")

    def train(self, *_: Any, **__: Any) -> dict[str, Any]:
        raise AdapterUnavailable(f"{self.title}: {self.capabilities().train_note}")


def python_ok(path: str | None) -> bool:
    return bool(path) and Path(path).is_file() and os.access(path, os.X_OK)


def run_runner(
    python: str, script: Path, request: dict[str, Any], on_event: Callable[[dict[str, Any]], None],
    cancelled: Callable[[], bool], timeout_s: float = 3600,
) -> dict[str, Any]:
    """Run an adapter runner in its own environment; JSON request on stdin, JSON-lines events on stdout.

    stderr is drained by a thread (bounded tail kept for errors) so a chatty runner cannot
    deadlock; a watchdog enforces cancellation and timeout even when the runner is silent.
    The final event must have `"event": "result"`.
    """
    kwargs: dict[str, Any] = {}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen(
        [python, str(script)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", **kwargs,
    )
    assert proc.stdin and proc.stdout and proc.stderr
    proc.stdin.write(json.dumps(request))
    proc.stdin.close()
    stderr_tail: deque[str] = deque(maxlen=40)
    stop_reason: list[str] = []
    done = threading.Event()

    def drain() -> None:
        for line in proc.stderr:  # type: ignore[union-attr]
            stderr_tail.append(line)

    def watchdog() -> None:
        started = time.time()
        while not done.wait(0.5):
            if cancelled():
                stop_reason.append("cancelled")
            elif time.time() - started > timeout_s:
                stop_reason.append("timeout")
            if stop_reason:
                _terminate(proc)
                return

    threads = [threading.Thread(target=drain, daemon=True), threading.Thread(target=watchdog, daemon=True)]
    for t in threads:
        t.start()
    result: dict[str, Any] | None = None
    try:
        for line in proc.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("event") == "result":
                result = event
            else:
                on_event(event)
        proc.wait()
    finally:
        done.set()
        threads[0].join(timeout=5)
    if stop_reason == ["cancelled"]:
        raise RunnerCancelled()
    if stop_reason:
        raise AdapterUnavailable(f"runner exceeded its {int(timeout_s)} s time limit and was stopped")
    if proc.returncode != 0 or result is None or not result.get("ok", False):
        detail = (result or {}).get("error") or "".join(stderr_tail)[-2000:].strip() or "no result event"
        raise RuntimeError(f"runner failed (exit {proc.returncode}): {detail}")
    return result


class RunnerCancelled(Exception):
    """The user cancelled while the runner was working."""


def _terminate(proc: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            # Kill the whole tree (runner plus any children it spawned).
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=30)
        else:
            os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired, OSError):
        proc.kill()


RUNNERS_DIR = Path(__file__).resolve().parent / "runners"
APP_PYTHON = sys.executable
