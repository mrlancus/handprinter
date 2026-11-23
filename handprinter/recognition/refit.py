"""Re-cut a line's word boxes so they match the recognised text.

Detection guesses words from gaps alone, so it splits wide words and joins close ones. Once the
recogniser says a line holds N words, the line's ink can be cut at its N-1 widest gaps instead.
This only ever replaces untouched machine word boxes, and the result is still a proposal.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

import numpy as np
from PIL import Image

from handprinter import detect, imaging, pages
from handprinter.geometry import Box
from handprinter.storage import Store

# A chosen cut must be this much wider than the widest gap left inside a word to count as clear.
CLEAR_RATIO = 1.3


@dataclass
class Segmentation:
    boxes: list[tuple[int, int, int, int]]  # x0, y0, x1, y1 in mask pixels, left to right
    clear: bool  # every cut is clearly wider than every gap kept inside a word, and nothing was dropped


DROP_PENALTY = 0.8  # per piece of ink left out at either end of the line (a doodle, a diagram edge)
MAX_DROP = 3
INNER_GAP_PENALTY = 0.7


def _pieces(mask: np.ndarray, n: int) -> list[tuple[int, int]]:
    """Ink column runs, with obvious letter gaps closed so each piece is a few letters at most."""
    h = mask.shape[0]
    min_ink = max(3, int(0.002 * h * h))
    runs = [(a, b) for a, b in detect._runs(mask.any(axis=0), min_len=1) if int(mask[:, a:b].sum()) >= min_ink]
    if len(runs) <= n:
        return runs
    gaps = sorted(b[0] - a[1] for a, b in zip(runs, runs[1:]))
    for limit in (gaps[len(gaps) // 2], gaps[len(gaps) // 4], 0):
        merged = detect._merge_runs(runs, max_gap=limit)
        if len(merged) >= n + 1 or limit == 0:
            return merged if len(merged) >= n else runs
    return runs


def segment_by_count(mask: np.ndarray, lengths: list[float], spaced: list[bool] | None = None) -> Segmentation | None:
    """Cut a line's ink mask into one box per word, given each word's number of characters.

    Words are contiguous groups of ink pieces. The cut minimises how far each word's width is
    from its share of the line (by character count), prefers cuts at gaps wider than any gap
    kept inside a word, and may leave out a few stray pieces at either end. Returns None when
    the ink has fewer pieces than words (words written touching). A `spaced` word (maths such as
    "λ = h/mv", written with spaces around the =) may hold gaps as wide as those between words.
    """
    n = len(lengths)
    spaced = spaced or [False] * n
    if n < 1 or mask.size == 0:
        return None
    runs = _pieces(mask, n)
    m = len(runs)
    if m < n:
        return None
    gap = [runs[i + 1][0] - runs[i][1] for i in range(m - 1)]
    best: tuple[float, list[tuple[int, int]], int, int] | None = None
    for s in range(min(MAX_DROP, m - n) + 1):
        for e in range(m, max(m - MAX_DROP, s + n) - 1, -1):
            span = runs[s:e]
            # letter width from the ink itself (gaps between words excluded), so far-off junk cannot stretch it
            char_w = max(1e-6, 1.1 * sum(b - a for a, b in span) / max(1, sum(lengths)))
            k_count = e - s
            inf = float("inf")
            cost = [[inf] * (k_count + 1) for _ in range(n + 1)]
            back = [[-1] * (k_count + 1) for _ in range(n + 1)]
            cost[0][0] = 0.0
            for k in range(1, n + 1):
                for i in range(k, k_count - (n - k) + 1):
                    for j in range(k - 1, i):
                        if cost[k - 1][j] == inf:
                            continue
                        width = runs[s + i - 1][1] - runs[s + j][0]
                        c = abs(np.log(max(width, 1) / (lengths[k - 1] * char_w)))
                        inner = max(gap[s + j:s + i - 1], default=0)
                        edges = [gap[s + j - 1]] if s + j > 0 else []
                        edges += [gap[s + i - 1]] if s + i < m else []
                        if edges and inner >= min(edges) and not spaced[k - 1]:
                            c += INNER_GAP_PENALTY
                        if cost[k - 1][j] + c < cost[k][i]:
                            cost[k][i], back[k][i] = cost[k - 1][j] + c, j
            # leaving out an end piece is cheaper when a wide gap sets it apart from the writing
            typical_gap = float(np.median(gap)) if gap else 0.0
            dropped_gaps = [gap[i] for i in range(s)] + [gap[i - 1] for i in range(e, m)]
            total = cost[n][k_count] + sum(DROP_PENALTY * (0.5 if g > 2.5 * max(1.0, typical_gap) else 1.0) for g in dropped_gaps)
            if total == inf:
                continue
            groups, i = [], k_count
            for k in range(n, 0, -1):
                j = back[k][i]
                groups.append((s + j, s + i))
                i = j
            groups.reverse()
            if best is None or total < best[0]:
                best = (total, groups, s, e)
    if best is None:
        return None
    _, groups, s, e = best
    cuts = [gap[b - 1] for (_, b) in groups[:-1]]
    inner = [g for (a, b), sp in zip(groups, spaced) if not sp for g in gap[a:b - 1]]
    clear = s == 0 and e == m and (not cuts or not inner or min(cuts) >= CLEAR_RATIO * max(1, max(inner)))
    boxes = []
    for a, b in groups:
        x0, x1 = runs[a][0], runs[b - 1][1]
        rows = np.flatnonzero(mask[:, x0:x1].any(axis=1))
        boxes.append((int(x0), int(rows[0]), int(x1), int(rows[-1]) + 1))
    return Segmentation(boxes, clear)


class LineInk:
    """Ink masks of line boxes on processed pages, loading each page image once."""

    def __init__(self, conn: sqlite3.Connection, store: Store):
        self.conn, self.store = conn, store
        self._page: tuple[str, int, np.ndarray, list[Box]] | None = None

    def _gray(self, source: dict[str, Any]) -> tuple[np.ndarray, list[Box]]:
        key = (source["id"], source["processing_rev"])
        if self._page is None or self._page[:2] != key:
            source = pages.get_source(self.conn, source["id"])  # parsed processing settings
            img = Image.open(self.store.processed(source["id"], source["processing_rev"])).convert("RGB")
            if source["processing"].get("lighting"):
                img = imaging.normalise_lighting(img)
            self._page = (*key, np.asarray(img.convert("L"), dtype=np.float32), pages.omitted_boxes(self.conn, source["id"]))
        return self._page[2], self._page[3]

    def mask(self, source: dict[str, Any], box: Box) -> tuple[np.ndarray, int, int]:
        """The ink mask inside `box` (processed pixels) and its top-left offset."""
        gray, omitted = self._gray(source)
        x0, y0 = max(0, int(box.x)), max(0, int(box.y))
        x1, y1 = min(gray.shape[1], int(np.ceil(box.x + box.w))), min(gray.shape[0], int(np.ceil(box.y + box.h)))
        mask = detect.ink_mask(gray[y0:y1, x0:x1]) if x1 > x0 and y1 > y0 else np.zeros((0, 0), dtype=bool)
        if mask.size:
            mask[detect.ruled_rows(mask, 0.6)] = False  # printed ruling crossing the line (one unbroken stroke)
            mask &= ~detect.thin_rule_pixels(mask)  # a faint or slightly sloped ruled line touching the letters
            for b in omitted:
                ox0, oy0 = max(0, int(b.x) - x0), max(0, int(b.y) - y0)
                ox1, oy1 = int(np.ceil(b.x + b.w)) - x0, int(np.ceil(b.y + b.h)) - y0
                if ox1 > 0 and oy1 > 0:
                    mask[oy0:oy1, ox0:ox1] = False
        return mask, x0, y0


OPERATOR_COMMANDS = r"\\(le|ge|leq|geq|neq|approx|times|cdot|pm|mp|to|rightarrow|leftarrow|Rightarrow|equiv|propto|sim|cup|cap|in|notin|subset|subseteq|supset|setminus)(?![a-zA-Z])"


def maths_width(src: str) -> float:
    """Roughly how many characters wide LaTeX is when written by hand.

    A command is one symbol, a fraction is as wide as its longer part, and a power or index
    (written small) counts half.
    """
    frac = re.compile(r"\\frac\{([^{}]*)\}\{([^{}]*)\}")
    while frac.search(src):
        src = frac.sub(lambda mm: "x" * (max(len(mm.group(1)), len(mm.group(2))) + 1), src)
    src = re.sub(r"\\(left|right|mathbf|mathrm|text|operatorname|vec|[,;!]|quad)", "", src)
    # a relation or operator (not one in a power or index, like H⁺) is written with space around it
    src = re.sub(OPERATOR_COMMANDS, "=", src)
    src = re.sub(r"\\[a-zA-Z]+", "x", src)
    ops = len(re.findall(r"[=+<>-]", re.sub(r"[\^_](\{[^{}]*\}|\S)", "", src)))
    small = sum(len(re.sub(r"[{}\s]", "", a or b)) for a, b in re.findall(r"[\^_](?:\{([^{}]*)\}|(\S))", src))
    return max(1.0, len(re.sub(r"[{}\s^_]", "", src)) - 0.5 * small + ops)


def maths_len(src: str) -> int:
    return max(1, round(maths_width(src)))


def is_spaced(token: str) -> bool:
    """Maths written with space inside it: around = and other operators, around a fraction or an integral."""
    for mm in re.finditer(r"\$([^$]*)\$", token):
        body = re.sub(r"[\^_](\{[^{}]*\}|\S)", "", re.sub(OPERATOR_COMMANDS, "=", mm.group(1)))
        if re.search(r"[=+<>-]|\\(frac|int|sum|prod)(?![a-zA-Z])", body):
            return True
    return False


def written_len(token: str) -> float:
    """How many characters wide a word is on paper: `$\\alpha$,` is two (α and the comma), not nine."""
    total, rest = 0.0, token
    for mm in re.finditer(r"\$([^$]*)\$", token):
        total += maths_width(mm.group(1))
    rest = re.sub(r"\$[^$]*\$", "", token)
    return max(1.0, total + len(rest))


def own_ink(mask: np.ndarray) -> np.ndarray:
    """The line's own ink: tails and tops of the lines above and below that reach into the box are left out.

    The letter bodies of the line are the rows holding the middle half of its ink. A separate stroke
    entirely outside them is a neighbour's when it is cut by the box edge (it continues outside) or
    lies far from them; dots, accents, a tail written as its own stroke and the parts of a fraction are kept.
    """
    if mask.size == 0 or not mask.any():
        return mask
    boxes, labels = detect.label_components(mask)
    h = mask.shape[0]
    b0, b1, _ = detect._core(mask, 0, 0, mask.shape[1], h)
    body = max(2, b1 - b0)
    lo, hi = b0 - 0.25 * body, b1 + 0.25 * body
    drop = []
    near = max(3, int(0.15 * body))
    for k, (x0, y0, x1, y1, _a) in enumerate(boxes):
        if y1 > lo and y0 < hi:
            continue  # reaches the letter bodies: this line's
        dist = (lo - y1) if y1 <= lo else (y0 - hi)
        # (the parts of a fraction or a power sit well above or below, but inside the box)
        if dist > 2.5 * body:
            drop.append(k)
        elif y0 == 0 or y1 == h:
            # Cut by the box edge: a neighbour's stroke, unless it carries on straight from this line's
            # letters (a tail split off them by a ruled line running along the foot of the writing).
            if y1 == h and y0 > 0:
                above = mask[max(0, y0 - near):y0, x0:x1] & (labels[max(0, y0 - near):y0, x0:x1] != k)
                if above.any():
                    continue
            if y0 == 0 and y1 < h:
                below = mask[y1:y1 + near, x0:x1] & (labels[y1:y1 + near, x0:x1] != k)
                if below.any():
                    continue
            drop.append(k)
    if not drop:
        return mask
    return mask & ~np.isin(labels, drop)


def implausible(mask: np.ndarray, tokens: list[str], typical_line_h: float | None) -> str | None:
    """Why this text cannot be the writing in this line box, or None if it plausibly is.

    Handwritten letters are roughly 0.1-1x as wide as a line is tall (ascenders to descenders).
    Far more letters than that (text from other lines) or a box much taller than the page's
    usual line (several lines in one box) means the boxes must not be cut from this text.
    """
    if mask.size == 0 or not mask.any():
        return "no ink found in this box"
    rows = np.flatnonzero(mask.any(axis=1))
    ink_h = rows[-1] - rows[0] + 1
    if typical_line_h and ink_h > 1.9 * typical_line_h:
        return "this box looks taller than one line of writing; split it into lines"
    ink_w = int(mask.any(axis=0).sum())
    chars = sum(written_len(t) for t in tokens)
    ratio = (ink_w / max(1, chars)) / max(1, ink_h)
    if ratio < 0.1:
        return "the text read is too long for this box (it may include words from other lines)"
    if ratio > 1.5:
        return "the text read is too short for this box (some words may be missing)"
    return None


def refit_words(
    ink: LineInk, source: dict[str, Any], line_box: Box, tokens: list[str], pad: float = 4.0, typical_line_h: float | None = None,
) -> tuple[list[Box], bool] | None:
    """Word boxes (processed pixels) for a line known to hold these words, and whether every cut was clear."""
    mask, ox, oy = ink.mask(source, line_box)
    mask = own_ink(mask)
    if implausible(mask, tokens, typical_line_h):
        return None
    seg = segment_by_count(mask, [written_len(t) for t in tokens], [is_spaced(t) for t in tokens])
    if seg is None:
        return None
    boxes = [Box(ox + x0 - pad, oy + y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad).clamp(source["processed_w"], source["processed_h"])
             for x0, y0, x1, y1 in seg.boxes]
    return boxes, seg.clear


def recut_line(conn: sqlite3.Connection, store: Store, line_id: str, text: str) -> dict[str, Any]:
    """Explicit user action: re-cut a line's word boxes from the text they typed (e.g. "itwas" -> "it was").

    Replaces the line's handwriting word boxes, whoever drew them, with boxes cut from the ink for
    the typed words. The new words carry the typed text and wait for approval. Runs in the worker.
    """
    from handprinter import review
    from handprinter.db import transaction
    from handprinter.recognition.align import tokenize

    tokens = tokenize(text)
    if not tokens:
        raise review.ReviewError("type the line's words first")
    line = review.get_region(conn, line_id)
    source = pages.get_source(conn, line["source_id"])
    if line["kind"] != "line" or line["deleted"]:
        raise review.ReviewError("not a line")
    if line["processing_rev"] != source["processing_rev"]:
        raise review.ReviewError("the page was corrected since this line was drawn; re-detect or fix it in Review")
    ink = LineInk(conn, store)
    box = Box(line["x"], line["y"], line["w"], line["h"])
    mask, _, _ = ink.mask(source, box)
    problem = implausible(mask, tokens, None)
    if problem:
        raise review.ReviewError(f"{problem}; fix the boxes in Review instead")
    fitted = refit_words(ink, source, box, tokens)
    if fitted is None:
        raise review.ReviewError(f"could not find {len(tokens)} separate words in this line's ink; split the boxes in Review instead")
    boxes, clear = fitted
    with transaction(conn):
        current = review.get_region(conn, line_id)
        if current["revision"] != line["revision"]:
            raise review.ConflictError("the line changed meanwhile; reload")
        for w in review._line_words(conn, line_id):
            if w["status"] != "excluded" and w["cls"] in ("handwriting", "uncertain", "equation"):
                review._write(conn, w, {"deleted": 1}, "human", "recut_from_text")
        for box, token in zip(boxes, tokens):
            review.create_region(conn, source, "word", box, actor="human", parent_id=line_id, text=token, status="needs_review",
                                 action="recut_from_text", flags=[] if clear else ["alignment_ambiguous"],
                                 review_reason=None if clear else "cut from your text; check the word edges")
        joined = " ".join(tokens)
        changes: dict[str, Any] = {"review_reason": None, "flags": [f for f in current["flags"] if f != "alignment_ambiguous"]}
        if joined != current["text"]:
            changes.update(text=joined, text_origin="human")
        review._write(conn, current, changes, "human", "recut_from_text")
    return {"line_id": line_id, "words": len(tokens), "clear": clear}
