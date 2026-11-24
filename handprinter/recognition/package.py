"""Build recognition packages for the Claude Code hand-off (see docs/architecture.md)."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from handprinter import imaging, pages
from handprinter.config import Settings
from handprinter.db import new_id, now_iso, transaction
from handprinter.geometry import Box, fit_within
from handprinter.recognition import MAX_LONG_EDGE, MAX_PIXELS, PROMPT_VERSION, SCHEMA_VERSION
from handprinter.storage import Store, atomic_write, sha256_bytes

REPO_ROOT = Path(__file__).resolve().parents[2]
MANUAL_MODEL = "Manual chat"
OPEN_STATUSES = ("unprocessed", "proposed", "needs_review")


def prompt_path(version: str = PROMPT_VERSION) -> Path:
    return REPO_ROOT / "prompts" / "recognition" / f"{version}.md"


def schema_text() -> str:
    return resources.files("handprinter.recognition").joinpath("result.schema.json").read_text(encoding="utf-8")


def load_schema() -> dict[str, Any]:
    return json.loads(schema_text())


def package_dir(store: Store, package_id: str) -> Path:
    return store.dir("recognition/jobs") / package_id


@dataclass
class PlannedItem:
    source_id: str
    region_id: str | None
    context_type: str
    target: Box
    context: Box
    local_boxes: list[tuple[str, Box, int]] = field(default_factory=list)
    region_revision: int | None = None


def _settings_signature() -> str:
    return f"edge{MAX_LONG_EDGE}-px{MAX_PIXELS}"


def cache_key(image_sha: str, model: str) -> str:
    return hashlib.sha256(f"{image_sha}|{model}|{PROMPT_VERSION}|{_settings_signature()}".encode()).hexdigest()


def plan(conn: sqlite3.Connection, source_ids: list[str], include_overview: bool, force: bool = False) -> list[PlannedItem]:
    """Decide which images would be sent: optional page overviews plus open line regions."""
    items: list[PlannedItem] = []
    for source_id in source_ids:
        source = pages.get_source(conn, source_id)
        full = Box(0, 0, source["processed_w"], source["processed_h"])
        lines = conn.execute(
            f"""SELECT * FROM regions WHERE source_id = ? AND deleted = 0 AND kind = 'line'
                AND cls IN ('handwriting', 'uncertain', 'equation') AND status IN ({','.join('?' * len(OPEN_STATUSES))})
                AND (text_origin IS NULL OR text_origin = 'machine') ORDER BY y""",
            (source_id, *OPEN_STATUSES),
        ).fetchall()
        if include_overview or not lines:
            items.append(PlannedItem(source_id, None, "page_overview", full, full))
        for line in lines:
            if not force and conn.execute(
                    "SELECT 1 FROM proposals WHERE region_id = ? AND stale = 0 LIMIT 1", (line["id"],)).fetchone():
                continue  # already read, and its box has not changed since: nothing new to read
            target = Box(line["x"], line["y"], line["w"], line["h"])
            margin = max(12.0, 0.25 * line["h"])
            context = target.pad(margin).clamp(full.w, full.h)
            words = conn.execute(
                "SELECT id, x, y, w, h, revision FROM regions WHERE parent_id = ? AND deleted = 0 AND kind = 'word' AND status != 'excluded' ORDER BY x", (line["id"],)
            ).fetchall()
            items.append(PlannedItem(source_id, line["id"], "line", target, context,
                                     [(w["id"], Box(w["x"], w["y"], w["w"], w["h"]), w["revision"]) for w in words], line["revision"]))
    return items


def preview(conn: sqlite3.Connection, store: Store, settings: Settings, source_ids: list[str], include_overview: bool) -> dict[str, Any]:
    """What a package would contain, before anything is written or sent."""
    planned = plan(conn, source_ids, include_overview)
    out = []
    total_pixels = 0
    for item in planned:
        scale = fit_within(round(item.context.w), round(item.context.h), MAX_LONG_EDGE, MAX_PIXELS)
        w, h = max(1, round(item.context.w * scale)), max(1, round(item.context.h * scale))
        total_pixels += w * h
        out.append({"source_id": item.source_id, "region_id": item.region_id, "context_type": item.context_type, "width": w, "height": h})
    return {
        "items": out,
        "item_count": len(out),
        "image_count": len(out),
        "total_pixels": total_pixels,
        "pages": sorted(set(source_ids)),
        "model": settings.recognition_model,
        "usage_note": "Runs on your Claude plan's usage limits through Claude Code. No dollar estimate is made.",
    }


def build(
    conn: sqlite3.Connection, store: Store, settings: Settings, source_ids: list[str], include_overview: bool, force: bool,
    progress: Callable[[float, str], None] = lambda f, m: None,
) -> dict[str, Any]:
    """Write `recognition/jobs/<id>/` with manifest, images, prompt, schema and empty results/."""
    planned = plan(conn, source_ids, include_overview, force)
    already = conn.execute(
        f"""SELECT COUNT(*) FROM regions r WHERE r.source_id IN ({','.join('?' * len(source_ids))}) AND r.deleted = 0 AND r.kind = 'line'
            AND r.status IN ('unprocessed', 'proposed', 'needs_review') AND (r.text_origin IS NULL OR r.text_origin = 'machine')
            AND EXISTS (SELECT 1 FROM proposals p WHERE p.region_id = r.id AND p.stale = 0)""", source_ids).fetchone()[0] if not force else 0
    package_id = new_id("rec")
    root = package_dir(store, package_id)
    (root / "images").mkdir(parents=True)
    (root / "results").mkdir()
    shutil.copyfile(prompt_path(), root / "prompt.md")
    (root / "result.schema.json").write_text(schema_text(), encoding="utf-8")

    entries, cached, reused, total_pixels = [], 0, 0, 0
    working_cache: dict[str, tuple[Image.Image, dict[str, Any]]] = {}
    omitted: dict[str, list[Box]] = {}
    for index, item in enumerate(planned):
        progress(index / max(1, len(planned)), f"image {index + 1}/{len(planned)}")
        if item.source_id not in working_cache:
            source = pages.get_source(conn, item.source_id)
            working_cache.clear()
            working_cache[item.source_id] = (Image.open(store.working(item.source_id)).convert("RGB"), source)
            omitted.setdefault(item.source_id, pages.omitted_boxes(conn, item.source_id))
        working, source = working_cache[item.source_id]
        # Areas you omitted (diagrams, printed text, other people's writing) are blanked before sending.
        crop = pages.paint_omitted(imaging.cut_crop(working, pages.working_to_processed(source), item.context), item.context, omitted[item.source_id])
        if item.context_type == "line":
            # Only the target line keeps full contrast: neighbours are faded context, so they are not read as part of it.
            words = [b.pad(0.15 * b.h) for _rid, b, _rev in item.local_boxes]
            crop = pages.fade_outside(crop, item.context, words or item.target)
        scale = fit_within(crop.width, crop.height, MAX_LONG_EDGE, MAX_PIXELS)
        if scale < 1:
            crop = crop.resize((max(1, round(crop.width * scale)), max(1, round(crop.height * scale))), Image.Resampling.LANCZOS)
        base_w, base_h = crop.width, crop.height  # the band added below keeps these pixels' coordinates
        if settings.recognition_model == MANUAL_MODEL:
            crop = imaging.with_label(crop, f"i{len(entries) + 1:04d}")  # a chat reads which image is which
        data = imaging.png_bytes(crop)
        digest = sha256_bytes(data)
        key = cache_key(digest, settings.recognition_model)
        reuse = None
        prev = None if force else conn.execute(
            "SELECT package_id, item_id FROM recognition_items WHERE cache_key = ? AND status = 'succeeded' ORDER BY updated_at DESC LIMIT 1", (key,)).fetchone()
        if prev is not None:
            # This exact image was read before. If that reading is already on this very box, there is nothing to do.
            # If the box is new (e.g. after Re-detect cleared the old guesses), reuse the earlier reading instead of
            # skipping it, so the line is not left without text and no new read is needed.
            on_this_region = item.region_id is None or conn.execute(
                "SELECT 1 FROM proposals WHERE region_id = ? AND image_sha256 = ? AND stale = 0 LIMIT 1", (item.region_id, digest)).fetchone()
            earlier = package_dir(store, prev["package_id"]) / "results" / f"{prev['item_id']}.json"
            if on_this_region or not earlier.is_file():
                cached += 1
                continue
            try:
                reuse = json.loads(earlier.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                reuse = None
        item_id = f"i{len(entries) + 1:04d}"
        atomic_write(root / "images" / f"{item_id}.png", data)
        sx, sy = item.context.w / base_w, item.context.h / base_h

        def to_image(b: Box) -> list[float]:
            return [round((b.x - item.context.x) / sx, 2), round((b.y - item.context.y) / sy, 2), round(b.w / sx, 2), round(b.h / sy, 2)]

        total_pixels += crop.width * crop.height
        entries.append({
            "item_id": item_id,
            "image": f"images/{item_id}.png",
            "image_sha256": digest,
            "width": crop.width,
            "height": crop.height,
            "frame": "recognition",
            "source_frame": {"name": "processed", "width": source["processed_w"], "height": source["processed_h"], "processing_rev": source["processing_rev"]},
            "to_source": {"scale_x": sx, "scale_y": sy, "offset_x": item.context.x, "offset_y": item.context.y},
            "context_type": item.context_type,
            "target_box": to_image(item.target),
            "local_boxes": [{"region_id": rid, "box": to_image(b), "region_revision": rev} for rid, b, rev in item.local_boxes],
            "source_id": item.source_id,
            "region_id": item.region_id,
            "region_revision": item.region_revision,
            "cache_key": key,
        })
        if reuse is not None:
            atomic_write(root / "results" / f"{item_id}.json",
                         json.dumps({**reuse, "item_id": item_id, "image_sha256": digest}, ensure_ascii=False).encode("utf-8"))
            reused += 1

    manifest = {
        "job_id": package_id,
        "created_at": now_iso(),
        "prompt_version": PROMPT_VERSION,
        "schema_version": SCHEMA_VERSION,
        "model_configured": settings.recognition_model,
        "instructions": "Write one results/<item_id>.json per item, valid against result.schema.json, following prompt.md.",
        "items": entries,
    }
    if settings.recognition_model == MANUAL_MODEL:
        manifest.update(chat_batch_size=10, chat_labelled=True)  # see chat.py
    atomic_write(root / "manifest.json", json.dumps(manifest, indent=2, ensure_ascii=False).encode("utf-8"))
    ts = now_iso()
    with transaction(conn):
        conn.execute(
            "INSERT INTO recognition_packages (id, created_at, prompt_version, schema_version, model_configured, status, item_count, total_pixels) VALUES (?,?,?,?,?,?,?,?)",
            (package_id, ts, PROMPT_VERSION, SCHEMA_VERSION, settings.recognition_model, "open" if entries else "empty", len(entries), total_pixels),
        )
        for e in entries:
            conn.execute(
                "INSERT INTO recognition_items (package_id, item_id, source_id, region_id, context_type, image_sha256, cache_key, status, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (package_id, e["item_id"], e["source_id"], e["region_id"], e["context_type"], e["image_sha256"], e["cache_key"], "pending", ts),
            )
    imported = None
    if reused:
        from handprinter.recognition import importer  # earlier readings go straight in; only new lines wait for Haiku
        imported = importer.import_package(conn, store, package_id)
    return {"package_id": package_id, "items": len(entries), "cached_skipped": cached + already, "reused": reused,
            "to_read": len(entries) - reused, "imported": imported, "total_pixels": total_pixels, "path": str(root)}


def load_manifest(store: Store, package_id: str) -> dict[str, Any]:
    if not package_id.startswith("rec_"):
        raise ValueError("not a recognition package id")
    path = package_dir(store, package_id) / "manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"no manifest for {package_id}")
    return json.loads(path.read_text(encoding="utf-8"))


def results_waiting(store: Store, conn: sqlite3.Connection, package_id: str) -> int:
    """Result files written by the recogniser that have not been imported yet."""
    folder = package_dir(store, package_id) / "results"
    if not folder.is_dir():
        return 0
    open_items = {r["item_id"] for r in conn.execute(
        "SELECT item_id FROM recognition_items WHERE package_id = ? AND status != 'succeeded'", (package_id,))}
    return sum(1 for f in folder.glob("*.json") if f.stem in open_items)


def item_readings(conn: sqlite3.Connection, package_id: str) -> list[dict[str, Any]]:
    """Each item with its page and the text the recogniser read (once imported)."""
    rows = conn.execute(
        """SELECT i.item_id, i.status, i.error, i.context_type, i.source_id, i.region_id, s.original_filename,
                  p.payload, p.applied, p.stale
           FROM recognition_items i
           LEFT JOIN sources s ON s.id = i.source_id
           LEFT JOIN proposals p ON p.package_id = i.package_id AND p.item_id = i.item_id
           WHERE i.package_id = ? ORDER BY i.item_id""", (package_id,)).fetchall()
    out = []
    for r in rows:
        payload = json.loads(r["payload"]) if r["payload"] else {}
        out.append({
            "item_id": r["item_id"], "status": r["status"], "error": r["error"], "context_type": r["context_type"],
            "source_id": r["source_id"], "region_id": r["region_id"], "page": r["original_filename"],
            "text": payload.get("text"), "flags": payload.get("flags", []),
            "applied": bool(r["applied"]) if r["payload"] else None, "stale": bool(r["stale"]) if r["payload"] else None,
        })
    return out


def pending_packages(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """SELECT p.id, p.created_at, p.model_configured, p.item_count,
                  SUM(CASE WHEN i.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                  SUM(CASE WHEN i.status = 'failed' THEN 1 ELSE 0 END) AS failed
           FROM recognition_packages p JOIN recognition_items i ON i.package_id = p.id
           WHERE p.model_configured != ? GROUP BY p.id HAVING pending > 0 OR failed > 0 ORDER BY p.created_at""", (MANUAL_MODEL,),
    ).fetchall()
    return [dict(r) for r in rows]
