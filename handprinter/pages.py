"""Page-level operations: processing revisions, detection proposals and cached crops."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from PIL import Image, ImageDraw

from handprinter import detect, imaging, review
from handprinter.db import dumps, row_dict, transaction
from handprinter.geometry import Box, Homography, processing_homography, remap_box
from handprinter.storage import Store, atomic_write

SOURCE_JSON = ("processing", "quality_flags", "near_duplicates")


def get_source(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    src = row_dict(conn.execute("SELECT * FROM sources WHERE id = ? AND deleted = 0", (source_id,)).fetchone(), SOURCE_JSON)
    if src is None:
        raise review.ReviewError(f"source {source_id} not found")
    return src


def working_to_processed(source: dict[str, Any]) -> Homography:
    h, _, _ = processing_homography(source["processing"], source["working_w"], source["working_h"])
    return h


def validate_processing(spec: dict[str, Any], working_w: int, working_h: int) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    if spec.get("corners"):
        corners = [[float(x), float(y)] for x, y in spec["corners"]]
        if len(corners) != 4:
            raise review.ReviewError("perspective needs exactly four corners")
        for x, y in corners:
            if not (-0.05 * working_w <= x <= 1.05 * working_w and -0.05 * working_h <= y <= 1.05 * working_h):
                raise review.ReviewError("a corner lies far outside the image")
        clean["corners"] = corners
    rotation = float(spec.get("rotation") or 0) % 360
    if rotation:
        clean["rotation"] = rotation
    if spec.get("lighting"):
        clean["lighting"] = True
    processing_homography(clean, working_w, working_h)  # raises on degenerate input
    return clean


def apply_processing(conn: sqlite3.Connection, store: Store, source_id: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Render a new processing revision and move dependent regions onto it, flagged stale.

    Heavy (full-resolution warp), so it runs in the worker.
    """
    source = get_source(conn, source_id)
    clean = validate_processing(spec, source["working_w"], source["working_h"])
    old_h = working_to_processed(source)
    working = Image.open(store.working(source_id)).convert("RGB")
    geometry_changed = {k: v for k, v in clean.items() if k != "lighting"} != {k: v for k, v in source["processing"].items() if k != "lighting"}
    new_rev = source["processing_rev"] + 1 if geometry_changed else source["processing_rev"]
    processed, new_h = imaging.render_processed(working, clean)
    if geometry_changed and new_rev > 0:
        atomic_write(store.processed(source_id, new_rev), imaging.png_bytes(processed))
    imaging.save_preview(processed, store.preview(source_id, new_rev, "thumb"), imaging.THUMB_EDGE)
    imaging.save_preview(processed, store.preview(source_id, new_rev, "display"), imaging.DISPLAY_EDGE)
    if clean.get("lighting"):
        imaging.save_preview(imaging.normalise_lighting(processed), store.preview(source_id, new_rev, "display-norm"), imaging.DISPLAY_EDGE)
    moved = 0
    with transaction(conn):
        conn.execute(
            "UPDATE sources SET processing = ?, processing_rev = ?, processed_w = ?, processed_h = ? WHERE id = ?",
            (dumps(clean), new_rev, processed.width, processed.height, source_id),
        )
        if geometry_changed:
            back = old_h.inverse()
            rows = conn.execute("SELECT id FROM regions WHERE source_id = ? AND deleted = 0", (source_id,)).fetchall()
            for row in rows:
                region = review.get_region(conn, row["id"])
                box = remap_box(Box(region["x"], region["y"], region["w"], region["h"]), back, new_h, processed.width, processed.height)
                if box.w < 1 or box.h < 1:
                    box = Box(0, 0, 1, 1)
                changes = {
                    "x": box.x, "y": box.y, "w": box.w, "h": box.h, "frame_w": processed.width, "frame_h": processed.height,
                    "processing_rev": new_rev, "flags": sorted(set(region["flags"]) | {"geometry_stale"}),
                }
                review._write(conn, region, changes, "system", f"remap_to_processing_rev:{new_rev}")
                conn.execute("UPDATE proposals SET stale = 1 WHERE region_id = ?", (region["id"],))
                moved += 1
    return {"processing_rev": new_rev, "regions_remapped": moved, "processed_w": processed.width, "processed_h": processed.height}


def run_detection(conn: sqlite3.Connection, store: Store, source_id: str, replace: bool = False) -> dict[str, Any]:
    """Create detector proposals (lines, words, ruling). Never touches human-created regions."""
    source = get_source(conn, source_id)
    img = Image.open(store.processed(source_id, source["processing_rev"])).convert("RGB")
    if source["processing"].get("lighting"):
        img = imaging.normalise_lighting(img)
    result = detect.detect_layout(img, omitted_boxes(conn, source_id))
    created = {"lines": 0, "words": 0, "rulings": 0, "skipped_overlapping": 0}
    with transaction(conn):
        detector_rows = conn.execute(
            "SELECT id, revision FROM regions WHERE source_id = ? AND deleted = 0 AND created_by = 'detector'", (source_id,)
        ).fetchall()
        if detector_rows and not replace:
            return {"skipped": True, "reason": "detector proposals already exist; use replace to re-run", **created}
        # Only untouched detector proposals are replaced; anything edited or drawn by a person is kept.
        for row in detector_rows:
            if row["revision"] == 1:
                review._write(conn, review.get_region(conn, row["id"]), {"deleted": 1}, "detector", "replaced_by_redetect")
        kept = [Box(r["x"], r["y"], r["w"], r["h"]) for r in conn.execute(
            "SELECT x, y, w, h FROM regions WHERE source_id = ? AND deleted = 0 AND kind IN ('line', 'word', 'block')", (source_id,))]

        def overlaps_kept(box: Box) -> bool:
            return any(box.intersection_area(k) > 0.5 * min(box.w * box.h, k.w * k.h) for k in kept)

        for ruling in result.rulings:
            if overlaps_kept(ruling):
                created["skipped_overlapping"] += 1
                continue
            review.create_region(conn, source, "block", ruling, actor="detector", cls="printed_ruling")
            created["rulings"] += 1
        for line in result.lines:
            flags = ["ruling_overlap"] if line.overlaps_ruling else []
            if overlaps_kept(line.box):
                created["skipped_overlapping"] += 1
                continue
            line_region = review.create_region(conn, source, "line", line.box, actor="detector", flags=flags)
            created["lines"] += 1
            for wbox in line.words:
                review.create_region(conn, source, "word", wbox, actor="detector", parent_id=line_region["id"], flags=flags)
                created["words"] += 1
        conn.execute("UPDATE sources SET detection_status = 'done' WHERE id = ?", (source_id,))
    return created


# ---------- omitted areas ----------
# An omitted area is a human-drawn block (class "omitted", status "excluded") covering something that
# is not your handwriting: a diagram, a printed worksheet header, someone else's notes. Detection ignores
# ink inside it, recognition packages blank it out, and regions mostly inside it are excluded.

OMIT_REASONS = {
    "diagram": "Diagram or drawing",
    "printed": "Printed text or worksheet instructions",
    "other_author": "Someone else's writing",
    "scribble": "Scribbles or crossings-out",
    "other": "Other",
}
INSIDE_FRACTION = 0.6


def omitted_areas(conn: sqlite3.Connection, source_id: str) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM regions WHERE source_id = ? AND deleted = 0 AND cls = 'omitted' ORDER BY y, x", (source_id,)
    ).fetchall()


def omitted_boxes(conn: sqlite3.Connection, source_id: str) -> list[Box]:
    return [Box(r["x"], r["y"], r["w"], r["h"]) for r in omitted_areas(conn, source_id)]


def omit_area(conn: sqlite3.Connection, source_id: str, box: Box, reason: str, processing_rev: int | None) -> dict[str, Any]:
    """Mark an area of the page as not-for-use and exclude the regions that lie mostly inside it.

    Approved regions are left as you decided and reported back; everything else is excluded with a
    reason that lets `restore_area` put it back exactly.
    """
    if reason not in OMIT_REASONS:
        raise review.ReviewError(f"unknown omit reason {reason!r}")
    with transaction(conn):
        source = get_source(conn, source_id)
        review.check_frame(source, processing_rev)
        area = review.create_region(conn, source, "block", box, actor="human", cls="omitted", status="excluded",
                                    review_reason=reason, action="omit_area")
        area_box = Box(area["x"], area["y"], area["w"], area["h"])
        excluded, kept_approved = 0, 0
        for row in conn.execute(
            "SELECT id, x, y, w, h, status FROM regions WHERE source_id = ? AND deleted = 0 AND cls != 'omitted' AND id != ?",
            (source_id, area["id"]),
        ).fetchall():
            b = Box(row["x"], row["y"], row["w"], row["h"])
            if b.w * b.h <= 0 or b.intersection_area(area_box) < INSIDE_FRACTION * b.w * b.h:
                continue
            if row["status"] == "approved":
                kept_approved += 1
                continue
            if row["status"] == "excluded":
                continue
            region = review.get_region(conn, row["id"])
            review._write(conn, region, {"status": "excluded", "review_reason": f"omitted:{area['id']}", "deferred": 0},
                          "human", f"excluded_by_omit:{area['id']}")
            excluded += 1
    return {"area": area, "excluded": excluded, "kept_approved": kept_approved}


def restore_area(conn: sqlite3.Connection, source_id: str, area_id: str) -> dict[str, Any]:
    """Remove an omitted area and return the regions it excluded to the status they had before."""
    with transaction(conn):
        area = review.get_region(conn, area_id)
        if area["source_id"] != source_id or area["cls"] != "omitted" or area["deleted"]:
            raise review.ReviewError("not an omitted area on this page")
        review._write(conn, area, {"deleted": 1}, "human", "restore_area")
        restored = 0
        for row in conn.execute(
            "SELECT id FROM regions WHERE source_id = ? AND deleted = 0 AND status = 'excluded' AND review_reason = ?",
            (source_id, f"omitted:{area_id}"),
        ).fetchall():
            region = review.get_region(conn, row["id"])
            mark = conn.execute(
                "SELECT revision FROM region_revisions WHERE region_id = ? AND action = ? ORDER BY revision DESC LIMIT 1",
                (region["id"], f"excluded_by_omit:{area_id}"),
            ).fetchone()
            before = mark and conn.execute(
                "SELECT data FROM region_revisions WHERE region_id = ? AND revision < ? ORDER BY revision DESC LIMIT 1",
                (region["id"], mark["revision"]),
            ).fetchone()
            data = json.loads(before["data"]) if before else {"status": "needs_review", "review_reason": None, "deferred": 0}
            review._write(conn, region, {"status": data["status"], "review_reason": data["review_reason"], "deferred": data["deferred"]},
                          "human", f"restored_from_omit:{area_id}")
            restored += 1
    return {"restored": restored}


def paint_omitted(img: Image.Image, context: Box, areas: list[Box], fill: tuple[int, int, int] = (200, 200, 200)) -> Image.Image:
    """Cover omitted areas in an image cut at `context` (processed frame) so they are never sent."""
    if not areas:
        return img
    sx, sy = img.width / context.w, img.height / context.h
    out = img.copy()
    draw = ImageDraw.Draw(out)
    for a in areas:
        x0, y0 = (a.x - context.x) * sx, (a.y - context.y) * sy
        x1, y1 = (a.x + a.w - context.x) * sx, (a.y + a.h - context.y) * sy
        if x1 <= 0 or y1 <= 0 or x0 >= out.width or y0 >= out.height:
            continue
        draw.rectangle([max(0, x0), max(0, y0), min(out.width, x1) - 1, min(out.height, y1) - 1], fill=fill)
    return out


# ---------- per-page analysis ----------

QUALITY_TEXT = {
    "possible_blur": "The photo may be blurry. Fine strokes can be lost; consider retaking it.",
    "possible_glare": "Bright glare patches. Writing under glare may be missing.",
    "possible_shadow": "Uneven lighting or a shadow. Try \u201cEven out lighting\u201d, or retake in flat light.",
    "possible_clipped_writing": "Ink touches the photo edge. Some writing may be cut off.",
}
ORIENTATION_TEXT = {1: "already upright", 2: "mirrored", 3: "rotated 180\u00b0", 4: "flipped vertically", 5: "mirrored and rotated",
                    6: "rotated 90\u00b0 clockwise", 7: "mirrored and rotated", 8: "rotated 90\u00b0 counter-clockwise"}


def analysis(conn: sqlite3.Connection, source_id: str) -> dict[str, Any]:
    """Plain-language account of what the app did to a page and what it will use from it."""
    src = get_source(conn, source_id)
    rows = conn.execute(
        "SELECT kind, cls, status, text_origin, review_reason, COUNT(*) n FROM regions WHERE source_id = ? AND deleted = 0 GROUP BY 1,2,3,4,5",
        (source_id,),
    ).fetchall()

    def count(pred: Any) -> int:
        return sum(r["n"] for r in rows if pred(r))

    writing = lambda r: r["kind"] in ("line", "word") and r["cls"] in ("handwriting", "uncertain")  # noqa: E731
    words = count(lambda r: r["kind"] == "word" and r["status"] != "excluded")
    lines = count(lambda r: r["kind"] == "line" and r["status"] != "excluded")
    rulings = count(lambda r: r["cls"] == "printed_ruling" and r["status"] != "excluded")
    areas = [dict(r) for r in omitted_areas(conn, source_id)]
    by_omit = count(lambda r: (r["review_reason"] or "").startswith("omitted:"))
    excluded_other = count(lambda r: r["status"] == "excluded" and r["cls"] != "omitted" and not (r["review_reason"] or "").startswith("omitted:"))
    approved = count(lambda r: r["kind"] == "word" and r["status"] == "approved")
    with_text = count(lambda r: writing(r) and r["kind"] == "word" and r["status"] in ("proposed", "needs_review") and r["text_origin"] == "machine")
    to_review = count(lambda r: r["status"] in ("unprocessed", "proposed", "needs_review") and r["cls"] != "printed_ruling")
    stale = conn.execute("SELECT COUNT(*) FROM regions WHERE source_id = ? AND deleted = 0 AND processing_rev != ?", (source_id, src["processing_rev"])).fetchone()[0]
    proc = src["processing"]
    corrections = [x for x in (
        "perspective from 4 corners" if proc.get("corners") else None,
        f"rotated {proc['rotation']:g}\u00b0" if proc.get("rotation") else None,
        "lighting evened out for display and detection" if proc.get("lighting") else None,
    ) if x]

    def step(key: str, label: str, state: str, detail: str) -> dict[str, str]:
        return {"key": key, "label": label, "state": state, "detail": detail}

    steps = [
        step("original", "Original kept", "done", f"{src['mime']} · {src['original_w']} \u00d7 {src['original_h']} px, stored byte-for-byte (sha256 {src['sha256'][:12]}\u2026)."),
        step("orientation", "Turned upright", "done", f"Phone orientation tag: {ORIENTATION_TEXT.get(src['exif_orientation'], 'unknown')}."),
        step("correction", "Camera correction", "done" if corrections else "info",
             "; ".join(corrections) + "." if corrections else "None applied. If the page is photographed at an angle, use Adjust corners."),
        step("duplicates", "Duplicate check", "warn" if src["near_duplicates"] else "done",
             f"Looks like {len(src['near_duplicates'])} other page(s); they will share a split." if src["near_duplicates"] else "No matching pages found."),
        step("quality", "Photo quality", "warn" if src["quality_flags"] else "done",
             " ".join(QUALITY_TEXT.get(f, f) for f in src["quality_flags"]) if src["quality_flags"] else "No blur, glare, shadow or cut-off edges detected (automatic and fallible)."),
        step("detection", "Writing found", "done" if src["detection_status"] == "done" else "todo",
             f"{lines} line(s) and {words} word box(es) proposed; {rulings} printed ruling line(s) recognised and ignored."
             if src["detection_status"] == "done" else "Detection has not run yet."),
        step("omitted", "Areas you omitted", "done" if areas else "info",
             f"{len(areas)} area(s) omitted; {by_omit} box(es) inside them excluded. Detection ignores them and they are blanked out before anything is sent for recognition."
             if areas else "None. Draw a box over diagrams, printed text or anything that is not your handwriting to leave it out."),
        step("recognition", "Suggested text", "done" if with_text or approved else "todo",
             f"{with_text} word(s) have a machine suggestion waiting for review." if with_text else "No suggestions yet. Select the page in Sources and choose Prepare recognition\u2026"),
        step("training", "Ready for training", "done" if approved else "todo",
             f"{approved} approved word(s) will be exported. Only approved, unflagged handwriting is used." if approved else "Nothing approved yet. Approve words in Review to use them."),
    ]
    if stale:
        steps.insert(3, step("stale", "Boxes from an older correction", "warn", f"{stale} box(es) were moved after re-processing and need a quick re-check in Review."))
    return {
        "source_id": source_id,
        "frame": {"name": "processed", "width": src["processed_w"], "height": src["processed_h"], "processing_rev": src["processing_rev"]},
        "steps": steps,
        "counts": {"lines": lines, "words": words, "rulings": rulings, "omitted_areas": len(areas), "excluded_by_omit": by_omit,
                   "excluded_other": excluded_other, "approved_words": approved, "to_review": to_review},
        "omitted_areas": [{"id": a["id"], "x": a["x"], "y": a["y"], "w": a["w"], "h": a["h"], "reason": a["review_reason"],
                           "label": OMIT_REASONS.get(a["review_reason"] or "", "Omitted")} for a in areas],
        "omit_reasons": OMIT_REASONS,
    }


class WorkingImages:
    """Keeps the most recently decoded working image so batch crops decode each page once."""

    def __init__(self, store: Store):
        self.store = store
        self._key: tuple[str, int] | None = None
        self._img: Image.Image | None = None

    def get(self, source: dict[str, Any]) -> Image.Image:
        key = (source["id"], source["processing_rev"])
        if key != self._key or self._img is None:
            self._img = Image.open(self.store.working(source["id"])).convert("RGB")
            self._key = key
        return self._img


def region_crop(
    conn: sqlite3.Connection, store: Store, region: dict[str, Any], pad: float = 0.0, images: WorkingImages | None = None,
) -> tuple[Image.Image, str]:
    """Return a region's crop from the full-resolution working image, cached per revision on disk."""
    path = store.crop(region["id"], region["revision"], region["processing_rev"])
    if pad == 0 and path.exists():
        return Image.open(path), str(path)
    source = get_source(conn, region["source_id"])
    if region["processing_rev"] != source["processing_rev"]:
        raise review.ReviewError("region geometry is on an older processing revision")
    working = (images or WorkingImages(store)).get(source)
    box = Box(region["x"], region["y"], region["w"], region["h"])
    if pad:
        box = box.pad(pad).clamp(source["processed_w"], source["processed_h"])
    crop = imaging.cut_crop(working, working_to_processed(source), box)
    if pad == 0:
        atomic_write(path, imaging.png_bytes(crop))
    return crop, str(path)


def suggest_corners(store: Store, source: dict[str, Any]) -> list[list[float]] | None:
    working = Image.open(store.working(source["id"])).convert("RGB")
    corners = detect.suggest_page_corners(working)
    return [list(c) for c in corners] if corners else None
