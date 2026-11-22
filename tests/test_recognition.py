"""Spec §5: package → results → validate → import, with malformed, partial and mismatched cases.

Results here are hand-written fixtures, not model output; they test the plumbing only.
"""

from __future__ import annotations

import json

import pytest
from PIL import Image

from handprinter import pages, review
from handprinter.recognition import importer, package
from handprinter.storage import sha256_file


def _result(entry, **over):
    base = {
        "schema_version": "1", "item_id": entry["item_id"], "image_sha256": entry["image_sha256"], "model": "claude-haiku-test",
        "region_type": "handwriting", "transcription": None, "uncertain_spans": [], "marks": [], "quality_flags": [],
        "review_reasons": [], "author_ambiguous": False,
    }
    base.update(over)
    return base


@pytest.fixture
def built(conn, store, settings, fixture_source):
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    info = package.build(conn, store, settings, [sid], include_overview=True, force=False)
    manifest = package.load_manifest(store, info["package_id"])
    return sid, info, manifest


def _write(store, pkg_id, item_id, payload):
    path = package.package_dir(store, pkg_id) / "results" / f"{item_id}.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")


def test_package_layout_and_manifest(store, built):
    sid, info, manifest = built
    root = package.package_dir(store, info["package_id"])
    assert (root / "prompt.md").is_file() and (root / "result.schema.json").is_file() and (root / "results").is_dir()
    assert manifest["prompt_version"] == "v1" and manifest["model_configured"] == "haiku"
    kinds = [e["context_type"] for e in manifest["items"]]
    assert kinds[0] == "page_overview" and kinds.count("line") == 4
    for e in manifest["items"]:
        img = Image.open(root / e["image"])
        assert img.size == (e["width"], e["height"]) and max(img.size) <= 1568
        assert sha256_file(root / e["image"]) == e["image_sha256"]
        assert e["source_frame"]["name"] == "processed"


def test_round_trip_with_partial_malformed_and_mismatched_results(conn, store, built):
    sid, info, manifest = built
    pkg = info["package_id"]
    lines = [e for e in manifest["items"] if e["context_type"] == "line"]
    good, bad_hash, bad_schema, missing = lines
    _write(store, pkg, manifest["items"][0]["item_id"], _result(manifest["items"][0], region_type="mixed"))
    _write(store, pkg, good["item_id"], _result(good, transcription="the quick brown fox"))
    _write(store, pkg, bad_hash["item_id"], _result(bad_hash, transcription="x", image_sha256="0" * 64))
    _write(store, pkg, bad_schema["item_id"], '{"schema_version": "1", "item_id": "' + bad_schema["item_id"] + '", "oops": true')

    checks = {c.item_id: c for c in importer.validate_package(store, pkg)}
    assert checks[good["item_id"]].ok
    assert not checks[bad_hash["item_id"]].ok and "image_sha256" in checks[bad_hash["item_id"]].errors[0]
    assert checks[bad_schema["item_id"]].errors[0].startswith("not valid JSON")
    assert checks[missing["item_id"]].errors == ["missing result file"]

    summary = importer.import_package(conn, store, pkg)
    assert summary["imported"] == 2 and summary["failed"] == 2 and summary["pending"] == 1
    states = dict(conn.execute("SELECT item_id, status FROM recognition_items WHERE package_id = ?", (pkg,)).fetchall())
    assert states[good["item_id"]] == "succeeded" and states[missing["item_id"]] == "pending" and states[bad_hash["item_id"]] == "failed"
    line = review.get_region(conn, good["region_id"])
    assert line["text"] == "the quick brown fox" and line["text_origin"] == "machine" and line["status"] == "proposed"
    words = conn.execute("SELECT text FROM regions WHERE parent_id = ? AND deleted = 0 AND kind = 'word' ORDER BY x", (good["region_id"],)).fetchall()
    assert [w["text"] for w in words] == ["the", "quick", "brown", "fox"]
    prop = conn.execute("SELECT model_reported, prompt_version FROM proposals WHERE item_id = ? AND package_id = ?", (good["item_id"], pkg)).fetchone()
    assert tuple(prop) == ("claude-haiku-test", "v1")

    # the missing item arrives later: import reconciles it alone, nothing is re-imported
    _write(store, pkg, missing["item_id"], _result(missing, transcription="Meet me at 4pm", uncertain_spans=[{"start": 11, "end": 14, "reason": "4 or A?"}]))
    again = importer.import_package(conn, store, pkg)
    assert again["imported"] == 1 and again["already_imported"] == 2
    flagged = review.get_region(conn, missing["region_id"])
    assert flagged["status"] == "needs_review" and "uncertain_span" in flagged["flags"]


def test_misaligned_word_counts_are_flagged_not_zipped(conn, store, built):
    sid, info, manifest = built
    line = [e for e in manifest["items"] if e["context_type"] == "line"][0]
    _write(store, info["package_id"], line["item_id"], _result(line, transcription="thequick brown fox jumps"))
    importer.import_package(conn, store, info["package_id"])
    words = conn.execute("SELECT text, flags FROM regions WHERE parent_id = ? AND deleted = 0 AND kind = 'word' ORDER BY x", (line["region_id"],)).fetchall()
    assert any("alignment_ambiguous" in w["flags"] for w in words)
    assert "alignment_ambiguous" in review.get_region(conn, line["region_id"])["flags"]


def test_cache_skips_already_recognised_items_unless_forced(conn, store, settings, built):
    sid, info, manifest = built
    for e in manifest["items"]:
        _write(store, info["package_id"], e["item_id"], _result(e, transcription=None if e["context_type"] == "page_overview" else "x y z w"))
    importer.import_package(conn, store, info["package_id"])
    # same images, model and prompt version → nothing new to send
    again = package.build(conn, store, settings, [sid], include_overview=True, force=False)
    assert again["items"] == 0 and again["cached_skipped"] >= 1
    forced = package.build(conn, store, settings, [sid], include_overview=True, force=True)
    assert forced["items"] >= 1


def test_human_edits_survive_reimport(conn, store, built):
    sid, info, manifest = built
    line = [e for e in manifest["items"] if e["context_type"] == "line"][0]
    review.human_edit(conn, line["region_id"], {"text": "the quick brown fox", "status": "approved"})
    _write(store, info["package_id"], line["item_id"], _result(line, transcription="the quick brawn fox"))
    summary = importer.import_package(conn, store, info["package_id"])
    after = review.get_region(conn, line["region_id"])
    assert after["text"] == "the quick brown fox" and after["status"] == "approved"
    assert summary["imported"] == 1
    # the region changed after packaging, so the proposal is recorded but stale and unapplied
    prop = conn.execute("SELECT applied, stale FROM proposals WHERE region_id = ?", (line["region_id"],)).fetchone()
    assert (prop["applied"], prop["stale"]) == (0, 1)


def test_human_text_is_protected_even_when_region_unchanged(conn, store, built):
    sid, info, manifest = built
    line = [e for e in manifest["items"] if e["context_type"] == "line"][1]
    # simulate a human decision recorded without a revision bump visible to the package (direct protection path)
    conn.execute("UPDATE regions SET text = 'jumps over the lazy dog', text_origin = 'human' WHERE id = ?", (line["region_id"],))
    _write(store, info["package_id"], line["item_id"], _result(line, transcription="jumps ovar the lazy dog"))
    summary = importer.import_package(conn, store, info["package_id"])
    after = review.get_region(conn, line["region_id"])
    assert after["text"] == "jumps over the lazy dog" and "disagreement" in after["flags"]
    assert summary["protected_human_edits"] >= 1


def test_tampered_manifest_is_rejected(conn, store, built):
    sid, info, manifest = built
    root = package.package_dir(store, info["package_id"])
    entry = [e for e in manifest["items"] if e["context_type"] == "line"][0]
    _write(store, info["package_id"], entry["item_id"], _result(entry, transcription="x"))
    data = json.loads((root / "manifest.json").read_text())
    data["items"][1]["region_id"] = "reg_0000000000000000"
    data["items"][1]["image_sha256"] = "f" * 64
    (root / "manifest.json").write_text(json.dumps(data))
    summary = importer.import_package(conn, store, info["package_id"])
    assert summary["imported"] == 0


def test_marks_become_separate_regions(conn, store, built):
    sid, info, manifest = built
    line = [e for e in manifest["items"] if e["context_type"] == "line"][2]
    mark = {"type": "strike_through", "box": [10, 10, 50, 8], "target_text": "late", "original_text_visible": False}
    _write(store, info["package_id"], line["item_id"], _result(line, transcription="becuase it was late", marks=[mark]))
    importer.import_package(conn, store, info["package_id"])
    marks = conn.execute("SELECT * FROM regions WHERE kind = 'mark' AND parent_id = ?", (line["region_id"],)).fetchall()
    assert len(marks) == 1 and marks[0]["cls"] == "strike_through" and marks[0]["status"] == "needs_review"
    s = line["to_source"]
    assert marks[0]["x"] == pytest.approx(s["offset_x"] + 10 * s["scale_x"], abs=0.01)
