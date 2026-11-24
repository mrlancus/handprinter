"""Ready to train? The checklist reads what exists and names the next step; nothing starts on its own."""

from __future__ import annotations

from handprinter import dataset, training
from handprinter.db import transaction
from tests.helpers import detect_and_label


def test_suggested_steps_are_about_25_passes_within_bounds():
    assert training.suggested_steps(0) == training.MIN_STEPS
    assert training.suggested_steps(320) == 1000            # 25 × 320 / 8
    assert training.suggested_steps(1000) == 3150           # 3125 rounded to 50
    assert training.suggested_steps(100_000) == training.MAX_STEPS


def test_empty_collection_starts_at_approving_words(conn, store, settings):
    r = training.readiness(conn, store, settings)
    assert r["next"] == "words" and not r["ready_to_train"]
    assert [s["key"] for s in r["steps"]][:2] == ["words", "snapshot"]
    env = next(s for s in r["steps"] if s["key"] == "environment")
    assert not env["done"] and "Next:" in env["detail"]  # says exactly what blocks it


def test_ipad_pages_count_and_a_paper_only_snapshot_is_called_out(conn, store, settings, fixture_source):
    paper = fixture_source(shift=3)
    detect_and_label(conn, store, paper, approve=True)
    sid = fixture_source()
    detect_and_label(conn, store, sid, approve=True)
    with transaction(conn):
        conn.execute("UPDATE sources SET source_type = 'ipad' WHERE id = ?", (sid,))
    cov = dataset.coverage(conn)
    words = cov["exportable"].get("word", 0)
    assert cov["words_by_type"].get("ipad", 0) > 0  # iPad writing shows in Dataset coverage too

    r = training.readiness(conn, store, settings)
    assert r["approved_words"] == words and r["trainable_words"] <= words

    dataset.export_snapshot(conn, store, "paper only", "approved_only", dataset.Filters(), dataset.SplitConfig())
    snap = next(s for s in training.readiness(conn, store, settings)["steps"] if s["key"] == "snapshot")
    assert not snap["done"] and "ipad" in snap["detail"]
