"""Page-level operations: processing revisions, detection proposals and cached crops."""

from __future__ import annotations

import sqlite3
from typing import Any

from PIL import Image

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
    result = detect.detect_layout(img)
    created = {"lines": 0, "words": 0, "rulings": 0}
    with transaction(conn):
        existing = conn.execute(
            "SELECT COUNT(*) FROM regions WHERE source_id = ? AND deleted = 0 AND created_by = 'detector' AND revision = 1",
            (source_id,),
        ).fetchone()[0]
        if replace:
            # Only untouched detector proposals are replaced; anything edited is kept.
            for row in conn.execute(
                "SELECT id FROM regions WHERE source_id = ? AND deleted = 0 AND created_by = 'detector' AND revision = 1", (source_id,)
            ).fetchall():
                review._write(conn, review.get_region(conn, row["id"]), {"deleted": 1}, "detector", "replaced_by_redetect")
        elif existing:
            return {"skipped": True, "reason": "detector proposals already exist; use replace to re-run", **created}
        for ruling in result.rulings:
            review.create_region(conn, source, "block", ruling, actor="detector", cls="printed_ruling", flags=["detector_class_guess"])
            created["rulings"] += 1
        for line in result.lines:
            flags = ["ruling_overlap"] if line.overlaps_ruling else []
            line_region = review.create_region(conn, source, "line", line.box, actor="detector", flags=flags)
            created["lines"] += 1
            for wbox in line.words:
                review.create_region(conn, source, "word", wbox, actor="detector", parent_id=line_region["id"], flags=flags)
                created["words"] += 1
        conn.execute("UPDATE sources SET detection_status = 'done' WHERE id = ?", (source_id,))
    return created


def region_crop(conn: sqlite3.Connection, store: Store, region: dict[str, Any], pad: float = 0.0) -> tuple[Image.Image, str]:
    """Return a region's crop from the full-resolution working image, cached per revision."""
    source = get_source(conn, region["source_id"])
    if region["processing_rev"] != source["processing_rev"]:
        raise review.ReviewError("region geometry is on an older processing revision")
    path = store.crop(region["id"], region["revision"], region["processing_rev"])
    if pad == 0 and path.exists():
        return Image.open(path), str(path)
    working = Image.open(store.working(source["id"])).convert("RGB")
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
