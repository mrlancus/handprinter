"""Spec §6 state model: transitions, versioned edits, undo, and human edits surviving recognition."""

from __future__ import annotations

import pytest

from handprinter import pages, review
from handprinter.db import transaction
from handprinter.geometry import Box


@pytest.fixture
def region(conn, fixture_source):
    sid = fixture_source()
    src = pages.get_source(conn, sid)
    with transaction(conn):
        return review.create_region(conn, src, "word", Box(80, 120, 90, 45), actor="detector")


def test_annotation_states_and_transitions(conn, region):
    assert region["status"] == "unprocessed"
    with pytest.raises(review.ReviewError, match="needs a transcription"):
        review.human_edit(conn, region["id"], {"status": "approved"})
    r = review.human_edit(conn, region["id"], {"text": "the"})
    r = review.human_edit(conn, region["id"], {"status": "approved"})
    assert r["status"] == "approved" and r["text_origin"] == "human"
    r = review.human_edit(conn, region["id"], {"status": "excluded"}, reason="smudged")
    assert r["status"] == "excluded" and r["review_reason"] == "smudged"
    with pytest.raises(review.ReviewError):
        review.human_edit(conn, region["id"], {"status": "proposed"})  # humans never mark machine states
    with pytest.raises(review.ReviewError):
        review.human_edit(conn, region["id"], {"status": "bogus"})


def test_approve_resolves_flags_and_defer(conn, region):
    with transaction(conn):
        review.machine_propose(conn, region["id"], "teh", ["uncertain_span"], "2nd letter unclear")
    r = review.get_region(conn, region["id"])
    assert r["status"] == "needs_review" and r["flags"] == ["uncertain_span"]
    r = review.human_edit(conn, region["id"], {"deferred": True})
    assert r["deferred"] == 1 and r["status"] == "needs_review"
    r = review.human_edit(conn, region["id"], {"text": "teh", "status": "approved"})
    assert r["flags"] == [] and r["resolved_flags"] == ["uncertain_span"] and r["deferred"] == 0


def test_every_change_is_a_revision_and_undo_restores(conn, region):
    review.human_edit(conn, region["id"], {"text": "fox"})
    r = review.human_edit(conn, region["id"], {"text": "Fox"})
    assert r["revision"] == 3
    r = review.undo(conn, region["id"])
    assert r["text"] == "fox" and r["revision"] == 4
    actors = [row["actor"] for row in conn.execute("SELECT actor FROM region_revisions WHERE region_id = ? ORDER BY revision", (region["id"],))]
    assert actors == ["detector", "human", "human", "human"]


def test_stale_revision_is_a_conflict(conn, region):
    review.human_edit(conn, region["id"], {"text": "a"})
    with pytest.raises(review.ConflictError):
        review.human_edit(conn, region["id"], {"text": "b"}, expected_revision=1)


def test_rerunning_recognition_never_overwrites_human_text(conn, region):
    review.human_edit(conn, region["id"], {"text": "becuase"})
    with transaction(conn):
        applied, r = review.machine_propose(conn, region["id"], "because", [], None)
    assert not applied and r["text"] == "becuase" and r["text_origin"] == "human"
    assert "disagreement" in r["flags"]


def test_machine_never_touches_approved_or_excluded(conn, region):
    review.human_edit(conn, region["id"], {"text": "dog", "status": "approved"})
    before = review.get_region(conn, region["id"])
    with transaction(conn):
        applied, after = review.machine_propose(conn, region["id"], "dag", ["uncertain_span"], "x")
    assert not applied and after == before


def test_explicit_apply_is_the_only_override(conn, region):
    review.human_edit(conn, region["id"], {"text": "lazy"})
    conn.execute(
        "INSERT INTO proposals (id, region_id, source_id, package_id, item_id, model_reported, prompt_version, schema_version, image_sha256, payload, created_at) "
        "VALUES ('prp_0000000000000001', ?, ?, 'rec_0000000000000001', 'i0001', 'haiku', 'v1', '1', ?, ?, 'now')",
        (region["id"], region["source_id"], "0" * 64, '{"text": "lazy?", "flags": []}'),
    )
    r = review.apply_proposal_explicitly(conn, region["id"], "prp_0000000000000001")
    assert r["text"] == "lazy?" and r["text_origin"] == "machine" and r["status"] == "needs_review"


def test_split_and_merge_keep_provenance(conn, region):
    left, right = review.split_region(conn, region["id"], 120, ("th", "e"))
    assert review.get_region(conn, region["id"])["deleted"] == 1
    assert left["w"] + right["w"] == pytest.approx(90)
    merged = review.merge_regions(conn, [left["id"], right["id"]])
    assert merged["text"] == "th e"
    actions = {row["action"] for row in conn.execute("SELECT action FROM region_revisions")}
    assert f"split_from:{region['id']}" in actions and any(a.startswith("merge_from:") for a in actions)


def test_mark_link_allows_unknown_target(conn, region):
    src = pages.get_source(conn, region["source_id"])
    with transaction(conn):
        mark = review.create_region(conn, src, "mark", Box(80, 140, 90, 6), actor="human", cls="strike_through")
    with pytest.raises(review.ReviewError):
        review.link_mark(conn, mark["id"], None, None, unknown_target=False, unknown_original=True)
    assert review.link_mark(conn, mark["id"], None, None, unknown_target=True, unknown_original=True)


def test_editing_an_approved_region_reopens_it(conn, region):
    review.human_edit(conn, region["id"], {"text": "fox", "status": "approved"})
    r = review.human_edit(conn, region["id"], {"text": "f0x"})
    assert r["status"] == "needs_review" and "after approval" in r["review_reason"]
    r = review.human_edit(conn, region["id"], {"text": "fox", "status": "approved"})
    assert r["status"] == "approved"


def test_box_edit_must_name_the_current_frame(conn, region):
    src = pages.get_source(conn, region["source_id"])
    box = {"x": 10, "y": 10, "w": 20, "h": 20}
    with pytest.raises(review.ConflictError):
        review.human_edit(conn, region["id"], {"box": box}, processing_rev=src["processing_rev"] + 1)
    r = review.human_edit(conn, region["id"], {"box": box}, processing_rev=src["processing_rev"])
    assert (r["x"], r["w"]) == (10, 20)


def test_undo_does_not_revive_a_split_region(conn, region):
    review.split_region(conn, region["id"], 120)
    with pytest.raises(review.ReviewError, match="split or merged"):
        review.undo(conn, region["id"])


def test_split_into_two_lines_moves_each_word_to_its_line(conn, store, fixture_source):
    from handprinter import pages
    from handprinter.geometry import Box
    sid = fixture_source()
    src = pages.get_source(conn, sid)
    line = review.create_region(conn, src, "line", Box(80, 100, 600, 200), actor="human")
    upper = review.create_region(conn, src, "word", Box(90, 110, 80, 60), actor="human", parent_id=line["id"])
    lower = review.create_region(conn, src, "word", Box(90, 220, 80, 60), actor="human", parent_id=line["id"])
    top, bottom = review.split_region_rows(conn, line["id"], 200)
    assert (top["y"], top["h"], bottom["y"], bottom["h"]) == (100, 100, 200, 100)
    assert review.get_region(conn, upper["id"])["parent_id"] == top["id"]
    assert review.get_region(conn, lower["id"])["parent_id"] == bottom["id"]
    assert review.get_region(conn, line["id"])["deleted"] == 1
