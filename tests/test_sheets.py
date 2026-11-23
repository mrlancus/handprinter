"""Writing sheets: labels come from the sheet, matched to the written lines in order. Synthetic pages."""

from __future__ import annotations

import json

from handprinter import review, sheets
from handprinter.db import new_id, now_iso
from tests.fixtures import encode, page

LINES = ["the quick brown fox jumps", "pack my box with five jugs", "seven wizards jumped over",
         "a gentle breeze moved", "bring a jacket for rain"]


def _sheet(conn, lines):
    sid = new_id("sht")
    conn.execute("INSERT INTO sheets (id, number, created_at, focus, seed, lines) VALUES (?,?,?,?,?,?)",
                 (sid, 1, now_iso(), "everyday", 1, json.dumps(lines)))
    return sid


def _texts(conn, source_id):
    rows = conn.execute("SELECT * FROM regions WHERE source_id = ? AND kind = 'line' AND deleted = 0 ORDER BY y", (source_id,)).fetchall()
    return [r["text"] for r in rows], rows


def test_a_written_sheet_is_labelled_in_order_with_words_cut(conn, store, import_bytes):
    sheet_id = _sheet(conn, LINES)
    src = import_bytes(encode(page(LINES, ruling=False)))[0].source_ids[0]
    out = sheets.apply_sheet(conn, store, src, sheet_id)
    assert out["matched"] == 5 and out["missing_sheet_lines"] == [] and out["unmatched_lines"] == 0
    texts, rows = _texts(conn, src)
    assert texts == LINES
    for row in rows:
        words = [w["text"] for w in review._line_words(conn, row["id"]) if w["status"] != "excluded"]
        assert " ".join(words) == row["text"]
    assert all(r["status"] in ("proposed", "needs_review") for r in rows)  # still yours to approve
    assert sheets.origin_of(conn) == {src: 1}


def test_a_skipped_sheet_line_does_not_shift_the_labels(conn, store, import_bytes):
    sheet_id = _sheet(conn, LINES)
    written = [LINES[0], LINES[1], LINES[3], LINES[4]]  # forgot the third line
    src = import_bytes(encode(page(written, ruling=False)))[0].source_ids[0]
    out = sheets.apply_sheet(conn, store, src, sheet_id)
    assert out["missing_sheet_lines"] == [3]
    assert _texts(conn, src)[0] == written


def test_sheets_never_overwrite_your_own_text(conn, store, import_bytes):
    from handprinter import pages
    sheet_id = _sheet(conn, LINES)
    src = import_bytes(encode(page(LINES, ruling=False)))[0].source_ids[0]
    pages.run_detection(conn, store, src)
    first = _texts(conn, src)[1][0]
    review.human_edit(conn, first["id"], {"text": "my own words"})
    sheets.apply_sheet(conn, store, src, sheet_id)
    assert review.get_region(conn, first["id"])["text"] == "my own words"


def test_new_sheets_cover_what_is_missing(conn):
    a = sheets.make_sheet(conn, "mixed", seed=3)
    assert len(a["lines"]) == sheets.LINES_PER_SHEET and any("$" in ln for ln in a["lines"])
    b = sheets.make_sheet(conn, "pairs", seed=3)
    c = sheets.make_sheet(conn, "pairs", seed=3)
    assert b["number"] == 2 and len(set(b["lines"]) & set(c["lines"])) < len(b["lines"])  # later sheets favour what is still rare


def test_whichever_line_is_skipped_it_is_found(conn, store, import_bytes):
    sheet_id = _sheet(conn, LINES)
    for skip in range(len(LINES)):
        written = [ln for k, ln in enumerate(LINES) if k != skip]
        src = import_bytes(encode(page(written, ruling=False, seed_shift=skip + 1)), session=f"s{skip}")[0].source_ids[0]
        out = sheets.apply_sheet(conn, store, src, sheet_id)
        assert out["missing_sheet_lines"] == [skip + 1], (skip, out)
        assert _texts(conn, src)[0] == written
