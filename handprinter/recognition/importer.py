"""Validate recogniser results and import them as machine proposals.

Partial completion is normal: every item is validated and imported on its own. Invalid or
missing items stay pending with a reason and can be repackaged alone.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator

from handprinter import review
from handprinter.dataset import INLINE_MATH as MATH_SPAN
from handprinter.db import dumps, new_id, now_iso, transaction
from handprinter.geometry import Box, Homography
from handprinter.recognition import align as aligner
from handprinter.recognition import refit
from handprinter.recognition.package import load_manifest, load_schema, package_dir
from handprinter.storage import Store, sha256_file

UNKNOWN_MODEL = "haiku (subagent alias)"
FLAG_MAP = {
    "blurry": "possible_blur", "glare": "possible_glare", "clipped": "clipped", "low_contrast": "low_contrast",
    "printed_text_present": "printed_text_present", "ruling_overlap": "ruling_overlap", "cannot_read": "unreadable",
    "partial_word": "partial_word", "other_author_suspected": "other_author_suspected",
}
MARK_CLASS = {"underline": "underline", "strike_through": "strike_through", "overwrite": "overwrite", "insertion": "insertion", "other": "uncertain"}


class _UnsureWords:
    """Which words of a transcription contain an uncertain span, walking the words left to right."""

    def __init__(self, text: str, spans: list[dict[str, Any]]):
        self.text, self.spans, self.pos = text, [(s["start"], s["end"]) for s in spans], 0

    def __call__(self, piece: str | None) -> bool:
        if not piece or not self.spans:
            return False
        i = self.text.find(piece, self.pos)
        if i < 0:
            return True  # cannot place it: stay cautious
        self.pos = i + len(piece)
        return any(a < i + len(piece) and b > i for a, b in self.spans)


@dataclass
class ItemCheck:
    item_id: str
    ok: bool
    errors: list[str] = field(default_factory=list)
    result: dict[str, Any] | None = None


def validate_package(store: Store, package_id: str) -> list[ItemCheck]:
    """Check every manifest item's result file; never touches the database."""
    manifest = load_manifest(store, package_id)
    root = package_dir(store, package_id)
    validator = Draft202012Validator(load_schema())
    checks = []
    for entry in manifest["items"]:
        checks.append(_check_item(root, entry, validator))
    known = {e["item_id"] for e in manifest["items"]}
    for extra in sorted((root / "results").glob("*.json")):
        if extra.stem not in known:
            checks.append(ItemCheck(extra.stem, False, ["result file does not match any manifest item"]))
    return checks


def _check_item(root, entry: dict[str, Any], validator: Draft202012Validator) -> ItemCheck:
    item_id = entry["item_id"]
    path = root / "results" / f"{item_id}.json"
    if not path.is_file():
        return ItemCheck(item_id, False, ["missing result file"])
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return ItemCheck(item_id, False, [f"not valid JSON: {exc.__class__.__name__}"])
    errors = [f"{'/'.join(map(str, e.absolute_path)) or '(root)'}: {e.message}" for e in validator.iter_errors(result)]
    if errors:
        return ItemCheck(item_id, False, errors[:10])
    if result["item_id"] != item_id:
        errors.append(f"item_id {result['item_id']!r} does not match manifest item {item_id!r}")
    if result["image_sha256"] != entry["image_sha256"]:
        errors.append("image_sha256 does not match the manifest")
    image_path = root / entry["image"]
    if not image_path.is_file() or sha256_file(image_path) != entry["image_sha256"]:
        errors.append("package image is missing or was modified")
    text = result["transcription"]
    for span in result["uncertain_spans"]:
        if text is None or not span["start"] < span["end"] <= len(text):
            errors.append(f"uncertain span {span['start']}–{span['end']} lies outside the transcription")
    for mark in result["marks"]:
        if mark["box"] is not None and not _box_inside(mark["box"], entry["width"], entry["height"]):
            errors.append("a mark box lies outside the image")
    return ItemCheck(item_id, not errors, errors, result if not errors else None)


def _box_inside(box: list[float], w: int, h: int, slack: float = 0.05) -> bool:
    x, y, bw, bh = box
    return x >= -slack * w and y >= -slack * h and x + bw <= w * (1 + slack) and y + bh <= h * (1 + slack)


def import_package(conn: sqlite3.Connection, store: Store, package_id: str) -> dict[str, Any]:
    manifest = load_manifest(store, package_id)
    entries = {e["item_id"]: e for e in manifest["items"]}
    summary = {"imported": 0, "already_imported": 0, "failed": 0, "pending": 0, "protected_human_edits": 0, "lines_refit": 0, "errors": {}}
    ink = refit.LineInk(conn, store)
    for check in validate_package(store, package_id):
        entry = entries.get(check.item_id)
        if entry is None:
            summary["errors"][check.item_id] = check.errors
            continue
        state = conn.execute("SELECT status, image_sha256 FROM recognition_items WHERE package_id = ? AND item_id = ?", (package_id, check.item_id)).fetchone()
        if state is None or state["image_sha256"] != entry["image_sha256"]:
            # The manifest on disk no longer matches what the app packaged: never trust it.
            summary["failed"] += 1
            summary["errors"][check.item_id] = ["manifest entry does not match the package recorded by the app"]
            continue
        if state and state["status"] == "succeeded":
            summary["already_imported"] += 1
            continue
        if not check.ok:
            missing = check.errors == ["missing result file"]
            summary["pending" if missing else "failed"] += 1
            if not missing:
                summary["errors"][check.item_id] = check.errors
            conn.execute(
                "UPDATE recognition_items SET status = ?, error = ?, updated_at = ? WHERE package_id = ? AND item_id = ?",
                ("pending" if missing else "failed", "; ".join(check.errors)[:1000], now_iso(), package_id, check.item_id),
            )
            continue
        assert check.result is not None
        with transaction(conn):
            proposal_id, protected, was_refit = _store_result(conn, manifest, entry, check.result, ink)
            conn.execute(
                "UPDATE recognition_items SET status = 'succeeded', error = NULL, proposal_id = ?, updated_at = ? WHERE package_id = ? AND item_id = ?",
                (proposal_id, now_iso(), package_id, check.item_id),
            )
        summary["imported"] += 1
        summary["protected_human_edits"] += protected
        summary["lines_refit"] += int(was_refit)
    remaining = conn.execute("SELECT COUNT(*) FROM recognition_items WHERE package_id = ? AND status != 'succeeded'", (package_id,)).fetchone()[0]
    conn.execute("UPDATE recognition_packages SET status = ? WHERE id = ?", ("complete" if remaining == 0 else "partial", package_id))
    return summary


def _store_result(
    conn: sqlite3.Connection, manifest: dict[str, Any], entry: dict[str, Any], result: dict[str, Any], ink: refit.LineInk | None = None,
) -> tuple[str, int, bool]:
    """Record the proposal and apply it where no human decision is in the way.

    Returns (proposal id, protected count, whether the line's word boxes were re-cut to fit the text).
    """
    model = result["model"].strip() or UNKNOWN_MODEL
    to_source = Homography.scale_offset(
        entry["to_source"]["scale_x"], entry["to_source"]["scale_y"], entry["to_source"]["offset_x"], entry["to_source"]["offset_y"]
    )
    source = conn.execute("SELECT * FROM sources WHERE id = ?", (entry["source_id"],)).fetchone()
    stale = source is None or source["processing_rev"] != entry["source_frame"]["processing_rev"]
    flags = sorted({FLAG_MAP[f] for f in result["quality_flags"]})
    if result["uncertain_spans"]:
        flags.append("uncertain_span")
    if result["author_ambiguous"]:
        flags.append("other_author_suspected")
    if result["transcription"] is None and entry["context_type"] != "page_overview":
        flags.append("unreadable")
    reasons = list(result["review_reasons"]) + [f"uncertain {s['start']}–{s['end']}: {s['reason']}" for s in result["uncertain_spans"]]

    if entry["context_type"] != "page_overview" and result["region_type"] not in ("handwriting", "mixed"):
        flags.append(f"recogniser_type_{result['region_type']}")
    has_maths = result["region_type"] == "equation" or bool(result["transcription"] and MATH_SPAN.search(result["transcription"]))
    if has_maths and entry["context_type"] != "page_overview":
        flags.append("contains_math")  # LaTeX is always checked by a person
    region_id = entry.get("region_id")
    region_revision = None
    if region_id:
        row = conn.execute("SELECT revision FROM regions WHERE id = ?", (region_id,)).fetchone()
        region_revision = row["revision"] if row else None
        # The region was edited (box, split, merge, text) after packaging: the crop no longer matches.
        stale = stale or row is None or (entry.get("region_revision") is not None and region_revision != entry["region_revision"])
    payload = {"text": result["transcription"], "flags": sorted(set(flags)), "reasons": reasons, "raw": result}
    proposal_id = new_id("prp")
    conn.execute(
        """INSERT INTO proposals (id, region_id, source_id, package_id, item_id, model_reported, prompt_version, schema_version,
           image_sha256, region_revision, payload, applied, stale, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (proposal_id, region_id, entry["source_id"], manifest["job_id"], entry["item_id"], model, manifest["prompt_version"],
         manifest["schema_version"], entry["image_sha256"], region_revision, dumps(payload), 0, int(stale), now_iso()),
    )
    if stale or not region_id or entry["context_type"] == "page_overview":
        return proposal_id, 0, False

    src = dict(source)
    protected = 0
    reason = "; ".join(reasons)[:500] or None
    line_flags = set(flags)
    words = entry.get("local_boxes") or []
    text = result["transcription"]
    was_refit = False
    # A whole-maths line is one LaTeX example; spreading LaTeX over word boxes would be guesswork.
    if text and result["region_type"] != "equation":
        alignment = aligner.align(text, [w["box"][2] for w in words]) if words else None
        if ink is not None and (alignment is None or alignment.ambiguous):
            outcome = _refit_line(conn, ink, src, region_id, words, text, alignment, flags, proposal_id, result["uncertain_spans"])
            if outcome is not None:
                was_refit = True
                clear, note = outcome
                if not clear:
                    line_flags.add("alignment_ambiguous")
                reason = ((reason + "; ") if reason else "") + note
    if text and words and result["region_type"] != "equation" and not was_refit:
        assert alignment is not None
        if alignment.ambiguous:
            line_flags.add("alignment_ambiguous")
        if alignment.unplaced_tokens:
            reason = ((reason + "; ") if reason else "") + "no box for: " + " ".join(alignment.unplaced_tokens)
        unsure = _UnsureWords(text, result["uncertain_spans"])
        for assignment in alignment.boxes:
            word = words[assignment.box_index]
            current = conn.execute("SELECT revision, deleted FROM regions WHERE id = ?", (word["region_id"],)).fetchone()
            if current is None or current["deleted"] or (word.get("region_revision") is not None and current["revision"] != word["region_revision"]):
                line_flags.add("alignment_ambiguous")
                reason = ((reason + "; ") if reason else "") + "some word boxes changed after packaging; words not updated"
                protected += 1
                continue
            # The line's uncertain letters flag only the words that hold them.
            word_flags = (set(flags) - {"contains_math", "uncertain_span"}) | ({"alignment_ambiguous"} if assignment.ambiguous else set())
            if unsure(assignment.text):
                word_flags.add("uncertain_span")
            if assignment.text and MATH_SPAN.search(assignment.text):
                word_flags.add("contains_math")
            applied, _ = review.machine_propose(conn, word["region_id"], assignment.text, word_flags, assignment.reason)
            protected += 0 if applied else 1
    applied, _ = review.machine_propose(conn, region_id, result["transcription"], line_flags, reason)
    protected += 0 if applied else 1
    conn.execute("UPDATE proposals SET applied = ? WHERE id = ?", (int(applied), proposal_id))

    for mark in result["marks"]:
        if mark["box"] is None:
            continue
        box = to_source.map_box(Box(*mark["box"])).clamp(src["processed_w"], src["processed_h"])
        if box.w < 1 or box.h < 1:
            continue
        note = f"target text: {mark['target_text']}" if mark["target_text"] else "target unknown"
        review.create_region(
            conn, src, "mark", box, actor="machine", cls=MARK_CLASS[mark["type"]], parent_id=region_id,
            status="needs_review", flags=["machine_mark"] + ([] if mark["original_text_visible"] else ["original_not_visible"]),
            action=f"proposal:{proposal_id}", review_reason=note,
        )
    return proposal_id, protected, was_refit


def _untouched_machine_box(conn: sqlite3.Connection, region_id: str, packaged_revision: int | None) -> bool:
    """True if no person has drawn, moved, labelled or decided on this region."""
    row = conn.execute("SELECT * FROM regions WHERE id = ?", (region_id,)).fetchone()
    if row is None or row["deleted"] or row["created_by"] == "human" or row["text_origin"] == "human":
        return False
    if row["status"] not in ("unprocessed", "proposed", "needs_review"):
        return False
    if packaged_revision is not None and row["revision"] != packaged_revision:
        return False
    return conn.execute("SELECT 1 FROM region_revisions WHERE region_id = ? AND actor = 'human' LIMIT 1", (region_id,)).fetchone() is None


def _refit_line(
    conn: sqlite3.Connection, ink: refit.LineInk, source: dict[str, Any], line_id: str, words: list[dict[str, Any]],
    text: str, alignment: aligner.Alignment | None, flags: list[str], proposal_id: str, spans: list[dict[str, Any]] | None = None,
) -> tuple[bool, str] | None:
    """Replace a line's untouched machine word boxes with boxes cut to match the recognised words.

    Returns (every cut was clear, note for the reviewer), or None when nothing was changed: a person
    touched a box, the ink cannot be cut into that many words, or the new cut fits no better.
    """
    if not all(_untouched_machine_box(conn, w["region_id"], w.get("region_revision")) for w in words):
        return None
    line = review.get_region(conn, line_id)
    tokens = aligner.tokenize(text)
    fitted = refit.refit_words(ink, source, Box(line["x"], line["y"], line["w"], line["h"]), tokens)
    if fitted is None:
        return None
    boxes, clear = fitted
    new_alignment = aligner.align(text, [b.w for b in boxes])
    if alignment is not None and new_alignment.cost >= alignment.cost:
        return None
    clear = clear and not new_alignment.ambiguous
    for w in words:
        review._write(conn, review.get_region(conn, w["region_id"]), {"deleted": 1}, "machine", f"refit:{proposal_id}")
    base = set(flags) - {"contains_math", "uncertain_span"}
    unsure = _UnsureWords(text, spans or [])
    for box, token in zip(boxes, tokens):
        word = review.create_region(conn, source, "word", box, actor="machine", parent_id=line_id, action=f"refit:{proposal_id}")
        word_flags = base | ({"contains_math"} if MATH_SPAN.search(token) else set()) | (set() if clear else {"alignment_ambiguous"})
        if unsure(token):
            word_flags.add("uncertain_span")
        review.machine_propose(conn, word["id"], token, word_flags, None if clear else "cut from the text; check the word edges")
    # An untouched detector line grows to hold its words, so no tail or loop is cut off.
    if _untouched_machine_box(conn, line_id, None):
        grown = Box(line["x"], line["y"], line["w"], line["h"])
        for b in boxes:
            grown = grown.union(b)
        if (grown.x, grown.y, grown.w, grown.h) != (line["x"], line["y"], line["w"], line["h"]):
            review._write(conn, line, {"x": grown.x, "y": grown.y, "w": grown.w, "h": grown.h}, "machine", f"refit:{proposal_id}")
    return clear, f"word boxes re-cut to match the {len(tokens)} recognised words"
