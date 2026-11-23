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
    found = detect.detect_layout(_three_then((90, 530, "jumpy quay gyp"), (90, 576, "Hold The Bell")))
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


def test_columns_are_found_before_lines():
    """Dense two-column notes with a heading and a boxed diagram: no line may run across columns."""
    img = Image.new("RGB", (1600, 1100), (250, 250, 247))
    draw = ImageDraw.Draw(img)
    small = _font(22)
    draw.text((60, 30), "Acids and alkalis", fill=(30, 30, 40), font=_font(34))
    left = ["An acid is a species that", "when dissolved in water", "forms an aqueous solution", "that will release H+ ions.",
            "A common acid is HCl", "Bases and alkalis react", "with an acid and accept", "H+ ions in neutralisation"]
    right = ["Water of crystallisation", "a crystalline compound", "that contains water", "molecules is hydrated",
             "Thermal decomposition of", "salts can occur on heating", "MgSO4 gives MgO and gas"]
    for i, t in enumerate(left):
        draw.text((60, 110 + i * 34), t, fill=(30, 30, 40), font=small)
    for i, t in enumerate(right):
        draw.text((860, 104 + i * 34), t, fill=(30, 30, 40), font=small)  # slightly offset baselines
    draw.rectangle([860, 520, 1300, 760], outline=(30, 30, 40), width=3)  # a diagram box
    found = detect.detect_layout(img)
    texty = [line for line in found.lines if line.box.y < 450]
    assert len(texty) == 1 + len(left) + len(right), [(round(line.box.x), round(line.box.y)) for line in texty]
    for line in texty:
        assert line.box.x + line.box.w < 820 or line.box.x > 820, f"line runs across the gutter: {line.box}"
        assert line.box.h < 60, f"line box holds several lines: {line.box}"


def test_app_toolbar_on_a_dark_bar_is_not_writing():
    img = page(["first line here", "second line here", "third line here"], ruling=False)
    draw = ImageDraw.Draw(img)
    draw.rectangle([0, 0, img.width, 70], fill=(40, 60, 110))  # an app's dark title bar
    for x in range(40, 1100, 120):  # with icons and tab labels on it
        draw.rectangle([x, 20, x + 40, 50], outline=(200, 210, 230), width=3)
        draw.text((x + 50, 22), "Tab", fill=(220, 225, 235), font=_font(22))
    found = detect.detect_layout(img)
    assert all(line.box.y > 70 for line in found.lines), [line.box for line in found.lines]
    assert len(found.lines) == 3


def test_sloping_close_lines_in_two_columns_stay_one_line_each():
    """Like real notes: two columns, lines rising to the right, spaced-out words, lines close together."""
    page_img = Image.new("RGB", (1800, 900), (255, 255, 255))
    left = ["An acid is a species that when dissolved", "to form an aqueous solution will release", "Bases and alkalis are species that react",
            "in a neutralisation reaction and accept", "An alkali is a water soluble base that"]
    right = ["Water of crystallisation in a compound", "thermal decomposition of salts can occur", "this cannot be used for the reaction"]
    font = _font(30)

    def sloped(text: str) -> Image.Image:
        words = text.split()
        strip = Image.new("L", (900, 120), 0)
        d = ImageDraw.Draw(strip)
        x = 10
        for wd in words:
            d.text((x, 40), wd, fill=255, font=font)
            x += int(d.textlength(wd, font=font)) + 34  # wide gaps between words, as in spaced-out writing
        return strip.rotate(3, resample=Image.BICUBIC, expand=False)  # rising to the right

    for col_x, lines_ in ((40, left), (980, right)):
        for i, text in enumerate(lines_):
            strip = sloped(text)
            page_img.paste((30, 30, 40), (col_x, 60 + i * 56), strip)  # 56 px apart: tails nearly touch
    found = detect.detect_layout(page_img)
    words_per_line = sorted(len(line.words) for line in found.lines)
    assert len(found.lines) == len(left) + len(right), [(round(l.box.x), round(l.box.y), len(l.words)) for l in found.lines]
    for line in found.lines:
        assert line.box.x + line.box.w < 960 or line.box.x > 960, f"line runs across columns: {line.box}"
    assert words_per_line == sorted(len(t.split()) for t in left + right)


def test_specks_are_not_lines_and_sideways_writing_is_not_sent_for_reading():
    img = _hand_page([(90, 80, "first line here"), (90, 230, "second line here"), (90, 380, "third line here")], size=(1200, 700))
    draw = ImageDraw.Draw(img)
    for x, y in ((700, 150), (900, 320), (1000, 500)):  # stray dots and specks
        draw.ellipse([x, y, x + 6, y + 6], fill=(30, 30, 40))
    side = Image.new("L", (300, 60), 0)
    ImageDraw.Draw(side).text((5, 5), "margin note", fill=255, font=_font(36))
    img.paste((30, 30, 40), (1100, 150), side.rotate(90, expand=True))  # written up the margin
    found = detect.detect_layout(img)
    readable = [line for line in found.lines if not line.not_a_line]
    assert len(readable) == 3, [line.box for line in found.lines]
    assert any(line.not_a_line and line.box.x > 1090 for line in found.lines)


def test_a_big_heading_and_small_notes_are_each_read_at_their_own_size():
    img = Image.new("RGB", (1400, 900), (255, 255, 255))
    draw = ImageDraw.Draw(img)
    draw.text((60, 30), "Acids and Bases", fill=(30, 30, 40), font=_font(90))  # big heading
    small = ["an acid releases H+ ions in water", "a base accepts H+ ions from acids", "an alkali is a soluble base",
             "salts form when acids react", "with bases or with metals", "and carbonates too"]
    for i, t in enumerate(small):
        draw.text((60, 200 + i * 38), t, fill=(30, 30, 40), font=_font(26))  # small, tight notes
    found = detect.detect_layout(img)
    assert len(found.lines) == 1 + len(small), [(round(l.box.y), len(l.words)) for l in found.lines]
    heading = found.lines[0]
    assert len(heading.words) == 3 and heading.box.h > 60
    assert sorted(len(l.words) for l in found.lines[1:]) == sorted(len(t.split()) for t in small)


def test_a_faint_ruled_line_through_the_writing_does_not_glue_words_together():
    """iPad ruled paper: a faint, slightly sloped line runs through the letters (seen on a real GoodNotes page)."""
    from PIL import Image, ImageDraw

    from handprinter import detect
    from tests.fixtures import _font

    words = ["Twelve", "boxing", "wizards", "jumped", "over", "the", "fence"]
    img = Image.new("L", (1500, 110), 250)
    d = ImageDraw.Draw(img)
    x = 20
    for w in words:
        d.text((x, 20), w, fill=30, font=_font(44))
        x += int(d.textlength(w, font=_font(44))) + 45
    d.line([(0, 62), (1500, 64)], fill=200, width=2)  # faint ruling across the whole line, 2 px drop over the width
    mask = detect.ink_mask(np.asarray(img, dtype=np.float32))
    mask[detect.ruled_rows(mask, 0.6)] = False
    glued = segment_by_count(mask, [len(w) for w in words])
    cleaned = mask & ~detect.thin_rule_pixels(mask)
    seg = segment_by_count(cleaned, [len(w) for w in words])
    assert seg is not None and seg.clear and len(seg.boxes) == len(words)
    assert glued is None or not glued.clear  # without the filter the words are glued by the line


def test_thin_rule_filter_leaves_plain_writing_alone():
    from PIL import Image

    from handprinter import detect
    from tests.fixtures import page

    img = page(["the quick brown fox jumps over", "minimum illumination"], ruling=False).convert("L")
    gray = np.asarray(img, dtype=np.float32)
    for y0 in (110, 260):
        m = detect.ink_mask(gray[y0:y0 + 80, 60:1150])
        assert not detect.thin_rule_pixels(m).any()


def test_a_dot_inside_a_word_does_not_cut_the_word_short():
    from handprinter import detect

    # pieces sorted by left edge: a long word, then the dot of an i inside it, then the next word
    assert detect._merge_runs([(0, 100), (40, 45), (120, 130)], max_gap=5) == [(0, 100), (120, 130)]


def test_maths_is_measured_as_written_not_as_latex():
    from handprinter.recognition.refit import is_spaced, written_len

    assert written_len("$\\alpha$,") == 2           # α and the comma, not nine LaTeX characters
    assert written_len("$H^+$") == 1.5              # a power is written small
    assert written_len("$\\lambda = \\frac{h}{mv}$") == 6  # λ, a spaced =, a fraction as wide as "mv"
    assert is_spaced("$x = 1$") and is_spaced("$\\frac{a}{b}$") and not is_spaced("$H^+$") and not is_spaced("word")


def test_a_spaced_maths_word_is_not_split_at_its_own_spaces():
    from handprinter.recognition.refit import segment_by_count

    # "λ  =   h/mv   (de  Broglie)": the widest gaps are inside the maths
    mask = np.zeros((30, 400), dtype=bool)
    for x0, x1 in [(0, 30), (40, 60), (95, 150), (178, 186), (196, 232), (250, 370)]:
        mask[8:22, x0:x1] = True
    seg = segment_by_count(mask, [6, 3, 8], [True, False, False])
    assert seg is not None and [(b[0], b[2]) for b in seg.boxes] == [(0, 150), (178, 232), (250, 370)]


def test_tails_of_neighbouring_lines_are_not_part_of_a_line():
    from handprinter.recognition.refit import own_ink

    mask = np.zeros((90, 300), dtype=bool)
    mask[35:55, 10:290] = True    # the line's letters
    mask[0:15, 100:104] = True    # a descender of the line above, cut by the box edge
    mask[80:90, 200:204] = True   # an ascender of the line below, cut by the box edge
    mask[25:29, 50:54] = True     # a dot over an i
    mask[60:75, 150:154] = True   # the line's own tail written as a separate stroke
    kept = own_ink(mask)
    assert not kept[0:15].any() and not kept[80:90].any()
    assert kept[25:29, 50:54].all() and kept[60:75, 150:154].all() and kept[35:55].sum() == mask[35:55].sum()


def test_a_ruled_line_along_the_foot_of_the_letters_is_removed_even_with_short_word_gaps():
    from handprinter import detect

    h, w = 60, 1000
    mask = np.zeros((h, w), dtype=bool)
    gaps = [38, 25, 25, 38, 25, 38, 25]  # only three gaps long enough to count on their own
    x, words = 10, []
    for g in gaps + [0]:
        words.append((x, x + 90))
        for sx in range(x, x + 90, 10):
            mask[20:46, sx:sx + 3] = True  # letter strokes down to the baseline
        x += 90 + g
    mask[44:46, 10:x - 0] = True  # the ruled line, through letters and gaps
    cleaned = mask & ~detect.thin_rule_pixels(mask)
    for (_a, b), (c, _d) in zip(words, words[1:]):
        assert not cleaned[:, b + 1:c - 1].any()  # every word gap is open again
    assert cleaned[20:44].sum() == mask[20:44].sum()  # the letters above the rule are untouched


def test_a_tail_split_off_by_the_ruled_line_stays_with_its_word():
    from handprinter.recognition.refit import own_ink

    mask = np.zeros((70, 300), dtype=bool)
    mask[30:50, 10:290] = True    # the line's letters, ending on the ruled line
    mask[51:70, 120:124] = True   # a y's tail just below, cut by the box edge
    mask[0:12, 200:204] = True    # a descender of the line above, cut by the top edge
    kept = own_ink(mask)
    assert kept[51:70, 120:124].all() and not kept[0:12].any()


def test_set_symbols_are_spaced_operators():
    from handprinter.recognition.refit import is_spaced, written_len

    assert is_spaced("$A \\cup B$,") and written_len("$A \\cup B$,") == 5
    assert not is_spaced("$\\infty$")


def test_a_ruled_line_is_found_in_a_tall_line_box_too():
    """A tail or fraction makes the box tall; the gaps between words are measured against the letters."""
    from handprinter import detect

    h, w = 110, 1000
    mask = np.zeros((h, w), dtype=bool)
    x, words = 10, []
    for g in [40, 34, 45, 36, 50, 38, 0]:
        words.append((x, x + 100))
        for sx in range(x, x + 100, 10):
            mask[40:62, sx:sx + 3] = True  # letters about 22 px tall
        x += 100 + g
    mask[60:62, 10:x] = True  # the ruled line at the foot of the letters
    mask[62:105, 300:304] = True  # one long tail makes the box tall
    cleaned = mask & ~detect.thin_rule_pixels(mask)
    for (_a, b), (c, _d) in zip(words, words[1:]):
        assert not cleaned[:, b + 1:c - 1].any()


def test_an_explicit_latex_space_keeps_the_maths_together():
    from handprinter.recognition.refit import is_spaced, written_len

    assert is_spaced("$\\forall x \\, \\exists y$") and written_len("$\\forall x \\, \\exists y$") == 5
