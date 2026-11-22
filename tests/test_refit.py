"""Boxes that fit the words: tails kept, margin lines ignored, word boxes re-cut from recognised text.

Synthetic font pages, not handwriting: these test the geometry and the human-edit rules only.
"""

from __future__ import annotations

import json

import numpy as np
from PIL import Image, ImageDraw

from handprinter import detect, pages, review
from handprinter.recognition import importer, package
from handprinter.recognition.refit import segment_by_count
from tests.fixtures import _font, page


def _words(conn, line_id):
    return conn.execute("SELECT * FROM regions WHERE parent_id = ? AND deleted = 0 AND kind = 'word' ORDER BY x", (line_id,)).fetchall()


def _answer(store, pkg, entry, text):
    result = {
        "schema_version": "1", "item_id": entry["item_id"], "image_sha256": entry["image_sha256"], "model": "claude-haiku-test",
        "region_type": "handwriting", "transcription": text, "uncertain_spans": [], "marks": [], "quality_flags": [],
        "review_reasons": [], "author_ambiguous": False,
    }
    (package.package_dir(store, pkg) / "results" / f"{entry['item_id']}.json").write_text(json.dumps(result), encoding="utf-8")


def test_margin_line_is_not_a_word_and_descenders_stay_inside():
    img = page(["jumpy dog", "quiet fog"], ruling=False)
    draw = ImageDraw.Draw(img)
    draw.line([(60, 0), (72, img.height)], fill=(200, 60, 60), width=3)  # red margin rule, slightly tilted
    found = detect.detect_layout(img)
    assert len(found.lines) == 2
    for line in found.lines:
        assert all(w.x > 76 for w in line.words), "the margin rule became part of a word"
    assert any(r.h == img.height and r.w < 40 for r in found.rulings)
    # the glyph descenders of j, p, y, g reach below the baseline: the word boxes must hold them
    gray = np.asarray(img.convert("L"), dtype=np.float32)
    ink = detect.ink_mask(gray)
    ink[:, :78] = False
    for line in found.lines:
        rows = np.flatnonzero(ink[:, int(line.box.x):int(line.box.x + line.box.w)].any(axis=1))
        band = rows[(rows > line.box.y - 40) & (rows < line.box.y + line.box.h + 40)]
        assert band.max() < line.box.y + line.box.h, "a tail was cut off"


def test_segment_by_count_cuts_at_the_widest_gaps():
    mask = np.zeros((20, 100), dtype=bool)
    for x0, x1 in [(0, 10), (12, 20), (35, 45), (47, 50), (70, 90)]:  # letters; word gaps are 15 and 20
        mask[5:15, x0:x1] = True
    seg = segment_by_count(mask, [2, 2, 3])
    assert seg is not None and seg.clear
    assert [(b[0], b[2]) for b in seg.boxes] == [(0, 20), (35, 50), (70, 90)]
    assert segment_by_count(mask, [1] * 6) is None  # more words than ink pieces: leave it to a person


def test_stray_ink_at_the_end_of_a_line_is_left_out():
    mask = np.zeros((20, 400), dtype=bool)
    for x0, x1 in [(0, 10), (12, 20), (35, 45), (47, 50), (70, 90), (250, 399)]:  # three words, then a big doodle far off
        mask[5:15, x0:x1] = True
    seg = segment_by_count(mask, [2, 2, 3])
    assert seg is not None and not seg.clear  # something was dropped: a person should glance at it
    assert [(b[0], b[2]) for b in seg.boxes] == [(0, 20), (35, 50), (70, 90)]


def _built(conn, store, settings, fixture_source):
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    return sid


def test_wrong_word_boxes_are_recut_from_the_text(conn, store, settings, fixture_source):
    sid = _built(conn, store, settings, fixture_source)
    line = conn.execute("SELECT * FROM regions WHERE source_id = ? AND kind = 'line' AND deleted = 0 ORDER BY y", (sid,)).fetchone()
    src = pages.get_source(conn, sid)
    # simulate a bad detection: the four words came out as one box spanning the line
    for w in _words(conn, line["id"]):
        review._write(conn, review.get_region(conn, w["id"]), {"deleted": 1}, "detector", "test")
    from handprinter.geometry import Box
    review.create_region(conn, src, "word", Box(line["x"], line["y"], line["w"], line["h"]), actor="detector", parent_id=line["id"])
    info = package.build(conn, store, settings, [sid], include_overview=False, force=False)
    entry = next(e for e in package.load_manifest(store, info["package_id"])["items"] if e["region_id"] == line["id"])
    _answer(store, info["package_id"], entry, "the quick brown fox")
    summary = importer.import_package(conn, store, info["package_id"])
    assert summary["lines_refit"] == 1
    words = _words(conn, line["id"])
    assert [w["text"] for w in words] == ["the", "quick", "brown", "fox"]
    assert all(a["x"] + a["w"] <= b["x"] + 1 for a, b in zip(words, words[1:])), "words overlap"
    assert all(w["created_by"] == "machine" and w["text_origin"] == "machine" for w in words)


def test_a_line_with_no_word_boxes_gets_them_from_the_text(conn, store, settings, fixture_source):
    sid = _built(conn, store, settings, fixture_source)
    line = conn.execute("SELECT * FROM regions WHERE source_id = ? AND kind = 'line' AND deleted = 0 ORDER BY y", (sid,)).fetchall()[1]
    for w in _words(conn, line["id"]):
        review._write(conn, review.get_region(conn, w["id"]), {"deleted": 1}, "detector", "test")
    info = package.build(conn, store, settings, [sid], include_overview=False, force=False)
    entry = next(e for e in package.load_manifest(store, info["package_id"])["items"] if e["region_id"] == line["id"])
    _answer(store, info["package_id"], entry, "jumps over the lazy dog")
    importer.import_package(conn, store, info["package_id"])
    assert [w["text"] for w in _words(conn, line["id"])] == ["jumps", "over", "the", "lazy", "dog"]


def test_boxes_a_person_touched_are_never_recut(conn, store, settings, fixture_source):
    sid = _built(conn, store, settings, fixture_source)
    line = conn.execute("SELECT * FROM regions WHERE source_id = ? AND kind = 'line' AND deleted = 0 ORDER BY y", (sid,)).fetchone()
    first = _words(conn, line["id"])[0]
    src = pages.get_source(conn, sid)
    review.human_edit(conn, first["id"], {"box": {"x": first["x"] - 2, "y": first["y"], "w": first["w"] + 2, "h": first["h"]}}, processing_rev=src["processing_rev"])
    before = {w["id"]: (w["x"], w["w"]) for w in _words(conn, line["id"])}
    info = package.build(conn, store, settings, [sid], include_overview=False, force=False)
    entry = next(e for e in package.load_manifest(store, info["package_id"])["items"] if e["region_id"] == line["id"])
    _answer(store, info["package_id"], entry, "the quickbrown fox")  # disagrees with the boxes
    summary = importer.import_package(conn, store, info["package_id"])
    assert summary["lines_refit"] == 0
    assert {w["id"]: (w["x"], w["w"]) for w in _words(conn, line["id"])} == before
    assert "alignment_ambiguous" in review.get_region(conn, line["id"])["flags"]


def _hand_page(rows: list[tuple[int, int, str]], size=(1200, 900)) -> Image.Image:
    img = Image.new("RGB", size, (246, 243, 236))
    draw = ImageDraw.Draw(img)
    for x, y, text in rows:
        draw.text((x, y), text, fill=(35, 38, 60), font=_font(44))
    return img


def _three_then(*rows):
    return _hand_page([(90, 80, "first line here"), (90, 230, "second line here"), (90, 380, "third line here"), *rows])


def test_touching_lines_are_split_and_flagged():
    # tails of the fourth line (j, p, y, q, g) reach the capitals of the fifth: one ink band
    found = detect.detect_layout(_three_then((90, 530, "jumpy quay gyp"), (90, 578, "Hold The Bell")))
    assert len(found.lines) == 5, [line.box for line in found.lines]
    fourth, fifth = found.lines[3], found.lines[4]
    assert fourth.touches_neighbour and fifth.touches_neighbour and not fourth.may_hold_several
    assert fourth.box.y < fifth.box.y and fifth.box.y + fifth.box.h > 610


def test_overlapping_lines_that_cannot_be_cut_are_flagged_for_a_person():
    found = detect.detect_layout(_three_then((90, 530, "jumpy quay gyp"), (90, 566, "Hold The Bell")))
    assert len(found.lines) == 4 and found.lines[3].may_hold_several


def test_words_running_off_the_photo_are_flagged():
    found = detect.detect_layout(_hand_page([(90, 80, "stays inside"), (90, 230, "goes right off the page"), (90, 380, "inside again")], size=(560, 900)))
    edge_lines = [line for line in found.lines if line.words_at_edge]
    assert len(edge_lines) == 1 and edge_lines[0].box.y > 200
