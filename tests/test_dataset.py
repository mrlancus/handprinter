"""Spec §6/§8: export policy, grouped splits without leakage, unseen words, snapshot immutability."""

from __future__ import annotations

import json

from PIL import Image

from handprinter import dataset, pages, review
from handprinter.db import transaction
from handprinter.geometry import Box
from tests.fixtures import encode, page
from tests.helpers import detect_and_label


def _records(store, snapshot_id):
    root = store.dir("datasets") / snapshot_id
    words = [json.loads(line) for line in (root / "words.jsonl").read_text(encoding="utf-8").splitlines()]
    lines = [json.loads(line) for line in (root / "lines.jsonl").read_text(encoding="utf-8").splitlines()]
    return root, words, lines


def test_default_export_is_approved_only_and_excludes_flags_marks_and_other_authors(conn, store, fixture_source):
    sid = fixture_source()
    labels = detect_and_label(conn, store, sid, approve=False)
    word_ids = [k for k, v in labels.items() if " " not in v]
    approved, flagged, other, marked = word_ids[:4]
    review.human_edit(conn, approved, {"status": "approved"})
    review.human_edit(conn, other, {"status": "approved", "other_author": True})
    review.human_edit(conn, marked, {"status": "approved"})
    review.human_edit(conn, flagged, {"status": "approved"})
    with transaction(conn):
        conn.execute("UPDATE regions SET flags = '[\"geometry_stale\"]' WHERE id = ?", (flagged,))
        src = pages.get_source(conn, sid)
        mark = review.create_region(conn, src, "mark", Box(1, 1, 5, 5), actor="human", cls="strike_through")
    review.link_mark(conn, mark["id"], marked, None, False, True)
    # a machine proposal on an untouched detector region that nobody reviewed
    with transaction(conn):
        fresh = review.create_region(conn, src, "word", Box(700, 700, 60, 40), actor="detector")
        review.machine_propose(conn, fresh["id"], "machine-said", [], None)
    unreviewed = fresh["id"]

    examples, excluded = dataset.eligible_examples(conn, "approved_only", dataset.Filters())
    ids = {e.region_id for e in examples}
    assert approved in ids
    assert not ids & {flagged, other, marked, unreviewed}
    assert excluded["someone else's handwriting"] == 1

    advanced, _ = dataset.eligible_examples(conn, "include_unreviewed", dataset.Filters())
    assert unreviewed in {e.region_id for e in advanced}
    assert flagged not in {e.region_id for e in advanced}


def test_splits_group_pages_duplicates_words_and_lines(conn, store, import_bytes):
    sources = []
    for i in range(12):
        res = import_bytes(encode(page([f"page {i} one two", f"line {i} three"], variant=i + 1)), f"p{i}.png")
        sources.append(res[0].source_ids[0])
    dup = import_bytes(encode(page(["page 0 one two", "line 0 three"], variant=1), "JPEG"), "dup.jpg")[0].source_ids[0]
    for sid in sources + [dup]:
        detect_and_label(conn, store, sid)
    examples, _ = dataset.eligible_examples(conn, "approved_only", dataset.Filters())
    groups = dataset.leakage_groups(conn, examples)
    splits, warnings = dataset.assign_splits(examples, groups, dataset.SplitConfig(seed=3))
    by_source: dict[str, set[str]] = {}
    for ex in examples:
        by_source.setdefault(ex.source_id, set()).add(splits[ex.region_id])
    assert all(len(s) == 1 for s in by_source.values()), "a page's words and lines crossed a split"
    near = json.loads(conn.execute("SELECT near_duplicates FROM sources WHERE id = ?", (dup,)).fetchone()[0])
    for other in near:
        assert by_source[other] == by_source[dup], "near-duplicate photos crossed a split"
    assert set(splits.values()) == {"train", "val", "test"}


def test_unseen_words_never_appear_in_training(conn, store, fixture_source):
    for i in range(10):
        detect_and_label(conn, store, fixture_source(variant=i + 1))
    examples, _ = dataset.eligible_examples(conn, "approved_only", dataset.Filters())
    # all pages share fixture tokens (w0x0 ...), so every test word type also occurs in train
    groups = dataset.leakage_groups(conn, examples)
    cfg = dataset.SplitConfig(seed=5, unseen_word_fraction=0.5)
    splits, _ = dataset.assign_splits(examples, groups, cfg)
    vocab, dropped = dataset.unseen_words(examples, splits, cfg)
    assert vocab
    for ex in examples:
        if splits[ex.region_id] != "test" and ex.region_id not in dropped:
            tokens = ex.text.split()
            assert not set(tokens) & vocab


def test_snapshot_export_integrity_and_immutability(conn, store, fixture_source):
    sid = fixture_source()
    detect_and_label(conn, store, sid)
    result = dataset.export_snapshot(conn, store, "t", "approved_only", dataset.Filters(), dataset.SplitConfig())
    root, words, lines = _records(store, result["snapshot_id"])
    assert len(words) == result["counts"]["words"] > 0 and len(lines) == result["counts"]["lines"] > 0
    working = Image.open(store.working(sid)).convert("RGB")
    for rec in words[:3]:
        crop = Image.open(root / rec["image"])
        c = rec["crop"]
        expected = working.crop((round(c["x"]), round(c["y"]), round(c["x"]) + rec["width"], round(c["y"]) + rec["height"]))
        assert crop.size == (rec["width"], rec["height"])
        assert crop.convert("RGB").tobytes() == expected.tobytes(), "crop must come from the working image, unscaled"
        assert abs(rec["aspect"] - rec["width"] / rec["height"]) < 1e-4
        assert rec["review"] == "human_approved"
    snap = dict(conn.execute("SELECT * FROM snapshots WHERE id = ?", (result["snapshot_id"],)).fetchone())
    assert dataset.snapshot_is_intact(store, snap)
    # editing afterwards creates a new revision; the snapshot files stay untouched
    review.human_edit(conn, words[0]["id"], {"text": "changed"})
    assert dataset.snapshot_is_intact(store, snap)
    _, words_after, _ = _records(store, result["snapshot_id"])
    assert words_after[0]["text"] == words[0]["text"]


def test_too_few_groups_warns(conn, store, fixture_source):
    detect_and_label(conn, store, fixture_source())
    result = dataset.export_snapshot(conn, store, "tiny", "approved_only", dataset.Filters(), dataset.SplitConfig())
    assert any("unreliable" in w for w in result["warnings"])
