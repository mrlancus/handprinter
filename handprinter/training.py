"""Ready to train? One checklist from approved words to a fine-tuned DiffusionPen, with a suggested step count.

Nothing here starts work: it only reads what exists (approvals, snapshots, the model environment, past
jobs and runs) and says which step is next and why. Experiments shows it at the top.
"""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from handprinter import dataset, evaluation
from handprinter.adapters import all_adapters
from handprinter.config import Settings
from handprinter.storage import Store

ADAPTER = "diffusionpen"
GOOD_WORDS = 1000          # the pilot target (dataset.coverage); fewer still trains, just less faithfully
EPOCHS = 25                # suggested passes over the training words
BATCH = 8                  # the runner's default batch size
MIN_STEPS, MAX_STEPS = 500, 6000


def suggested_steps(train_words: int, batch: int = BATCH) -> int:
    """About EPOCHS passes over the words, rounded to 50, within sensible bounds (a starting point, not tuned)."""
    steps = EPOCHS * train_words / max(1, batch)
    return int(min(MAX_STEPS, max(MIN_STEPS, int(steps / 50 + 0.5) * 50)))


def _step(key: str, title: str, done: bool, detail: str, link: str | None = None, blocking: bool = True) -> dict[str, Any]:
    return {"key": key, "title": title, "done": done, "detail": detail, "link": link, "blocking": blocking}


def readiness(conn: sqlite3.Connection, store: Store, settings: Settings) -> dict[str, Any]:
    adapter = all_adapters(settings.adapter_env)[ADAPTER]
    charset = adapter.capabilities().charset or ""
    examples, _ = dataset.eligible_examples(conn, "approved_only", dataset.Filters(source_types=("paper", "ipad")))
    words = [ex for ex in examples if ex.kind == "word" and not ex.maths]
    writable = [ex for ex in words if ex.text.strip() and all(c in charset for c in ex.text)]
    open_lines = conn.execute(
        "SELECT COUNT(*) FROM regions WHERE deleted = 0 AND kind = 'line' AND status IN ('unprocessed', 'proposed', 'needs_review')"
    ).fetchone()[0]
    steps = []

    steps.append(_step(
        "words", "Approve words", len(writable) >= evaluation.MIN_TRAIN_WORDS,
        f"{len(words)} approved words, {len(writable)} of them in DiffusionPen's character set "
        f"(maths and symbols such as £ or é are kept for the dataset but cannot be trained on). "
        f"At least {evaluation.MIN_TRAIN_WORDS} are needed; about {GOOD_WORDS} or more give a much closer likeness."
        + (f" {open_lines} line(s) still wait in Check." if open_lines else ""), "#/check"))

    snap = conn.execute("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 1").fetchone()
    train_words = 0
    if snap is None:
        steps.append(_step("snapshot", "Export a snapshot", False,
                           "A snapshot freezes the approved words and splits them into training, validation and test.", "#/dataset"))
    else:
        counts = json.loads(snap["counts"])
        try:
            train_words = len(evaluation.training_items(store, conn, snap["id"], charset)[0])
        except (ValueError, OSError):
            train_words = 0
        behind = len(words) - counts.get("words", 0)
        filters = json.loads(snap["filters"])
        missing_types = sorted({ex.source_type for ex in words} - set(filters.get("source_types", [])))
        detail = f"Latest: “{snap['name']}” with {counts.get('words', 0)} words, {train_words} usable for training."
        if missing_types:
            detail += f" It leaves out your {' and '.join(missing_types)} pages: tick them when you export."
        if behind > 0:
            detail += f" {behind} word(s) were approved since: export a new snapshot to include them."
        steps.append(_step("snapshot", "Export a snapshot", train_words >= evaluation.MIN_TRAIN_WORDS and behind <= 0 and not missing_types,
                           detail, "#/dataset"))

    status = adapter.status()
    steps.append(_step("environment", "Set up DiffusionPen on the PC", status.state == "ready",
                       "Ready." + (f" {status.next_step}" if status.next_step else "") if status.state == "ready"
                       else f"{'; '.join(status.blockers)}. Next: {status.next_step}", "#/experiments"))
    steps.append(_step("cuda", "Use the graphics card", bool(status.checks.get("cuda")),
                       "CUDA works: training runs on the GPU." if status.checks.get("cuda")
                       else "No CUDA GPU found by the model environment: training would run on the CPU (very slow). "
                            "Check the NVIDIA driver, then re-run SetupModels.bat.", "#/experiments", blocking=False))

    preflight = conn.execute(
        "SELECT COUNT(*) FROM jobs WHERE kind = 'adapter_preflight' AND status = 'succeeded' AND params LIKE ?", (f'%"{ADAPTER}"%',)
    ).fetchone()[0]
    steps.append(_step("preflight", "Run the memory preflight", preflight > 0,
                       "Checks that generation fits in the GPU's memory before anything long starts.", "#/experiments"))

    profiles = conn.execute("SELECT COUNT(*) FROM style_profiles").fetchone()[0]
    steps.append(_step("profile", "Create a style profile", profiles > 0,
                       "Picks which of your crops show the model your style.", "#/experiments"))

    baseline = conn.execute(
        "SELECT COUNT(*) FROM runs WHERE adapter = ? AND kind = 'baseline' AND status = 'succeeded'", (ADAPTER,)).fetchone()[0]
    steps.append(_step("baseline", "Run the baseline (before training)", baseline > 0,
                       "Shows how close the untrained model gets from your style crops alone, so you can judge what training adds.",
                       "#/experiments", blocking=False))

    tuned = [c for c in evaluation.personal_checkpoints(conn) if c["adapter"] == ADAPTER]
    steps.append(_step("finetune", "Fine-tune on your words", bool(tuned),
                       f"Done: “{tuned[0]['name']}” ({tuned[0]['train_words']} words). Choose it under Weights in Generate."
                       if tuned else (f"Suggested: {suggested_steps(train_words)} steps for {train_words} training words "
                                      f"(about {EPOCHS} passes; a starting point)." if train_words
                                      else "A suggested number of steps appears once a snapshot has training words."),
                       "#/experiments"))

    first_open = next((s for s in steps if not s["done"] and s["blocking"]), None)
    return {"steps": steps, "next": first_open["key"] if first_open else None,
            "ready_to_train": all(s["done"] for s in steps if s["blocking"] and s["key"] != "finetune"),
            "approved_words": len(words), "trainable_words": len(writable), "train_words": train_words,
            "suggested_steps": suggested_steps(train_words), "good_words": GOOD_WORDS}
