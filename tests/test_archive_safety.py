"""Spec §4 input safety: content sniffing, traversal, symlinks, nested archives, bounds, per-entry results."""

from __future__ import annotations

import io
import zipfile

from handprinter import ingest
from tests.fixtures import encode, page, zip_bytes


def statuses(results):
    return {r.name.split("/", 1)[1] if "/" in r.name else r.name: (r.status, r.detail) for r in results}


def test_each_zip_entry_gets_its_own_result(import_bytes):
    good = encode(page(["alpha"]))
    other = encode(page(["beta"]), "JPEG")
    archive = zip_bytes({
        "a.png": good,
        "sub/b.jpg": other,
        "../evil.png": good,
        "nested.zip": zip_bytes({"x.png": good}),
        "notes.txt": b"hello",
        "broken.png": b"\x89PNG\r\n\x1a\n" + b"\x00" * 40,
        "__MACOSX/._a.png": b"junk",
    }, symlinks=("link.png",))
    results = import_bytes(archive, "batch.zip")
    got = statuses(results)
    assert got["a.png"][0] == "imported"
    assert got["sub/b.jpg"][0] == "imported"
    assert got["../evil.png"] == ("rejected", "path traversal")
    assert got["nested.zip"] == ("rejected", "nested archives are not accepted")
    assert got["notes.txt"][0] == "rejected"
    assert got["broken.png"][0] == "rejected" and "cannot decode" in got["broken.png"][1]
    assert got["link.png"] == ("rejected", "symbolic link")
    assert got["__MACOSX/._a.png"][0] == "skipped"


def test_extension_is_ignored_content_decides(import_bytes):
    assert import_bytes(b"GIF89a fake", "photo.jpg")[0].status == "rejected"
    png_named_jpg = import_bytes(encode(page(["gamma"])), "photo.jpg")
    assert png_named_jpg[0].status == "imported"


def test_zip_is_never_a_single_image(import_bytes):
    results = import_bytes(zip_bytes({"one.png": encode(page(["delta"]))}), "pages.zip")
    assert [r.status for r in results] == ["imported"]
    assert results[0].name == "pages.zip/one.png"


def test_entry_count_and_expanded_size_bounds(import_bytes, monkeypatch):
    monkeypatch.setattr(ingest, "MAX_ARCHIVE_ENTRIES", 2)
    results = import_bytes(zip_bytes({f"{i}.png": b"x" for i in range(3)}), "many.zip")
    assert results[0].status == "rejected" and "entries" in results[0].detail

    monkeypatch.setattr(ingest, "MAX_ARCHIVE_ENTRIES", 100)
    monkeypatch.setattr(ingest, "MAX_ARCHIVE_EXPANDED_BYTES", 5_000)
    big = encode(page(["epsilon"]))
    assert len(big) > 5_000
    results = import_bytes(zip_bytes({"big.png": big}), "bomb.zip")
    assert results[0].status == "rejected" and "expanded size" in results[0].detail


def test_lying_zip_header_cannot_expand_past_limit(import_bytes, monkeypatch):
    # Header claims a small size; the bounded reader still stops at the real limit.
    monkeypatch.setattr(ingest, "MAX_ARCHIVE_EXPANDED_BYTES", 5_000)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("zeros.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 50_000)
    results = import_bytes(buf.getvalue(), "z.zip")
    assert results[0].status == "rejected"


def test_exact_duplicate_detected_and_original_bytes_preserved(import_bytes, store, conn):
    data = encode(page(["zeta"]), "JPEG")
    first = import_bytes(data, "a.jpg")[0]
    second = import_bytes(data, "b.jpg")[0]
    assert second.status == "duplicate" and first.source_ids == second.source_ids
    row = conn.execute("SELECT original_path FROM sources WHERE id = ?", (first.source_ids[0],)).fetchone()
    assert store.resolve_relative(row["original_path"]).read_bytes() == data


def test_near_duplicate_warned_not_deleted(import_bytes, conn):
    a = import_bytes(encode(page(["eta theta"])), "a.png")[0]
    b = import_bytes(encode(page(["eta theta"]), "JPEG"), "b.jpg")[0]  # same picture, different bytes
    assert b.status == "imported" and "near-duplicate" in b.detail
    count = conn.execute("SELECT COUNT(*) FROM sources WHERE deleted = 0").fetchone()[0]
    assert count == 2
    assert a.source_ids[0] in conn.execute("SELECT near_duplicates FROM sources WHERE id = ?", (b.source_ids[0],)).fetchone()[0]


def test_unsafe_member_names():
    def info(name, attr=0):
        zi = zipfile.ZipInfo(name)
        zi.external_attr = attr
        return zi

    assert ingest.unsafe_member_reason(info("/abs.png")) is not None
    assert ingest.unsafe_member_reason(info("C:/x.png")) is not None
    assert ingest.unsafe_member_reason(info("a\\..\\b.png")) is not None
    assert ingest.unsafe_member_reason(info("ok/inner.png")) is None


def test_pdf_pages_become_grouped_sources(import_bytes, conn):
    import io

    pages_ = [page([f"pdf page {i}"], size=(600, 800), variant=i + 1) for i in range(3)]
    buf = io.BytesIO()
    pages_[0].save(buf, "PDF", save_all=True, append_images=pages_[1:], resolution=100)
    result = import_bytes(buf.getvalue(), "notebook.pdf")[0]
    assert result.status == "imported" and len(result.source_ids) == 3
    rows = conn.execute("SELECT group_id, page_index FROM sources WHERE id IN (?,?,?) ORDER BY page_index", result.source_ids).fetchall()
    assert len({r["group_id"] for r in rows}) == 1 and [r["page_index"] for r in rows] == [0, 1, 2]
