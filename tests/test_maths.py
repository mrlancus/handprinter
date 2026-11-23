"""Maths as LaTeX: recognised as LaTeX, never spread over word boxes by guesswork, always human-checked,
and exported as its own file so word-level models never see LaTeX as vocabulary."""

from __future__ import annotations

import json

from handprinter import dataset, pages, review
from handprinter.recognition import importer, package
from handprinter.recognition.align import tokenize
from tests.test_recognition import _result, _write


def test_tokenize_keeps_latex_spans_whole():
    assert tokenize("so $x + 1 = 5$ here") == ["so", "$x + 1 = 5$", "here"]
    assert tokenize(r"$\frac{3}{4} + y$") == [r"$\frac{3}{4} + y$"]
    assert tokenize("costs $5 each") == ["costs", "$5", "each"]  # a lone dollar is just a character


def test_maths_results_are_flagged_and_equations_are_not_split_into_words(conn, store, settings, fixture_source):
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    info = package.build(conn, store, settings, [sid], include_overview=False, force=False)
    manifest = package.load_manifest(store, info["package_id"])
    eq, mixed = [e for e in manifest["items"] if e["context_type"] == "line"][:2]
    _write(store, info["package_id"], eq["item_id"], _result(eq, region_type="equation", transcription=r"$\frac{3}{4} + y = 2$"))
    _write(store, info["package_id"], mixed["item_id"], _result(mixed, transcription="so $x^2 + 1$ is"))
    importer.import_package(conn, store, info["package_id"])

    line = review.get_region(conn, eq["region_id"])
    assert line["text"] == r"$\frac{3}{4} + y = 2$" and line["status"] == "needs_review" and "contains_math" in line["flags"]
    words = conn.execute("SELECT text FROM regions WHERE parent_id = ? AND deleted = 0 AND kind = 'word'", (eq["region_id"],)).fetchall()
    assert all(w["text"] is None for w in words), "LaTeX is not guessed onto word boxes"
    mixed_line = review.get_region(conn, mixed["region_id"])
    assert "contains_math" in mixed_line["flags"]


def test_approved_maths_exports_to_maths_jsonl_not_words(conn, store, fixture_source):
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    lines = conn.execute("SELECT id FROM regions WHERE source_id = ? AND kind = 'line' AND deleted = 0 ORDER BY y", (sid,)).fetchall()
    eq_id, prose_id = lines[0]["id"], lines[1]["id"]
    try:
        review.human_edit(conn, eq_id, {"cls": "equation", "status": "approved"})
        raise AssertionError("maths without LaTeX must not be approvable")
    except review.ReviewError:
        pass
    review.human_edit(conn, eq_id, {"cls": "equation", "text": r"\sqrt{2} \ne 1.4"})
    review.human_edit(conn, eq_id, {"status": "approved"})
    review.human_edit(conn, prose_id, {"text": "area $= \\pi r^2$ ok"})
    review.human_edit(conn, prose_id, {"status": "approved"})

    snap = dataset.export_snapshot(conn, store, "maths", "approved_only", dataset.Filters(), dataset.SplitConfig())
    assert snap["counts"]["maths"] == 2 and snap["counts"]["lines"] == 0
    root = store.dir("datasets") / snap["snapshot_id"]
    rows = {r["id"]: r for r in map(json.loads, (root / "maths.jsonl").read_text(encoding="utf-8").splitlines())}
    assert rows[eq_id]["format"] == "latex" and rows[eq_id]["text"] == r"\sqrt{2} \ne 1.4"
    assert rows[prose_id]["format"] == "text+inline_latex"
    assert (root / rows[eq_id]["image"]).is_file()
    assert "$" not in (root / "lines.jsonl").read_text(encoding="utf-8")

    no_maths = dataset.Filters(include_maths=False)
    examples, excluded = dataset.eligible_examples(conn, "approved_only", no_maths)
    assert not any(e.maths for e in examples) and excluded["maths not selected"] == 2
