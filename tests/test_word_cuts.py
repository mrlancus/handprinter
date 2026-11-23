"""Word cuts chosen by Haiku: the app numbers the gaps, an answer picks them, boxes are cut exactly there.

The answers here stand in for Haiku (written from the known geometry of a synthetic page)."""

from __future__ import annotations

import json

from handprinter import review, sheets
from handprinter.recognition import cuts
from tests.fixtures import encode, page
from tests.test_sheets import LINES, _sheet, _texts


def _page(conn, store, import_bytes):
    sheet_id = _sheet(conn, LINES)
    src = import_bytes(encode(page(LINES, ruling=False)))[0].source_ids[0]
    sheets.apply_sheet(conn, store, src, sheet_id)
    return src


def _true_cuts(conn, item):
    """The mark nearest the middle of each real gap between words (from the cut the sheet made)."""
    words = review._line_words(conn, item["line_id"])
    mids = [(a["x"] + a["w"] + b["x"]) / 2 for a, b in zip(words, words[1:])]
    return [min(range(1, item["marks"] + 1), key=lambda k: abs(item["mark_x"][k - 1] - m)) for m in mids]


def _answer(store, pkg, item, cuts_, **extra):
    path = cuts.package_dir(store, pkg) / "results" / f"{item['item_id']}.json"
    path.write_text(json.dumps({"item_id": item["item_id"], "cuts": cuts_, "unsure": False, "note": "", **extra}), encoding="utf-8")


def test_lines_are_sent_with_numbered_gaps_and_cut_where_the_answer_says(conn, store, import_bytes):
    src = _page(conn, store, import_bytes)
    before = {ln["id"]: [(round(w["x"]), round(w["x"] + w["w"])) for w in review._line_words(conn, ln["id"])] for ln in _texts(conn, src)[1]}
    built = cuts.build(conn, store, "all")
    assert built["items"] == len(LINES)
    manifest = cuts.load_manifest(store, built["package_id"])
    for item in manifest["items"]:
        assert (cuts.package_dir(store, built["package_id"]) / item["image"]).is_file()
        assert item["marks"] >= len(item["words"]) - 1
        _answer(store, built["package_id"], item, _true_cuts(conn, item))
    assert cuts.validate(store, built["package_id"]) == {}
    out = cuts.apply(conn, store, built["package_id"])
    assert out["lines_cut"] == len(LINES) and not out["failed"]
    for ln in _texts(conn, src)[1]:
        words = review._line_words(conn, ln["id"])
        assert [w["text"] for w in words] == ln["text"].split()
        after = [(round(w["x"]), round(w["x"] + w["w"])) for w in words]
        assert all(abs(a[0] - b[0]) <= 6 and abs(a[1] - b[1]) <= 6 for a, b in zip(after, before[ln["id"]]))
        assert all(w["status"] != "approved" and w["text_origin"] == "machine" for w in words)  # still waits for you


def test_touching_words_left_open_are_split_by_the_app(conn, store, import_bytes):
    _page(conn, store, import_bytes)
    built = cuts.build(conn, store, "all")
    item = cuts.load_manifest(store, built["package_id"])["items"][0]
    answer = _true_cuts(conn, item)
    answer[1] = None  # "these two touch"
    _answer(store, built["package_id"], item, answer)
    out = cuts.apply(conn, store, built["package_id"])
    assert out["lines_cut"] == 1 and out["unsure"] == 1
    words = review._line_words(conn, item["line_id"])
    assert [w["text"] for w in words] == item["words"] and all("alignment_ambiguous" in w["flags"] for w in words)


def test_unusable_answers_and_changed_lines_are_left_alone(conn, store, import_bytes):
    src = _page(conn, store, import_bytes)
    built = cuts.build(conn, store, "all")
    items = cuts.load_manifest(store, built["package_id"])["items"]
    _answer(store, built["package_id"], items[0], [1])                     # wrong number of cuts
    _answer(store, built["package_id"], items[1], list(reversed(_true_cuts(conn, items[1]))))  # out of order
    _answer(store, built["package_id"], items[2], _true_cuts(conn, items[2]))
    first_word = review._line_words(conn, items[2]["line_id"])[0]
    review.human_edit(conn, first_word["id"], {"text": "mine"})            # you touched this line meanwhile
    errors = cuts.validate(store, built["package_id"])
    assert items[0]["item_id"] in errors and items[1]["item_id"] in errors
    out = cuts.apply(conn, store, built["package_id"])
    assert out["lines_cut"] == 0 and len(out["failed"]) == 2 and any("changed" in s["why"] for s in out["skipped"])
    assert review._line_words(conn, items[2]["line_id"])[0]["text"] == "mine"


def test_without_headless_the_package_waits_for_claude_code(conn, store, settings, import_bytes):
    _page(conn, store, import_bytes)
    from dataclasses import replace
    out = cuts.haiku_cuts(conn, store, replace(settings, headless_recognition=False), "all", None, lambda f, m: None, lambda: False)
    assert out["waiting"] and out["lines"] == len(LINES)
    assert [p["id"] for p in cuts.pending(store)] == [out["package_id"]]
    assert cuts.haiku_cuts(conn, store, settings, "amber", None, lambda f, m: None, lambda: False)["lines"] == 0  # clean page: nothing amber
