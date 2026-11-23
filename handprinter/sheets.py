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
    # Printed-pack sheets count only once a page of them has been uploaded (most may never be written).
    for (lines,) in conn.execute("SELECT lines FROM sheets WHERE number <= ? OR id IN (SELECT sheet_id FROM sheet_pages)", (PACK_BASE,)):
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


# ---------- the printable pack: fixed sheets you can write without the app running ----------

PACK_FILE = "sheet_pack.json"
PACK_BASE = 1000  # printed sheets are numbered 1001, 1002, ... so they never clash with sheets made in the app


def pack_id(number: int) -> str:
    """A fixed id per printed sheet, the same on every PC (digits are valid hex)."""
    return f"sht_{number:016d}"


def is_pack(sheet_id: str) -> bool:
    return sheet_id.startswith("sht_") and sheet_id[4:].isdigit() and int(sheet_id[4:]) > PACK_BASE


def load_pack() -> list[dict[str, Any]]:
    from importlib import resources

    return json.loads(resources.files("handprinter").joinpath(PACK_FILE).read_text(encoding="utf-8"))["sheets"]


def ensure_pack(conn: sqlite3.Connection) -> None:
    """Make the printed sheets known to this database (idempotent; their text never changes)."""
    have = {r[0] for r in conn.execute("SELECT id FROM sheets WHERE number > ?", (PACK_BASE,))}
    rows = [(pack_id(s["number"]), s["number"], now_iso(), s["focus"], s["number"], json.dumps(s["lines"]))
            for s in load_pack() if pack_id(s["number"]) not in have]
    if rows:
        conn.executemany("INSERT OR IGNORE INTO sheets (id, number, created_at, focus, seed, lines) VALUES (?,?,?,?,?,?)", rows)


# Which focus each printed sheet has: mostly mixed, then a few of each subject.
PACK_PLAN = (["mixed"] * 20 + ["maths"] * 4 + ["chemistry"] * 4 + ["physics"] * 4 + ["python"] * 4
             + ["pairs"] * 2 + ["capitals"] * 2)


def _one_row(line: str) -> bool:
    """Matrices and other multi-row layouts cannot be written as one line, so a sheet cannot match them."""
    return "\\begin{" not in line and "\\\\" not in line


def build_pack(seed: int = 20261123) -> list[dict[str, Any]]:
    """The printable pack, from the banks only (no database): every line appears once across the pack."""
    rng = random.Random(seed)
    have: Counter = Counter()
    used: Counter = Counter()
    out = []
    for i, focus in enumerate(PACK_PLAN, start=1):
        lines: list[str] = []
        plan = {k: v for k, v in MIX.items() if k != "mine"} if focus == "mixed" else {focus: LINES_PER_SHEET}
        if focus == "mixed":
            plan["everyday"] += MIX["mine"]  # no notes of yours exist offline
        for bank, k in plan.items():
            pool = [line for line in _pool(bank, rng) if _one_row(line) and line not in lines]
            fresh = [line for line in pool if not used[line]]
            picked = _pick(fresh, have, k, rng)
            if len(picked) < k:  # the bank is used up: repeat its least-written lines (a second sample helps too)
                again = sorted((line for line in pool if line not in picked), key=lambda line: (used[line], rng.random()))
                picked += again[:k - len(picked)]
            lines += picked
            for line in picked:
                have.update(_units(line))
        used.update(lines)
        out.append({"number": PACK_BASE + i, "focus": focus, "focus_label": FOCUSES[focus], "lines": lines[:LINES_PER_SHEET]})
    return out


def sheet_by_number(conn: sqlite3.Connection, number: int) -> str | None:
    if number > PACK_BASE:
        ensure_pack(conn)
    row = conn.execute("SELECT id FROM sheets WHERE number = ? ORDER BY created_at LIMIT 1", (number,)).fetchone()
    return row["id"] if row else None


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
    number = conn.execute("SELECT COUNT(*) FROM sheets WHERE number <= ?", (PACK_BASE,)).fetchone()[0] + 1
    conn.execute("INSERT INTO sheets (id, number, created_at, focus, seed, lines) VALUES (?,?,?,?,?,?)",
                 (sheet_id, number, now_iso(), focus, seed, json.dumps(lines)))
    return get_sheet(conn, sheet_id)


def get_sheet(conn: sqlite3.Connection, sheet_id: str) -> dict[str, Any]:
    if is_pack(sheet_id):
        ensure_pack(conn)
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
    """Sheets made in the app, newest first, then the printed pack in number order."""
    ensure_pack(conn)
    made = [r["id"] for r in conn.execute("SELECT id FROM sheets WHERE number <= ? ORDER BY created_at DESC LIMIT 100", (PACK_BASE,))]
    pack = [r["id"] for r in conn.execute("SELECT id FROM sheets WHERE number > ? ORDER BY number", (PACK_BASE,))]
    return [{**get_sheet(conn, i), "printed": i in pack} for i in made + pack]


# ---------- matching a written page to its sheet ----------

SKIP_LINE = 1.6   # a written line that is not on the sheet (a note, a crossed-out attempt)
SKIP_PROMPT = 1.2  # a sheet line you did not write
SKIP_REST = 0.8    # sheet lines after your last written line: you simply stopped there (common)
FIT_REACH = 4     # written line i is only tried against sheet lines i-4 .. i+4
MAX_PAIR = 1.0     # below SKIP_LINE + SKIP_REST (and SKIP_LINE + SKIP_PROMPT): writing the lines in order is the first guess


_maths_len = refit.maths_len


def match_lines(widths: list[float], texts: list[str], word_widths: list[list[float]] | None = None,
                fits: list[list[float]] | None = None) -> list[tuple[int | None, int | None]]:
    """Pair written lines (top to bottom) with sheet lines (in order), allowing skipped or extra lines.

    The cost of a pair combines how far the line's width per character is from the page's typical
    width per character and, when word boxes are known, how well the line's pattern of word widths
    fits the sheet line's word lengths (a line's rhythm tells similar-length lines apart). `fits`, when
    given, is that rhythm measured on the ink itself (cutting the line into the sheet line's words),
    which is far more telling than the detector's word boxes.
    Returns (written index or None, sheet index or None) in order.
    """
    m, n = len(widths), len(texts)
    chars = [max(1, len(re.sub(r"\$([^$]*)\$", lambda mm: "x" * _maths_len(mm.group(1)), t))) for t in texts]
    per_char = float(np.median([w / c for w, c in zip(widths, chars)])) if m and n else 1.0
    # Rhythm (word widths against word lengths) is scored relative to the best sheet line for each written
    # line: when word gaps are found poorly, every pairing fits badly by about the same amount, and that
    # shared misfit must not make skipping look cheaper than pairing.
    rhythm = [[0.0] * n for _ in range(m)]
    if fits:
        for i in range(m):
            low = min(fits[i]) if fits[i] else 0.0
            rhythm[i] = [f - low for f in fits[i]]
    elif word_widths:
        for i in range(m):
            if word_widths[i]:
                row = [min(2.0, align(texts[j], word_widths[i]).cost / max(1, len(tokenize(texts[j])))) for j in range(n)]
                low = min(row) if row else 0.0
                rhythm[i] = [r - low for r in row]
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
                    c = abs(math.log(max(widths[i], 1) / (chars[j] * per_char))) + rhythm[i][j]
                    moves.append((i + 1, j + 1, "pair", min(c, MAX_PAIR)))
                if i < m:
                    moves.append((i + 1, j, "skip_line", SKIP_LINE))
                if j < n:
                    moves.append((i, j + 1, "skip_prompt", SKIP_REST if i == m else SKIP_PROMPT))
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

    Only open lines without your own text are labelled; anything you typed or approved is left alone
    (but still holds its place in the order).
    Matched lines become suggestions to approve in Check; unmatched ones are flagged.
    """
    sheet = get_sheet(conn, sheet_id)
    source = pages.get_source(conn, source_id)
    if source["detection_status"] == "none":
        pages.run_detection(conn, store, source_id)
    # Every written line takes part in the matching, so the order holds; only open lines without your
    # own text are labelled. (Leaving your edited lines out would shift every line below them.)
    rows = conn.execute(
        """SELECT * FROM regions WHERE source_id = ? AND deleted = 0 AND kind = 'line' AND cls IN ('handwriting', 'uncertain', 'equation')
           AND status != 'excluded'""", (source_id,)).fetchall()
    lines = sorted((review.get_region(conn, r["id"]) for r in rows), key=lambda r: (r["y"] + r["h"] / 2, r["x"]))
    ink = refit.LineInk(conn, store)
    lines = _join_rows(conn, ink, source, lines, sheet_id)
    widths = []
    for ln in lines:
        mask, _, _ = ink.mask(source, Box(ln["x"], ln["y"], ln["w"], ln["h"]))
        widths.append(float(mask.any(axis=0).sum()) if mask.size else ln["w"])
    word_widths = [[w["w"] for w in review._line_words(conn, ln["id"]) if w["status"] != "excluded"] for ln in lines]
    # How well each written line's ink cuts into each sheet line's words. Lines more than a few places
    # apart cannot pair (a handful of skipped or extra lines at most), so they are not tried.
    fits = []
    for i, ln in enumerate(lines):
        mask, _, _ = ink.mask(source, Box(ln["x"], ln["y"], ln["w"], ln["h"]))
        own = refit.own_ink(mask)
        fits.append([refit.fit_cost(own, t) if abs(i - j) <= FIT_REACH else 2.0 for j, t in enumerate(sheet["lines"])])
    pairs = match_lines(widths, sheet["lines"], word_widths, fits)
    matched, unmatched_lines, missing = 0, 0, []
    with transaction(conn):
        for li, pj in pairs:
            if li is None and pj is not None:
                missing.append(pj + 1)
                continue
            if li is None:
                continue
            line = review.get_region(conn, lines[li]["id"])
            if line["status"] not in ("unprocessed", "proposed", "needs_review") or line["text_origin"] not in (None, "machine"):
                continue  # yours: kept as it is
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


def refill_all(conn: sqlite3.Connection, store: Store, progress: Any = None) -> dict[str, Any]:
    """Fill every page labelled from a sheet in again (after the word cutting improved).

    Same as "Fill in from sheet" on each page: only untouched machine boxes and text are redone;
    anything you typed, moved or approved stays.
    """
    rows = conn.execute("SELECT p.source_id, p.sheet_id FROM sheet_pages p JOIN sources s ON s.id = p.source_id "
                        "WHERE s.deleted = 0 ORDER BY s.created_at").fetchall()
    out = {"pages": 0, "lines_matched": 0, "failed": []}
    for i, r in enumerate(rows):
        if progress:
            progress(i / max(1, len(rows)), f"page {i + 1} of {len(rows)}")
        try:
            res = apply_sheet(conn, store, r["source_id"], r["sheet_id"])
        except Exception as exc:  # one bad page must not stop the rest
            out["failed"].append({"source_id": r["source_id"], "error": str(exc)[:200]})
            continue
        out["pages"] += 1
        out["lines_matched"] += res["matched"]
    return out


def _untouched(conn: sqlite3.Connection, region: dict[str, Any]) -> bool:
    return (region["created_by"] in ("detector", "machine") and region["text_origin"] != "human" and region["status"] != "approved"
            and not conn.execute("SELECT 1 FROM region_revisions WHERE region_id = ? AND actor = 'human' LIMIT 1", (region["id"],)).fetchone())


def _join_rows(conn: sqlite3.Connection, ink: refit.LineInk, source: dict[str, Any], lines: list[dict[str, Any]],
               sheet_id: str) -> list[dict[str, Any]]:
    """On a sheet each written line is one row: join the pieces detection split it into.

    Detection works without knowing the page, so a wide gap can end a line early (the last word
    becomes a line of its own) and a descender or a dot can become a "line" of its own. Those pieces
    would be matched to sheet lines and steal their text. Untouched machine lines only; returns the
    lines top to bottom.
    """
    if len(lines) < 2:
        return lines
    info = []
    for ln in lines:
        mask, _, oy = ink.mask(source, Box(ln["x"], ln["y"], ln["w"], ln["h"]))
        if not mask.any():
            info.append(None)
            continue
        rows = np.flatnonzero(mask.any(axis=1))
        c0, c1, total = detect_core(mask)
        info.append({"line": ln, "x0": ln["x"], "x1": ln["x"] + ln["w"], "y0": oy + rows[0], "y1": oy + rows[-1] + 1,
                     "b0": oy + c0, "b1": oy + c1, "ink": total, "ok": _untouched(conn, ln)})
    real = [i for i in info if i]
    if len(real) < 2:
        return lines
    widest = max(i["x1"] - i["x0"] for i in real)
    wide = [i for i in real if i["x1"] - i["x0"] >= 0.3 * widest]
    body = float(np.median([i["b1"] - i["b0"] for i in wide] or [1]))
    # A row of writing has a lot of ink; a strip of tails or dots under a row can be just as wide but has little.
    typical_ink = float(np.median([i["ink"] for i in wide] or [1]))
    big = [i for i in real if (i in wide and i["ink"] >= 0.3 * typical_ink) or not i["ok"]]
    small = [i for i in real if i not in big]

    def mid(i: dict[str, Any]) -> float:
        return (i["b0"] + i["b1"]) / 2

    # The spacing between rows sets every distance below: letter sizes vary (a fraction, capitals), the
    # spacing of the lines on a sheet does not.
    mids = sorted(mid(i) for i in big)
    steps = [b - a for a, b in zip(mids, mids[1:]) if b - a > 1.5 * body]
    pitch = float(np.median(steps)) if steps else 4.0 * body
    # Rows: the long lines, plus any piece at the same height beside them (the end of a row, a split equation).
    rows_: list[list[dict[str, Any]]] = []
    for i in sorted(big, key=mid):
        home = None
        for r in rows_:
            rmid = sum(mid(j) for j in r) / len(r)
            apart = all(min(i["x1"], j["x1"]) - max(i["x0"], j["x0"]) < 0.3 * min(i["x1"] - i["x0"], j["x1"] - j["x0"]) for j in r)
            if abs(mid(i) - rmid) <= 0.3 * pitch and apart and all(j["ok"] for j in r) and i["ok"]:
                home = r
        if home is None:
            rows_.append([i])
        else:
            home.append(i)
    # Short or faint pieces (the end of a row, tails, dots, the bottom of a fraction): the row whose letters
    # are nearest in height, if closer than about half the spacing between rows (else a line of its own).
    for i in small:
        best, dist = None, 0.6 * pitch
        centre = (i["y0"] + i["y1"]) / 2
        for r in rows_:
            if not all(j["ok"] for j in r):
                continue
            x0, x1 = min(j["x0"] for j in r), max(j["x1"] for j in r)
            if i["x1"] < x0 - 4 * pitch or i["x0"] > x1 + 4 * pitch:
                continue
            d = abs(centre - sum(mid(j) for j in r) / len(r))
            if d < dist:
                best, dist = r, d
        if best is None:
            rows_.append([i])
        else:
            best.append(i)
    # Rows made of pieces only (an equation detected in parts) can sit side by side at one height: join them.
    merged = True
    while merged:
        merged = False
        for a in range(len(rows_)):
            for b in range(a + 1, len(rows_)):
                ra, rb = rows_[a], rows_[b]
                if not all(j["ok"] for j in ra + rb):
                    continue
                ma, mb = sum(mid(j) for j in ra) / len(ra), sum(mid(j) for j in rb) / len(rb)
                ax0, ax1 = min(j["x0"] for j in ra), max(j["x1"] for j in ra)
                bx0, bx1 = min(j["x0"] for j in rb), max(j["x1"] for j in rb)
                if abs(ma - mb) <= 0.3 * pitch and min(ax1, bx1) - max(ax0, bx0) < 0.3 * min(ax1 - ax0, bx1 - bx0):
                    rows_[a] = ra + rb
                    del rows_[b]
                    merged = True
                    break
            if merged:
                break
    out = []
    for r in rows_:
        if len(r) == 1:
            out.append(r[0]["line"])
            continue
        box = Box(r[0]["line"]["x"], r[0]["line"]["y"], r[0]["line"]["w"], r[0]["line"]["h"])
        for j in r[1:]:
            box = box.union(Box(j["line"]["x"], j["line"]["y"], j["line"]["w"], j["line"]["h"]))
        flags = sorted({f for j in r for f in j["line"]["flags"]} - {"may_be_several_lines"})
        with transaction(conn):
            joined = review.create_region(conn, source, "line", box, actor="machine", flags=flags,
                                          action=f"sheet:{sheet_id}:join_row")
            for j in r:
                review._write(conn, j["line"], {"deleted": 1}, "machine", f"joined_into:{joined['id']}")
                conn.execute("UPDATE regions SET parent_id = ? WHERE parent_id = ? AND deleted = 0", (joined["id"], j["line"]["id"]))
        out.append(review.get_region(conn, joined["id"]))
    # Detection drops small marks on their own (the dots of ∴, an ellipsis, a lone full stop) and so ends a
    # line before them; a sheet line includes them, so its box is widened to take in ink at its height.
    for k, ln in enumerate(out):
        if _untouched(conn, ln):
            wider = _reach_marks(ink, source, ln, pitch)
            if wider is not None:
                with transaction(conn):
                    out[k] = review._write(conn, ln, {"x": wider.x, "w": wider.w}, "machine", f"sheet:{sheet_id}:widen")
    leftover = [ln for ln, i in zip(lines, info) if i is None]
    return sorted(out + leftover, key=lambda r: (r["y"] + r["h"] / 2, r["x"]))


def _reach_marks(ink: refit.LineInk, source: dict[str, Any], ln: dict[str, Any], pitch: float) -> Box | None:
    """The line's box widened to take in ink just beyond its ends at the height of its letters, or None."""
    reach = 1.5 * pitch
    x0 = max(0.0, ln["x"] - reach)
    x1 = min(float(source["processed_w"]), ln["x"] + ln["w"] + reach)
    band_y0, band_h = ln["y"] + 0.2 * ln["h"], 0.6 * ln["h"]
    mask, ox, _ = ink.mask(source, Box(x0, band_y0, x1 - x0, band_h))
    if mask.size == 0:
        return None
    cols = mask.any(axis=0)
    left, right = int(round(ln["x"] - ox)), int(round(ln["x"] + ln["w"] - ox))
    gap = int(0.8 * pitch)
    new_left, new_right = left, right
    x = right
    while x < len(cols):  # walk right: each next bit of ink within `gap` of the current end joins the line
        nxt = next((c for c in range(x, min(len(cols), x + gap)) if cols[c]), None)
        if nxt is None:
            break
        end = nxt
        while end < len(cols) and cols[end]:
            end += 1
        if ox + end >= source["processed_w"] - 1:
            break  # ink running off the page edge is a border or shadow, not writing
        new_right, x = end, end
    x = left - 1
    while x >= 0:
        nxt = next((c for c in range(x, max(-1, x - gap), -1) if cols[c]), None)
        if nxt is None:
            break
        start = nxt
        while start >= 0 and cols[start]:
            start -= 1
        if ox + start + 1 <= 0:
            break  # ink running off the page edge is a border or shadow, not writing
        new_left, x = start + 1, start
    if new_left == left and new_right == right:
        return None
    pad = 4.0
    a = max(0.0, ox + new_left - (pad if new_left < left else 0))
    b = min(float(source["processed_w"]), ox + new_right + (pad if new_right > right else 0))
    return Box(a, ln["y"], b - a, ln["h"])


def detect_core(mask: np.ndarray) -> tuple[int, int, int]:
    from handprinter import detect
    return detect._core(mask, 0, 0, mask.shape[1], mask.shape[0])


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
    untouched = all(_untouched(conn, w) for w in words)
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
