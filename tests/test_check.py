"""Quick check: approve a whole line in one step, without ever overwriting decisions silently."""

from __future__ import annotations

import json

import pytest

from handprinter import pages, review
from handprinter.recognition import importer, package


@pytest.fixture
def page_lines(conn, store, fixture_source):
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    return sid, review.check_lines(conn, sid)


def test_approve_line_takes_the_confirmed_texts_and_approves_everything(conn, page_lines):
    sid, lines = page_lines
    item = lines[0]
    words = item["words"]
    texts = {w["id"]: t for w, t in zip(words, ["the", "quick", "brown", "fox"])}
    out = review.approve_line(conn, item["line"]["id"], texts)
    assert out["line"]["status"] == "approved" and out["line"]["text"] == "the quick brown fox"
    assert [w["status"] for w in out["words"]] == ["approved"] * 4
    assert all(w["text_origin"] == "human" for w in out["words"])
    assert item["line"]["id"] not in {x["line"]["id"] for x in review.check_lines(conn, sid)}


def test_an_emptied_word_is_left_out_not_approved(conn, page_lines):
    _, lines = page_lines
    item = lines[0]
    w = item["words"]
    out = review.approve_line(conn, item["line"]["id"], {w[0].get("id"): "the", w[1]["id"]: "quick", w[2]["id"]: "  ", w[3]["id"]: "fox"})
    assert [x["status"] for x in out["words"]] == ["approved", "approved", "excluded", "approved"]
    assert out["line"]["text"] == "the quick fox"


def test_nothing_changes_if_the_line_cannot_be_approved(conn, page_lines):
    _, lines = page_lines
    item = lines[0]
    w = item["words"]
    review.human_edit(conn, item["line"]["id"], {"cls": "equation"})
    # every word emptied: the words would be left out, then the maths line has no LaTeX and cannot be approved
    with pytest.raises(review.ReviewError):
        review.approve_line(conn, item["line"]["id"], {x["id"]: "" for x in w})
    assert all(review.get_region(conn, x["id"])["status"] == "unprocessed" for x in w)


def test_exclude_line_leaves_out_its_words(conn, page_lines):
    _, lines = page_lines
    item = lines[1]
    out = review.exclude_line(conn, item["line"]["id"])
    assert out["line"]["status"] == "excluded" and all(w["status"] == "excluded" for w in out["words"])


def test_uncertain_letters_flag_only_their_word(conn, store, settings, page_lines):
    sid, lines = page_lines
    info = package.build(conn, store, settings, [sid], include_overview=False, force=False)
    entry = next(e for e in package.load_manifest(store, info["package_id"])["items"] if e["region_id"] == lines[0]["line"]["id"])
    result = {
        "schema_version": "1", "item_id": entry["item_id"], "image_sha256": entry["image_sha256"], "model": "claude-haiku-test",
        "region_type": "handwriting", "transcription": "the quick brown fox", "marks": [], "quality_flags": [],
        "uncertain_spans": [{"start": 10, "end": 11, "reason": "b or h?"}], "review_reasons": [], "author_ambiguous": False,
    }
    (package.package_dir(store, info["package_id"]) / "results" / f"{entry['item_id']}.json").write_text(json.dumps(result), encoding="utf-8")
    importer.import_package(conn, store, info["package_id"])
    words = review.check_lines(conn, sid)[0]["words"]
    assert [w["text"] for w in words] == ["the", "quick", "brown", "fox"]
    assert ["uncertain_span" in w["flags"] for w in words] == [False, False, True, False]


def test_recut_line_follows_the_words_the_person_typed(conn, store, page_lines):
    from handprinter.recognition import refit
    sid, lines = page_lines
    item = lines[2]  # "becuase it was late"
    first = item["words"][0]
    review.human_edit(conn, first["id"], {"text": "becuase"})  # a human touch must not block an explicit re-cut
    out = refit.recut_line(conn, store, item["line"]["id"], "becuase it was late")
    assert out["words"] == 4
    words = review.check_lines(conn, sid)[2]["words"]
    assert [w["text"] for w in words] == ["becuase", "it", "was", "late"]
    assert all(w["text_origin"] == "human" and w["status"] == "needs_review" for w in words)
    assert all(a["x"] + a["w"] <= b["x"] + 1 for a, b in zip(words, words[1:]))
    with pytest.raises(review.ReviewError):
        refit.recut_line(conn, store, item["line"]["id"], "b e c u a s e i t w a s l a t e x y z")  # more words than ink pieces
