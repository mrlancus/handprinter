"""Seed a data directory with labelled, NON-PERSONAL synthetic fixture pages (for demos and screenshots).

Usage: HANDPRINTER_DATA_DIR=/tmp/hp-demo python scripts/seed_fixtures.py
The pages are rendered from a system font. They are not handwriting.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from handprinter import pages, review  # noqa: E402
from handprinter.config import get_settings  # noqa: E402
from handprinter.db import connect, init_db, new_id, now_iso, transaction  # noqa: E402
from handprinter.ingest import BatchInfo, import_upload  # noqa: E402
from handprinter.storage import Store  # noqa: E402
from tests.fixtures import encode, page  # noqa: E402

SETS = [
    ["the quick brown fox", "jumps over the lazy dog", "becuase it was late", "Meet me at 4pm"],
    ["notes for monday", "read chapter three", "ask about the essay"],
    ["shopping list", "eggs, milk, bread", "call Sam back"],
    ["weekly plan", "finish the draft", "post the letters"],
]


def main() -> None:
    settings = get_settings()
    store = Store(settings)
    conn = connect(settings.db_path)
    init_db(conn)
    batch_id = new_id("bat")
    conn.execute("INSERT INTO batches (id, created_at, source_type, session) VALUES (?, ?, 'paper', 'fixture-session')", (batch_id, now_iso()))
    ids = []
    for i, lines in enumerate(SETS):
        staged = store.staging(new_id("stg"))
        staged.write_bytes(encode(page(lines, variant=i * 3 + 1 if i else 0), "JPEG"))
        res = import_upload(conn, store, staged, f"fixture-page-{i + 1}.jpg", BatchInfo(batch_id, "paper", "fixture-session"))
        ids += res[0].source_ids
    for sid in ids:
        pages.run_detection(conn, store, sid)
    first = ids[0]
    lines = conn.execute("SELECT id FROM regions WHERE source_id = ? AND kind = 'line' AND deleted = 0 ORDER BY y", (first,)).fetchall()
    for line, text in zip(lines, SETS[0]):
        words = conn.execute("SELECT id FROM regions WHERE parent_id = ? AND deleted = 0 ORDER BY x", (line["id"],)).fetchall()
        tokens = text.split()
        if len(words) == len(tokens):
            for w, t in zip(words, tokens):
                review.human_edit(conn, w["id"], {"text": t, "status": "approved"})
        review.human_edit(conn, line["id"], {"text": text})
    # simulate one flagged machine proposal so the amber review state is visible (labelled as fixture)
    second_lines = conn.execute("SELECT id FROM regions WHERE source_id = ? AND kind = 'line' AND deleted = 0 ORDER BY y", (ids[1],)).fetchall()
    with transaction(conn):
        review.machine_propose(conn, second_lines[0]["id"], "notes for monday", ["uncertain_span"], "fixture: 'm' could be 'rn'")
    print(f"Seeded {len(ids)} fixture pages into {settings.data_dir}")


if __name__ == "__main__":
    main()
