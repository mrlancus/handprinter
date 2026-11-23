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


def test_every_focus_fills_a_sheet_and_maths_is_well_formed(conn):
    for focus in sheets.FOCUSES:
        sh = sheets.make_sheet(conn, focus, seed=11)
        assert len(sh["lines"]) == sheets.LINES_PER_SHEET or focus == "mine", focus
        for line in sh["lines"]:
            assert line.count("$") % 2 == 0, line  # every maths span is closed
    assert any("def " in ln or "for " in ln or "import" in ln for ln in sheets.make_sheet(conn, "python", seed=2)["lines"])


# ---------- printed pack (written while the app is off, uploaded later) ----------

def test_printed_pack_is_fixed_complete_and_writable_on_one_line():
    pack = sheets.load_pack()
    assert [s["number"] for s in pack] == list(range(1001, 1001 + len(pack)))
    for s in pack:
        assert len(s["lines"]) == sheets.LINES_PER_SHEET and s["focus"] in sheets.FOCUSES
        assert all(line.count("$") % 2 == 0 and sheets._one_row(line) for line in s["lines"])
    assert sheets.pack_id(1007) == "sht_0000000000001007" and sheets.is_pack("sht_0000000000001007")
    assert not sheets.is_pack(new_id("sht"))


def test_pack_is_installed_once_and_does_not_disturb_app_sheets(conn):
    listed = sheets.list_sheets(conn)
    sheets.ensure_pack(conn)
    assert not sheets._have(conn)  # unwritten printed sheets do not count as already practised
    assert conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] == len(sheets.load_pack())
    assert all(s["printed"] for s in listed)
    made = sheets.make_sheet(conn, "everyday")
    assert made["number"] == 1  # numbering of sheets made in the app ignores the pack
    listed = sheets.list_sheets(conn)
    assert listed[0]["id"] == made["id"] and not listed[0]["printed"] and listed[1]["number"] == 1001



def test_a_page_of_a_printed_sheet_is_labelled_from_it(conn, store, import_bytes):
    sheet = sheets.get_sheet(conn, sheets.pack_id(1037))  # installs the pack on first use
    written = sheet["lines"][:5]
    src = import_bytes(encode(page(written, ruling=False)))[0].source_ids[0]
    out = sheets.apply_sheet(conn, store, src, sheet["id"])
    texts, _ = _texts(conn, src)
    assert texts == written and out["matched"] == 5


def test_a_line_detection_split_in_two_is_joined_back_for_the_sheet(conn, store, import_bytes):
    """A wide gap before the last words (or a stray tail) must not become a sheet line of its own."""
    from PIL import ImageDraw

    from tests.fixtures import _font

    sheet_id = _sheet(conn, LINES)
    img = page(LINES[:1] + [""] + LINES[2:], ruling=False)
    d = ImageDraw.Draw(img)
    d.text((90, 270), "pack my box with", fill=(35, 38, 60), font=_font(44))
    d.text((820, 270), "five jugs", fill=(35, 38, 60), font=_font(44))  # far to the right, past the joining distance
    src = import_bytes(encode(img))[0].source_ids[0]
    from handprinter import pages
    pages.run_detection(conn, store, src)
    assert len(_texts(conn, src)[0]) == 6  # detection alone: two pieces for line 2
    out = sheets.apply_sheet(conn, store, src, sheet_id)
    texts, rows = _texts(conn, src)
    assert texts == LINES and out["unmatched_lines"] == 0 and out["matched"] == 5
    words = [w["text"] for w in review._line_words(conn, rows[1]["id"])]
    assert words == ["pack", "my", "box", "with", "five", "jugs"]


def test_redetecting_a_sheet_page_keeps_its_sheet_text(conn, store, import_bytes, settings):
    from handprinter import jobs, worker

    sheet_id = _sheet(conn, LINES)
    src = import_bytes(encode(page(LINES, ruling=False)))[0].source_ids[0]
    sheets.apply_sheet(conn, store, src, sheet_id)
    job_id = jobs.enqueue(conn, "detect", {"source_id": src, "replace": True})
    worker.run_one(conn, store, settings, jobs.claim(conn, "general", 1))
    job = jobs.get(conn, job_id)
    assert job["status"] == "succeeded", job
    assert _texts(conn, src)[0] == LINES  # fresh boxes, the sheet's text again
