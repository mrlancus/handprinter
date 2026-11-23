"""Omitted areas: the user can leave diagrams, printed text and other non-handwriting out of everything.

Spec: nothing inside an omitted area is detected, sent for recognition or exported; approved work is
never silently undone; restoring puts excluded boxes back exactly as they were.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

from handprinter import dataset, pages, review
from handprinter.geometry import Box
from handprinter.recognition import package


def _words(conn, sid):
    return conn.execute("SELECT * FROM regions WHERE source_id = ? AND deleted = 0 AND kind = 'word' ORDER BY y, x", (sid,)).fetchall()


def _area_over_first_line(conn, sid):
    line = conn.execute("SELECT * FROM regions WHERE source_id = ? AND deleted = 0 AND kind = 'line' ORDER BY y LIMIT 1", (sid,)).fetchone()
    return Box(line["x"] - 2, line["y"] - 2, line["w"] + 4, line["h"] + 4), line


def test_omit_excludes_inside_keeps_approved_and_restore_is_exact(conn, store, fixture_source):
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    src = pages.get_source(conn, sid)
    area_box, line = _area_over_first_line(conn, sid)
    inside = [w for w in _words(conn, sid) if w["parent_id"] == line["id"]]
    assert len(inside) >= 2
    approved_id, open_id = inside[0]["id"], inside[1]["id"]
    review.human_edit(conn, approved_id, {"text": "kept"})
    review.human_edit(conn, approved_id, {"status": "approved"})
    review.human_edit(conn, open_id, {"text": "mine"})  # human text, still open -> becomes needs_review? stays as edited
    before_open = review.get_region(conn, open_id)

    result = pages.omit_area(conn, sid, area_box, "diagram", src["processing_rev"])
    assert result["kept_approved"] == 1
    assert review.get_region(conn, approved_id)["status"] == "approved"
    assert review.get_region(conn, open_id)["status"] == "excluded"
    assert review.get_region(conn, line["id"])["status"] == "excluded"
    outside = [w for w in _words(conn, sid) if w["parent_id"] != line["id"]]
    assert outside and all(w["status"] != "excluded" for w in outside)

    a = pages.analysis(conn, sid)
    assert a["counts"]["omitted_areas"] == 1 and a["omitted_areas"][0]["label"] == "Diagram or drawing"

    restored = pages.restore_area(conn, sid, result["area"]["id"])
    assert restored["restored"] >= 2
    after = review.get_region(conn, open_id)
    assert (after["status"], after["text"], after["review_reason"]) == (before_open["status"], "mine", before_open["review_reason"])
    assert not pages.omitted_areas(conn, sid)


def test_detection_ignores_ink_inside_omitted_areas(conn, store, fixture_source):
    sid = fixture_source()
    src = pages.get_source(conn, sid)
    pages.omit_area(conn, sid, Box(0, 0, src["processed_w"], src["processed_h"] / 2), "printed", src["processing_rev"])
    pages.run_detection(conn, store, sid)
    top = [w for w in _words(conn, sid) if w["y"] + w["h"] / 2 < src["processed_h"] / 2 - 10]
    assert top == []
    assert _words(conn, sid), "writing in the lower half is still found"


def test_omitted_areas_are_blanked_in_recognition_images_and_kept_out_of_export(conn, store, settings, fixture_source):
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    src = pages.get_source(conn, sid)
    area_box, line = _area_over_first_line(conn, sid)
    pages.omit_area(conn, sid, area_box, "other_author", src["processing_rev"])

    info = package.build(conn, store, settings, [sid], include_overview=True, force=False)
    manifest = package.load_manifest(store, info["package_id"])
    assert all(e["region_id"] != line["id"] for e in manifest["items"]), "omitted line is not sent"
    overview = next(e for e in manifest["items"] if e["context_type"] == "page_overview")
    img = np.asarray(Image.open(package.package_dir(store, info["package_id"]) / overview["image"]).convert("L"))
    t = overview["to_source"]
    x0, y0 = int((area_box.x - t["offset_x"]) / t["scale_x"]) + 2, int((area_box.y - t["offset_y"]) / t["scale_y"]) + 2
    x1, y1 = int((area_box.x + area_box.w - t["offset_x"]) / t["scale_x"]) - 2, int((area_box.y + area_box.h - t["offset_y"]) / t["scale_y"]) - 2
    patch = img[y0:y1, x0:x1]
    assert patch.size and patch.min() == patch.max(), "the omitted area is one flat fill: no ink is visible"

    examples, _ = dataset.eligible_examples(conn, "include_unreviewed", dataset.Filters())
    assert all(e["id"] not in {w["id"] for w in _words(conn, sid) if w["parent_id"] == line["id"]} for e in examples)


def test_paint_omitted_survives_areas_that_only_graze_the_image():
    from PIL import Image
    from handprinter.geometry import Box
    img = Image.new("RGB", (100, 40), (255, 255, 255))
    context = Box(10, 10, 100, 40)
    grazing = [Box(10, 49.6, 50, 5), Box(109.7, 10, 5, 20), Box(0, 0, 10.2, 10.3), Box(30, 20, 20, 0.4), Box(200, 200, 5, 5)]
    out = pages.paint_omitted(img, context, grazing)
    assert out.size == img.size
    inside = pages.paint_omitted(img, context, [Box(20, 20, 10, 10)])
    assert inside.getpixel((15, 15)) != (255, 255, 255)


def test_omit_on_all_pages_of_the_same_size(conn, store, fixture_source):
    a, b, c = fixture_source(), fixture_source(shift=7), fixture_source(shift=14)
    for sid in (a, b, c):
        pages.run_detection(conn, store, sid)
    pages.apply_processing(conn, store, c, {"rotation": 1.5})  # corrected page: same numbers, different place
    src = pages.get_source(conn, a)
    bar = Box(0, 0, 1200, 60)
    pages.omit_area(conn, a, bar, "other", src["processing_rev"])
    assert pages.omit_on_same_size_pages(conn, a, bar, "other") == 1
    assert len(pages.omitted_areas(conn, b)) == 1 and len(pages.omitted_areas(conn, c)) == 0
    assert pages.omit_on_same_size_pages(conn, a, bar, "other") == 0  # already there: not added twice


def test_page_overview_suggests_areas_to_leave_out_and_waits_for_you(conn, store, settings, fixture_source):
    import json
    from handprinter.recognition import importer, package
    sid = fixture_source(variant=3)  # has a printed block
    pages.run_detection(conn, store, sid)
    info = package.build(conn, store, settings, [sid], include_overview=True, force=False)
    entry = package.load_manifest(store, info["package_id"])["items"][0]
    assert entry["context_type"] == "page_overview"
    sx = entry["to_source"]["scale_x"]
    result = {
        "schema_version": "1", "item_id": entry["item_id"], "image_sha256": entry["image_sha256"], "model": "claude-haiku-test",
        "region_type": "mixed", "transcription": None, "uncertain_spans": [], "marks": [], "quality_flags": [], "review_reasons": [],
        "author_ambiguous": False,
        "regions": [{"box": [800 / sx, 300 / sx, 250 / sx, 240 / sx], "region_type": "diagram"},
                    {"box": [60 / sx, 20 / sx, 400 / sx, 60 / sx], "region_type": "printed_text"},
                    {"box": [90 / sx, 120 / sx, 600 / sx, 50 / sx], "region_type": "handwriting"}],
    }
    (package.package_dir(store, info["package_id"]) / "results" / f"{entry['item_id']}.json").write_text(json.dumps(result), encoding="utf-8")
    importer.import_package(conn, store, info["package_id"])
    sugg = pages.analysis(conn, sid)["suggestions"]
    assert sorted(s["reason"] for s in sugg) == ["diagram", "printed"]
    assert pages.omitted_areas(conn, sid) == []  # nothing left out until you say so
    diagram = next(s for s in sugg if s["reason"] == "diagram")
    pages.accept_suggestion(conn, sid, diagram["id"])
    pages.dismiss_suggestion(conn, sid, next(s for s in sugg if s["reason"] == "printed")["id"])
    assert len(pages.omitted_areas(conn, sid)) == 1 and pages.analysis(conn, sid)["suggestions"] == []
