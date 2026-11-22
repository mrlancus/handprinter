"""Region annotation: the review state machine, versioned edits, undo, split/merge and marks."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from handprinter.db import dumps, new_id, now_iso, row_dict, transaction
from handprinter.geometry import Box

STATUSES = ("unprocessed", "proposed", "needs_review", "approved", "excluded")
KINDS = ("line", "word", "block", "mark")
CLASSES = (
    "handwriting", "printed_text", "printed_ruling", "underline", "strike_through",
    "overwrite", "insertion", "diagram", "equation", "uncertain", "omitted",
)
MARK_CLASSES = ("underline", "strike_through", "overwrite", "insertion")

# Human transitions. Machine transitions are narrower (see MACHINE_TRANSITIONS).
TRANSITIONS: dict[str, set[str]] = {
    "unprocessed": {"proposed", "needs_review", "approved", "excluded"},
    "proposed": {"needs_review", "approved", "excluded"},
    "needs_review": {"approved", "excluded"},
    "approved": {"needs_review", "excluded"},
    "excluded": {"needs_review", "approved"},
}
MACHINE_TRANSITIONS: dict[str, set[str]] = {
    "unprocessed": {"proposed", "needs_review"},
    "proposed": {"proposed", "needs_review"},
    "needs_review": {"needs_review"},
}

JSON_FIELDS = ("flags", "resolved_flags")
SNAPSHOT_FIELDS = (
    "parent_id", "kind", "cls", "frame", "frame_w", "frame_h", "processing_rev", "x", "y", "w", "h",
    "status", "deferred", "text", "text_origin", "flags", "resolved_flags", "review_reason",
    "other_author", "approved_revision", "deleted",
)


class ReviewError(ValueError):
    pass


class ConflictError(ReviewError):
    pass


def get_region(conn: sqlite3.Connection, region_id: str) -> dict[str, Any]:
    region = row_dict(conn.execute("SELECT * FROM regions WHERE id = ?", (region_id,)).fetchone(), JSON_FIELDS)
    if region is None:
        raise ReviewError(f"region {region_id} not found")
    return region


def _snapshot(region: dict[str, Any]) -> dict[str, Any]:
    return {k: region[k] for k in SNAPSHOT_FIELDS}


def _record(conn: sqlite3.Connection, region: dict[str, Any], actor: str, action: str) -> None:
    conn.execute(
        "INSERT INTO region_revisions (region_id, revision, actor, action, data, created_at) VALUES (?,?,?,?,?,?)",
        (region["id"], region["revision"], actor, action, dumps(_snapshot(region)), now_iso()),
    )


def check_frame(source: dict[str, Any], processing_rev: int | None) -> None:
    if processing_rev != source["processing_rev"]:
        raise ConflictError("the page was re-processed since you loaded it; reload before drawing")


def create_region(
    conn: sqlite3.Connection,
    source: dict[str, Any],
    kind: str,
    box: Box,
    *,
    actor: str,
    cls: str = "handwriting",
    parent_id: str | None = None,
    text: str | None = None,
    status: str = "unprocessed",
    flags: Iterable[str] = (),
    action: str = "create",
    review_reason: str | None = None,
) -> dict[str, Any]:
    if kind not in KINDS:
        raise ReviewError(f"unknown region kind {kind!r}")
    if cls not in CLASSES:
        raise ReviewError(f"unknown class {cls!r}")
    box = _valid_box(box, source["processed_w"], source["processed_h"])
    ts = now_iso()
    region = {
        "id": new_id("reg"), "source_id": source["id"], "parent_id": parent_id, "kind": kind, "cls": cls,
        "frame": "processed", "frame_w": source["processed_w"], "frame_h": source["processed_h"],
        "processing_rev": source["processing_rev"], "x": box.x, "y": box.y, "w": box.w, "h": box.h,
        "status": status, "deferred": 0, "text": text, "text_origin": ("human" if actor == "human" and text is not None else None),
        "flags": sorted(set(flags)), "resolved_flags": [], "review_reason": review_reason, "other_author": 0,
        "created_by": actor, "revision": 1, "approved_revision": None, "created_at": ts, "updated_at": ts, "deleted": 0,
    }
    cols = list(region)
    conn.execute(
        f"INSERT INTO regions ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
        [dumps(region[c]) if c in JSON_FIELDS else region[c] for c in cols],
    )
    _record(conn, region, actor, action)
    return region


def _valid_box(box: Box, frame_w: int, frame_h: int) -> Box:
    clamped = box.clamp(frame_w, frame_h)
    if clamped.w < 1 or clamped.h < 1:
        raise ReviewError("box lies outside the page or is empty")
    return clamped


def _write(conn: sqlite3.Connection, region: dict[str, Any], changes: dict[str, Any], actor: str, action: str) -> dict[str, Any]:
    updated = {**region, **changes, "revision": region["revision"] + 1, "updated_at": now_iso()}
    sets = list(changes) + ["revision", "updated_at"]
    conn.execute(
        f"UPDATE regions SET {', '.join(f'{c} = ?' for c in sets)} WHERE id = ? AND revision = ?",
        [dumps(updated[c]) if c in JSON_FIELDS else updated[c] for c in sets] + [region["id"], region["revision"]],
    )
    _record(conn, updated, actor, action)
    return updated


def _load_for_edit(conn: sqlite3.Connection, region_id: str, expected_revision: int | None) -> dict[str, Any]:
    region = get_region(conn, region_id)
    if region["deleted"]:
        raise ReviewError("region was deleted")
    if expected_revision is not None and expected_revision != region["revision"]:
        raise ConflictError(f"region changed since you loaded it (revision {region['revision']}, you had {expected_revision})")
    return region


def human_edit(
    conn: sqlite3.Connection, region_id: str, changes: dict[str, Any], expected_revision: int | None = None, reason: str | None = None,
    processing_rev: int | None = None,
) -> dict[str, Any]:
    """Apply a human edit: text, box, class, kind, other-author flag, deferral or status change.

    A box must say which processing revision its coordinates belong to, so a client still
    showing an older frame cannot silently misplace it. Changing the text or box of an
    approved region sends it back to review unless the same edit re-approves it.
    """
    with transaction(conn):
        region = _load_for_edit(conn, region_id, expected_revision)
        source = conn.execute("SELECT processed_w, processed_h, processing_rev FROM sources WHERE id = ?", (region["source_id"],)).fetchone()
        if "box" in changes and processing_rev != source["processing_rev"]:
            raise ConflictError("the page was re-processed since you loaded it; reload before moving boxes")
        out: dict[str, Any] = {}
        actions = []
        if "text" in changes:
            text = changes["text"]
            if text is not None and not isinstance(text, str):
                raise ReviewError("text must be a string or null")
            out["text"], out["text_origin"] = text, "human"
            actions.append("edit_text")
        if "box" in changes:
            b = changes["box"]
            box = _valid_box(Box(float(b["x"]), float(b["y"]), float(b["w"]), float(b["h"])), source["processed_w"], source["processed_h"])
            out.update(x=box.x, y=box.y, w=box.w, h=box.h)
            flags = [f for f in region["flags"] if f != "geometry_stale"]
            if flags != region["flags"]:
                out["flags"] = flags
            actions.append("edit_box")
            _mark_proposals_stale(conn, region_id)
        if "cls" in changes:
            if changes["cls"] not in CLASSES:
                raise ReviewError(f"unknown class {changes['cls']!r}")
            out["cls"] = changes["cls"]
            actions.append("classify")
        if "kind" in changes:
            if changes["kind"] not in KINDS:
                raise ReviewError(f"unknown kind {changes['kind']!r}")
            out["kind"] = changes["kind"]
            actions.append("set_kind")
        if "other_author" in changes:
            out["other_author"] = 1 if changes["other_author"] else 0
            actions.append("other_author")
        if "deferred" in changes:
            out["deferred"] = 1 if changes["deferred"] else 0
            if out["deferred"] and region["status"] not in ("needs_review",):
                if "needs_review" not in TRANSITIONS[region["status"]]:
                    raise ReviewError(f"cannot defer a {region['status']} region")
                out["status"] = "needs_review"
            actions.append("defer" if out["deferred"] else "undefer")
        if "status" in changes:
            out.update(_status_change({**region, **out}, changes["status"], reason))
            actions.append(f"status:{changes['status']}")
        elif region["status"] == "approved" and (("text" in out and out["text"] != region["text"]) or "x" in out):
            out.update(status="needs_review", review_reason="edited after approval; approve again to keep it")
            actions.append("reopened_by_edit")
        if reason is not None and "review_reason" not in out:
            out["review_reason"] = reason
        if not out:
            raise ReviewError("no changes given")
        return _write(conn, region, out, "human", "+".join(actions))


def _status_change(region: dict[str, Any], status: str, reason: str | None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ReviewError(f"unknown status {status!r}")
    if status == region["status"]:
        return {}
    if status not in TRANSITIONS[region["status"]]:
        raise ReviewError(f"cannot move from {region['status']} to {status}")
    out: dict[str, Any] = {"status": status, "review_reason": reason, "deferred": 0}
    if status == "approved":
        if region["kind"] in ("line", "word") and region["cls"] == "handwriting" and not region["text"]:
            raise ReviewError("an approved handwriting example needs a transcription (use exclude or defer otherwise)")
        if region["cls"] == "equation" and not region["text"]:
            raise ReviewError("approved maths needs its LaTeX (for example \\frac{1}{2})")
        out["resolved_flags"] = sorted(set(region["resolved_flags"]) | set(region["flags"]))
        out["flags"] = []
        out["approved_revision"] = region["revision"] + 1
        if region["text"] is not None and region["text_origin"] is None:
            out["text_origin"] = "human"
    return out


def machine_propose(
    conn: sqlite3.Connection, region_id: str, text: str | None, flags: Iterable[str], reason: str | None
) -> tuple[bool, dict[str, Any]]:
    """Apply a recogniser proposal if (and only if) no human decision would be overwritten.

    Returns (applied, region). Callers run this inside a transaction.
    """
    region = get_region(conn, region_id)
    flags = sorted(set(flags))
    protected = region["deleted"] or region["text_origin"] == "human" or region["status"] not in MACHINE_TRANSITIONS
    if protected:
        # The proposal is stored unapplied. Open (unapproved) regions get a disagreement flag;
        # approved/excluded regions are left exactly as the human decided.
        open_for_review = not region["deleted"] and region["status"] not in ("approved", "excluded")
        if open_for_review and text != region["text"] and "disagreement" not in region["flags"]:
            region = _write(conn, region, {"flags": sorted(set(region["flags"]) | {"disagreement"})}, "machine", "proposal_conflict")
        return False, region
    status = "needs_review" if flags or text is None else "proposed"
    if region["status"] == "needs_review":
        status = "needs_review"
    changes = {"text": text, "text_origin": "machine", "flags": sorted(set(region["flags"]) | set(flags)), "status": status, "review_reason": reason}
    return True, _write(conn, region, changes, "machine", "proposal")


def apply_proposal_explicitly(conn: sqlite3.Connection, region_id: str, proposal_id: str, expected_revision: int | None = None) -> dict[str, Any]:
    """The explicit user action that lets a machine proposal replace a human edit."""
    with transaction(conn):
        region = _load_for_edit(conn, region_id, expected_revision)
        proposal = conn.execute("SELECT * FROM proposals WHERE id = ? AND region_id = ?", (proposal_id, region_id)).fetchone()
        if proposal is None:
            raise ReviewError("proposal not found for this region")
        if proposal["stale"]:
            raise ReviewError("proposal is stale (the crop changed since it was made)")
        payload = json.loads(proposal["payload"])
        changes = {
            "text": payload.get("text"), "text_origin": "machine", "status": "needs_review", "deferred": 0,
            "flags": sorted((set(region["flags"]) - {"disagreement"}) | set(payload.get("flags", []))),
            "review_reason": "machine proposal applied by user",
        }
        conn.execute("UPDATE proposals SET applied = 1 WHERE id = ?", (proposal_id,))
        return _write(conn, region, changes, "human", "apply_proposal")


def undo(conn: sqlite3.Connection, region_id: str, expected_revision: int | None = None) -> dict[str, Any]:
    """Restore the previous revision's fields as a new revision (history is never rewritten)."""
    with transaction(conn):
        region = _load_for_edit(conn, region_id, expected_revision) if expected_revision is not None else get_region(conn, region_id)
        prev = conn.execute(
            "SELECT data FROM region_revisions WHERE region_id = ? AND revision < ? ORDER BY revision DESC LIMIT 1",
            (region_id, region["revision"]),
        ).fetchone()
        if prev is None:
            raise ReviewError("nothing to undo")
        last = conn.execute("SELECT action FROM region_revisions WHERE region_id = ? AND revision = ?", (region_id, region["revision"])).fetchone()
        if region["deleted"] and last and last["action"].startswith(("split_into", "merged_into")):
            raise ReviewError("this region was split or merged; undo the parts instead")
        data = json.loads(prev["data"])
        changes = {k: data[k] for k in SNAPSHOT_FIELDS if data[k] != region[k]}
        if not changes:
            raise ReviewError("nothing to undo")
        return _write(conn, region, changes, "human", "undo")


def split_region(conn: sqlite3.Connection, region_id: str, at_x: float, texts: tuple[str | None, str | None] = (None, None)) -> list[dict[str, Any]]:
    """Split a region vertically at processed-frame x. The original is retired, not destroyed."""
    with transaction(conn):
        region = _load_for_edit(conn, region_id, None)
        if not region["x"] + 1 <= at_x <= region["x"] + region["w"] - 1:
            raise ReviewError("split position must fall inside the region")
        source = _source(conn, region["source_id"])
        left = Box(region["x"], region["y"], at_x - region["x"], region["h"])
        right = Box(at_x, region["y"], region["x"] + region["w"] - at_x, region["h"])
        parts = [
            create_region(conn, source, region["kind"], b, actor="human", cls=region["cls"], parent_id=region["parent_id"],
                          text=t, status="needs_review", action=f"split_from:{region_id}")
            for b, t in zip((left, right), texts)
        ]
        _write(conn, region, {"deleted": 1}, "human", f"split_into:{parts[0]['id']},{parts[1]['id']}")
        conn.execute("UPDATE regions SET parent_id = ? WHERE parent_id = ? AND deleted = 0 AND x < ?", (parts[0]["id"], region_id, at_x))
        conn.execute("UPDATE regions SET parent_id = ? WHERE parent_id = ? AND deleted = 0", (parts[1]["id"], region_id))
        return parts


def split_region_rows(conn: sqlite3.Connection, region_id: str, at_y: float) -> list[dict[str, Any]]:
    """Split a region into a top and a bottom part at processed-frame y (two lines in one box).

    Child regions (words, marks) move to the part holding their centre. Text is cleared: it
    belonged to the combined box.
    """
    with transaction(conn):
        region = _load_for_edit(conn, region_id, None)
        if not region["y"] + 1 <= at_y <= region["y"] + region["h"] - 1:
            raise ReviewError("split position must fall inside the region")
        source = _source(conn, region["source_id"])
        top = Box(region["x"], region["y"], region["w"], at_y - region["y"])
        bottom = Box(region["x"], at_y, region["w"], region["y"] + region["h"] - at_y)
        parts = [
            create_region(conn, source, region["kind"], b, actor="human", cls=region["cls"], parent_id=region["parent_id"],
                          status="needs_review", action=f"split_from:{region_id}")
            for b in (top, bottom)
        ]
        _write(conn, region, {"deleted": 1}, "human", f"split_into:{parts[0]['id']},{parts[1]['id']}")
        conn.execute("UPDATE regions SET parent_id = ? WHERE parent_id = ? AND deleted = 0 AND y + h / 2.0 < ?", (parts[0]["id"], region_id, at_y))
        conn.execute("UPDATE regions SET parent_id = ? WHERE parent_id = ? AND deleted = 0", (parts[1]["id"], region_id))
        return parts


OPEN = ("unprocessed", "proposed", "needs_review")


def _line_words(conn: sqlite3.Connection, line_id: str) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id FROM regions WHERE parent_id = ? AND deleted = 0 AND kind = 'word' ORDER BY x", (line_id,)).fetchall()
    return [get_region(conn, r["id"]) for r in rows]


def approve_line(
    conn: sqlite3.Connection, line_id: str, word_texts: dict[str, str | None], line_text: str | None = None,
    expected_revision: int | None = None,
) -> dict[str, Any]:
    """Quick check: approve a line and its words in one step, with the texts the person confirmed.

    `word_texts` maps word id -> text as typed (missing ids keep their current text). A word whose
    text is emptied is left out (excluded), not approved. A line without word boxes takes
    `line_text`. Everything happens in one transaction: all approved or nothing changes.
    """
    with transaction(conn):
        line = _load_for_edit(conn, line_id, expected_revision)
        if line["kind"] != "line":
            raise ReviewError("quick check approves lines")
        words = [w for w in _line_words(conn, line_id) if w["status"] != "excluded" and w["cls"] in ("handwriting", "uncertain", "equation")]
        unknown = set(word_texts) - {w["id"] for w in words}
        if unknown:
            raise ReviewError("some words changed since the page loaded; reload")
        kept: list[dict[str, Any]] = []
        for w in words:
            text = word_texts.get(w["id"], w["text"])
            text = text.strip() if isinstance(text, str) else text
            if not text:
                out = _status_change(w, "excluded", "left out in quick check")
                _write(conn, w, out, "human", "quick_check_leave_out")
                continue
            changes: dict[str, Any] = {}
            if text != w["text"]:
                changes.update(text=text, text_origin="human")
            if w["status"] == "approved":
                if changes:
                    changes["approved_revision"] = w["revision"] + 1
            else:
                changes.update(_status_change({**w, **changes}, "approved", None))
            kept.append(_write(conn, w, changes, "human", "quick_check_approve") if changes else w)
        if kept:
            text = " ".join(w["text"] for w in kept)
        else:
            text = (line_text if line_text is not None else line["text"]) or None
            text = text.strip() or None if isinstance(text, str) else text
        changes = {}
        if text != line["text"]:
            changes.update(text=text, text_origin="human")
        if line["status"] == "approved":
            if changes:
                changes["approved_revision"] = line["revision"] + 1
        else:
            changes.update(_status_change({**line, **changes}, "approved", None))
        line = _write(conn, line, changes, "human", "quick_check_approve") if changes else line
        return {"line": line, "words": _line_words(conn, line_id)}


def exclude_line(conn: sqlite3.Connection, line_id: str, reason: str | None = None) -> dict[str, Any]:
    """Quick check: leave a whole line out, with its words."""
    with transaction(conn):
        line = _load_for_edit(conn, line_id, None)
        for w in _line_words(conn, line_id):
            out = _status_change(w, "excluded", reason or "line left out in quick check")
            if out:
                _write(conn, w, out, "human", "quick_check_exclude")
        out = _status_change(line, "excluded", reason or "left out in quick check")
        line = _write(conn, line, out, "human", "quick_check_exclude") if out else line
        return {"line": line, "words": _line_words(conn, line_id)}


def check_lines(conn: sqlite3.Connection, source_id: str) -> list[dict[str, Any]]:
    """Open handwriting lines on a page, top to bottom, each with its word boxes."""
    rows = conn.execute(
        f"""SELECT id FROM regions WHERE source_id = ? AND deleted = 0 AND kind = 'line'
            AND cls IN ('handwriting', 'uncertain', 'equation') AND status IN ({','.join('?' * len(OPEN))}) ORDER BY y, x""",
        (source_id, *OPEN)).fetchall()
    return [{"line": get_region(conn, r["id"]), "words": _line_words(conn, r["id"])} for r in rows]


def merge_regions(conn: sqlite3.Connection, region_ids: list[str], text: str | None = None) -> dict[str, Any]:
    if len(region_ids) < 2:
        raise ReviewError("select at least two regions to merge")
    with transaction(conn):
        regions = [_load_for_edit(conn, rid, None) for rid in region_ids]
        if len({r["source_id"] for r in regions}) != 1 or len({r["kind"] for r in regions}) != 1:
            raise ReviewError("only regions of the same kind on the same page can be merged")
        regions.sort(key=lambda r: (r["x"], r["y"]))
        box = Box(regions[0]["x"], regions[0]["y"], regions[0]["w"], regions[0]["h"])
        for r in regions[1:]:
            box = box.union(Box(r["x"], r["y"], r["w"], r["h"]))
        if text is None and all(r["text"] for r in regions):
            text = " ".join(r["text"] for r in regions)
        merged = create_region(
            conn, _source(conn, regions[0]["source_id"]), regions[0]["kind"], box, actor="human", cls=regions[0]["cls"],
            parent_id=regions[0]["parent_id"], text=text, status="needs_review", action="merge_from:" + ",".join(region_ids),
        )
        for r in regions:
            _write(conn, r, {"deleted": 1}, "human", f"merged_into:{merged['id']}")
            conn.execute("UPDATE regions SET parent_id = ? WHERE parent_id = ? AND deleted = 0", (merged["id"], r["id"]))
        return merged


def delete_region(conn: sqlite3.Connection, region_id: str) -> dict[str, Any]:
    with transaction(conn):
        region = _load_for_edit(conn, region_id, None)
        return _write(conn, region, {"deleted": 1}, "human", "delete")


def link_mark(
    conn: sqlite3.Connection, mark_id: str, target_id: str | None, span: tuple[int, int] | None,
    unknown_target: bool, unknown_original: bool, note: str | None = None,
) -> str:
    mark = get_region(conn, mark_id)
    if mark["kind"] != "mark" and mark["cls"] not in MARK_CLASSES:
        raise ReviewError("only marks can be linked to a target")
    if target_id is None and not unknown_target:
        raise ReviewError("choose a target or mark the target as unknown")
    if target_id is not None:
        target = get_region(conn, target_id)
        if target["source_id"] != mark["source_id"]:
            raise ReviewError("mark and target must be on the same page")
        if span is not None and (target["text"] is None or not 0 <= span[0] < span[1] <= len(target["text"])):
            raise ReviewError("span lies outside the target's text")
    link_id = new_id("lnk")
    with transaction(conn):
        conn.execute("DELETE FROM links WHERE kind = 'mark_target' AND a_id = ?", (mark_id,))
        conn.execute(
            "INSERT INTO links (id, kind, a_id, b_id, span_start, span_end, unknown_target, unknown_original, note, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (link_id, "mark_target", mark_id, target_id, span[0] if span else None, span[1] if span else None,
             int(unknown_target), int(unknown_original), note, now_iso()),
        )
    return link_id


def link_before_after(conn: sqlite3.Connection, before_id: str, after_id: str, note: str | None = None) -> str:
    get_region(conn, before_id), get_region(conn, after_id)
    link_id = new_id("lnk")
    with transaction(conn):
        conn.execute(
            "INSERT INTO links (id, kind, a_id, b_id, note, created_at) VALUES (?,?,?,?,?,?)",
            (link_id, "before_after", before_id, after_id, note, now_iso()),
        )
    return link_id


def _source(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    row = row_dict(conn.execute("SELECT * FROM sources WHERE id = ?", (source_id,)).fetchone())
    if row is None:
        raise ReviewError("source not found")
    return row


def _mark_proposals_stale(conn: sqlite3.Connection, region_id: str) -> None:
    conn.execute("UPDATE proposals SET stale = 1 WHERE region_id = ? AND stale = 0", (region_id,))
