"""Import of uploaded files and ZIP archives into sources. Runs in the worker."""

from __future__ import annotations

import json
import sqlite3
import stat
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from handprinter import imaging
from handprinter.config import MAX_ARCHIVE_ENTRIES, MAX_ARCHIVE_EXPANDED_BYTES, MAX_PDF_PAGES, MAX_UPLOAD_BYTES
from handprinter.db import dumps, new_id, now_iso, transaction
from handprinter.storage import Store, atomic_write, sha256_bytes

NEAR_DUPLICATE_DISTANCE = 24   # of 256 dHash bits
NEAR_DUPLICATE_INK_IOU = 0.6    # ink-layout overlap; see docs/architecture.md (open question)


@dataclass
class EntryResult:
    name: str
    status: str  # imported | duplicate | rejected | skipped
    detail: str = ""
    source_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail, "source_ids": self.source_ids}


@dataclass
class BatchInfo:
    batch_id: str
    source_type: str
    session: str | None = None
    captured_date: str | None = None
    style_era: str = "current"


class ArchiveError(ValueError):
    pass


def unsafe_member_reason(info: zipfile.ZipInfo) -> str | None:
    """Why an archive member must not be extracted, or None if it is acceptable."""
    name = info.filename
    if "\\" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        return "absolute or non-portable path"
    parts = PurePosixPath(name).parts
    if any(part == ".." for part in parts):
        return "path traversal"
    if stat.S_ISLNK(info.external_attr >> 16):
        return "symbolic link"
    if info.flag_bits & 0x1:
        return "encrypted entry"
    if info.file_size > MAX_UPLOAD_BYTES:
        return f"entry expands to {info.file_size:,} bytes (limit {MAX_UPLOAD_BYTES:,})"
    return None


def iter_archive(path: Path) -> Iterator[tuple[str, bytes | None, str | None]]:
    """Yield (entry name, bytes or None, rejection reason or None) for each file entry.

    Enforces entry count, per-entry and total expanded size limits while reading, so a
    lying header cannot expand past the bounds.
    """
    try:
        zf = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise ArchiveError(f"not a readable ZIP archive ({exc.__class__.__name__})") from exc
    with zf:
        infos = [i for i in zf.infolist() if not i.is_dir()]
        if len(infos) > MAX_ARCHIVE_ENTRIES:
            raise ArchiveError(f"archive has {len(infos)} entries (limit {MAX_ARCHIVE_ENTRIES})")
        expanded = 0
        for info in infos:
            name = info.filename
            base = PurePosixPath(name).name
            if name.startswith("__MACOSX/") or base.startswith("._") or base in {".DS_Store", "Thumbs.db"}:
                yield name, None, "skip:system metadata file"
                continue
            reason = unsafe_member_reason(info)
            if reason:
                yield name, None, reason
                continue
            try:
                data = _read_bounded(zf, info, MAX_UPLOAD_BYTES, MAX_ARCHIVE_EXPANDED_BYTES - expanded)
            except ArchiveError as exc:
                yield name, None, str(exc)
                continue
            except (zipfile.BadZipFile, OSError, RuntimeError, EOFError) as exc:
                yield name, None, f"corrupt entry ({exc.__class__.__name__})"
                continue
            expanded += len(data)
            yield name, data, None


def _read_bounded(zf: zipfile.ZipFile, info: zipfile.ZipInfo, entry_limit: int, total_left: int) -> bytes:
    limit = min(entry_limit, total_left)
    chunks, size = [], 0
    with zf.open(info) as fh:
        while chunk := fh.read(1 << 20):
            size += len(chunk)
            if size > limit:
                which = "entry" if limit == entry_limit else "archive total"
                raise ArchiveError(f"{which} expanded size limit exceeded")
            chunks.append(chunk)
    return b"".join(chunks)


def import_upload(
    conn: sqlite3.Connection,
    store: Store,
    staged: Path,
    filename: str,
    batch: BatchInfo,
    progress: Callable[[float, str], None] = lambda f, m: None,
) -> list[EntryResult]:
    """Import one staged upload (an image, PDF or ZIP). Each entry succeeds or fails alone."""
    size = staged.stat().st_size
    if size > MAX_UPLOAD_BYTES:
        return [EntryResult(filename, "rejected", f"file is {size:,} bytes (limit {MAX_UPLOAD_BYTES:,})")]
    with staged.open("rb") as fh:
        kind = imaging.sniff(fh.read(16))
    if kind is None:
        return [EntryResult(filename, "rejected", "unsupported or unrecognised file content")]
    if kind != "zip":
        return [_import_bytes(conn, store, staged.read_bytes(), kind, filename, None, batch)]
    results: list[EntryResult] = []
    try:
        with zipfile.ZipFile(staged) as zf:
            total = sum(1 for i in zf.infolist() if not i.is_dir())
    except (zipfile.BadZipFile, OSError) as exc:
        return [EntryResult(filename, "rejected", f"not a readable ZIP archive ({exc.__class__.__name__})")]
    entries = iter_archive(staged)  # lazy: one entry's bytes in memory at a time
    index = 0
    while True:
        try:
            name, data, reason = next(entries)
        except StopIteration:
            break
        except ArchiveError as exc:
            results.append(EntryResult(filename, "rejected", str(exc)))
            break
        index += 1
        progress(index / max(1, total), name)
        label = f"{filename}/{name}"
        if reason:
            status = "skipped" if reason.startswith("skip:") else "rejected"
            results.append(EntryResult(label, status, reason.removeprefix("skip:")))
            continue
        assert data is not None
        entry_kind = imaging.sniff(data[:16])
        if entry_kind == "zip":
            results.append(EntryResult(label, "rejected", "nested archives are not accepted"))
        elif entry_kind is None:
            results.append(EntryResult(label, "rejected", "unsupported or unrecognised file content"))
        else:
            results.append(_import_bytes(conn, store, data, entry_kind, name, filename, batch))
    return results or [EntryResult(filename, "rejected", "archive contains no files")]


def _import_bytes(
    conn: sqlite3.Connection, store: Store, data: bytes, kind: str, name: str, container: str | None, batch: BatchInfo
) -> EntryResult:
    label = f"{container}/{name}" if container else name
    name = PurePosixPath(name).name
    digest = sha256_bytes(data)
    existing = conn.execute("SELECT id FROM sources WHERE sha256 = ? AND deleted = 0 LIMIT 1", (digest,)).fetchone()
    if existing:
        return EntryResult(label, "duplicate", f"identical bytes already imported as {existing['id']}", [existing["id"]])
    try:
        pages = imaging.render_pdf(data, MAX_PDF_PAGES) if kind == "pdf" else iter([imaging.decode_image(data, kind)])
    except imaging.UnsupportedInput as exc:
        return EntryResult(label, "rejected", str(exc))
    group_id = new_id("grp")
    original_written: Path | None = None
    ids: list[str] = []
    while True:
        try:
            page = next(pages)
        except StopIteration:
            break
        except imaging.UnsupportedInput as exc:
            if not ids:
                return EntryResult(label, "rejected", str(exc))
            return EntryResult(label, "imported", f"{len(ids)} page(s) imported, then stopped: {exc}", ids)
        source_id = new_id("src")
        if original_written is None:
            original_written = store.original(source_id, imaging.EXT[kind])
            atomic_write(original_written, data)
        working = imaging.orient(page)
        atomic_write(store.working(source_id), imaging.png_bytes(working))
        imaging.save_preview(working, store.preview(source_id, 0, "thumb"), imaging.THUMB_EDGE)
        imaging.save_preview(working, store.preview(source_id, 0, "display"), imaging.DISPLAY_EDGE)
        page_hash = imaging.dhash(working)
        ink = imaging.ink_signature(working)
        near = _near_duplicates(conn, page_hash, ink)
        with transaction(conn):
            conn.execute(
                """INSERT INTO sources (id, batch_id, group_id, original_filename, container_filename, page_index,
                   source_type, session, captured_date, style_era, mime, sha256, dhash, original_ext,
                   original_w, original_h, exif_orientation, working_w, working_h, processed_w, processed_h,
                   quality_flags, near_duplicates, created_at, original_path, ink_sig)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    source_id, batch.batch_id, group_id, name, container, page.page_index,
                    batch.source_type, batch.session, batch.captured_date, batch.style_era,
                    imaging.MIME[kind], digest, page_hash, imaging.EXT[kind],
                    page.image.width, page.image.height, page.orientation, working.width, working.height,
                    working.width, working.height, dumps(imaging.quality_flags(working)), dumps(near), now_iso(),
                    store.relative(original_written), ink,
                ),
            )
            for other in near:
                row = conn.execute("SELECT near_duplicates FROM sources WHERE id = ?", (other,)).fetchone()
                linked = sorted(set(_loads_list(row["near_duplicates"])) | {source_id})
                conn.execute("UPDATE sources SET near_duplicates = ? WHERE id = ?", (dumps(linked), other))
        ids.append(source_id)
    detail = f"{len(ids)} page(s)" if kind == "pdf" else ""
    near_any = conn.execute(
        f"SELECT COUNT(*) FROM sources WHERE id IN ({','.join('?' * len(ids))}) AND near_duplicates != '[]'", ids
    ).fetchone()[0]
    if near_any:
        detail = (detail + "; " if detail else "") + "looks like a near-duplicate of an existing page (kept, please check)"
    return EntryResult(label, "imported", detail, ids)


def _loads_list(value: str) -> list[str]:
    return list(json.loads(value or "[]"))


def _near_duplicates(conn: sqlite3.Connection, page_hash: str, ink: str) -> list[str]:
    """Likely re-photographs of the same page: close layout hash AND overlapping ink. Warned, never deleted."""
    rows = conn.execute("SELECT id, dhash, ink_sig FROM sources WHERE deleted = 0").fetchall()
    return [
        r["id"] for r in rows
        if len(r["dhash"]) == len(page_hash) and imaging.hamming(r["dhash"], page_hash) <= NEAR_DUPLICATE_DISTANCE
        and imaging.ink_similarity(r["ink_sig"], ink) >= NEAR_DUPLICATE_INK_IOU
    ]
