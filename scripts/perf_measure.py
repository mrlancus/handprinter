"""Measure API latencies against a running server (warm). Usage: python scripts/perf_measure.py http://127.0.0.1:8792"""

from __future__ import annotations

import re
import statistics
import sys
import time

import httpx

base = sys.argv[1]
c = httpx.Client(base_url=base, timeout=30)
token = re.search(r'name="hp-token" content="([^"]+)"', c.get("/").text).group(1)
c.headers["x-handprinter-token"] = token


def timed(label: str, fn, n: int = 15) -> None:
    fn()  # warm
    samples = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    print(f"{label:<44} median {statistics.median(samples):7.1f} ms   p95 {sorted(samples)[int(0.95 * len(samples)) - 1]:7.1f} ms")


timed("GET /api/status", lambda: c.get("/api/status").raise_for_status())
timed("GET /api/sources (page of 48)", lambda: c.get("/api/sources?limit=48").raise_for_status())
timed("GET /api/regions needs_review (page of 50)", lambda: c.get("/api/regions?status=needs_review&limit=50").raise_for_status())
timed("GET /api/regions offset 9000", lambda: c.get("/api/regions?limit=50&offset=9000").raise_for_status())
sid = c.get("/api/sources?limit=1").json()["items"][0]["id"]
timed("GET /api/sources/{id}/regions (200 regions)", lambda: c.get(f"/api/sources/{sid}/regions").raise_for_status())
timed("GET /api/dataset/coverage (10k)", lambda: c.get("/api/dataset/coverage").raise_for_status(), n=5)
regions = c.get("/api/regions?status=unprocessed&kind=word&limit=40").json()["items"]


def edit() -> None:
    r = regions.pop()
    c.patch(f"/api/regions/{r['id']}", json={"text": "edited", "expected_revision": r["revision"]}).raise_for_status()


timed("PATCH /api/regions/{id} (review action)", edit, n=15)
rid = c.get("/api/regions?limit=1").json()["items"][0]["id"]
timed("GET /api/regions/{id}/crop (cached)", lambda: c.get(f"/api/regions/{rid}/crop").raise_for_status())
