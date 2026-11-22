"""Durable job queue in SQLite: enqueue, claim, heartbeat, cancel and restart recovery."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable

from handprinter.db import dumps, new_id, now_iso, row_dict, transaction

HEARTBEAT_SECONDS = 2.0
STALE_AFTER_SECONDS = 20.0
ACTIVE = ("queued", "running")
JOB_JSON = ("params", "result")

# Kinds safe to re-run after an interruption (idempotent or resumable).
RESUMABLE = {"import_upload", "detect", "process", "build_recognition", "import_recognition", "export_snapshot", "headless_recognition"}
GPU_KINDS = {"adapter_preflight", "adapter_infer", "adapter_train", "adapter_download"}


class JobCancelled(Exception):
    pass


def enqueue(conn: sqlite3.Connection, kind: str, params: dict[str, Any], max_attempts: int | None = None) -> str:
    job_id = new_id("job")
    lane = "gpu" if kind in GPU_KINDS else "general"
    attempts = max_attempts if max_attempts is not None else (3 if kind in RESUMABLE else 1)
    conn.execute(
        "INSERT INTO jobs (id, kind, lane, status, params, max_attempts, created_at) VALUES (?,?,?,?,?,?,?)",
        (job_id, kind, lane, "queued", dumps(params), attempts, now_iso()),
    )
    return job_id


def get(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    return row_dict(conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone(), JOB_JSON)


def list_jobs(conn: sqlite3.Connection, active_only: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    where = "WHERE status IN ('queued','running')" if active_only else ""
    rows = conn.execute(f"SELECT * FROM jobs {where} ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [row_dict(r, JOB_JSON) for r in rows]  # type: ignore[misc]


def request_cancel(conn: sqlite3.Connection, job_id: str) -> bool:
    with transaction(conn):
        job = get(conn, job_id)
        if job is None or job["status"] not in ACTIVE:
            return False
        if job["status"] == "queued":
            conn.execute("UPDATE jobs SET status='cancelled', finished_at=?, message='cancelled before start' WHERE id=?", (now_iso(), job_id))
        else:
            conn.execute("UPDATE jobs SET cancel_requested = 1 WHERE id = ?", (job_id,))
    return True


def retry(conn: sqlite3.Connection, job_id: str) -> str:
    job = get(conn, job_id)
    if job is None or job["status"] not in ("failed", "cancelled", "interrupted"):
        raise ValueError("only failed, cancelled or interrupted jobs can be retried")
    return enqueue(conn, job["kind"], job["params"])


def claim(conn: sqlite3.Connection, lane: str, pid: int) -> dict[str, Any] | None:
    """Atomically move the oldest queued job in `lane` to running."""
    row = conn.execute(
        """UPDATE jobs SET status='running', worker_pid=?, heartbeat_at=?, started_at=?, attempts=attempts+1,
               cancel_requested=0, error=NULL
           WHERE id = (SELECT id FROM jobs WHERE status='queued' AND lane=? ORDER BY created_at LIMIT 1)
           RETURNING *""",
        (pid, time.time(), now_iso(), lane),
    ).fetchone()
    return row_dict(row, JOB_JSON)


def _pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if pid == os.getpid() or os.name == "nt":
        # On Windows os.kill(pid, 0) sends CTRL_C_EVENT, so rely on heartbeat staleness alone.
        return True
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, SystemError):
        return False
    return True


def recover(conn: sqlite3.Connection, now: float | None = None, pid_alive: Callable[[int | None], bool] = _pid_alive) -> list[str]:
    """Reconcile `running` jobs whose worker died or stopped heartbeating.

    They become `interrupted` and are requeued when resumable and attempts remain; otherwise
    they fail with an explanation. Returns the affected job IDs.
    """
    now = time.time() if now is None else now
    affected = []
    with transaction(conn):
        for job in conn.execute("SELECT id, kind, attempts, max_attempts, heartbeat_at, worker_pid FROM jobs WHERE status='running'").fetchall():
            stale = job["heartbeat_at"] is None or now - job["heartbeat_at"] > STALE_AFTER_SECONDS
            if not stale and pid_alive(job["worker_pid"]):
                continue
            affected.append(job["id"])
            if job["kind"] in RESUMABLE and job["attempts"] < job["max_attempts"]:
                conn.execute(
                    "UPDATE jobs SET status='queued', worker_pid=NULL, message='interrupted; requeued after worker restart' WHERE id=?",
                    (job["id"],),
                )
            else:
                conn.execute(
                    "UPDATE jobs SET status='interrupted', finished_at=?, error=? WHERE id=?",
                    (now_iso(), "worker stopped while this job was running; retry it explicitly", job["id"]),
                )
    return affected


@dataclass
class JobContext:
    conn: sqlite3.Connection
    job_id: str
    _last_beat: float = 0.0

    def progress(self, fraction: float, message: str = "") -> None:
        """Report progress; raises JobCancelled if the user asked to cancel."""
        now = time.time()
        if now - self._last_beat < 0.25 and fraction < 1:
            return
        self._last_beat = now
        row = self.conn.execute(
            "UPDATE jobs SET progress=?, message=?, heartbeat_at=? WHERE id=? RETURNING cancel_requested",
            (max(0.0, min(1.0, fraction)), message[:300], now, self.job_id),
        ).fetchone()
        if row and row["cancel_requested"]:
            raise JobCancelled()

    def heartbeat(self) -> bool:
        row = self.conn.execute("UPDATE jobs SET heartbeat_at=? WHERE id=? RETURNING cancel_requested", (time.time(), self.job_id)).fetchone()
        return bool(row and row["cancel_requested"])


def finish(conn: sqlite3.Connection, job_id: str, status: str, result: Any = None, error: str | None = None, pid: int | None = None) -> bool:
    """Record a terminal state, but only if this runner still owns the job.

    A job that recovery requeued (or that another worker claimed) is not overwritten by a
    stale runner. Returns whether the update applied.
    """
    cur = conn.execute(
        """UPDATE jobs SET status=?, result=?, error=?, finished_at=?, progress=CASE WHEN ?='succeeded' THEN 1 ELSE progress END
           WHERE id=? AND status='running' AND (? IS NULL OR worker_pid=?)""",
        (status, json.dumps(result) if result is not None else None, error, now_iso(), status, job_id, pid, pid),
    )
    return cur.rowcount == 1
