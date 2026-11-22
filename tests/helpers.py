"""Shared test helpers for labelling fixture regions by hand."""

from __future__ import annotations

from handprinter import pages, review


def detect_and_label(conn, store, source_id: str, approve: bool = True) -> dict[str, str]:
    """Run detection, then label each word with a fixture token, as a human reviewer would."""
    pages.run_detection(conn, store, source_id)
    labels = {}
    lines = conn.execute("SELECT id FROM regions WHERE source_id = ? AND kind = 'line' AND deleted = 0 ORDER BY y", (source_id,)).fetchall()
    for li, line in enumerate(lines):
        words = conn.execute("SELECT id FROM regions WHERE parent_id = ? AND deleted = 0 ORDER BY x", (line["id"],)).fetchall()
        tokens = [f"w{li}x{wi}" for wi in range(len(words))]
        for wid, tok in zip(words, tokens):
            review.human_edit(conn, wid["id"], {"text": tok, **({"status": "approved"} if approve else {})})
            labels[wid["id"]] = tok
        review.human_edit(conn, line["id"], {"text": " ".join(tokens), **({"status": "approved"} if approve else {})})
        labels[line["id"]] = " ".join(tokens)
    return labels
