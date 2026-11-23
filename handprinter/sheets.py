"""Writing sheets: text chosen by the app, written out by hand, labelled without recognition.

A sheet is a short list of lines to copy (one written line per sheet line). Because the text is
known, an uploaded sheet page is labelled by matching its detected lines to the sheet's lines in
order; the word boxes are then cut from the known words. Nothing is sent to a recogniser.

Lines are real words and sentences, never letters on their own: letters are shaped by their
neighbours, so they must be practised in context. Sheets are picked to cover what the dataset is
short of (rare letters, letter pairs, capitals, digits, maths, Greek, chemistry, your own words).
"""

from __future__ import annotations

import json
import math
import random
import re
import sqlite3
from collections import Counter
from typing import Any

import numpy as np

from handprinter import pages, review
from handprinter import sheet_banks as banks
from handprinter.db import new_id, now_iso, transaction
from handprinter.geometry import Box
from handprinter.recognition import refit
from handprinter.recognition.align import align, tokenize
from handprinter.storage import Store

FOCUSES = {
    "mixed": "A bit of everything (recommended)",
    "everyday": "Everyday sentences",
    "pairs": "Tricky letter pairs and joins",
    "capitals": "Capitals, digits and punctuation",
    "maths": "Maths: letters, Greek, fractions, powers, calculus",
    "chemistry": "Chemistry formulas and equations",
    "physics": "Physics equations, units and calculations",
    "python": "Python code",
    "mine": "Words from your own notes",
}
LINES_PER_SHEET = 12

BANKS = {"everyday": banks.EVERYDAY, "pairs": banks.PAIRS, "capitals": banks.CAPITALS, "maths": banks.MATHS,
         "chemistry": banks.CHEMISTRY, "physics": banks.PHYSICS, "python": banks.PYTHON}
GENERATED = {"maths", "physics"}  # these also get fresh lines with random numbers every time
MIX = {"everyday": 4, "pairs": 2, "capitals": 1, "maths": 2, "chemistry": 1, "physics": 1, "mine": 1}


def _pool(bank: str, rng: random.Random) -> list[str]:
    pool = list(BANKS[bank])
    if bank in GENERATED:
        pool += banks.generated(bank, rng, 40)
    return pool


def _units(text: str) -> Counter:
    """What a line practises: characters and letter pairs (bigrams), maths spans as whole symbols."""
    plain = re.sub(r"\$[^$]*\$", " ", text)
    maths = re.findall(r"\\[a-zA-Z]+|[\^_]", " ".join(re.findall(r"\$([^$]*)\$", text)))
    out: Counter = Counter(ch for ch in plain if not ch.isspace())
    out.update(a + b for a, b in zip(plain, plain[1:]) if a.isalpha() and b.isalpha())
    out.update(maths)
    return out


def _have(conn: sqlite3.Connection) -> Counter:
    """What the dataset already has: approved text plus every line already put on a sheet."""
    have: Counter = Counter()
    for (text,) in conn.execute("SELECT text FROM regions WHERE deleted = 0 AND kind = 'line' AND status = 'approved' AND text IS NOT NULL"):
        have.update(_units(text))
    for (lines,) in conn.execute("SELECT lines FROM sheets"):
        for line in json.loads(lines):
            have.update(_units(line))
    return have


def _my_lines(conn: sqlite3.Connection, rng: random.Random, n: int) -> list[str]:
    """Lines made of words you actually write (approved, not maths), rarer words first."""
    counts = Counter(t for (t,) in conn.execute(
        "SELECT text FROM regions WHERE deleted = 0 AND kind = 'word' AND status = 'approved' AND text IS NOT NULL AND text NOT LIKE '%$%'"))
    words = [w for w, _ in sorted(counts.items(), key=lambda kv: (kv[1], rng.random())) if 2 <= len(w) <= 14]
    lines, cur = [], []
    for w in words:
        cur.append(w)
        if len(" ".join(cur)) >= 38:
            lines.append(" ".join(cur))
            cur = []
        if len(lines) >= n:
            break
    return lines


def _pick(candidates: list[str], have: Counter, n: int, rng: random.Random) -> list[str]:
    """Greedy: each next line is the one that adds the most of what is still rare."""
    chosen: list[str] = []
    seen = Counter(have)
    pool = list(dict.fromkeys(candidates))
    rng.shuffle(pool)
    while pool and len(chosen) < n:
        def gain(line: str) -> float:
            return sum(c / (1 + seen[u]) for u, c in _units(line).items()) / math.sqrt(max(1, len(line)))
        best = max(pool, key=gain)
        chosen.append(best)
        seen.update(_units(best))
        pool.remove(best)
    return chosen


def make_sheet(conn: sqlite3.Connection, focus: str = "mixed", seed: int | None = None) -> dict[str, Any]:
    if focus not in FOCUSES:
        raise review.ReviewError(f"unknown sheet focus {focus!r}")
    seed = seed if seed is not None else random.randrange(1, 10**9)
    rng = random.Random(seed)
    have = _have(conn)
    if focus == "mixed":
        lines: list[str] = []
        mine = _my_lines(conn, rng, 4)
        for bank, k in MIX.items():
            pool = mine if bank == "mine" else _pool(bank, rng)
            if not pool:
                pool = [line for line in banks.EVERYDAY if line not in lines]
            picked = _pick([line for line in pool if line not in lines], have, k, rng)
            lines += picked
            for line in picked:
                have.update(_units(line))
        lines = lines[:LINES_PER_SHEET]
    elif focus == "mine":
        lines = _my_lines(conn, rng, LINES_PER_SHEET) or _pick(banks.EVERYDAY, have, LINES_PER_SHEET, rng)
    else:
        lines = _pick(_pool(focus, rng), have, LINES_PER_SHEET, rng)
    sheet_id = new_id("sht")
    number = conn.execute("SELECT COUNT(*) FROM sheets").fetchone()[0] + 1
    conn.execute("INSERT INTO sheets (id, number, created_at, focus, seed, lines) VALUES (?,?,?,?,?,?)",
                 (sheet_id, number, now_iso(), focus, seed, json.dumps(lines)))
    return get_sheet(conn, sheet_id)


def get_sheet(conn: sqlite3.Connection, sheet_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM sheets WHERE id = ?", (sheet_id,)).fetchone()
    if row is None:
        raise review.ReviewError("sheet not found")
    used = [dict(r) for r in conn.execute(
        "SELECT p.source_id, p.applied_at, p.summary, s.original_filename FROM sheet_pages p JOIN sources s ON s.id = p.source_id "
        "WHERE p.sheet_id = ? AND s.deleted = 0 ORDER BY p.applied_at", (sheet_id,))]
    for u in used:
        u["summary"] = json.loads(u["summary"])
    return {"id": row["id"], "number": row["number"], "created_at": row["created_at"], "focus": row["focus"],
            "focus_label": FOCUSES.get(row["focus"], row["focus"]), "lines": json.loads(row["lines"]), "pages": used}


def list_sheets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [get_sheet(conn, r["id"]) for r in conn.execute("SELECT id FROM sheets ORDER BY created_at DESC LIMIT 100")]


# ---------- matching a written page to its sheet ----------

SKIP_LINE = 1.6   # a written line that is not on the sheet (a note, a crossed-out attempt)
SKIP_PROMPT = 1.2  # a sheet line you did not write


def match_lines(widths: list[float], texts: list[str], word_widths: list[list[float]] | None = None) -> list[tuple[int | None, int | None]]:
    """Pair written lines (top to bottom) with sheet lines (in order), allowing skipped or extra lines.

    The cost of a pair combines how far the line's width per character is from the page's typical
    width per character and, when word boxes are known, how well the line's pattern of word widths
    fits the sheet line's word lengths (a line's rhythm tells similar-length lines apart).
    Returns (written index or None, sheet index or None) in order.
    """
    m, n = len(widths), len(texts)
    chars = [max(1, len(re.sub(r"\$[^$]*\$", "xxxx", t))) for t in texts]
    per_char = float(np.median([w / c for w, c in zip(widths, chars)])) if m and n else 1.0
    pairs: list[tuple[int | None, int | None]] = []
    for _ in range(2):  # estimate, then re-estimate from the pairs found
        inf = float("inf")
        cost = [[inf] * (n + 1) for _ in range(m + 1)]
        back: list[list[tuple[int, int, str] | None]] = [[None] * (n + 1) for _ in range(m + 1)]
        cost[0][0] = 0.0
        for i in range(m + 1):
            for j in range(n + 1):
                c0 = cost[i][j]
                if c0 == inf:
                    continue
                moves = []
                if i < m and j < n:
                    c = abs(math.log(max(widths[i], 1) / (chars[j] * per_char)))
                    if word_widths and word_widths[i]:
                        fit = align(texts[j], word_widths[i])
                        c += min(2.0, fit.cost / max(1, len(tokenize(texts[j]))))
                    moves.append((i + 1, j + 1, "pair", c))
                if i < m:
                    moves.append((i + 1, j, "skip_line", SKIP_LINE))
                if j < n:
                    moves.append((i, j + 1, "skip_prompt", SKIP_PROMPT))
                for ni, nj, move, c in moves:
                    if c0 + c < cost[ni][nj]:
                        cost[ni][nj], back[ni][nj] = c0 + c, (i, j, move)
        pairs = []
        i, j = m, n
        while (i, j) != (0, 0):
            pi, pj, move = back[i][j]  # type: ignore[misc]
            pairs.append((pi, pj) if move == "pair" else (pi, None) if move == "skip_line" else (None, pj))
            i, j = pi, pj
        pairs.reverse()
        matched = [(a, b) for a, b in pairs if a is not None and b is not None]
        if matched:
            per_char = float(np.median([widths[a] / chars[b] for a, b in matched]))
    return pairs


def apply_sheet(conn: sqlite3.Connection, store: Store, source_id: str, sheet_id: str) -> dict[str, Any]:
    """Label an uploaded sheet page from its sheet: match lines in order, cut words from the known text.

    Only open lines without your own text are used; anything you typed or approved is left alone.
    Matched lines become suggestions to approve in Check; unmatched ones are flagged.
    """
    sheet = get_sheet(conn, sheet_id)
    source = pages.get_source(conn, source_id)
    if source["detection_status"] == "none":
        pages.run_detection(conn, store, source_id)
    rows = conn.execute(
        """SELECT * FROM regions WHERE source_id = ? AND deleted = 0 AND kind = 'line' AND cls IN ('handwriting', 'uncertain', 'equation')
           AND status IN ('unprocessed', 'proposed', 'needs_review') AND (text_origin IS NULL OR text_origin = 'machine')""",
        (source_id,)).fetchall()
    lines = sorted((review.get_region(conn, r["id"]) for r in rows), key=lambda r: (r["y"] + r["h"] / 2, r["x"]))
    ink = refit.LineInk(conn, store)
    widths = []
    for ln in lines:
        mask, _, _ = ink.mask(source, Box(ln["x"], ln["y"], ln["w"], ln["h"]))
        widths.append(float(mask.any(axis=0).sum()) if mask.size else ln["w"])
    word_widths = [[w["w"] for w in review._line_words(conn, ln["id"]) if w["status"] != "excluded"] for ln in lines]
    pairs = match_lines(widths, sheet["lines"], word_widths)
    matched, unmatched_lines, missing = 0, 0, []
    with transaction(conn):
        for li, pj in pairs:
            if li is None and pj is not None:
                missing.append(pj + 1)
                continue
            if li is None:
                continue
            line = review.get_region(conn, lines[li]["id"])
            if pj is None:
                review._write(conn, line, {"flags": sorted(set(line["flags"]) | {"sheet_unmatched"}), "status": "needs_review",
                                           "review_reason": f"not matched to sheet {sheet['number']}: extra writing, or a sheet line was skipped"},
                              "machine", f"sheet:{sheet_id}")
                unmatched_lines += 1
                continue
            text = sheet["lines"][pj]
            _label_line(conn, ink, source, line, text, sheet, pj)
            matched += 1
        summary = {"matched": matched, "unmatched_lines": unmatched_lines, "missing_sheet_lines": missing, "sheet_lines": len(sheet["lines"])}
        conn.execute("INSERT OR REPLACE INTO sheet_pages (source_id, sheet_id, applied_at, summary) VALUES (?,?,?,?)",
                     (source_id, sheet_id, now_iso(), json.dumps(summary)))
    return {"source_id": source_id, "sheet_id": sheet_id, "sheet_number": sheet["number"], **summary}


def _label_line(conn: sqlite3.Connection, ink: refit.LineInk, source: dict[str, Any], line: dict[str, Any], text: str,
                sheet: dict[str, Any], index: int) -> None:
    tokens = tokenize(text)
    flags: set[str] = set()
    note = f"from sheet {sheet['number']}, line {index + 1}"
    words = [w for w in review._line_words(conn, line["id"]) if w["status"] != "excluded"]
    box = Box(line["x"], line["y"], line["w"], line["h"])
    mask, _, _ = ink.mask(source, box)
    misfit = refit.implausible(mask, tokens, None)
    if misfit:
        flags.add("text_may_not_fit")
        note += f"; {misfit}"
    untouched = all(w["created_by"] in ("detector", "machine") and w["text_origin"] != "human" and w["status"] != "approved"
                    and not conn.execute("SELECT 1 FROM region_revisions WHERE region_id = ? AND actor = 'human' LIMIT 1", (w["id"],)).fetchone()
                    for w in words)
    fitted = None if misfit or not untouched else refit.refit_words(ink, source, box, tokens)
    if fitted is not None:
        boxes, clear = fitted
        for w in words:
            review._write(conn, w, {"deleted": 1}, "machine", f"sheet:{sheet['id']}")
        for b, token in zip(boxes, tokens):
            wr = review.create_region(conn, source, "word", b, actor="machine", parent_id=line["id"], action=f"sheet:{sheet['id']}")
            review.machine_propose(conn, wr["id"], token, set() if clear else {"alignment_ambiguous"}, None if clear else "cut from the sheet text; check the word edges")
        if not clear:
            flags.add("alignment_ambiguous")
    elif words and not misfit:
        result = align(text, [w["w"] for w in words])
        for a in result.boxes:
            review.machine_propose(conn, words[a.box_index]["id"], a.text, {"alignment_ambiguous"} if a.ambiguous else set(), a.reason)
        if result.ambiguous:
            flags.add("alignment_ambiguous")
    review.machine_propose(conn, line["id"], text, flags, note)


def origin_of(conn: sqlite3.Connection) -> dict[str, int]:
    """source_id -> sheet number, for pages that are writing sheets (for export provenance)."""
    return {r["source_id"]: r["number"] for r in conn.execute(
        "SELECT p.source_id, s.number FROM sheet_pages p JOIN sheets s ON s.id = p.sheet_id")}
