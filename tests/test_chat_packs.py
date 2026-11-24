"""Portable packs cross a manual trust boundary. Exercise the HTTP and worker round trip."""
import io
import json
import zipfile

from handprinter import pages, review
from PIL import Image

from handprinter.recognition import chat, cuts, package
from tests.test_api_e2e import drain, make_client
from tests.test_word_cuts import _page, _true_cuts


def finish(client, settings, response):
    assert response.status_code == 200, response.text
    drain(settings)
    job = client.get("/api/jobs/" + response.json()["job_id"]).json()
    assert job["status"] == "succeeded", job["error"]
    return job["result"]


def reply(pack, batch, model, *results):
    return {"reply": "```json\n" + json.dumps(dict(pack_id=pack, batch=batch, model=model, results=results)) + "\n```"}


def test_transcription_zip_partial_import_replay_and_resume(conn, store, settings, fixture_source):
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    client = make_client(settings)
    pack = finish(client, settings, client.post("/api/recognition/packages", json={
        "manual_chat": True, "source_ids": [sid], "include_overview": True}))["package_id"]
    assert not package.pending_packages(conn)
    base = "/api/chat-packs/" + pack
    state = client.get(base).json()
    all_ids = [it["item_id"] for it in package.load_manifest(store, pack)["items"]]
    assert 4 <= len(all_ids) <= chat.BATCH_SIZE  # a page's lines fit one chat message
    assert state["batches"] == 1 and state["images"] == all_ids
    url = base + "/batches/1"
    with zipfile.ZipFile(io.BytesIO(client.get(url + "/download").content)) as z:
        assert set(z.namelist()) == {"prompt.txt", *(key + ".png" for key in state["images"])}
        assert z.read("prompt.txt").decode() == state["prompt"]
        assert z.read("i0002.png") == client.get(url + "/images/i0002.png").content
    assert client.get(url + "/images/i9999.png").status_code == 404
    it = package.load_manifest(store, pack)["items"][1]
    review.human_edit(conn, it["region_id"], {"text": "my correction", "status": "approved"})
    good = dict(item_id="i0002", region_type="handwriting", transcription="example",
                uncertain_spans=[], marks=[], quality_flags=[], review_reasons=[], author_ambiguous=False)
    invalid = {**good, "item_id": "i0003", "region_type": "???", "transcription": "mistake"}
    data = reply(pack, 1, "Gemini", good, invalid)
    for wrong in (reply(pack, 2, "Gemini", good), reply("other-pack", 1, "ChatGPT", good),
                  reply(pack, 1, "unknown", good, good), {"reply": "not json"}):
        assert client.post(url + "/reply", json=wrong).status_code == 422
    out = finish(client, settings, client.post(url + "/reply", json=data))
    assert out["applied"] == 1 and set(out["errors"]) == {"i0003"}
    prop = conn.execute("SELECT model_reported, stale, applied FROM proposals WHERE package_id=?", (pack,)).fetchone()
    assert tuple(prop) == ("Chat: Gemini", 1, 0)
    assert review.get_region(conn, it["region_id"])["text"] == "my correction"
    assert make_client(settings).get(base).json()["images"] == [i for i in all_ids if i != "i0002"]
    again = finish(client, settings, client.post(url + "/reply", json=reply(pack, 1, "Gemini", good)))
    assert again["previous"] == 1 and again["applied"] == 0


def test_manual_cuts_keep_human_edits_and_resume_across_chats(conn, store, settings, import_bytes):
    sid = _page(conn, store, import_bytes)
    client = make_client(settings)
    packs = "/api/chat-packs/word-cuts"
    pack = finish(client, settings, client.post(packs, json={"scope": "all", "source_ids": [sid]}))["package_id"]
    base = "/api/chat-packs/" + pack
    assert not cuts.pending(store)
    items = cuts.load_manifest(store, pack)["items"]
    def result(it):
        return dict(item_id=it["item_id"], cuts=_true_cuts(conn, it), unsure=False, note="")
    first, edited, third = [result(it) for it in items[:3]]
    word = review._line_words(conn, items[1]["line_id"])[0]
    review.human_edit(conn, word["id"], {"text": "keep me", "status": "excluded"})
    url = base + "/batches/1/reply"
    out = finish(client, settings, client.post(url, json=reply(pack, 1, "ChatGPT", first, edited, {**third, "cuts": []})))
    assert out["applied"] == 1 and len(out["skipped"]) == 1 and set(out["errors"]) == {third["item_id"]}
    assert review.get_region(conn, word["id"])["text"] == "keep me"
    out = finish(client, settings, client.post(url, json=reply(pack, 1, "Gemini", first, third)))
    assert out["previous"] == out["applied"] == 1
    assert make_client(settings).get(packs).json()[0]["processed"] == 3
    assert client.get(base).json()["images"] == [it["item_id"] for it in items[3:chat.BATCH_SIZE]]
    models = {r[0] for r in conn.execute("SELECT model FROM chat_cut_receipts WHERE state='applied'")}
    assert models == {"Chat: Gemini", "Chat: ChatGPT"}


def test_each_chat_image_shows_its_id_and_keeps_its_coordinates(conn, store, settings, fixture_source, import_bytes):
    """A chat may not see filenames and may reorder attachments: the ID printed on the image decides."""
    sid = fixture_source()
    pages.run_detection(conn, store, sid)
    from dataclasses import replace
    rec = package.build(conn, store, replace(settings, recognition_model=package.MANUAL_MODEL), [sid], False, False)
    root, items = chat.open_pack(store, rec["package_id"])
    assert items.labelled and items.size == chat.BATCH_SIZE
    for it in items:
        with Image.open(root / it["image"]) as im:
            assert (im.width, im.height) == (it["width"], it["height"])
            band = im.convert("L").crop((0, im.height - 30, im.width, im.height))
            assert min(band.getdata()) < 80  # dark text printed in the band
            # the band is extra rows below: a box in image pixels still maps to the same place on the page
            x, y, w, h = it["target_box"]
            assert y + h <= im.height - 40 + 1  # the line sits in the writing part, above the band
        assert abs(it["to_source"]["scale_x"] - it["to_source"]["scale_y"]) < 0.05  # not stretched by the band
    text = chat.describe(conn, store, rec["package_id"], None)["prompt"]
    assert "printed" in text and "do not rely on filenames" in text

    cut = cuts.build(conn, store, "all", [_page(conn, store, import_bytes)], manual=True)
    root, items = chat.open_pack(store, cut["package_id"])
    assert items.labelled and "printed" in chat.describe(conn, store, cut["package_id"], None)["prompt"]


def test_old_packs_keep_their_batches_of_four(conn, store, settings, import_bytes):
    cut = cuts.build(conn, store, "all", [_page(conn, store, import_bytes)], manual=True)
    path = cuts.package_dir(store, cut["package_id"]) / "manifest.json"
    m = json.loads(path.read_text())
    for k in ("chat_batch_size", "chat_labelled"):
        m.pop(k)
    path.write_text(json.dumps(m))
    state = chat.describe(conn, store, cut["package_id"], None)
    assert len(state["images"]) == min(4, cut["items"]) and "filename" in state["prompt"]


def test_several_batches_can_be_pasted_at_once(conn, store, settings, import_bytes):
    """A chat asked for many batches answers with a list of envelopes: one paste imports them all."""
    sid = _page(conn, store, import_bytes)
    cut = cuts.build(conn, store, "all", [sid], manual=True)
    path = cuts.package_dir(store, cut["package_id"]) / "manifest.json"
    m = json.loads(path.read_text())
    m["chat_batch_size"] = 2
    path.write_text(json.dumps(m))
    items = m["items"]
    envs = [dict(pack_id=cut["package_id"], batch=n + 1, model="unknown",
                 results=[dict(item_id=it["item_id"], cuts=_true_cuts(conn, it), unsure=False, note="") for it in items[2 * n:2 * n + 2]])
            for n in range(2)]
    bad = [*envs, {**envs[0], "pack_id": "ccut_other"}]
    try:
        chat.check_replies(store, cut["package_id"], json.dumps(bad))
    except ValueError as exc:
        assert "different pack" in str(exc)
    else:
        raise AssertionError("a list with another pack's batch was accepted")
    out = chat.import_replies(conn, store, cut["package_id"], "```json\n" + json.dumps(envs) + "\n```")
    assert out["batches"] == [1, 2] and out["applied"] == 4 and not out["errors"]
    for it in items[:4]:
        assert [w["text"] for w in review._line_words(conn, it["line_id"])] == it["words"]
