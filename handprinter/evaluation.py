"""Model runs, generations, style profiles and blind A/B comparison."""

from __future__ import annotations

import json
import platform
import random
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from PIL import Image

from handprinter import __version__
from handprinter.adapters import all_adapters
from handprinter.adapters.base import InferRequest
from handprinter.config import Settings
from handprinter.db import dumps, new_id, now_iso, row_dict, transaction
from handprinter.recognition.package import REPO_ROOT
from handprinter.storage import Store

# Fixed prompt set: identical for every model and run. Words and short lines are compared separately.
DEFAULT_PROMPTS = {
    "word": ["the", "handwriting", "because", "Quickly", "jumped", "seventeen", "notebook", "Wednesday", "it's", "maybe?"],
    "line": ["the quick brown fox jumps", "Meet me at 4pm, not later.", "I wrote this by hand", "Notes for Monday's class"],
}
DEFAULT_SEEDS = [11, 22, 33]


def code_version() -> str:
    try:
        rev = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        rev = ""
    return f"handprinter {__version__}" + (f" @ {rev}" if rev else "")


def create_style_profile(conn: sqlite3.Connection, name: str, snapshot_id: str, count: int = 5, seed: int = 7) -> dict[str, Any]:
    """Pick reference crops from a snapshot's training split. These are cached references, not learned weights."""
    rows = conn.execute(
        "SELECT region_id FROM snapshot_items WHERE snapshot_id = ? AND split = 'train' AND kind = 'word' ORDER BY region_id", (snapshot_id,)
    ).fetchall()
    if not rows:
        raise ValueError("the snapshot has no training words to use as style references")
    ids = random.Random(seed).sample([r["region_id"] for r in rows], min(count, len(rows)))
    profile_id = new_id("sty")
    conn.execute("INSERT INTO style_profiles (id, name, snapshot_id, region_ids, created_at) VALUES (?,?,?,?,?)",
                 (profile_id, name, snapshot_id, dumps(ids), now_iso()))
    return {"id": profile_id, "name": name, "snapshot_id": snapshot_id, "region_ids": ids}


def _profile_refs(conn: sqlite3.Connection, store: Store, profile_id: str) -> tuple[list[Path], list[str]]:
    profile = conn.execute("SELECT * FROM style_profiles WHERE id = ?", (profile_id,)).fetchone()
    if profile is None:
        raise ValueError("style profile not found")
    snap = conn.execute("SELECT path FROM snapshots WHERE id = ?", (profile["snapshot_id"],)).fetchone()
    root = store.resolve_relative(snap["path"])
    paths, texts = [], []
    records = {json.loads(line)["id"]: json.loads(line) for line in (root / "words.jsonl").read_text(encoding="utf-8").splitlines()}
    for rid in json.loads(profile["region_ids"]):
        rec = records[rid]
        paths.append(root / rec["image"])
        texts.append(rec["text"])
    return paths, texts


def run_inference(
    conn: sqlite3.Connection, store: Store, settings: Settings, job_id: str, params: dict[str, Any],
    progress: Callable[[float, str], None], cancelled: Callable[[], bool],
) -> dict[str, Any]:
    adapter = all_adapters(settings.adapter_env)[params["adapter"]]
    level = params["level"]
    texts = params.get("texts") or DEFAULT_PROMPTS[level]
    seeds = params.get("seeds") or DEFAULT_SEEDS
    skipped = {t: adapter.unsupported(t, level) for t in texts}
    supported = [t for t in texts if not skipped[t]]
    skipped = {t: r for t, r in skipped.items() if r}
    run_id = new_id("run")
    out_dir = store.dir("runs") / run_id
    out_dir.mkdir(parents=True)
    style_paths, style_texts = _profile_refs(conn, store, params["style_profile_id"]) if params.get("style_profile_id") else ([], [])
    config = {"level": level, "texts": supported, "seeds": seeds, "skipped": skipped, "style_profile_id": params.get("style_profile_id"),
              "options": params.get("options", {}), "kind": params.get("kind", "baseline")}
    env = {"python": platform.python_version(), "platform": platform.platform(), "adapter_status": adapter.status().as_dict()}
    started = time.time()
    with transaction(conn):
        conn.execute(
            "INSERT INTO runs (id, adapter, kind, job_id, snapshot_id, seed, checkpoint, code_version, config, environment, status, started_at, outputs_dir, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, adapter.name, config["kind"], job_id, params.get("snapshot_id"), seeds[0], params.get("checkpoint", "pretrained"),
             code_version(), dumps(config), dumps(env), "running", now_iso(), store.relative(out_dir), now_iso()),
        )
    try:
        request = InferRequest(supported, level, style_paths, seeds, out_dir, {**params.get("options", {}), "style_text": style_texts[0] if style_texts else ""})
        result = adapter.infer(request, lambda e: progress(float(e.get("fraction", 0)), str(e.get("message", ""))), cancelled)
    except Exception as exc:
        conn.execute("UPDATE runs SET status='failed', error=?, finished_at=?, duration_s=? WHERE id=?",
                     (str(exc)[:2000], now_iso(), time.time() - started, run_id))
        raise
    with transaction(conn):
        for out in result.get("outputs", []):
            path = Path(out["path"])
            with Image.open(path) as img:
                w, h = img.size
            conn.execute(
                "INSERT INTO generations (id, run_id, adapter, prompt_text, level, variation, seed, image_path, width, height, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (new_id("gen"), run_id, adapter.name, out["text"], level, seeds.index(out["seed"]), out["seed"], store.relative(path), w, h, now_iso()),
            )
        conn.execute("UPDATE runs SET status='succeeded', finished_at=?, duration_s=?, peak_memory_mb=? WHERE id=?",
                     (now_iso(), time.time() - started, result.get("peak_memory_mb"), run_id))
    return {"run_id": run_id, "generated": len(result.get("outputs", [])), "skipped": skipped}


def comparable_prompts(conn: sqlite3.Connection, level: str) -> dict[str, Any]:
    """Prompts that both adapters produced output for; everything else is reported as excluded."""
    rows = conn.execute("SELECT DISTINCT adapter, prompt_text FROM generations WHERE level = ?", (level,)).fetchall()
    by_prompt: dict[str, set[str]] = {}
    for r in rows:
        by_prompt.setdefault(r["prompt_text"], set()).add(r["adapter"])
    adapters = {r["adapter"] for r in rows}
    both = sorted(p for p, a in by_prompt.items() if len(a) >= 2)
    return {"comparable": both, "excluded": {p: sorted(a) for p, a in by_prompt.items() if len(a) < 2}, "adapters": sorted(adapters)}


def next_trial(conn: sqlite3.Connection, level: str, seed: int | None = None) -> dict[str, Any] | None:
    """Create a blind trial: two generations of the same prompt from different adapters, order randomised."""
    info = comparable_prompts(conn, level)
    if not info["comparable"]:
        return None
    rng = random.Random(seed)
    prompt = rng.choice(info["comparable"])
    gens = conn.execute("SELECT id, adapter FROM generations WHERE level = ? AND prompt_text = ?", (level, prompt)).fetchall()
    by_adapter: dict[str, list[str]] = {}
    for g in gens:
        by_adapter.setdefault(g["adapter"], []).append(g["id"])
    a, b = rng.sample(sorted(by_adapter), 2)
    left, right = rng.choice(by_adapter[a]), rng.choice(by_adapter[b])
    if rng.random() < 0.5:
        left, right = right, left
    trial_id = new_id("abt")
    conn.execute("INSERT INTO ab_trials (id, prompt_text, level, left_id, right_id, created_at) VALUES (?,?,?,?,?,?)",
                 (trial_id, prompt, level, left, right, now_iso()))
    return {"id": trial_id, "prompt_text": prompt, "level": level, "left": left, "right": right}


def rate_trial(conn: sqlite3.Connection, trial_id: str, choice: str) -> dict[str, Any]:
    if choice not in ("left", "right", "tie", "neither"):
        raise ValueError("choice must be left, right, tie or neither")
    with transaction(conn):
        trial = conn.execute("SELECT * FROM ab_trials WHERE id = ?", (trial_id,)).fetchone()
        if trial is None:
            raise ValueError("trial not found")
        if trial["choice"] is not None:
            raise ValueError("trial already rated")
        conn.execute("UPDATE ab_trials SET choice = ?, revealed = 1, rated_at = ? WHERE id = ?", (choice, now_iso(), trial_id))
    left = conn.execute("SELECT adapter, run_id FROM generations WHERE id = ?", (trial["left_id"],)).fetchone()
    right = conn.execute("SELECT adapter, run_id FROM generations WHERE id = ?", (trial["right_id"],)).fetchone()
    return {"choice": choice, "left": dict(left), "right": dict(right)}


def ab_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    wins: dict[str, dict[str, int]] = {}
    for t in conn.execute(
        """SELECT t.level, t.choice, gl.adapter AS la, gr.adapter AS ra FROM ab_trials t
           JOIN generations gl ON gl.id = t.left_id JOIN generations gr ON gr.id = t.right_id WHERE t.choice IS NOT NULL"""
    ):
        bucket = wins.setdefault(t["level"], {})
        winner = {"left": t["la"], "right": t["ra"]}.get(t["choice"], t["choice"])
        bucket[winner] = bucket.get(winner, 0) + 1
    return {"ratings": wins, "note": "Blind preference counts only; small samples are not significant."}


def list_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [row_dict(r, ("config", "environment")) for r in rows]  # type: ignore[misc]
