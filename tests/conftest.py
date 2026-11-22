from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from handprinter.config import Settings, load_settings
from handprinter.db import connect, init_db, new_id, now_iso
from handprinter.ingest import BatchInfo, import_upload
from handprinter.storage import Store
from tests.fixtures import encode, page


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return load_settings({"HANDPRINTER_DATA_DIR": str(tmp_path / "data")}, dotenv=tmp_path / "none.env")


@pytest.fixture
def store(settings: Settings) -> Store:
    return Store(settings)


@pytest.fixture
def conn(settings: Settings) -> sqlite3.Connection:
    c = connect(settings.db_path)
    init_db(c)
    yield c
    c.close()


@pytest.fixture
def batch(conn: sqlite3.Connection) -> BatchInfo:
    batch_id = new_id("bat")
    conn.execute("INSERT INTO batches (id, created_at, source_type) VALUES (?, ?, 'paper')", (batch_id, now_iso()))
    return BatchInfo(batch_id, "paper", session="s1")


@pytest.fixture
def import_bytes(conn, store, batch):
    def _import(data: bytes, name: str = "page.png", session: str | None = None):
        staged = store.staging(new_id("stg"))
        staged.write_bytes(data)
        b = BatchInfo(batch.batch_id, "paper", session=session or batch.session)
        return import_upload(conn, store, staged, name, b)

    return _import


@pytest.fixture
def fixture_source(import_bytes):
    """One imported synthetic page (not handwriting). Returns its source id."""
    def _make(lines=None, session=None, shift=0, variant=0) -> str:
        res = import_bytes(encode(page(lines, seed_shift=shift, variant=variant)), session=session)
        assert res[0].status == "imported", res[0].detail
        return res[0].source_ids[0]

    return _make
