"""Seed ~10,000 synthetic word/line regions (non-personal) to measure query budgets.

Usage: HANDPRINTER_DATA_DIR=/tmp/hp-perf python scripts/perf_seed.py
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handprinter import review  # noqa: E402
from handprinter.config import get_settings  # noqa: E402
from handprinter.db import connect, init_db, new_id, now_iso, transaction  # noqa: E402
from handprinter.geometry import Box  # noqa: E402
from handprinter.ingest import BatchInfo, import_upload  # noqa: E402
from handprinter.pages import get_source  # noqa: E402
from handprinter.storage import Store  # noqa: E402
from tests.fixtures import encode, page  # noqa: E402

settings = get_settings()
store = Store(settings)
conn = connect(settings.db_path)
init_db(conn)
batch = new_id("bat")
conn.execute("INSERT INTO batches (id, created_at, source_type) VALUES (?, ?, 'paper')", (batch, now_iso()))
rng = random.Random(1)
total = 0
for p in range(50):
    staged = store.staging(new_id("stg"))
    staged.write_bytes(encode(page([f"perf page {p}", "line two"], variant=p % 12 + 1, seed_shift=p)))
    sid = import_upload(conn, store, staged, f"perf-{p}.png", BatchInfo(batch, "paper", f"s{p % 5}"))[0].source_ids[0]
    src = get_source(conn, sid)
    with transaction(conn):
        for li in range(20):
            line = review.create_region(conn, src, "line", Box(50, 40 + li * 40, 1000, 35), actor="detector")
            total += 1
            for wi in range(9):
                st = rng.choice(["unprocessed", "proposed", "needs_review", "approved", "approved"])
                review.create_region(conn, src, "word", Box(50 + wi * 110, 40 + li * 40, 100, 35), actor="detector",
                                     parent_id=line["id"], text=f"w{wi}", status=st, flags=["uncertain_span"] if st == "needs_review" else [])
                total += 1
print(f"{total} regions on 50 pages in {settings.data_dir}")
