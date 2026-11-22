"""Studio: image assets, page compositions in physical units, and image export."""

from __future__ import annotations

import io
import sqlite3
from typing import Any, Literal

from PIL import Image, ImageChops
from pydantic import BaseModel, Field, field_validator

from handprinter import imaging, pages, review
from handprinter.config import MAX_IMAGE_PIXELS
from handprinter.db import new_id, now_iso, row_dict
from handprinter.storage import Store, atomic_write

MM_PER_INCH = 25.4


class Element(BaseModel):
    id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,40}$")
    asset_id: str
    x_mm: float
    y_mm: float
    w_mm: float = Field(gt=0, le=2000)
    h_mm: float = Field(gt=0, le=2000)
    keep_aspect: bool = True
    blend: Literal["multiply", "normal"] = "multiply"


class Composition(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    page_w_mm: float = Field(gt=10, le=1200)
    page_h_mm: float = Field(gt=10, le=1200)
    margins_mm: list[float] = Field(default_factory=lambda: [20.0, 20.0, 20.0, 20.0], min_length=4, max_length=4)
    elements: list[Element] = Field(default_factory=list, max_length=5000)

    @field_validator("margins_mm")
    @classmethod
    def _non_negative(cls, v: list[float]) -> list[float]:
        if any(m < 0 for m in v):
            raise ValueError("margins must be non-negative")
        return v


def add_upload_asset(conn: sqlite3.Connection, store: Store, data: bytes, label: str) -> dict[str, Any]:
    kind = imaging.sniff(data[:16])
    if kind in (None, "zip", "pdf"):
        raise imaging.UnsupportedInput("Studio accepts JPEG, PNG, WebP (or HEIC with the optional decoder) images")
    img = imaging.orient(imaging.decode_image(data, kind))
    return _store_asset(conn, store, img, "upload", None, label)


def add_region_asset(conn: sqlite3.Connection, store: Store, region_id: str) -> dict[str, Any]:
    region = review.get_region(conn, region_id)
    crop, _ = pages.region_crop(conn, store, region)
    return _store_asset(conn, store, crop, "region", region_id, region["text"] or region_id)


def add_generation_asset(conn: sqlite3.Connection, store: Store, generation_id: str) -> dict[str, Any]:
    gen = conn.execute("SELECT * FROM generations WHERE id = ?", (generation_id,)).fetchone()
    if gen is None:
        raise ValueError("generation not found")
    img = Image.open(store.resolve_relative(gen["image_path"])).convert("RGB")
    return _store_asset(conn, store, img, "generation", generation_id, f"{gen['prompt_text']} ({gen['adapter']})")


def _store_asset(conn: sqlite3.Connection, store: Store, img: Image.Image, kind: str, ref: str | None, label: str) -> dict[str, Any]:
    asset_id = new_id("ast")
    path = store.dir("studio/assets") / f"{asset_id}.png"
    atomic_write(path, imaging.png_bytes(img.convert("RGB")))
    row = {"id": asset_id, "kind": kind, "ref": ref, "label": label[:120], "path": store.relative(path), "width": img.width, "height": img.height, "created_at": now_iso()}
    conn.execute("INSERT INTO studio_assets (id, kind, ref, label, path, width, height, created_at) VALUES (?,?,?,?,?,?,?,?)", tuple(row.values()))
    return row


def save_composition(conn: sqlite3.Connection, comp: Composition, comp_id: str | None, expected_revision: int | None) -> dict[str, Any]:
    known = {r["id"] for r in conn.execute("SELECT id FROM studio_assets")}
    missing = sorted({e.asset_id for e in comp.elements} - known)
    if missing:
        raise ValueError(f"unknown assets: {', '.join(missing[:5])}")
    ts = now_iso()
    if comp_id is None:
        comp_id = new_id("cmp")
        conn.execute("INSERT INTO compositions (id, name, data, revision, created_at, updated_at) VALUES (?,?,?,?,?,?)",
                     (comp_id, comp.name, comp.model_dump_json(), 1, ts, ts))
        return {"id": comp_id, "revision": 1}
    row = conn.execute("SELECT revision FROM compositions WHERE id = ?", (comp_id,)).fetchone()
    if row is None:
        raise ValueError("composition not found")
    if expected_revision is not None and row["revision"] != expected_revision:
        raise review.ConflictError("the page was saved elsewhere since you opened it")
    conn.execute("UPDATE compositions SET name = ?, data = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                 (comp.name, comp.model_dump_json(), ts, comp_id))
    return {"id": comp_id, "revision": row["revision"] + 1}


def get_composition(conn: sqlite3.Connection, comp_id: str) -> dict[str, Any]:
    row = row_dict(conn.execute("SELECT * FROM compositions WHERE id = ?", (comp_id,)).fetchone(), ("data",))
    if row is None:
        raise ValueError("composition not found")
    return row


def render(conn: sqlite3.Connection, store: Store, comp: Composition, dpi: int) -> bytes:
    """Render the page at an explicit resolution. Page pixels = round(mm / 25.4 × dpi)."""
    if not 50 <= dpi <= 600:
        raise ValueError("export resolution must be between 50 and 600 dpi")
    px = lambda mm: round(mm / MM_PER_INCH * dpi)  # noqa: E731
    page_w, page_h = px(comp.page_w_mm), px(comp.page_h_mm)
    if page_w * page_h > MAX_IMAGE_PIXELS:
        raise ValueError("page too large at this resolution")
    page = Image.new("RGB", (page_w, page_h), "white")
    for el in comp.elements:
        if px(el.w_mm) * px(el.h_mm) > MAX_IMAGE_PIXELS // 2:
            raise ValueError(f"element {el.id} is too large to render at {dpi} dpi; reduce its size or the resolution")
    assets = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM studio_assets")}
    for el in comp.elements:
        asset = assets.get(el.asset_id)
        if asset is None:
            continue
        w, h = max(1, px(el.w_mm)), max(1, px(el.h_mm))
        img = Image.open(store.resolve_relative(asset["path"])).convert("RGB").resize((w, h), Image.Resampling.LANCZOS)
        x, y = px(el.x_mm), px(el.y_mm)
        if el.blend == "multiply":
            # Multiply darkens only; the paper tone of the crop is not removed or made transparent.
            box = (max(0, x), max(0, y), min(page_w, x + w), min(page_h, y + h))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            region = page.crop(box)
            part = img.crop((box[0] - x, box[1] - y, box[2] - x, box[3] - y))
            page.paste(ImageChops.multiply(region, part), box[:2])
        else:
            page.paste(img, (x, y))
    buf = io.BytesIO()
    page.save(buf, "PNG", dpi=(dpi, dpi))
    return buf.getvalue()


def list_assets(conn: sqlite3.Connection, limit: int = 200, offset: int = 0) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM studio_assets ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))]


def list_compositions(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [{"id": r["id"], "name": r["name"], "revision": r["revision"], "updated_at": r["updated_at"]}
            for r in conn.execute("SELECT id, name, revision, updated_at FROM compositions ORDER BY updated_at DESC")]
