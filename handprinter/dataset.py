"""Dataset coverage, grouped splits, unseen-word subsets and immutable snapshot export."""

from __future__ import annotations

import hashlib
import json
import random
import re
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from handprinter import imaging, pages
from handprinter.db import dumps, new_id, now_iso, transaction
from handprinter.storage import Store, atomic_write, sha256_bytes

POLICIES = {
    "approved_only": "Human-approved examples only (default).",
    "include_unreviewed": "ADVANCED, UNVERIFIED: also includes unflagged machine proposals nobody has reviewed.",
}
MARK_TARGET_CLASSES = ("underline", "strike_through", "overwrite", "insertion")
EXPORT_SCHEMA_VERSION = "1"
INLINE_MATH = re.compile(r"\$[^$]+\$")


def is_maths(cls: str, text: str | None) -> bool:
    """Maths is stored as LaTeX: a whole region classed `equation`, or `$...$` inside a handwriting line."""
    return cls == "equation" or bool(text and INLINE_MATH.search(text))


@dataclass
class Filters:
    source_types: tuple[str, ...] = ("paper",)
    style_eras: tuple[str, ...] = ("current",)
    sessions: tuple[str, ...] | None = None
    include_maths: bool = True


@dataclass
class SplitConfig:
    seed: int = 1234
    train: float = 0.8
    val: float = 0.1
    test: float = 0.1
    holdout_sessions: tuple[str, ...] = ()
    unseen_word_fraction: float = 0.1


@dataclass
class Example:
    region_id: str
    kind: str
    text: str
    source_id: str
    group_key: str
    session: str | None
    source_type: str
    revision: int
    row: dict[str, Any] = field(repr=False, default_factory=dict)
    maths: bool = False


def eligible_examples(conn: sqlite3.Connection, policy: str, filters: Filters) -> tuple[list[Example], Counter]:
    """Clean word/line examples for export, and a count of why others were left out."""
    if policy not in POLICIES:
        raise ValueError(f"unknown policy {policy!r}")
    marked = {
        r["b_id"] for r in conn.execute(
            f"""SELECT l.b_id FROM links l JOIN regions m ON m.id = l.a_id
                WHERE l.kind = 'mark_target' AND l.b_id IS NOT NULL AND m.deleted = 0 AND m.cls IN ({','.join('?' * len(MARK_TARGET_CLASSES))})""",
            MARK_TARGET_CLASSES,
        )
    }
    marked |= {
        r["parent_id"] for r in conn.execute(
            f"SELECT parent_id FROM regions WHERE kind = 'mark' AND deleted = 0 AND parent_id IS NOT NULL AND status != 'excluded' AND cls IN ({','.join('?' * len(MARK_TARGET_CLASSES))})",
            MARK_TARGET_CLASSES,
        )
    }
    rows = conn.execute(
        """SELECT r.*, s.group_id, s.session, s.source_type, s.style_era, s.near_duplicates, s.processing_rev AS src_rev
           FROM regions r JOIN sources s ON s.id = r.source_id
           WHERE r.deleted = 0 AND s.deleted = 0 AND (r.kind IN ('word', 'line') OR (r.kind = 'block' AND r.cls = 'equation'))""",
    ).fetchall()
    excluded: Counter = Counter()
    out = []
    for r in rows:
        reason = _exclusion(r, policy, filters, marked)
        if reason:
            excluded[reason] += 1
            continue
        out.append(Example(r["id"], r["kind"], r["text"], r["source_id"], r["group_id"], r["session"], r["source_type"], r["revision"], dict(r),
                           maths=is_maths(r["cls"], r["text"])))
    return out, excluded


def _exclusion(r: sqlite3.Row, policy: str, filters: Filters, marked: set[str]) -> str | None:
    if r["source_type"] not in filters.source_types:
        return f"source type {r['source_type']} not selected"
    if r["style_era"] not in filters.style_eras:
        return f"{r['style_era']} style not selected"
    if filters.sessions is not None and r["session"] not in filters.sessions:
        return "session not selected"
    if r["other_author"]:
        return "someone else's handwriting"
    if r["cls"] not in ("handwriting", "equation"):
        return f"class {r['cls']}"
    if not filters.include_maths and is_maths(r["cls"], r["text"]):
        return "maths not selected"
    if r["id"] in marked:
        return "has an underline/correction mark (kept out of the clean set)"
    if r["processing_rev"] != r["src_rev"]:
        return "geometry on an old processing revision"
    if json.loads(r["flags"]):
        return "flagged, unresolved"
    if not r["text"]:
        return "no transcription"
    if r["status"] == "approved":
        return None
    if policy == "include_unreviewed" and r["status"] == "proposed" and r["text_origin"] == "machine":
        return None
    return f"status {r['status']}"


def leakage_groups(conn: sqlite3.Connection, examples: list[Example]) -> dict[str, str]:
    """Map each source ID to a canonical leakage group key.

    Groups are the union of source groups, identical bytes and near-duplicate links. The key
    is the smallest member source ID, so it does not depend on row order. Words and lines on
    the same page share a source, so they always share a group.
    """
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    for row in conn.execute("SELECT id, group_id, near_duplicates, sha256 FROM sources WHERE deleted = 0"):
        union(f"s:{row['id']}", f"t:g:{row['group_id']}")
        union(f"s:{row['id']}", f"t:h:{row['sha256']}")
        for other in json.loads(row["near_duplicates"]):
            union(f"s:{row['id']}", f"s:{other}")
    # "s:" sorts before "t:", so each root is the group's smallest source ID.
    return {ex.source_id: find(f"s:{ex.source_id}")[2:] for ex in examples}


def assign_splits(
    examples: list[Example], groups: dict[str, str], config: SplitConfig, existing: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str], dict[str, str]]:
    """Assign whole groups to train/val/test.

    `existing` maps source ID → split from earlier snapshots; those groups keep their split so
    every experiment shares one canonical split. Only new groups are placed, greedily by
    example count toward the target ratios. Returns (region → split, warnings, source → split).
    """
    existing = existing or {}
    warnings: list[str] = []
    by_group: dict[str, list[Example]] = defaultdict(list)
    for ex in examples:
        by_group[groups[ex.source_id]].append(ex)
    split_of_group: dict[str, str] = {}
    holdout = set(config.holdout_sessions)
    for g, exs in by_group.items():
        prior = Counter(existing[ex.source_id] for ex in exs if ex.source_id in existing)
        if holdout and any(ex.session in holdout for ex in exs):
            split_of_group[g] = "test"
            if prior and set(prior) != {"test"}:
                warnings.append(f"held-out session moved previously assigned page group {g} to test")
        elif prior:
            split_of_group[g] = prior.most_common(1)[0][0]
            if len(prior) > 1:
                warnings.append(f"page group {g} now joins pages from different earlier splits; all moved to {split_of_group[g]}")
    total = sum(len(v) for v in by_group.values())
    targets = {"train": config.train * total, "val": config.val * total, "test": config.test * total}
    counts = Counter({k: 0 for k in targets})
    for g, split in split_of_group.items():
        counts[split] += len(by_group[g])
    rest = sorted((g for g in by_group if g not in split_of_group), key=lambda g: hashlib.sha256(f"{config.seed}:{g}".encode()).hexdigest())
    for g in rest:
        choice = max(("val", "test", "train"), key=lambda k: (targets[k] - counts[k]) / targets[k] if targets[k] else -1)
        split_of_group[g] = choice
        counts[choice] += len(by_group[g])
    n_groups = len(by_group)
    if n_groups < 10:
        warnings.append(f"only {n_groups} independent page group(s): split percentages and evaluation are unreliable")
    biggest = max((len(v) for v in by_group.values()), default=0)
    if total and biggest / total > 0.3 and n_groups > 1:
        warnings.append(f"one page group holds {biggest / total:.0%} of examples (near-duplicate or source-group links may be chaining pages together)")
    for name in ("val", "test"):
        if not any(s == name for s in split_of_group.values()):
            warnings.append(f"the {name} split is empty")
    source_split = {ex.source_id: split_of_group[groups[ex.source_id]] for ex in examples}
    return {ex.region_id: split_of_group[groups[ex.source_id]] for ex in examples}, warnings, source_split


def canonical_splits(conn: sqlite3.Connection, examples: list[Example], config: SplitConfig) -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Load the stored canonical split, extend it to new pages, persist it. Returns (groups, region → split, warnings)."""
    groups = leakage_groups(conn, examples)
    existing = {r["source_id"]: r["split"] for r in conn.execute("SELECT source_id, split FROM source_splits")}
    splits, warnings, by_source = assign_splits(examples, groups, config, existing)
    with transaction(conn):
        conn.executemany(
            "INSERT INTO source_splits (source_id, split, assigned_at) VALUES (?,?,?) ON CONFLICT(source_id) DO UPDATE SET split = excluded.split",
            [(sid, split, now_iso()) for sid, split in by_source.items() if existing.get(sid) != split],
        )
    return groups, splits, warnings


def unseen_words(examples: list[Example], splits: dict[str, str], config: SplitConfig) -> tuple[set[str], set[str]]:
    """Pick word types to hold out of training entirely.

    Only words whose instances can all be removed from train/val qualify. Returns
    (unseen vocabulary, region IDs dropped from train/val to keep them unseen).
    """
    examples = [ex for ex in examples if not ex.maths]  # LaTeX is not a word vocabulary
    words = [ex for ex in examples if ex.kind == "word"]
    test_types = sorted({ex.text for ex in words if splits[ex.region_id] == "test"})
    rng = random.Random(config.seed)
    k = int(round(len(test_types) * config.unseen_word_fraction))
    vocab = set(rng.sample(test_types, k)) if k else set()
    dropped = set()
    for ex in examples:
        if splits[ex.region_id] == "test":
            continue
        tokens = [ex.text] if ex.kind == "word" else ex.text.split()
        if any(t in vocab for t in tokens):
            dropped.add(ex.region_id)
    return vocab, dropped


def coverage(conn: sqlite3.Connection) -> dict[str, Any]:
    status = {r["status"]: r["n"] for r in conn.execute(
        "SELECT status, COUNT(*) n FROM regions WHERE deleted = 0 AND kind IN ('word','line') GROUP BY status")}
    flagged = conn.execute("SELECT COUNT(*) FROM regions WHERE deleted = 0 AND flags != '[]' AND status != 'excluded'").fetchone()[0]
    examples, excluded = eligible_examples(conn, "approved_only", Filters())
    by_kind = Counter("maths" if ex.maths else ex.kind for ex in examples)
    chars = Counter(ch for ex in examples if ex.kind == "word" and not ex.maths for ch in ex.text)
    marks = {r["cls"]: r["n"] for r in conn.execute("SELECT cls, COUNT(*) n FROM regions WHERE deleted = 0 AND kind = 'mark' GROUP BY cls")}
    return {
        "status": status, "flagged_open": flagged, "exportable": dict(by_kind),
        "excluded_reasons": dict(excluded.most_common()), "characters": dict(chars.most_common()),
        "word_types": len({ex.text for ex in examples if ex.kind == "word" and not ex.maths}), "marks": marks,
        "pages": conn.execute("SELECT COUNT(*) FROM sources WHERE deleted = 0").fetchone()[0],
        "pilot_target_words": [1000, 3000],
    }


def export_snapshot(
    conn: sqlite3.Connection, store: Store, name: str, policy: str, filters: Filters, config: SplitConfig,
    progress: Callable[[float, str], None] = lambda f, m: None,
) -> dict[str, Any]:
    examples, excluded = eligible_examples(conn, policy, filters)
    if not examples:
        raise ValueError("nothing to export: no examples match this policy and filter yet")
    groups, splits, warnings = canonical_splits(conn, examples, config)
    vocab, dropped = unseen_words(examples, splits, config)
    snapshot_id = new_id("snap")
    root = store.dir("datasets") / snapshot_id
    for sub in ("words", "lines", "maths"):
        (root / sub).mkdir(parents=True)
    records: dict[str, list[dict[str, Any]]] = {"word": [], "line": [], "maths": []}
    items = []
    images = pages.WorkingImages(store)
    examples = sorted(examples, key=lambda e: (e.source_id, e.kind, e.region_id))
    for index, ex in enumerate(examples):
        progress(index / len(examples), f"{index + 1}/{len(examples)}")
        if ex.region_id in dropped:
            excluded["kept out of training to keep unseen words unseen"] += 1
            continue
        region = dict(ex.row)
        crop, _ = pages.region_crop(conn, store, region, images=images)
        data = imaging.png_bytes(crop)
        digest = sha256_bytes(data)
        bucket = "maths" if ex.maths else ex.kind
        folder = {"word": "words", "line": "lines", "maths": "maths"}[bucket]
        rel = f"{folder}/{ex.region_id}-r{ex.revision}.png"
        atomic_write(root / rel, data)
        split = splits[ex.region_id]
        is_unseen = not ex.maths and ex.kind == "word" and split == "test" and ex.text in vocab
        # Maths: `text` is LaTeX source. "latex" = the whole crop is maths; "text+inline_latex" = words with $...$ maths.
        extra = {"format": "latex" if region["cls"] == "equation" else "text+inline_latex", "region_kind": ex.kind} if ex.maths else {}
        records[bucket].append({**extra,
            "id": ex.region_id, "image": rel, "image_sha256": digest, "text": ex.text, "split": split,
            "unseen_word": is_unseen, "width": crop.width, "height": crop.height, "aspect": round(crop.width / crop.height, 5),
            "crop": {"frame": "processed", "x": region["x"], "y": region["y"], "w": region["w"], "h": region["h"],
                     "frame_w": region["frame_w"], "frame_h": region["frame_h"], "processing_rev": region["processing_rev"]},
            "source_id": ex.source_id, "line_id": region["parent_id"] if ex.kind == "word" else None,
            "group": groups[ex.source_id], "source_type": ex.source_type, "session": ex.session, "style_era": region["style_era"],
            "annotation_revision": ex.revision, "status": region["status"], "text_origin": region["text_origin"],
            "review": "human_approved" if region["status"] == "approved" else "unreviewed_machine_proposal",
        })
        items.append((ex.region_id, ex.revision, "maths" if ex.maths else ex.kind, split, int(is_unseen), digest))
    manifest_hash = hashlib.sha256()
    for kind, fname in (("word", "words.jsonl"), ("line", "lines.jsonl"), ("maths", "maths.jsonl")):
        body = "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in records[kind]).encode("utf-8")
        atomic_write(root / fname, body)
        manifest_hash.update(body)
    counts = {
        "words": len(records["word"]), "lines": len(records["line"]), "maths": len(records["maths"]),
        "splits": dict(Counter(r["split"] for k in records for r in records[k])),
        "unseen_words": sum(r["unseen_word"] for r in records["word"]), "unseen_vocabulary": len(vocab),
        "groups": len(set(groups.values())),
    }
    meta = {
        "snapshot_id": snapshot_id, "name": name, "created_at": now_iso(), "export_schema_version": EXPORT_SCHEMA_VERSION,
        "policy": policy, "policy_description": POLICIES[policy], "filters": filters.__dict__, "split_config": config.__dict__,
        "counts": counts, "excluded": dict(excluded), "warnings": warnings, "unseen_vocabulary": sorted(vocab),
        "manifest_sha256": manifest_hash.hexdigest(),
    }
    atomic_write(root / "snapshot.json", json.dumps(meta, indent=2, ensure_ascii=False, default=list).encode("utf-8"))
    atomic_write(root / "README.md", _readme(meta).encode("utf-8"))
    with transaction(conn):
        conn.execute(
            "INSERT INTO snapshots (id, name, created_at, policy, filters, split_config, counts, warnings, manifest_sha256, path) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (snapshot_id, name, meta["created_at"], policy, dumps(filters.__dict__), dumps(config.__dict__), dumps(counts),
             dumps(warnings), meta["manifest_sha256"], store.relative(root)),
        )
        conn.executemany(
            "INSERT INTO snapshot_items (snapshot_id, region_id, region_revision, kind, split, unseen, crop_sha256) VALUES (?,?,?,?,?,?,?)",
            [(snapshot_id, *item) for item in items],
        )
    return {"snapshot_id": snapshot_id, "counts": counts, "warnings": warnings, "excluded": dict(excluded)}


def _readme(meta: dict[str, Any]) -> str:
    return f"""# Handprinter dataset snapshot {meta['snapshot_id']}

Name: {meta['name']} · created {meta['created_at']} · policy `{meta['policy']}`: {meta['policy_description']}

Immutable. Files: `words.jsonl`, `lines.jsonl`, `maths.jsonl` (UTF-8, one JSON object per line), `words/*.png`, `lines/*.png`, `maths/*.png`
(lossless crops at native resolution, never stretched), `snapshot.json` (configuration, counts, warnings).

Fields: `id`, `image`, `image_sha256`, `text` (exactly as written), `split` (train/val/test, grouped by page,
source group and near-duplicates), `unseen_word`, `width`, `height`, `aspect`, `crop` (box in the processed
page frame), `source_id`, `line_id`, `group`, `source_type`, `session`, `style_era`, `annotation_revision`,
`status`, `text_origin`, `review`.

Maths (`maths.jsonl`, `maths/*.png`): `text` is LaTeX source as MathJax reads it. `format` is `latex` when the
whole crop is maths (e.g. `\\frac{{a}}{{b}} = c`) or `text+inline_latex` for writing with `$...$` maths inside.
Maths examples share the page-grouped split but are kept out of the word vocabulary and the unseen-word subset.

Warnings: {'; '.join(meta['warnings']) or 'none'}
"""


def snapshot_is_intact(store: Store, snapshot: dict[str, Any], check_images: bool = True) -> bool:
    """Manifests match the recorded hash, and every crop matches the hash in its manifest line."""
    root: Path = store.resolve_relative(snapshot["path"])
    digest = hashlib.sha256()
    for fname in ("words.jsonl", "lines.jsonl"):
        body = (root / fname).read_bytes()
        digest.update(body)
        if check_images:
            for line in body.decode("utf-8").splitlines():
                rec = json.loads(line)
                if sha256_bytes((root / rec["image"]).read_bytes()) != rec["image_sha256"]:
                    return False
    return digest.hexdigest() == snapshot["manifest_sha256"]
