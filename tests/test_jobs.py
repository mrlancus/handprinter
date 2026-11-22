"""Spec §11 jobs: durable state, atomic claim, cancellation and restart recovery."""

from __future__ import annotations

import time

from handprinter import jobs, worker
from handprinter.storage import Store


def test_claim_is_exclusive_and_ordered(conn):
    a = jobs.enqueue(conn, "detect", {"source_id": "x"})
    b = jobs.enqueue(conn, "detect", {"source_id": "y"})
    first = jobs.claim(conn, "general", 111)
    second = jobs.claim(conn, "general", 222)
    assert (first["id"], second["id"]) == (a, b)
    assert jobs.claim(conn, "general", 333) is None
    assert jobs.get(conn, a)["status"] == "running" and jobs.get(conn, a)["attempts"] == 1


def test_gpu_jobs_use_their_own_lane(conn):
    jobs.enqueue(conn, "adapter_infer", {})
    assert jobs.claim(conn, "general", 1) is None
    assert jobs.claim(conn, "gpu", 1)["kind"] == "adapter_infer"


def test_interrupted_resumable_job_is_requeued(conn):
    job_id = jobs.enqueue(conn, "export_snapshot", {})
    jobs.claim(conn, "general", 999_999)
    affected = jobs.recover(conn, now=time.time() + 120, pid_alive=lambda pid: False)
    assert affected == [job_id]
    job = jobs.get(conn, job_id)
    assert job["status"] == "queued" and "requeued" in job["message"]


def test_non_resumable_or_exhausted_job_becomes_interrupted_not_running(conn):
    job_id = jobs.enqueue(conn, "adapter_infer", {})
    jobs.claim(conn, "gpu", 999_999)
    jobs.recover(conn, now=time.time() + 120, pid_alive=lambda pid: False)
    job = jobs.get(conn, job_id)
    assert job["status"] == "interrupted" and "retry" in job["error"]
    assert jobs.retry(conn, job_id) != job_id


def test_live_heartbeat_is_left_alone(conn):
    job_id = jobs.enqueue(conn, "detect", {})
    jobs.claim(conn, "general", 1)
    assert jobs.recover(conn, pid_alive=lambda pid: True) == []
    assert jobs.get(conn, job_id)["status"] == "running"


def test_cancel_queued_and_running(conn, settings):
    queued = jobs.enqueue(conn, "detect", {})
    assert jobs.request_cancel(conn, queued)
    assert jobs.get(conn, queued)["status"] == "cancelled"

    def slow(job):
        for i in range(100):
            job.progress(i / 100)
            if i == 3:
                jobs.request_cancel(job.conn, job.id)
                time.sleep(0.3)
        return "finished"

    worker.HANDLERS["slow_test"] = slow
    try:
        job_id = jobs.enqueue(conn, "slow_test", {})
        row = jobs.claim(conn, "general", 1)
        worker.run_one(conn, Store(settings), settings, row)
    finally:
        del worker.HANDLERS["slow_test"]
    assert jobs.get(conn, job_id)["status"] == "cancelled"


def test_failed_job_records_error_and_worker_survives(conn, settings):
    job_id = jobs.enqueue(conn, "detect", {"source_id": "src_0000000000000000"})
    worker.run_one(conn, Store(settings), settings, jobs.claim(conn, "general", 1))
    job = jobs.get(conn, job_id)
    assert job["status"] == "failed" and "not found" in job["error"]


def test_stale_runner_cannot_overwrite_requeued_job(conn):
    job_id = jobs.enqueue(conn, "detect", {})
    jobs.claim(conn, "general", 4242)
    jobs.recover(conn, now=time.time() + 120, pid_alive=lambda pid: False)   # requeued
    assert not jobs.finish(conn, job_id, "succeeded", {"x": 1}, pid=4242)
    assert jobs.get(conn, job_id)["status"] == "queued"
