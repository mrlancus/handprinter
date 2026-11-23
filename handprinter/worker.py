"""Worker process: claims durable jobs from SQLite and runs heavy work outside the web process."""

from __future__ import annotations

import logging
import os
import signal
import sqlite3
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Callable

from handprinter import dataset, evaluation, ingest, jobs, pages, sheets
from handprinter.adapters import all_adapters
from handprinter.adapters.base import RunnerCancelled
from handprinter.config import Settings
from handprinter.db import connect, init_db
from handprinter.recognition import headless, importer, package
from handprinter.storage import Store

log = logging.getLogger("handprinter.worker")
Handler = Callable[["Job"], Any]


class Job:
    """Everything a handler needs: params, DB connection, storage, progress and cancellation."""

    def __init__(self, conn: sqlite3.Connection, store: Store, settings: Settings, row: dict[str, Any], cancel: threading.Event):
        self.conn, self.store, self.settings, self.row = conn, store, settings, row
        self.params: dict[str, Any] = row["params"]
        self._cancel = cancel
        self._ctx = jobs.JobContext(conn, row["id"])

    @property
    def id(self) -> str:
        return self.row["id"]

    def progress(self, fraction: float, message: str = "") -> None:
        if self._cancel.is_set():
            raise jobs.JobCancelled()
        self._ctx.progress(fraction, message)

    def cancelled(self) -> bool:
        return self._cancel.is_set()


def _import_upload(job: Job) -> Any:
    p = job.params
    staged = job.store.staging(p["staged_id"])
    wanted = ingest.parse_pages(p.get("pages"))
    batch = ingest.BatchInfo(p["batch_id"], p["source_type"], p.get("session"), p.get("captured_date"), p.get("style_era", "current"), wanted)
    results = ingest.import_upload(job.conn, job.store, staged, p["filename"], batch, job.progress)
    staged.unlink(missing_ok=True)
    out = [r.as_dict() for r in results]
    if p.get("detect", True) or p.get("sheet_id") or p.get("sheet_start"):
        for sid in {sid for r in results for sid in r.source_ids}:
            if pages.get_source(job.conn, sid)["detection_status"] == "none":
                pages.run_detection(job.conn, job.store, sid)
    sheet_results = []
    if p.get("sheet_id"):  # a written-out writing sheet: label it from the sheet, no recognition needed
        for sid in sorted({sid for r in results for sid in r.source_ids}):
            sheet_results.append(sheets.apply_sheet(job.conn, job.store, sid, p["sheet_id"]))
    elif p.get("sheet_start"):  # pages in sheet order: the first chosen page is sheet N, the next N+1, ...
        order = sorted(wanted) if wanted is not None else None
        imported = {sid: pages.get_source(job.conn, sid)["page_index"] for r in results if r.status == "imported" for sid in r.source_ids}
        for sid, index in sorted(imported.items(), key=lambda kv: kv[1]):
            number = p["sheet_start"] + (order.index(index) if order is not None else index)
            sheet_id = sheets.sheet_by_number(job.conn, number)
            if sheet_id is None:
                sheet_results.append({"source_id": sid, "error": f"there is no sheet {number}"})
                continue
            sheet_results.append({**sheets.apply_sheet(job.conn, job.store, sid, sheet_id), "sheet_number": number})
    return {"entries": out, "sheets": sheet_results}


def _apply_sheet(job: Job) -> Any:
    return sheets.apply_sheet(job.conn, job.store, job.params["source_id"], job.params["sheet_id"])


def _detect(job: Job) -> Any:
    source_id = job.params["source_id"]
    out = pages.run_detection(job.conn, job.store, source_id, replace=bool(job.params.get("replace")))
    # A page already labelled from a writing sheet stays labelled: the fresh boxes get the sheet's text again.
    sheet = job.conn.execute("SELECT sheet_id FROM sheet_pages WHERE source_id = ?", (source_id,)).fetchone()
    if sheet and not out.get("skipped"):
        out["sheet"] = sheets.apply_sheet(job.conn, job.store, source_id, sheet[0])
    return out


def _process(job: Job) -> Any:
    return pages.apply_processing(job.conn, job.store, job.params["source_id"], job.params["processing"])


def _build_recognition(job: Job) -> Any:
    p = job.params
    return package.build(job.conn, job.store, job.settings, p["source_ids"], bool(p.get("include_overview")), bool(p.get("force")), job.progress)


def _import_recognition(job: Job) -> Any:
    return importer.import_package(job.conn, job.store, job.params["package_id"])


def _headless_recognition(job: Job) -> Any:
    if not job.settings.headless_recognition:
        raise RuntimeError("headless recognition is off (set HANDPRINTER_HEADLESS_RECOGNITION=1); use /recognise in Claude Code instead")
    run = headless.run_package(job.store, job.settings, job.params["package_id"], job.params.get("item_ids"), job.progress, job.cancelled)
    run["import"] = importer.import_package(job.conn, job.store, job.params["package_id"])
    return run


def _recut_line(job: Job) -> Any:
    from handprinter.recognition import refit
    return refit.recut_line(job.conn, job.store, job.params["line_id"], job.params["text"])


def _export_snapshot(job: Job) -> Any:
    p = job.params
    filters = dataset.Filters(tuple(p.get("source_types", ["paper"])), tuple(p.get("style_eras", ["current"])),
                              tuple(p["sessions"]) if p.get("sessions") else None, bool(p.get("include_maths", True)))
    config = dataset.SplitConfig(seed=int(p.get("seed", 1234)), holdout_sessions=tuple(p.get("holdout_sessions", [])),
                                 unseen_word_fraction=float(p.get("unseen_word_fraction", 0.1)))
    return dataset.export_snapshot(job.conn, job.store, p.get("name") or "snapshot", p.get("policy", "approved_only"), filters, config, job.progress)


def _adapter(job: Job):
    return all_adapters(job.settings.adapter_env)[job.params["adapter"]]


def _adapter_preflight(job: Job) -> Any:
    out = job.store.dir("runs") / f"preflight-{job.id}"
    out.mkdir(exist_ok=True)
    return _adapter(job).preflight(out, lambda e: job.progress(float(e.get("fraction", 0)), str(e.get("message", ""))), job.cancelled)


def _adapter_download(job: Job) -> Any:
    adapter = _adapter(job)
    if not hasattr(adapter, "download"):
        raise RuntimeError(f"{adapter.title} has no in-app download")
    return adapter.download(lambda e: job.progress(float(e.get("fraction", 0)), str(e.get("message", ""))), job.cancelled)


def _adapter_infer(job: Job) -> Any:
    return evaluation.run_inference(job.conn, job.store, job.settings, job.id, job.params, job.progress, job.cancelled)


def _adapter_train(job: Job) -> Any:
    return evaluation.run_training(job.conn, job.store, job.settings, job.id, job.params, job.progress, job.cancelled)


HANDLERS: dict[str, Handler] = {
    "import_upload": _import_upload,
    "detect": _detect,
    "process": _process,
    "build_recognition": _build_recognition,
    "import_recognition": _import_recognition,
    "headless_recognition": _headless_recognition,
    "export_snapshot": _export_snapshot,
    "recut_line": _recut_line,
    "apply_sheet": _apply_sheet,
    "adapter_preflight": _adapter_preflight,
    "adapter_download": _adapter_download,
    "adapter_infer": _adapter_infer,
    "adapter_train": _adapter_train,
}


def run_one(conn: sqlite3.Connection, store: Store, settings: Settings, row: dict[str, Any]) -> None:
    """Run a claimed job to a terminal state, with a heartbeat thread while it runs."""
    cancel = threading.Event()
    stop = threading.Event()
    beat_conn = connect(settings.db_path)

    def beat() -> None:
        ctx = jobs.JobContext(beat_conn, row["id"])
        while not stop.wait(jobs.HEARTBEAT_SECONDS):
            try:
                if ctx.heartbeat():
                    cancel.set()
            except Exception as exc:  # a missed beat must not stop later beats
                log.warning("heartbeat for %s failed: %s", row["id"], exc)

    thread = threading.Thread(target=beat, daemon=True)
    thread.start()
    started = time.time()
    try:
        handler = HANDLERS.get(row["kind"])
        if handler is None:
            raise RuntimeError(f"unknown job kind {row['kind']}")
        result = handler(Job(conn, store, settings, row, cancel))
        if cancel.is_set():
            result = {"note": "cancel was requested after the work had already completed", "result": result}
        jobs.finish(conn, row["id"], "succeeded", result, pid=row["worker_pid"])
        log.info("job %s %s succeeded in %.1fs", row["id"], row["kind"], time.time() - started)
    except (jobs.JobCancelled, RunnerCancelled):
        jobs.finish(conn, row["id"], "cancelled", error="cancelled by user", pid=row["worker_pid"])
        log.info("job %s %s cancelled", row["id"], row["kind"])
    except Exception as exc:  # job failures are recorded, never crash the worker
        jobs.finish(conn, row["id"], "failed", error=f"{exc.__class__.__name__}: {exc}"[:2000], pid=row["worker_pid"])
        log.warning("job %s %s failed: %s\n%s", row["id"], row["kind"], exc, traceback.format_exc(limit=5))
    finally:
        stop.set()
        thread.join(timeout=5)
        beat_conn.close()


def _lane_loop(settings: Settings, lane: str, stopping: threading.Event, poll: float) -> None:
    conn = connect(settings.db_path)
    store = Store(settings)
    backoff = poll
    while not stopping.is_set():
        try:
            row = jobs.claim(conn, lane, os.getpid())
            if row is None:
                stopping.wait(poll)
                continue
            run_one(conn, store, settings, row)
            backoff = poll
        except Exception as exc:  # keep the lane alive (e.g. "database is locked")
            log.error("%s lane error: %s", lane, exc)
            stopping.wait(backoff)
            backoff = min(30.0, backoff * 2)
    conn.close()


def main(settings: Settings, poll: float = 0.5) -> None:
    logs = settings.sub("logs")
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=[logging.FileHandler(Path(logs) / "worker.log", encoding="utf-8"), logging.StreamHandler()],
    )
    conn = connect(settings.db_path)
    init_db(conn)
    recovered = jobs.recover(conn)
    if recovered:
        log.info("reconciled %d interrupted job(s): %s", len(recovered), ", ".join(recovered))
    stopping = threading.Event()

    def stop(*_: Any) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    lanes = [threading.Thread(target=_lane_loop, args=(settings, lane, stopping, poll), daemon=True, name=f"lane-{lane}") for lane in ("general", "gpu")]
    for t in lanes:
        t.start()
    log.info("worker %d started (data dir %s)", os.getpid(), settings.data_dir)
    while not stopping.wait(10):
        try:
            jobs.recover(conn)
        except Exception as exc:
            log.error("recovery pass failed: %s", exc)
    for t in lanes:
        t.join(timeout=30)
    conn.close()
