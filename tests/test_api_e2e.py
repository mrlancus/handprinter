"""End to end on labelled, non-personal fixtures: import → manual annotation → export, through the HTTP API.

Also checks the cross-origin protections and that state survives an app restart.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from handprinter import jobs, worker
from handprinter.db import connect
from handprinter.storage import Store
from handprinter.web.app import create_app
from tests.fixtures import encode, page, zip_bytes


def make_client(settings):
    client = TestClient(create_app(settings, extra_hosts={"testserver"}))
    token = re.search(r'name="hp-token" content="([^"]+)"', client.get("/").text).group(1)
    client.headers.update({"x-handprinter-token": token})
    return client


def drain(settings):
    conn = connect(settings.db_path)
    store = Store(settings)
    while True:
        row = jobs.claim(conn, "general", 1) or jobs.claim(conn, "gpu", 1)
        if row is None:
            break
        worker.run_one(conn, store, settings, row)
    conn.close()


@pytest.fixture
def client(settings):
    return make_client(settings)


def test_state_changing_requests_need_token_same_origin_and_host(settings):
    raw = TestClient(create_app(settings, extra_hosts={"testserver"}))
    assert raw.post("/api/batches", json={}).status_code == 403
    token = re.search(r'name="hp-token" content="([^"]+)"', raw.get("/").text).group(1)
    hdr = {"x-handprinter-token": token}
    assert raw.post("/api/batches", json={}, headers={**hdr, "origin": "http://evil.example"}).status_code == 403
    assert raw.post("/api/batches", json={}, headers={**hdr, "host": "evil.example"}).status_code == 400
    assert raw.post("/api/batches", json={}, headers=hdr).status_code == 200
    assert "access-control-allow-origin" not in raw.get("/api/status").headers


def test_lan_mode_refuses_without_access_token(tmp_path):
    from handprinter.config import load_settings

    s = load_settings({"HANDPRINTER_DATA_DIR": str(tmp_path / "d"), "HANDPRINTER_HOST": "0.0.0.0"}, dotenv=tmp_path / "x")
    with pytest.raises(RuntimeError, match="ACCESS_TOKEN"):
        create_app(s)


def test_import_annotate_export_and_restart(client, settings):
    batch = client.post("/api/batches", json={"source_type": "paper", "session": "fixture-session"}).json()["batch_id"]
    archive = zip_bytes({"one.png": encode(page(["alpha beta"], variant=1)), "two.jpg": encode(page(["gamma delta"], variant=6), "JPEG"), "bad.txt": b"no"})
    up = client.post(f"/api/batches/{batch}/files", files={"file": ("pages.zip", archive, "application/zip")}).json()
    assert up["status"] == "queued"
    drain(settings)
    job = client.get(f"/api/jobs/{up['job_id']}").json()
    entries = {e["name"]: e["status"] for e in job["result"]["entries"]}
    assert entries == {"pages.zip/one.png": "imported", "pages.zip/two.jpg": "imported", "pages.zip/bad.txt": "rejected"}

    sources = client.get("/api/sources").json()
    assert sources["total"] == 2 and sources["sessions"] == ["fixture-session"]
    sid = sources["items"][0]["id"]
    assert client.get(f"/api/sources/{sid}/image?variant=thumb").status_code == 200
    regions = client.get(f"/api/sources/{sid}/regions").json()
    words = [r for r in regions["regions"] if r["kind"] == "word"]
    assert regions["frame"]["name"] == "processed" and len(words) >= 2

    # manual annotation: fix text, approve one, exclude the other with a reason
    w0, w1 = words[:2]
    r = client.patch(f"/api/regions/{w0['id']}", json={"text": "alpha", "status": "approved", "expected_revision": w0["revision"]}).json()
    assert r["status"] == "approved"
    stale = client.patch(f"/api/regions/{w0['id']}", json={"text": "x", "expected_revision": w0["revision"]})
    assert stale.status_code == 409
    client.patch(f"/api/regions/{w1['id']}", json={"status": "excluded", "reason": "fixture test"})
    crop = client.get(f"/api/regions/{w0['id']}/crop")
    assert crop.status_code == 200 and crop.headers["content-type"] == "image/png"

    snap_job = client.post("/api/dataset/snapshots", json={"name": "fixture export"}).json()["job_id"]
    drain(settings)
    result = client.get(f"/api/jobs/{snap_job}").json()
    assert result["status"] == "succeeded", result["error"]
    assert result["result"]["counts"]["words"] == 1

    # restart: a fresh app on the same data directory sees the same state
    again = make_client(settings)
    assert again.get("/api/sources").json()["total"] == 2
    assert again.get(f"/api/sources/{sid}/regions").json()["regions"]
    assert len(again.get("/api/dataset/snapshots").json()) == 1


def test_unavailable_operations_explain_themselves(client):
    adapters = {a["name"]: a for a in client.get("/api/experiments/adapters").json()}
    assert adapters["eruku"]["state"] == "blocked" and adapters["eruku"]["next_step"]
    r = client.post("/api/experiments/runs", json={"adapter": "eruku"})
    assert r.status_code == 409 and "Next step" in r.json()["detail"]
    r = client.post("/api/recognition/packages/rec_0000000000000000/headless", json={})
    assert r.status_code == 409


def test_studio_works_with_imported_images_only(client):
    asset = client.post("/api/studio/assets", files={"file": ("w.png", encode(page(["ink"], size=(300, 100))), "image/png")}).json()
    comp = {"name": "p", "page_w_mm": 100, "page_h_mm": 50, "elements": [
        {"id": "e1", "asset_id": asset["id"], "x_mm": 10, "y_mm": 10, "w_mm": 30, "h_mm": 10}]}
    saved = client.post("/api/studio/compositions", json={"composition": comp}).json()
    assert client.get(f"/api/studio/compositions/{saved['id']}").json()["data"]["elements"][0]["asset_id"] == asset["id"]
    png = client.post("/api/studio/render", json={"composition": comp, "dpi": 254})
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(png.content))
    assert img.size == (1000, 500)  # 100 mm × 50 mm at 254 dpi = 10 px/mm


def test_lan_mode_requires_sign_in_for_remote_clients(tmp_path):
    from handprinter.config import load_settings

    s = load_settings({"HANDPRINTER_DATA_DIR": str(tmp_path / "d"), "HANDPRINTER_HOST": "0.0.0.0",
                       "HANDPRINTER_ACCESS_TOKEN": "a-long-enough-token-123"}, dotenv=tmp_path / "x")
    remote = TestClient(create_app(s), client=("192.168.1.20", 50000))
    assert remote.get("/api/status", headers={"host": "192.168.1.5:8765"}).status_code == 401
    assert remote.post("/api/login", json={"token": "wrong"}, headers={"host": "192.168.1.5:8765"}).status_code == 403
    assert remote.post("/api/login", json={"token": "a-long-enough-token-123"}, headers={"host": "192.168.1.5:8765"}).status_code == 200
    assert remote.get("/api/status", headers={"host": "192.168.1.5:8765"}).status_code == 200
    # a loopback client with a foreign Host (DNS rebinding) is still refused
    local = TestClient(create_app(s), client=("127.0.0.1", 50000))
    assert local.get("/api/status", headers={"host": "evil.example"}).status_code == 400
