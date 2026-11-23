"""Personal model training and the DiffusionPen driver: no leakage, honest status, faithful preprocessing.

The heavy model code runs in its own environment; these tests use the runner's pure helpers and a stand-in
adapter. The real driver was verified separately with the real weights (docs/model-experiments.md).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest
from PIL import Image

from handprinter import evaluation
from handprinter.adapters import diffusionpen as dp_module
from handprinter.adapters.base import AdapterStatus, Capabilities
from handprinter.adapters.diffusionpen import DiffusionPenAdapter
from handprinter.config import load_settings
from handprinter.db import dumps, new_id, now_iso

RUNNER = Path(dp_module.__file__).parent / "runners" / "diffusionpen_runner.py"


def _runner():
    spec = importlib.util.spec_from_file_location("dp_runner", RUNNER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _snapshot(conn, store, words: list[tuple[str, str]]) -> str:
    """A minimal exported snapshot: (text, split) per word, each with a real image file."""
    snap_id = new_id("snp")
    root = store.dir("datasets") / snap_id
    (root / "words").mkdir(parents=True)
    lines = []
    for i, (text, split) in enumerate(words):
        rel = f"words/w{i}.png"
        Image.new("RGB", (40 + 10 * len(text), 30), "white").save(root / rel)
        lines.append(json.dumps({"id": f"reg_{i:016x}", "image": rel, "text": text, "split": split}))
    (root / "words.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    conn.execute("INSERT INTO snapshots (id, name, created_at, policy, filters, split_config, counts, manifest_sha256, path) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (snap_id, "s", now_iso(), "approved_only", "{}", "{}", "{}", "x", store.relative(root)))
    return snap_id


def test_dotenv_with_byte_order_mark_keeps_first_key(tmp_path):
    env = tmp_path / ".env"
    env.write_bytes("﻿HANDPRINTER_RELAY_URL=https://x.workers.dev\nHANDPRINTER_PORT=9000\n".encode("utf-8"))
    s = load_settings({"HANDPRINTER_DATA_DIR": str(tmp_path / "d")}, dotenv=env)
    assert s.port == 9000
    from handprinter.config import _load_dotenv

    assert _load_dotenv(env)["HANDPRINTER_RELAY_URL"] == "https://x.workers.dev"


def test_weights_default_to_the_data_directory(tmp_path):
    s = load_settings({"HANDPRINTER_DATA_DIR": str(tmp_path / "d"), "HANDPRINTER_DIFFUSIONPEN_REPO": ""}, dotenv=tmp_path / "none")
    assert s.adapter_env["HANDPRINTER_DIFFUSIONPEN_WEIGHTS"] == str((tmp_path / "d").resolve() / "models" / "diffusionpen")
    assert "HANDPRINTER_DIFFUSIONPEN_REPO" not in s.adapter_env  # empty values do not override


def test_training_uses_only_the_training_split_and_writable_characters(conn, store):
    snap = _snapshot(conn, store, [("hello", "train"), ("world", "val"), ("unseen", "test"), ("$x^2$", "train"),
                                   ("café", "train"), ("", "train"), ("Mg", "train")])
    items, left_out = evaluation.training_items(store, conn, snap, dp_module.CHARSET)
    assert [i["text"] for i in items] == ["hello", "Mg"]
    assert left_out == {"not in the training split": 2, "characters the model cannot write": 2, "empty text": 1}
    assert all(Path(i["image"]).is_file() for i in items)


class _FakeTrainer(DiffusionPenAdapter):
    """Stands in for the GPU runner: writes a checkpoint and reports losses, like the real one."""

    def status(self):
        return AdapterStatus(self.name, self.title, "ready", {}, [], "", {})

    def train(self, request, on_event, cancelled):  # type: ignore[override]
        self.seen = request
        (request.out_dir / "ema_ckpt.pt").write_bytes(b"weights")
        return {"losses": [{"step": request.steps, "loss": 0.1}], "items_skipped": 0}

    def infer(self, request, on_event, cancelled):
        self.infer_options = request.options
        path = request.out_dir / "t000-s1.png"
        Image.new("L", (20, 10), 255).save(path)
        return {"outputs": [{"text": request.texts[0], "seed": request.seeds[0], "path": str(path)}]}


def test_training_run_records_a_personal_checkpoint_used_by_generation(conn, store, settings, monkeypatch):
    snap = _snapshot(conn, store, [(f"word{i % 7}", "train" if i % 5 else "test") for i in range(80)])
    fake = _FakeTrainer(settings.adapter_env)
    monkeypatch.setattr(evaluation, "all_adapters", lambda env: {"diffusionpen": fake})
    monkeypatch.setattr(evaluation, "MIN_TRAIN_WORDS", 10)
    out = evaluation.run_training(conn, store, settings, "job_1", {"adapter": "diffusionpen", "snapshot_id": snap, "steps": 100},
                                  lambda f, m: None, lambda: False)
    assert out["train_words"] == 64 and out["left_out"] == {"not in the training split": 16}
    assert all(it["text"].startswith("word") for it in fake.seen.items)
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (out["run_id"],)).fetchone()
    assert run["kind"] == "train" and run["status"] == "succeeded" and json.loads(run["config"])["losses"]
    ckpt = evaluation.personal_checkpoint(conn, store, "diffusionpen", out["run_id"])
    assert ckpt.read_bytes() == b"weights" and store.dir("models") in ckpt.parents
    assert [c["run_id"] for c in evaluation.personal_checkpoints(conn)] == [out["run_id"]]

    gen = evaluation.run_inference(conn, store, settings, "job_2", {"adapter": "diffusionpen", "level": "word", "texts": ["hi"],
                                                                    "seeds": [1], "checkpoint": out["run_id"]},
                                   lambda f, m: None, lambda: False)
    assert fake.infer_options["checkpoint_path"] == str(ckpt)
    assert conn.execute("SELECT checkpoint FROM runs WHERE id = ?", (gen["run_id"],)).fetchone()[0] == out["run_id"]


def test_failed_or_unknown_training_runs_are_not_usable(conn, store):
    run_id = new_id("run")
    conn.execute("INSERT INTO runs (id, adapter, kind, config, environment, status, outputs_dir, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (run_id, "diffusionpen", "train", dumps({}), dumps({}), "failed", "models/x", now_iso()))
    with pytest.raises(ValueError):
        evaluation.personal_checkpoint(conn, store, "diffusionpen", run_id)
    with pytest.raises(ValueError):
        evaluation.personal_checkpoint(conn, store, "diffusionpen", "run_0000000000000000")


def test_too_few_words_refuses_to_train(conn, store, settings, monkeypatch):
    snap = _snapshot(conn, store, [("one", "train")] * 3)
    monkeypatch.setattr(evaluation, "all_adapters", lambda env: {"diffusionpen": _FakeTrainer(settings.adapter_env)})
    with pytest.raises(ValueError, match="approve at least"):
        evaluation.run_training(conn, store, settings, "j", {"adapter": "diffusionpen", "snapshot_id": snap}, lambda f, m: None, lambda: False)
    assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_status_names_missing_libraries_instead_of_pretending(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "unet.py").write_text("")
    (repo / "feature_extractor.py").write_text("")
    adapter = DiffusionPenAdapter({"HANDPRINTER_DIFFUSIONPEN_PYTHON": sys.executable, "HANDPRINTER_DIFFUSIONPEN_REPO": str(repo),
                                   "HANDPRINTER_DIFFUSIONPEN_WEIGHTS": str(tmp_path / "w")})
    st = adapter.status()
    assert st.state == "blocked" and st.checks["environment"]
    if not st.checks["torch"]:  # the test environment has no torch
        assert any("missing in the DiffusionPen environment" in b for b in st.blockers)
    assert DiffusionPenAdapter({}).status().next_step.startswith("run SetupModels.bat")


def test_capabilities_reject_maths_and_unknown_characters():
    a = DiffusionPenAdapter({})
    assert a.unsupported("hello", "word") == []
    assert a.unsupported("x²", "word") and a.unsupported("hello there", "line")
    assert isinstance(a.capabilities(), Capabilities) and a.capabilities().can_train_in_app


def test_runner_prepares_images_like_upstream(tmp_path):
    r = _runner()
    small, wide = tmp_path / "s.png", tmp_path / "w.png"
    Image.new("RGB", (60, 32), "black").save(small)   # -> 120x64, padded to 256x64
    Image.new("RGB", (900, 100), "black").save(wide)  # -> 576x64, shrunk below 256 wide, centred
    a, b = r.prepare(small), r.prepare(wide)
    assert a.size == b.size == (256, 64)
    assert a.getpixel((0, 32)) == (255, 255, 255) and a.getpixel((128, 32)) == (0, 0, 0)
    assert b.getpixel((128, 0)) == (255, 255, 255) and b.getpixel((128, 32)) == (0, 0, 0)  # shrunk height leaves white rows


def test_runner_maps_dataparallel_checkpoint_keys():
    r = _runner()
    mapped = r._strip({"module.out.0.weight": 1, "module.text_encoder.module.encoder.x": 2, "style_lin.bias": 3})
    assert mapped == {"out.0.weight": 1, "text_encoder.encoder.x": 2, "style_lin.bias": 3}
