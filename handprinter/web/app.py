"""FastAPI application: JSON API, ID-based file endpoints and the static frontend."""

from __future__ import annotations

import json
import random
import secrets
import sqlite3
import time
from collections.abc import Iterator
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from PIL import Image

from handprinter import dataset, doctor, evaluation, imaging, ingest, jobs, pages, review, sheets, studio
from handprinter.adapters import all_adapters
from handprinter.config import MAX_UPLOAD_BYTES, ONLINE_MAX_UPLOAD_BYTES, Settings
from handprinter.db import connect, dumps, init_db, new_id, now_iso, row_dict, transaction
from handprinter.geometry import Box
from handprinter.recognition import package as recpkg
from handprinter.storage import Store, check_id
from handprinter.web import schemas as S
from handprinter.web.security import SESSION_COOKIE, SESSION_MAX_AGE, LoginLimiter, client_key, is_https, make_guard, session_value

STATIC_DIR = Path(str(resources.files("handprinter").joinpath("static")))
REGION_JSON = review.JSON_FIELDS


def create_app(settings: Settings, extra_hosts: set[str] | None = None) -> FastAPI:
    if settings.lan_mode and len(settings.access_token) < 16:
        raise RuntimeError("LAN mode needs HANDPRINTER_ACCESS_TOKEN (16+ characters); refusing to start")
    store = Store(settings)
    with connect(settings.db_path) as boot:
        init_db(boot)
    request_token = secrets.token_urlsafe(24)
    app = FastAPI(title="Handprinter", docs_url=None, redoc_url=None, openapi_url="/api/openapi.json")
    app.middleware("http")(make_guard(settings, request_token, extra_hosts or set()))
    app.state.settings, app.state.store, app.state.request_token = settings, store, request_token
    doctor_cache: dict[str, Any] = {}

    def db() -> Iterator[sqlite3.Connection]:
        conn = connect(settings.db_path)
        try:
            yield conn
        finally:
            conn.close()

    @app.exception_handler(review.ConflictError)
    def _conflict(_: Request, exc: review.ConflictError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=409)

    @app.exception_handler(review.ReviewError)
    def _review_error(_: Request, exc: review.ReviewError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(ValueError)
    def _value_error(_: Request, exc: ValueError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    # ---------- shell ----------
    index_template = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(index_template.replace("__REQUEST_TOKEN__", request_token), headers={"cache-control": "no-store"})

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        return HTMLResponse((STATIC_DIR / "login.html").read_text(encoding="utf-8"))

    limiter = LoginLimiter()

    @app.post("/api/login")
    def login(body: S.LoginIn, request: Request) -> JSONResponse:
        if not settings.login_required:
            raise HTTPException(403, "sign-in is not used on this computer")
        who = client_key(request, settings.relay_secret)
        wait = limiter.retry_after(who)
        if wait:
            minutes = max(1, round(wait / 60))
            return JSONResponse({"detail": f"Too many wrong attempts. Try again in about {minutes} minute{'s' if minutes != 1 else ''}."},
                                status_code=429, headers={"retry-after": str(wait)})
        if not secrets.compare_digest(body.token.encode(), settings.secret.encode()):
            limiter.failed(who)
            raise HTTPException(403, "wrong password")
        resp = JSONResponse({"ok": True})
        resp.set_cookie(SESSION_COOKIE, session_value(settings.secret), httponly=True, samesite="lax",
                        secure=is_https(request), max_age=SESSION_MAX_AGE, path="/")
        return resp

    @app.post("/api/logout")
    def logout() -> JSONResponse:
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.get("/api/status")
    def status(conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {
            "recognition_model": settings.recognition_model,
            "headless_recognition": settings.headless_recognition,
            "data_dir": str(settings.data_dir),
            "decoders": {"heic": imaging.heif_available(), "pdf": imaging.pdf_available()},
            "lan_mode": settings.lan_mode,
            "online": settings.online,
            "login_required": settings.login_required,
            "counts": {
                "sources": conn.execute("SELECT COUNT(*) FROM sources WHERE deleted = 0").fetchone()[0],
                "needs_review": conn.execute("SELECT COUNT(*) FROM regions WHERE deleted = 0 AND status = 'needs_review'").fetchone()[0],
                "active_jobs": conn.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0],
            },
            "limits": {"max_upload_bytes": ONLINE_MAX_UPLOAD_BYTES if settings.online else MAX_UPLOAD_BYTES},
        }

    @app.get("/api/doctor")
    def doctor_report(refresh: bool = False) -> dict[str, Any]:
        if refresh or "data" not in doctor_cache or time.time() - doctor_cache["at"] > 300:
            doctor_cache.update(data=doctor.report(settings), at=time.time())
        return doctor_cache["data"]

    # ---------- import ----------
    @app.post("/api/batches")
    def create_batch(body: S.BatchIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        try:
            ingest.parse_pages(body.pages)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        if body.sheet_start is not None and sheets.sheet_by_number(conn, body.sheet_start) is None:
            raise HTTPException(422, f"there is no writing sheet {body.sheet_start}")
        batch_id = new_id("bat")
        conn.execute("INSERT INTO batches (id, created_at, note, source_type, session, results) VALUES (?,?,?,?,?,?)",
                     (batch_id, now_iso(), body.note, body.source_type, body.session, dumps(body.model_dump())))
        return {"batch_id": batch_id}

    @app.post("/api/batches/{batch_id}/files")
    async def upload(batch_id: str, file: UploadFile = File(...)) -> dict[str, Any]:
        check_id(batch_id)
        conn = connect(settings.db_path)
        try:
            batch = conn.execute("SELECT * FROM batches WHERE id = ?", (batch_id,)).fetchone()
            if batch is None:
                raise HTTPException(404, "batch not found")
            opts = json.loads(batch["results"])
            staged_id = new_id("stg")
            path = store.staging(staged_id)
            size = 0
            with path.open("wb") as fh:
                while chunk := await file.read(1 << 20):
                    size += len(chunk)
                    if size > MAX_UPLOAD_BYTES:
                        fh.close()
                        path.unlink(missing_ok=True)
                        return {"filename": file.filename, "status": "rejected", "detail": f"larger than {MAX_UPLOAD_BYTES // 2**20} MB"}
                    fh.write(chunk)
            name = Path(file.filename or "upload").name[:200]
            job_id = jobs.enqueue(conn, "import_upload", {
                "staged_id": staged_id, "filename": name, "batch_id": batch_id, "source_type": batch["source_type"],
                "session": batch["session"], "captured_date": opts.get("captured_date"), "style_era": opts.get("style_era", "current"),
                "detect": opts.get("detect", True), "sheet_id": opts.get("sheet_id"),
                "pages": opts.get("pages"), "sheet_start": opts.get("sheet_start"),
            })
            return {"filename": name, "status": "queued", "job_id": job_id, "bytes": size}
        finally:
            conn.close()

    # ---------- writing sheets ----------
    @app.get("/api/sheets")
    def list_sheets(conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {"sheets": sheets.list_sheets(conn), "focuses": sheets.FOCUSES}

    @app.post("/api/sheets")
    def create_sheet(body: S.SheetIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        with transaction(conn):
            return sheets.make_sheet(conn, body.focus)

    @app.get("/api/sheets/{sheet_id}")
    def get_sheet(sheet_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return sheets.get_sheet(conn, sheet_id)

    @app.post("/api/word-cuts")
    def haiku_word_cuts(body: S.WordCutsIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        for sid in body.source_ids or []:
            pages.get_source(conn, sid)
        return {"job_id": jobs.enqueue(conn, "haiku_cuts", {"scope": body.scope, "source_ids": body.source_ids})}

    @app.post("/api/sheets/refill")
    def refill_sheets(conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {"job_id": jobs.enqueue(conn, "refill_sheets", {})}

    @app.post("/api/sources/{source_id}/sheet")
    def apply_sheet(source_id: str, body: S.ApplySheetIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        pages.get_source(conn, source_id)
        sheets.get_sheet(conn, body.sheet_id)
        return {"job_id": jobs.enqueue(conn, "apply_sheet", {"source_id": source_id, "sheet_id": body.sheet_id})}

    # ---------- sources ----------
    @app.get("/api/sources")
    def list_sources(
        conn: sqlite3.Connection = Depends(db), limit: int = Query(60, le=200), offset: int = 0,
        source_type: str | None = None, session: str | None = None, batch: str | None = None,
    ) -> dict[str, Any]:
        where, args = ["s.deleted = 0"], []
        for col, val in (("source_type", source_type), ("session", session), ("batch_id", batch)):
            if val:
                where.append(f"s.{col} = ?")
                args.append(val)
        sql_where = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) FROM sources s WHERE {sql_where}", args).fetchone()[0]
        rows = conn.execute(f"SELECT s.* FROM sources s WHERE {sql_where} ORDER BY s.created_at DESC, s.id LIMIT ? OFFSET ?", args + [limit, offset]).fetchall()
        ids = [r["id"] for r in rows]
        counts = {r["source_id"]: r for r in conn.execute(
            f"""SELECT source_id, SUM(kind IN ('word','line')) AS n_regions, SUM(status = 'needs_review') AS n_review,
                       SUM(status = 'approved') AS n_approved
                FROM regions WHERE deleted = 0 AND source_id IN ({','.join('?' * len(ids)) or "''"}) GROUP BY source_id""", ids)}
        items = []
        for r in rows:
            item = row_dict(r, pages.SOURCE_JSON)
            c = counts.get(r["id"])
            item.update(n_regions=(c["n_regions"] if c else 0) or 0, n_review=(c["n_review"] if c else 0) or 0, n_approved=(c["n_approved"] if c else 0) or 0)  # type: ignore[union-attr]
            items.append(item)
        sessions = [r[0] for r in conn.execute("SELECT DISTINCT session FROM sources WHERE deleted = 0 AND session IS NOT NULL ORDER BY session")]
        return {"total": total, "items": items, "sessions": sessions}

    @app.get("/api/sources/{source_id}")
    def get_source(source_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        src = pages.get_source(conn, source_id)
        src["image_url"] = f"/api/sources/{source_id}/image?variant=display&rev={src['processing_rev']}"
        # Neighbours in the Sources grid order (newest first), for stepping through pages one by one.
        c, i = src["created_at"], source_id
        newer = conn.execute("SELECT id FROM sources WHERE deleted = 0 AND (created_at > ? OR (created_at = ? AND id < ?)) ORDER BY created_at, id DESC LIMIT 1", (c, c, i)).fetchone()
        older = conn.execute("SELECT id FROM sources WHERE deleted = 0 AND (created_at < ? OR (created_at = ? AND id > ?)) ORDER BY created_at DESC, id LIMIT 1", (c, c, i)).fetchone()
        src["prev_id"], src["next_id"] = (newer[0] if newer else None), (older[0] if older else None)
        src["siblings"] = [r[0] for r in conn.execute("SELECT id FROM sources WHERE group_id = ? AND id != ? AND deleted = 0", (src["group_id"], source_id))]
        src["proposals"] = [row_dict(r, ("payload",)) for r in conn.execute(
            "SELECT id, model_reported, prompt_version, payload, created_at, stale FROM proposals WHERE source_id = ? AND region_id IS NULL ORDER BY created_at DESC LIMIT 5", (source_id,))]
        return src

    @app.patch("/api/sources/{source_id}")
    def patch_source(source_id: str, body: S.SourcePatch, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        src = pages.get_source(conn, source_id)
        changes = body.model_dump(exclude_unset=True)
        with transaction(conn):
            other = changes.pop("group_with", None)
            if other:
                target = pages.get_source(conn, other)
                conn.execute("UPDATE sources SET group_id = ? WHERE group_id = ?", (target["group_id"], src["group_id"]))
            for col in ("source_type", "session", "captured_date", "style_era"):
                if col in changes:
                    conn.execute(f"UPDATE sources SET {col} = ? WHERE id = ?", (changes[col], source_id))
        return pages.get_source(conn, source_id)

    @app.get("/api/sources/{source_id}/image")
    def source_image(source_id: str, variant: str = "display", conn: sqlite3.Connection = Depends(db)) -> Response:
        src = pages.get_source(conn, source_id)
        cache = {"cache-control": "private, max-age=86400"}
        if variant == "original":
            return FileResponse(store.resolve_relative(src["original_path"]), media_type=src["mime"], headers=cache)
        if variant == "working-display":
            path = store.preview(source_id, 0, "display")
        elif variant in ("thumb", "display", "display-norm"):
            path = store.preview(source_id, src["processing_rev"], variant)
            if variant == "display-norm" and not path.exists():
                img = Image.open(store.processed(source_id, src["processing_rev"])).convert("RGB")
                imaging.save_preview(imaging.normalise_lighting(img), path, imaging.DISPLAY_EDGE)
        elif variant == "full":
            path = store.processed(source_id, src["processing_rev"])
        else:
            raise HTTPException(400, "unknown variant")
        if not path.exists():
            raise HTTPException(404, "image not rendered yet")
        return FileResponse(path, headers=cache)

    @app.get("/api/sources/{source_id}/suggest-corners")
    def suggest_corners(source_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        src = pages.get_source(conn, source_id)
        corners = pages.suggest_corners(store, src)
        return {"corners": corners, "frame": "working", "frame_w": src["working_w"], "frame_h": src["working_h"],
                "note": None if corners else "no clear page edge found; drag the corners manually"}

    @app.post("/api/sources/{source_id}/processing")
    def set_processing(source_id: str, body: S.ProcessingIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        src = pages.get_source(conn, source_id)
        spec = pages.validate_processing(body.model_dump(), src["working_w"], src["working_h"])
        return {"job_id": jobs.enqueue(conn, "process", {"source_id": source_id, "processing": spec})}

    @app.post("/api/sources/{source_id}/detect")
    def detect(source_id: str, body: S.DetectIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        pages.get_source(conn, source_id)
        return {"job_id": jobs.enqueue(conn, "detect", {"source_id": source_id, "replace": body.replace})}

    @app.get("/api/sources/{source_id}/analysis")
    def source_analysis(source_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return pages.analysis(conn, source_id)

    @app.post("/api/sources/{source_id}/omit")
    def omit_area(source_id: str, body: S.OmitIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        box = Box(**body.box.model_dump())
        out = pages.omit_area(conn, source_id, box, body.reason, body.processing_rev)
        if body.same_size_pages:
            out["other_pages"] = pages.omit_on_same_size_pages(conn, source_id, box, body.reason)
        return out

    @app.post("/api/sources/{source_id}/suggestions/{region_id}/accept")
    def accept_suggestion(source_id: str, region_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return pages.accept_suggestion(conn, source_id, region_id)

    @app.post("/api/sources/{source_id}/suggestions/{region_id}/dismiss")
    def dismiss_suggestion(source_id: str, region_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        pages.dismiss_suggestion(conn, source_id, region_id)
        return {"ok": True}

    @app.delete("/api/sources/{source_id}/omit/{area_id}")
    def restore_area(source_id: str, area_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return pages.restore_area(conn, source_id, area_id)

    @app.delete("/api/sources/{source_id}")
    def delete_source(source_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        """Remove a page from the app: it and its boxes disappear from Sources, Check, Review and future
        exports. The original file stays on disk; the same file can be uploaded again later."""
        pages.get_source(conn, source_id)
        with transaction(conn):
            conn.execute("UPDATE sources SET deleted = 1 WHERE id = ?", (source_id,))
            n = conn.execute("UPDATE regions SET deleted = 1 WHERE source_id = ? AND deleted = 0", (source_id,)).rowcount
        return {"ok": True, "regions": n, "note": "removed from the app; the original file is kept on disk"}

    # ---------- regions ----------
    @app.get("/api/sources/{source_id}/regions")
    def page_regions(source_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        src = pages.get_source(conn, source_id)
        rows = conn.execute("SELECT * FROM regions WHERE source_id = ? AND deleted = 0 ORDER BY y, x LIMIT 5000", (source_id,)).fetchall()
        links = [dict(r) for r in conn.execute(
            "SELECT l.* FROM links l JOIN regions r ON r.id = l.a_id WHERE r.source_id = ? AND r.deleted = 0", (source_id,))]
        pending = {r["region_id"] for r in conn.execute(
            "SELECT DISTINCT region_id FROM proposals WHERE source_id = ? AND applied = 0 AND stale = 0 AND region_id IS NOT NULL", (source_id,))}
        regions = [row_dict(r, REGION_JSON) for r in rows]
        for r in regions:
            r["has_unapplied_proposal"] = r["id"] in pending  # type: ignore[index]
        return {"frame": {"name": "processed", "width": src["processed_w"], "height": src["processed_h"], "processing_rev": src["processing_rev"]},
                "regions": regions, "links": links}

    @app.get("/api/regions")
    def region_queue(
        conn: sqlite3.Connection = Depends(db), status: str | None = None, flag: str | None = None, kind: str | None = None,
        source_type: str | None = None, session: str | None = None, source_id: str | None = None, flagged: bool = False,
        limit: int = Query(50, le=200), offset: int = 0,
    ) -> dict[str, Any]:
        where, args = ["r.deleted = 0", "s.deleted = 0"], []
        for clause, val in (("r.status = ?", status), ("r.kind = ?", kind), ("s.source_type = ?", source_type),
                            ("s.session = ?", session), ("r.source_id = ?", source_id)):
            if val:
                where.append(clause)
                args.append(val)
        if flag:
            where.append("EXISTS (SELECT 1 FROM json_each(r.flags) WHERE value = ?)")
            args.append(flag)
        if flagged:
            where.append("r.flags != '[]'")
        sql = " AND ".join(where)
        total = conn.execute(f"SELECT COUNT(*) FROM regions r JOIN sources s ON s.id = r.source_id WHERE {sql}", args).fetchone()[0]
        rows = conn.execute(
            f"""SELECT r.*, s.original_filename, s.source_type, s.session FROM regions r JOIN sources s ON s.id = r.source_id
                WHERE {sql} ORDER BY r.deferred, s.created_at, r.source_id, r.y, r.x LIMIT ? OFFSET ?""", args + [limit, offset],
        ).fetchall()
        flags = [r[0] for r in conn.execute(
            "SELECT DISTINCT j.value FROM regions r, json_each(r.flags) j WHERE r.deleted = 0 ORDER BY 1")]
        return {"total": total, "items": [row_dict(r, REGION_JSON) for r in rows], "flags": flags}

    @app.get("/api/audit")
    def audit_sample(n: int = Query(20, le=100), seed: int | None = None, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        """Random sample of unflagged, unreviewed machine proposals: confidently wrong labels exist."""
        ids = [r[0] for r in conn.execute("SELECT id FROM regions WHERE deleted = 0 AND status = 'proposed' AND flags = '[]'")]
        pick = random.Random(seed).sample(ids, min(n, len(ids)))
        rows = [row_dict(conn.execute("SELECT * FROM regions WHERE id = ?", (i,)).fetchone(), REGION_JSON) for i in pick]
        return {"population": len(ids), "items": rows}

    @app.post("/api/sources/{source_id}/regions")
    def create_region(source_id: str, body: S.RegionCreate, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        src = pages.get_source(conn, source_id)
        review.check_frame(src, body.processing_rev)
        with transaction(conn):
            return review.create_region(conn, src, body.kind, Box(**body.box.model_dump()), actor="human", cls=body.cls,
                                        parent_id=body.parent_id, text=body.text, status="needs_review")

    @app.patch("/api/regions/{region_id}")
    def patch_region(region_id: str, body: S.RegionPatch, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        changes = body.model_dump(exclude_unset=True, exclude={"expected_revision", "reason", "clear_text", "processing_rev"})
        if body.clear_text:
            changes["text"] = None
        if "box" in changes and body.box is not None:
            changes["box"] = body.box.model_dump()
        return review.human_edit(conn, region_id, changes, body.expected_revision, body.reason, body.processing_rev)

    # ---------- quick check: a line at a time ----------
    @app.get("/api/check")
    def check(source_id: str | None = None, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        open_marks = ",".join("?" * len(review.OPEN))
        pages_open = [dict(r) for r in conn.execute(
            f"""SELECT s.id, s.original_filename, COUNT(*) AS open_lines,
                       SUM(r.text IS NOT NULL) AS read_lines
                FROM regions r JOIN sources s ON s.id = r.source_id
                WHERE r.deleted = 0 AND s.deleted = 0 AND r.kind = 'line' AND r.cls IN ('handwriting', 'uncertain', 'equation')
                  AND r.status IN ({open_marks})
                GROUP BY s.id ORDER BY s.created_at, s.id""", review.OPEN)]
        if source_id is None and pages_open:
            source_id = next((p["id"] for p in pages_open if p["read_lines"]), pages_open[0]["id"])
        if source_id is None:
            return {"pages": [], "source": None, "lines": []}
        src = pages.get_source(conn, source_id)
        return {"pages": pages_open, "source": {"id": src["id"], "name": src["original_filename"], "processing_rev": src["processing_rev"]},
                "lines": review.check_lines(conn, source_id)}

    @app.post("/api/check/approve-clear")
    def approve_clear(body: S.ApproveClearIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        if body.source_id:
            pages.get_source(conn, body.source_id)
        return review.approve_clear_words(conn, body.source_id)

    @app.post("/api/lines/{line_id}/approve")
    def approve_line(line_id: str, body: S.ApproveLineIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return review.approve_line(conn, line_id, body.words, body.text, body.expected_revision)

    @app.post("/api/lines/{line_id}/recut")
    def recut_line(line_id: str, body: S.RecutLineIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        review.get_region(conn, line_id)
        return {"job_id": jobs.enqueue(conn, "recut_line", {"line_id": line_id, "text": body.text})}

    @app.post("/api/lines/{line_id}/exclude")
    def exclude_line(line_id: str, body: S.ExcludeLineIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return review.exclude_line(conn, line_id, body.reason)

    @app.post("/api/regions/{region_id}/undo")
    def undo(region_id: str, body: S.RevisionIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return review.undo(conn, region_id, body.expected_revision)

    @app.post("/api/regions/{region_id}/split")
    def split(region_id: str, body: S.SplitIn, conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        if (body.at_x is None) == (body.at_y is None):
            raise review.ReviewError("give either at_x (side by side) or at_y (top and bottom)")
        if body.at_y is not None:
            return review.split_region_rows(conn, region_id, body.at_y)
        return review.split_region(conn, region_id, body.at_x, body.texts)

    @app.post("/api/regions/merge")
    def merge(body: S.MergeIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return review.merge_regions(conn, body.ids, body.text)

    @app.delete("/api/regions/{region_id}")
    def delete_region(region_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return review.delete_region(conn, region_id)

    @app.get("/api/regions/{region_id}/crop")
    def crop(region_id: str, conn: sqlite3.Connection = Depends(db)) -> Response:
        region = review.get_region(conn, region_id)
        _, path = pages.region_crop(conn, store, region)
        return FileResponse(path, media_type="image/png", headers={"cache-control": "private, max-age=86400"})

    @app.get("/api/regions/{region_id}/history")
    def history(region_id: str, conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        return [row_dict(r, ("data",)) for r in conn.execute(
            "SELECT revision, actor, action, data, created_at FROM region_revisions WHERE region_id = ? ORDER BY revision DESC LIMIT 100", (region_id,))]  # type: ignore[misc]

    @app.get("/api/regions/{region_id}/proposals")
    def proposals(region_id: str, conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        return [row_dict(r, ("payload",)) for r in conn.execute(
            "SELECT * FROM proposals WHERE region_id = ? ORDER BY created_at DESC LIMIT 20", (region_id,))]  # type: ignore[misc]

    @app.post("/api/regions/{region_id}/apply-proposal")
    def apply_proposal(region_id: str, body: S.ApplyProposalIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return review.apply_proposal_explicitly(conn, region_id, body.proposal_id, body.expected_revision)

    @app.post("/api/links/mark")
    def link_mark(body: S.MarkLinkIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {"id": review.link_mark(conn, body.mark_id, body.target_id, body.span, body.unknown_target, body.unknown_original, body.note)}

    @app.post("/api/links/before-after")
    def link_before_after(body: S.BeforeAfterIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {"id": review.link_before_after(conn, body.before_id, body.after_id, body.note)}

    # ---------- recognition ----------
    @app.post("/api/recognition/preview")
    def recognition_preview(body: S.RecognitionIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return recpkg.preview(conn, store, settings, body.source_ids, body.include_overview)

    @app.post("/api/recognition/packages")
    def build_package(body: S.RecognitionIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        for sid in body.source_ids:
            pages.get_source(conn, sid)
        return {"job_id": jobs.enqueue(conn, "build_recognition", body.model_dump())}

    @app.get("/api/recognition/packages")
    def list_packages(conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        rows = [dict(r) for r in conn.execute(
            """SELECT p.*, SUM(i.status = 'pending') AS pending, SUM(i.status = 'failed') AS failed, SUM(i.status = 'succeeded') AS succeeded
               FROM recognition_packages p LEFT JOIN recognition_items i ON i.package_id = p.id
               GROUP BY p.id ORDER BY p.created_at DESC LIMIT 50""")]
        for r in rows:
            r["ready_to_import"] = recpkg.results_waiting(store, conn, r["id"]) if (r["pending"] or r["failed"]) else 0
        return rows

    @app.get("/api/recognition/packages/{package_id}")
    def get_package(package_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        pkg = conn.execute("SELECT * FROM recognition_packages WHERE id = ?", (package_id,)).fetchone()
        if pkg is None:
            raise HTTPException(404, "package not found")
        return {**dict(pkg), "items": recpkg.item_readings(conn, package_id), "path": str(recpkg.package_dir(store, package_id))}

    @app.post("/api/recognition/packages/{package_id}/import")
    def import_package(package_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        recpkg.load_manifest(store, package_id)
        return {"job_id": jobs.enqueue(conn, "import_recognition", {"package_id": package_id})}

    @app.post("/api/recognition/packages/{package_id}/headless")
    def headless_run(package_id: str, body: S.HeadlessIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        if not settings.headless_recognition:
            raise HTTPException(409, "headless recognition is off; set HANDPRINTER_HEADLESS_RECOGNITION=1 or run /recognise in Claude Code")
        recpkg.load_manifest(store, package_id)
        return {"job_id": jobs.enqueue(conn, "headless_recognition", {"package_id": package_id, "item_ids": body.item_ids})}

    @app.post("/api/recognition/packages/{package_id}/retry-failed")
    def retry_failed(package_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        recpkg.load_manifest(store, check_id(package_id))
        root = recpkg.package_dir(store, package_id)
        rejected = root / "rejected"
        rejected.mkdir(exist_ok=True)
        failed = [r["item_id"] for r in conn.execute("SELECT item_id FROM recognition_items WHERE package_id = ? AND status = 'failed'", (package_id,))]
        for item_id in failed:
            result = root / "results" / f"{check_item(item_id)}.json"
            if result.exists():
                result.replace(rejected / f"{item_id}-{int(time.time())}.json")
        conn.execute("UPDATE recognition_items SET status = 'pending' WHERE package_id = ? AND status = 'failed'", (package_id,))
        return {"reset": failed}

    # ---------- dataset ----------
    @app.get("/api/dataset/coverage")
    def coverage(conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return dataset.coverage(conn)

    @app.post("/api/dataset/snapshots")
    def create_snapshot(body: S.SnapshotIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {"job_id": jobs.enqueue(conn, "export_snapshot", body.model_dump())}

    @app.get("/api/dataset/snapshots")
    def list_snapshots(conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        rows = conn.execute("SELECT * FROM snapshots ORDER BY created_at DESC LIMIT 100").fetchall()
        out = []
        for r in rows:
            item = row_dict(r, ("filters", "split_config", "counts", "warnings"))
            item["path"] = str(store.resolve_relative(r["path"]))  # type: ignore[index]
            out.append(item)
        return out  # type: ignore[return-value]

    # ---------- experiments ----------
    @app.get("/api/experiments/adapters")
    def adapters() -> list[dict[str, Any]]:
        out = []
        for a in all_adapters(settings.adapter_env).values():
            caps = a.capabilities()
            out.append({**a.status().as_dict(), "capabilities": caps.__dict__})
        return out

    @app.post("/api/experiments/adapters/{name}/{action}")
    def adapter_action(name: str, action: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        if name not in all_adapters(settings.adapter_env) or action not in ("preflight", "download"):
            raise HTTPException(404, "unknown adapter or action")
        return {"job_id": jobs.enqueue(conn, f"adapter_{action}", {"adapter": name})}

    @app.post("/api/experiments/runs")
    def start_run(body: S.RunIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        adapter = all_adapters(settings.adapter_env).get(body.adapter)
        if adapter is None:
            raise HTTPException(404, "unknown adapter")
        st = adapter.status()
        if st.state != "ready":
            raise HTTPException(409, f"{adapter.title} is not ready: {'; '.join(st.blockers)}. Next step: {st.next_step}")
        return {"job_id": jobs.enqueue(conn, "adapter_infer", body.model_dump())}

    @app.post("/api/experiments/adapters/{name}/train")
    def start_training(name: str, body: S.TrainIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        adapter = all_adapters(settings.adapter_env).get(name)
        if adapter is None:
            raise HTTPException(404, "unknown adapter")
        caps = adapter.capabilities()
        if not caps.can_train_in_app:
            raise HTTPException(409, f"{adapter.title}: training is not offered ({caps.train_note})")
        st = adapter.status()
        if st.state != "ready":
            raise HTTPException(409, f"{adapter.title} is not ready: {'; '.join(st.blockers)}. Next step: {st.next_step}")
        try:
            items, left_out = evaluation.training_items(store, conn, body.snapshot_id, caps.charset)
        except ValueError as exc:
            raise HTTPException(404, str(exc)) from None
        if len(items) < evaluation.MIN_TRAIN_WORDS:
            raise HTTPException(409, f"only {len(items)} usable training words in this snapshot "
                                     f"(left out: {left_out or 'none'}); approve at least {evaluation.MIN_TRAIN_WORDS} first")
        return {"job_id": jobs.enqueue(conn, "adapter_train", {"adapter": name, **body.model_dump()}), "train_words": len(items)}

    @app.get("/api/experiments/checkpoints")
    def checkpoints(conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        return evaluation.personal_checkpoints(conn)

    @app.get("/api/experiments/runs")
    def runs(conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        return evaluation.list_runs(conn)

    @app.get("/api/experiments/prompts")
    def prompt_set() -> dict[str, Any]:
        return {"prompts": evaluation.DEFAULT_PROMPTS, "seeds": evaluation.DEFAULT_SEEDS}

    @app.get("/api/experiments/style-profiles")
    def style_profiles(conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        return [row_dict(r, ("region_ids",)) for r in conn.execute("SELECT * FROM style_profiles ORDER BY created_at DESC")]  # type: ignore[misc]

    @app.post("/api/experiments/style-profiles")
    def create_profile(body: S.StyleProfileIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return evaluation.create_style_profile(conn, body.name, body.snapshot_id, body.count)

    @app.get("/api/experiments/comparison")
    def comparison(level: str = "word", conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {**evaluation.comparable_prompts(conn, level), "summary": evaluation.ab_summary(conn)}

    @app.post("/api/experiments/trials")
    def new_trial(body: S.TrialIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        trial = evaluation.next_trial(conn, body.level)
        if trial is None:
            raise HTTPException(409, "no prompt has outputs from two models yet; run both adapters on the fixed prompt set first")
        return trial

    @app.post("/api/experiments/trials/{trial_id}/rate")
    def rate(trial_id: str, body: S.RateIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return evaluation.rate_trial(conn, trial_id, body.choice)

    @app.get("/api/generations")
    def generations(conn: sqlite3.Connection = Depends(db), limit: int = Query(60, le=200), offset: int = 0) -> list[dict[str, Any]]:
        return [dict(r) for r in conn.execute("SELECT * FROM generations ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset))]

    @app.get("/api/generations/{gen_id}/image")
    def generation_image(gen_id: str, conn: sqlite3.Connection = Depends(db)) -> Response:
        row = conn.execute("SELECT image_path FROM generations WHERE id = ?", (gen_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "generation not found")
        return FileResponse(store.resolve_relative(row["image_path"]), media_type="image/png")

    # ---------- studio ----------
    @app.get("/api/studio/assets")
    def assets(conn: sqlite3.Connection = Depends(db), limit: int = Query(100, le=200), offset: int = 0) -> list[dict[str, Any]]:
        return studio.list_assets(conn, limit, offset)

    @app.post("/api/studio/assets")
    async def upload_asset(file: UploadFile = File(...), label: str | None = Form(default=None, max_length=120)) -> dict[str, Any]:
        data = await file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            raise HTTPException(413, "file too large")
        conn = connect(settings.db_path)
        try:
            return studio.add_upload_asset(conn, store, data, label or Path(file.filename or "image").stem)
        except imaging.UnsupportedInput as exc:
            raise HTTPException(422, str(exc)) from exc
        finally:
            conn.close()

    @app.post("/api/studio/assets/from-region")
    def asset_from_region(body: S.RefIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return studio.add_region_asset(conn, store, body.id)

    @app.post("/api/studio/assets/from-generation")
    def asset_from_generation(body: S.RefIn, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return studio.add_generation_asset(conn, store, body.id)

    @app.get("/api/studio/assets/{asset_id}/image")
    def asset_image(asset_id: str, conn: sqlite3.Connection = Depends(db)) -> Response:
        row = conn.execute("SELECT path FROM studio_assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "asset not found")
        return FileResponse(store.resolve_relative(row["path"]), media_type="image/png", headers={"cache-control": "private, max-age=86400"})

    @app.get("/api/studio/compositions")
    def compositions(conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        return studio.list_compositions(conn)

    @app.get("/api/studio/compositions/{comp_id}")
    def get_composition(comp_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return studio.get_composition(conn, comp_id)

    @app.post("/api/studio/compositions")
    def new_composition(body: S.CompositionSave, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return studio.save_composition(conn, studio.Composition.model_validate(body.composition), None, None)

    @app.put("/api/studio/compositions/{comp_id}")
    def save_composition(comp_id: str, body: S.CompositionSave, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return studio.save_composition(conn, studio.Composition.model_validate(body.composition), comp_id, body.expected_revision)

    @app.post("/api/studio/render")
    def render(body: S.RenderIn, conn: sqlite3.Connection = Depends(db)) -> Response:
        comp = studio.Composition.model_validate(body.composition)
        png = studio.render(conn, store, comp, body.dpi)
        return Response(png, media_type="image/png", headers={"content-disposition": f'attachment; filename="page-{body.dpi}dpi.png"'})

    # ---------- jobs ----------
    @app.get("/api/jobs")
    def list_jobs(active: bool = False, conn: sqlite3.Connection = Depends(db)) -> list[dict[str, Any]]:
        return jobs.list_jobs(conn, active_only=active)

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        job = jobs.get(conn, job_id)
        if job is None:
            raise HTTPException(404, "job not found")
        return job

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {"ok": jobs.request_cancel(conn, job_id)}

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str, conn: sqlite3.Connection = Depends(db)) -> dict[str, Any]:
        return {"job_id": jobs.retry(conn, job_id)}

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app


def check_item(item_id: str) -> str:
    if not item_id.isalnum():
        raise ValueError("invalid item id")
    return item_id
