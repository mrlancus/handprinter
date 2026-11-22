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
from handprinter.db import dumps, new_id, now_iso, transaction
from handprinter.geometry import Box, Homography
from handprinter.recognition import align as aligner
from handprinter.recognition.package import load_manifest, load_schema, package_dir
from handprinter.storage import Store, sha256_file

UNKNOWN_MODEL = "haiku (subagent alias)"
FLAG_MAP = {
    "blurry": "possible_blur", "glare": "possible_glare", "clipped": "clipped", "low_contrast": "low_contrast",
    "printed_text_present": "printed_text_present", "ruling_overlap": "ruling_overlap", "cannot_read": "unreadable",
    "partial_word": "partial_word", "other_author_suspected": "other_author_suspected",
}
MARK_CLASS = {"underline": "underline", "strike_through": "strike_through", "overwrite": "overwrite", "insertion": "insertion", "other": "uncertain"}


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
    summary = {"imported": 0, "already_imported": 0, "failed": 0, "pending": 0, "protected_human_edits": 0, "errors": {}}
    for check in validate_package(store, package_id):
        entry = entries.get(check.item_id)
        if entry is None:
            summary["errors"][check.item_id] = check.errors
            continue
        state = conn.execute("SELECT status FROM recognition_items WHERE package_id = ? AND item_id = ?", (package_id, check.item_id)).fetchone()
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
            proposal_id, protected = _store_result(conn, manifest, entry, check.result)
            conn.execute(
                "UPDATE recognition_items SET status = 'succeeded', error = NULL, proposal_id = ?, updated_at = ? WHERE package_id = ? AND item_id = ?",
                (proposal_id, now_iso(), package_id, check.item_id),
            )
        summary["imported"] += 1
        summary["protected_human_edits"] += protected
    remaining = conn.execute("SELECT COUNT(*) FROM recognition_items WHERE package_id = ? AND status != 'succeeded'", (package_id,)).fetchone()[0]
    conn.execute("UPDATE recognition_packages SET status = ? WHERE id = ?", ("complete" if remaining == 0 else "partial", package_id))
    return summary


def _store_result(conn: sqlite3.Connection, manifest: dict[str, Any], entry: dict[str, Any], result: dict[str, Any]) -> tuple[str, int]:
    """Record the proposal and apply it where no human decision is in the way. Returns (proposal id, protected count)."""
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

    region_id = entry.get("region_id")
    region_revision = None
    if region_id:
        row = conn.execute("SELECT revision FROM regions WHERE id = ?", (region_id,)).fetchone()
        region_revision = row["revision"] if row else None
    payload = {"text": result["transcription"], "flags": sorted(set(flags)), "reasons": reasons, "raw": result}
    proposal_id = new_id("prp")
    conn.execute(
        """INSERT INTO proposals (id, region_id, source_id, package_id, item_id, model_reported, prompt_version, schema_version,
           image_sha256, region_revision, payload, applied, stale, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (proposal_id, region_id, entry["source_id"], manifest["job_id"], entry["item_id"], model, manifest["prompt_version"],
         manifest["schema_version"], entry["image_sha256"], region_revision, dumps(payload), 0, int(stale), now_iso()),
    )
    if stale or not region_id or entry["context_type"] == "page_overview":
        return proposal_id, 0

    protected = 0
    reason = "; ".join(reasons)[:500] or None
    line_flags = set(flags)
    words = entry.get("local_boxes") or []
    if result["transcription"] and words:
        alignment = aligner.align(result["transcription"], [w["box"][2] for w in words])
        if alignment.ambiguous:
            line_flags.add("alignment_ambiguous")
        if alignment.unplaced_tokens:
            reason = ((reason + "; ") if reason else "") + "no box for: " + " ".join(alignment.unplaced_tokens)
        for assignment in alignment.boxes:
            word = words[assignment.box_index]
            word_flags = set(flags) | ({"alignment_ambiguous"} if assignment.ambiguous else set())
            applied, _ = review.machine_propose(conn, word["region_id"], assignment.text, word_flags, assignment.reason)
            protected += 0 if applied else 1
    applied, _ = review.machine_propose(conn, region_id, result["transcription"], line_flags, reason)
    protected += 0 if applied else 1
    conn.execute("UPDATE proposals SET applied = ? WHERE id = ?", (int(applied), proposal_id))

    src = dict(source)
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
    return proposal_id, protected
