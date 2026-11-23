"""Local, fallible detection: page corners, printed ruling, line bands and word boxes.

Everything here produces editable proposals. Simple projection profiles do not segment
messy worksheets perfectly, and callers must present results as such.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageFilter

from handprinter.geometry import Box, Point

WORK_EDGE = 1600


@dataclass
class DetectedLine:
    box: Box
    words: list[Box]
    overlaps_ruling: bool
    may_hold_several: bool = False  # tall band that could not be split cleanly into lines
    touches_neighbour: bool = False  # split from a line whose strokes touch this one
    words_at_edge: frozenset[int] = frozenset()  # indexes of words that run off the photo edge
    not_a_line: bool = False  # sideways writing, a bracket or a drawing stroke: kept, but not sent for reading


@dataclass
class Detection:
    lines: list[DetectedLine]
    rulings: list[Box]


def otsu_threshold(gray: np.ndarray) -> float:
    hist = np.bincount(gray.astype(np.uint8).ravel(), minlength=256).astype(float)
    total = hist.sum()
    levels = np.arange(256)
    w0 = np.cumsum(hist)
    w1 = total - w0
    m0 = np.cumsum(hist * levels)
    mean_total = m0[-1]
    with np.errstate(divide="ignore", invalid="ignore"):
        between = (mean_total * w0 / total - m0) ** 2 / (w0 * w1 / total)
    between[~np.isfinite(between)] = 0
    return float(np.argmax(between))


def suggest_page_corners(img: Image.Image) -> list[Point] | None:
    """Guess the paper quadrilateral (TL, TR, BR, BL) in the given image's coordinates.

    Returns None when the paper already fills the frame or no clear page is found.
    """
    scale = min(1.0, 800 / max(img.size))
    small = img.convert("L").resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))))
    gray = np.asarray(small.filter(ImageFilter.GaussianBlur(2)), dtype=np.float32)
    mask = gray > otsu_threshold(gray)
    ys, xs = np.nonzero(mask)
    if len(xs) < 0.2 * mask.size:
        return None
    s, d = xs + ys, xs - ys
    pts = [(xs[s.argmin()], ys[s.argmin()]), (xs[d.argmax()], ys[d.argmax()]), (xs[s.argmax()], ys[s.argmax()]), (xs[d.argmin()], ys[d.argmin()])]
    area = _quad_area(pts)
    h, w = mask.shape
    if area < 0.3 * w * h or area > 0.97 * w * h:
        return None
    return [(float(x) / scale, float(y) / scale) for x, y in pts]


def _quad_area(pts: list[tuple[float, float]]) -> float:
    acc = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        acc += x0 * y1 - x1 * y0
    return abs(acc) / 2


def ink_mask(gray: np.ndarray) -> np.ndarray:
    """Adaptive ink mask: darker than the local paper estimate. Keeps faint strokes."""
    img = Image.fromarray(gray.astype(np.uint8), "L")
    radius = max(6, max(gray.shape) // 60)
    local = np.asarray(img.filter(ImageFilter.BoxBlur(radius)), dtype=np.float32)
    # Writing sits on light paper or a light page: marks on a dark surround (app toolbars, coloured bars,
    # the desk around the paper) are not handwriting.
    # judged over a wide area, so dense or heavy handwriting never counts as a dark background
    wide = np.asarray(img.filter(ImageFilter.BoxBlur(radius * 3)), dtype=np.float32)
    dark = Image.fromarray(((wide <= 120) * 255).astype(np.uint8), "L").filter(ImageFilter.BoxBlur(radius))
    near_dark = np.asarray(dark) > 0  # the dark area plus a margin, so its edge is not read as a stroke
    return (gray < local - 18) & (gray < 215) & ~near_dark


def detect_layout(img: Image.Image, omit: list[Box] | None = None) -> Detection:
    """Find ruling, lines and words. Ink inside `omit` boxes (image coordinates) is ignored."""
    scale = min(1.0, WORK_EDGE / max(img.size))
    small = img.convert("L").resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
    gray = np.asarray(small, dtype=np.float32)
    mask = ink_mask(gray)
    h, w = mask.shape
    for b in omit or []:
        x0, y0 = max(0, int(b.x * scale)), max(0, int(b.y * scale))
        x1, y1 = min(w, int(np.ceil((b.x + b.w) * scale))), min(h, int(np.ceil((b.y + b.h) * scale)))
        mask[y0:y1, x0:x1] = False

    # Printed ruling is one unbroken stroke across much of the page; a row through a line of writing
    # has lots of ink too, but in short runs letter by letter.
    ruling_rows = ruled_rows(mask)
    rulings = [Box(0, y0, w, y1 - y0) for y0, y1 in _runs(ruling_rows, min_len=1)]
    clean = mask.copy()
    clean[ruling_rows] = False
    # Margin lines, table borders and other long vertical rules: ink in most of a column's height.
    vertical = vertical_rule_columns(clean)
    clean[:, vertical] = False
    vertical_rulings = [Box(x0, 0, x1 - x0, h) for x0, x1 in _runs(vertical, min_len=1)]

    # Word-first: letters join into word pieces, pieces into a line only when their letter bodies sit at
    # the same height and close together. Columns, headings and side notes separate by themselves.
    th = text_height(components(clean))
    raw_lines = find_lines(clean, th or None)
    boxes = [Box(min(wd[0] for wd in ws), min(wd[1] for wd in ws), max(wd[2] for wd in ws) - min(wd[0] for wd in ws),
                 max(wd[3] for wd in ws) - min(wd[1] for wd in ws)) for ws in raw_lines]
    heights = sorted(b.h for b in boxes)
    typical = float(heights[len(heights) // 2]) if heights else 0.0
    lines: list[DetectedLine] = []
    for ws, line_box in zip(raw_lines, boxes):
        if line_box.w * line_box.h < 12:
            continue
        ink = int(clean[int(line_box.y):int(line_box.y + line_box.h), int(line_box.x):int(line_box.x + line_box.w)].sum())
        # Specks, dots and stray strokes on their own are not lines of writing: reading them wastes a request.
        if th and ((line_box.w < 1.6 * th and line_box.h < 1.6 * th) or ink < 0.6 * th * th):
            continue
        # Much taller than wide: sideways margin writing, a bracket, part of a drawing.
        sideways = bool(th) and line_box.h > 1.5 * line_box.w and line_box.h > 1.6 * th
        words = [Box(x0, y0, x1 - x0, y1 - y0) for x0, y0, x1, y1 in ws]
        overlaps = any(line_box.intersection_area(r.pad(2)) > 0 for r in rulings)
        edge = 2  # px at work scale: writing that runs to the photo edge is probably cut off
        at_edge = frozenset(i for i, wb in enumerate(words) if wb.x <= edge or wb.x + wb.w >= w - edge or wb.y <= edge or wb.y + wb.h >= h - edge)
        several = len(heights) >= 3 and line_box.h > 1.9 * typical
        touching = any(o is not line_box and line_box.intersection_area(o) > 0.02 * line_box.w * line_box.h for o in boxes)
        lines.append(DetectedLine(line_box, words, overlaps, several and not sideways, touching, at_edge, sideways))
    # reading order: column by column (blocks from an XY cut), top to bottom inside each
    blocks = xy_blocks(clean)

    def block_of(line: DetectedLine) -> int:
        cx, cy = line.box.x + line.box.w / 2, line.box.y + line.box.h / 2
        for k, (bx0, by0, bx1, by1) in enumerate(blocks):
            if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                return k
        return len(blocks)
    lines.sort(key=lambda line: (block_of(line), line.box.y, line.box.x))

    inv = 1 / scale
    pad = 4 * inv

    def up(b: Box) -> Box:
        return Box(b.x * inv - pad, b.y * inv - pad, b.w * inv + 2 * pad, b.h * inv + 2 * pad).clamp(img.width, img.height)

    return Detection(
        lines=[DetectedLine(up(line.box), [up(wb) for wb in line.words], line.overlaps_ruling, line.may_hold_several,
                            line.touches_neighbour, line.words_at_edge, line.not_a_line) for line in lines],
        rulings=[Box(0, r.y * inv, img.width, max(1.0, r.h * inv)) for r in rulings]
        + [Box(r.x * inv, 0, max(1.0, r.w * inv), img.height) for r in vertical_rulings],
    )


def xy_blocks(mask: np.ndarray, min_gutter: int | None = None, min_gap: int | None = None,
              min_column_h: int | None = None) -> list[tuple[int, int, int, int]]:
    """Split the page's ink into blocks (columns, headings, separate notes) by recursive XY cut.

    A block is cut at the widest empty vertical gutter (at least ~2.5% of the page width) or,
    failing that, the tallest empty horizontal gap. A column or row counts as empty when it has
    almost no ink, so stray specks do not hold two columns together. Returns (x0, y0, x1, y1).
    """
    h, w = mask.shape
    min_gutter = min_gutter or max(8, int(0.025 * w))
    min_gap = min_gap or max(4, h // 120)
    out: list[tuple[int, int, int, int]] = []

    def interior_gaps(counts: np.ndarray, tol: float, min_len: int) -> list[tuple[int, int]]:
        empty = counts <= tol
        return [(a, b) for a, b in _runs(empty, min_len=min_len) if a > 0 and b < len(counts)]

    def cut(x0: int, y0: int, x1: int, y1: int, depth: int) -> None:
        sub = mask[y0:y1, x0:x1]
        rows, cols = np.flatnonzero(sub.any(axis=1)), np.flatnonzero(sub.any(axis=0))
        if len(rows) == 0:
            return
        x0, x1, y0, y1 = x0 + int(cols[0]), x0 + int(cols[-1]) + 1, y0 + int(rows[0]), y0 + int(rows[-1]) + 1
        sub = mask[y0:y1, x0:x1]
        if depth < 12:
            gutters = interior_gaps(sub.sum(axis=0), max(1.0, 0.003 * (y1 - y0)), min_gutter)
            # A real column gap has writing on both sides down most of its height; the empty strip beside
            # a few short lines does not count.
            rows_all = max(1, int(sub.any(axis=1).sum()))

            def column_like(part: np.ndarray) -> bool:
                rows_here = part.any(axis=1)
                return rows_here.sum() >= 0.5 * rows_all and len(_runs(rows_here, min_len=2)) >= 3

            gutters = [(a, b) for a, b in gutters if column_like(sub[:, :a]) and column_like(sub[:, b:])]
            if gutters and (y1 - y0) >= (min_column_h or 3 * min_gap):
                a, b = max(gutters, key=lambda g: g[1] - g[0])
                cut(x0, y0, x0 + a, y1, depth + 1)
                cut(x0 + b, y0, x1, y1, depth + 1)
                return
            # rows must be truly empty: a thin tail crossing the gap keeps two lines in one block
            gaps = interior_gaps(sub.sum(axis=1), 0, min_gap)
            if gaps:
                a, b = max(gaps, key=lambda g: g[1] - g[0])
                cut(x0, y0, x1, y0 + a, depth + 1)
                cut(x0, y0 + b, x1, y1, depth + 1)
                return
        out.append((x0, y0, x1, y1))

    cut(0, 0, w, h, 0)
    return out


def ruled_rows(mask: np.ndarray, min_run: float = 0.3) -> np.ndarray:
    """Rows holding an unbroken stroke over `min_run` of the width (printed ruling, box edges)."""
    h, w = mask.shape
    need = int(min_run * w)
    out = np.zeros(h, dtype=bool)
    for y in np.flatnonzero(mask.sum(axis=1) >= need):
        # allow tiny breaks in a printed line (photo noise), not the gaps between letters
        row = mask[y].copy()
        row[1:-1] |= mask[y, :-2] & mask[y, 2:]
        out[y] = any(b - a >= need for a, b in _runs(row, min_len=need))
    return out


def thin_rule_pixels(mask: np.ndarray) -> np.ndarray:
    """Pixels of a thin horizontal rule running through writing (e.g. the ruled line on an iPad page).

    Unlike `ruled_rows`, the rule may be faint, slightly sloped and broken: next to letters the
    paper looks darker, so a faint rule drops out of the ink mask there, and what is left are
    thin level pieces in the gaps between words, which glue the words together. A rule is a
    3-row band holding long, thin pieces (each at least 0.6x the box height) that add up to a
    quarter of the width; joins in joined-up writing are far shorter. A rule along the foot of the
    letters leaves only short gaps between words: there, thin ink right across the writing in one
    band also counts. Only thin pixels are returned, so letters keep their ink.
    """
    h, w = mask.shape
    out = np.zeros_like(mask)
    if h < 4 or w < 20 or not mask.any():
        return out
    up = np.zeros((h, w), dtype=np.int32)
    down = np.zeros((h, w), dtype=np.int32)
    for y in range(h):
        up[y] = (up[y - 1] + 1) * mask[y] if y else mask[y]
    for y in range(h - 1, -1, -1):
        down[y] = (down[y + 1] + 1) * mask[y] if y < h - 1 else mask[y]
    # Lengths are measured against the writing, not the box: a box may be tall (a tail, a fraction) while
    # the letters, and so the gaps between words, stay small. Line height is about 3.5x the letter bodies.
    b0, b1, _ = _core(mask, 0, 0, w, h)
    ref = min(h, max(12, 3.5 * (b1 - b0)))
    thin = mask & (up + down - 1 <= max(3, round(0.05 * ref)))  # a few pixels tall where it is
    long_piece, part = max(8, int(0.6 * ref)), max(6, int(0.3 * ref))
    cols = np.flatnonzero(mask.any(axis=0))
    x0, x1 = int(cols[0]), int(cols[-1]) + 1
    bins = np.linspace(x0, x1, 11).astype(int)
    for y in range(1, h - 1):
        band = thin[y - 1:y + 2].any(axis=0)
        if band.sum() < 0.25 * w:
            continue
        runs = _merge_runs(_runs(band, min_len=1), max_gap=2)
        pieces = [(a, b) for a, b in runs if b - a >= part]
        long_total = sum(b - a for a, b in pieces if b - a >= long_piece)
        if long_total >= 0.25 * w:
            for a, b in pieces:
                out[y - 1:y + 2, a:b] |= thin[y - 1:y + 2, a:b]
            continue
        # A rule along the foot of the letters shows only in the (short) gaps between words, and in bits
        # between the letters: thin ink in this band right across the writing, with several gaps' worth of
        # long pieces. The letters' own joins cross the band at an angle and cover far less of it.
        spread = sum(band[a:b].any() for a, b in zip(bins, bins[1:]) if b > a)
        if (band[x0:x1].sum() >= 0.3 * (x1 - x0) and spread >= 9
                and sum(1 for a, b in pieces if b - a >= long_piece) >= 2 and long_total >= 0.08 * w):
            out[y - 1:y + 2] |= thin[y - 1:y + 2]
    return out


def vertical_rule_columns(mask: np.ndarray, min_run: float = 0.15) -> np.ndarray:
    """Columns crossed by one unbroken vertical stroke spanning many lines (margin rules, borders).

    Handwriting never runs unbroken through `min_run` of the page height; a slightly tilted rule
    still does once the mask is widened by a few pixels. Only meaningful on a whole page.
    """
    h, w = mask.shape
    if h < 200:
        return np.zeros(w, dtype=bool)
    wide = mask.copy()
    for shift in (1, 2):
        wide[:, shift:] |= mask[:, :-shift]
        wide[:, :-shift] |= mask[:, shift:]
    need = int(min_run * h)
    cols = np.zeros(w, dtype=bool)
    for x in np.flatnonzero(wide.sum(axis=0) >= need):
        cols[x] = any(b - a >= need for a, b in _runs(wide[:, x], min_len=need))
    return cols | np.roll(cols, 1) | np.roll(cols, -1) | np.roll(cols, 2) | np.roll(cols, -2)


def word_gap_threshold(ink_runs: list[tuple[int, int]], line_h: int) -> int:
    """Largest gap that still counts as inside a word, from the line's own gap distribution.

    Splits sorted gap lengths at the biggest relative jump (letter gaps vs word gaps);
    falls back to a fraction of line height when the distribution has no clear jump.
    """
    gaps = sorted(b[0] - a[1] for a, b in zip(ink_runs, ink_runs[1:]))
    fallback = max(2, int(line_h * 0.3))
    if len(gaps) < 2:
        return fallback
    best_ratio, cut = 1.0, None
    for small, big in zip(gaps, gaps[1:]):
        ratio = (big + 1) / (small + 1)
        if ratio > best_ratio:
            best_ratio, cut = ratio, small
    if cut is None or best_ratio < 1.6:
        return fallback
    return max(2, min(cut, int(line_h * 0.6)))


def _smooth(values: np.ndarray, radius: int) -> np.ndarray:
    kernel = np.ones(2 * radius + 1) / (2 * radius + 1)
    return np.convolve(values, kernel, mode="same")


def _runs(flags: np.ndarray, min_len: int) -> list[tuple[int, int]]:
    padded = np.concatenate([[False], flags.astype(bool), [False]])
    edges = np.flatnonzero(padded[1:] != padded[:-1])
    return [(int(a), int(b)) for a, b in zip(edges[::2], edges[1::2]) if b - a >= min_len]


def _merge_runs(runs: list[tuple[int, int]], max_gap: int) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in runs:
        if merged and start - merged[-1][1] <= max_gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))  # a dot inside a word must not cut its end off
        else:
            merged.append((start, end))
    return merged


# ---------- word-first line finding (connected components) ----------

def components(mask: np.ndarray) -> np.ndarray:
    """Bounding boxes of 8-connected ink components: array of (x0, y0, x1, y1, area), x1/y1 exclusive.

    Run-length labelling with union-find, numpy for the runs and plain Python for the joins
    (fast enough for a page at working resolution).
    """
    return _label(mask)[0]


def label_components(mask: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Like `components`, plus an image holding each ink pixel's component number (row in the boxes), -1 elsewhere."""
    return _label(mask, with_image=True)


def _label(mask: np.ndarray, with_image: bool = False) -> tuple[np.ndarray, np.ndarray]:
    h, w = mask.shape
    padded = np.zeros((h, w + 2), dtype=np.int8)
    padded[:, 1:-1] = mask
    diff = np.diff(padded, axis=1)
    ys, xs = np.nonzero(diff)
    starts_mask = diff[ys, xs] == 1
    run_y = ys[starts_mask]
    run_s = xs[starts_mask]
    run_e = xs[~starts_mask]  # exclusive
    n = len(run_y)
    if n == 0:
        return np.zeros((0, 5), dtype=np.int64), np.full(mask.shape, -1, dtype=np.int32)
    parent = list(range(n))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    row_start = np.searchsorted(run_y, np.arange(h + 1))
    for y in range(1, h):
        a0, a1 = row_start[y - 1], row_start[y]
        b0, b1 = row_start[y], row_start[y + 1]
        i, j = a0, b0
        while i < a1 and j < b1:
            # 8-connectivity: runs touch if they overlap or meet diagonally
            if run_s[i] <= run_e[j] and run_s[j] <= run_e[i]:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[rj] = ri
            if run_e[i] < run_e[j]:
                i += 1
            else:
                j += 1
    roots = np.array([find(k) for k in range(n)])
    order = np.argsort(roots, kind="stable")
    roots_sorted = roots[order]
    cut = np.flatnonzero(np.diff(roots_sorted)) + 1
    groups = np.split(order, cut)
    out = np.empty((len(groups), 5), dtype=np.int64)
    lengths = run_e - run_s
    for k, g in enumerate(groups):
        out[k] = (run_s[g].min(), run_y[g].min(), run_e[g].max(), run_y[g].max() + 1, lengths[g].sum())
    image = np.full(mask.shape, -1, dtype=np.int32)
    if with_image:
        for k, g in enumerate(groups):
            for r in g:
                image[run_y[r], run_s[r]:run_e[r]] = k
    return out, image


def _dilate_h(m: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return m.copy()
    c = np.cumsum(np.pad(m.astype(np.int32), ((0, 0), (r + 1, r))), axis=1)
    return (c[:, 2 * r + 1:] - c[:, :-2 * r - 1]) > 0


def _dilate_v(m: np.ndarray, r: int) -> np.ndarray:
    return _dilate_h(m.T, r).T


def close_gaps(m: np.ndarray, rx: int, ry: int) -> np.ndarray:
    """Morphological closing with a rectangle: joins letters into word pieces without growing them."""
    d = _dilate_v(_dilate_h(m, rx), ry)
    return ~_dilate_v(_dilate_h(~d, rx), ry)


def text_height(comps: np.ndarray) -> float:
    """Typical height of the writing, from its ink components (dots and specks ignored)."""
    if len(comps) == 0:
        return 0.0
    hs = comps[:, 3] - comps[:, 1]
    ws = comps[:, 2] - comps[:, 0]
    big = hs[(comps[:, 4] >= 12) & (hs >= 4) & (ws >= 2)]
    if len(big) == 0:
        return float(np.median(hs))
    return float(np.percentile(big, 60))


@dataclass
class _Piece:
    x0: int
    y0: int
    x1: int
    y1: int
    c0: int  # core rows: the body of the letters, without ascenders and descenders
    c1: int
    ink: int


def _core(mask: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> tuple[int, int, int]:
    """The rows holding the middle half of a piece's ink: the body of the letters.

    Quartiles of the ink, so tails, loops and a thin stroke touching the word (a dash, an
    underline) barely move it.
    """
    rows = mask[y0:y1, x0:x1].sum(axis=1)
    total = int(rows.sum())
    if total == 0:
        return y0, y1, 0
    cum = np.cumsum(rows)
    lo = int(np.searchsorted(cum, 0.25 * total))
    hi = int(np.searchsorted(cum, 0.75 * total)) + 1
    return y0 + lo, y0 + max(hi, lo + 1), total


def _split_tall(mask: np.ndarray, p: _Piece, th: float) -> list[_Piece]:
    """A piece much taller than one line (tails of one line touching the next) is cut at its emptiest row."""
    if p.y1 - p.y0 < 2.2 * th:
        return [p]
    rows = mask[p.y0:p.y1, p.x0:p.x1].sum(axis=1).astype(float)
    lo, hi = int(0.6 * th), len(rows) - int(0.6 * th)
    if hi <= lo:
        return [p]
    smooth = _smooth(rows, max(1, int(th // 6)))
    cut = lo + int(np.argmin(smooth[lo:hi]))
    if smooth[cut] > 0.5 * np.median(smooth[smooth > 0]):
        return [p]  # no clear gap: one block (a diagram, a big symbol)
    out = []
    for a, b in ((p.y0, p.y0 + cut), (p.y0 + cut, p.y1)):
        sub = mask[a:b, p.x0:p.x1]
        cols, rws = np.flatnonzero(sub.any(axis=0)), np.flatnonzero(sub.any(axis=1))
        if len(cols) == 0:
            continue
        q = (p.x0 + int(cols[0]), a + int(rws[0]), p.x0 + int(cols[-1]) + 1, a + int(rws[-1]) + 1)
        c0, c1, ink = _core(mask, *q)
        out.extend(_split_tall(mask, _Piece(*q, c0, c1, ink), th))
    return out or [p]


def find_lines(clean: np.ndarray, th: float | None = None) -> list[list[tuple[int, int, int, int]]]:
    """Lines as lists of word boxes (x0, y0, x1, y1), word-first, with the writing size measured per area.

    The page is split into blocks (columns, headings, separate notes); each block is measured on
    its own, so a big heading and small notes on one page are both handled at their own size.
    """
    th = th or text_height(components(clean))
    if th <= 0:
        return []
    blocks = xy_blocks(clean, min_column_h=int(6 * th))
    out: list[list[tuple[int, int, int, int]]] = []
    rest = clean.copy()
    for bx0, by0, bx1, by1 in blocks if len(blocks) > 1 else []:
        sub = clean[by0:by1, bx0:bx1]
        comps = components(sub)
        if len(comps) < 5:
            continue
        local = text_height(comps)
        # Only an area whose writing is clearly a different size (a big heading, a small side note) is
        # handled on its own; the rest of the page stays together, so lines are never cut at block edges.
        if 0.6 * th <= local <= 1.6 * th:
            continue
        local = min(3 * th, max(0.5 * th, local))
        for line in _find_lines_in(sub, local):
            out.append([(x0 + bx0, y0 + by0, x1 + bx0, y1 + by0) for x0, y0, x1, y1 in line])
        rest[by0:by1, bx0:bx1] = False
    out.extend(_find_lines_in(rest, th))
    return out


def _find_lines_in(clean: np.ndarray, th: float) -> list[list[tuple[int, int, int, int]]]:
    """Lines in one area whose writing is about `th` tall.

    Letters are joined into word pieces (small closing), pieces into lines when their letter
    bodies sit at the same height and the gap between them is less than a few letters tall.
    A column gap, a heading above or a note beside therefore start a new line by themselves.
    """
    comps = components(clean)
    if th <= 0:
        return []
    # Long, very thin horizontal strokes (dashed or faint rules, underlines, box edges) are not letters:
    # left in, they glue words from different places into one piece.
    clean = clean.copy()
    for x0, y0, x1, y1, _a in comps:
        flat = (y1 - y0) <= max(2, 0.2 * th)
        if flat and ((x1 - x0) >= 0.8 * th or (x1 - x0) >= 2.5 * (y1 - y0) + 2):  # rules, and the dashes of dashed/dotted rules
            clean[y0:y1, x0:x1] = False
    joined = close_gaps(clean, max(1, round(0.3 * th)), max(1, round(0.2 * th)))
    pieces: list[_Piece] = []
    for x0, y0, x1, y1, _area in components(joined):
        ink_box = clean[y0:y1, x0:x1]
        if not ink_box.any():
            continue
        c0, c1, ink = _core(clean, x0, y0, x1, y1)
        pieces.extend(_split_tall(clean, _Piece(int(x0), int(y0), int(x1), int(y1), c0, c1, ink), th))
    if not pieces:
        return []
    # tiny marks (dots, commas, stray specks) do not decide where lines are; they join the nearest line later
    major = [p for p in pieces if p.ink >= 0.15 * th * th or (p.y1 - p.y0) >= 0.6 * th]
    minor = [p for p in pieces if p not in major]
    major.sort(key=lambda p: p.x0)
    # Columns and separate notes (XY cut) bound the lines: inside a column the gap between words may be
    # wide (spaced-out handwriting), but a line never jumps from one column to the next.
    # only gutters running down several lines are columns; a gap between two words is not
    blocks = xy_blocks(clean, min_column_h=int(6 * th))

    def block_of(p: _Piece) -> int:
        cx, cy = (p.x0 + p.x1) / 2, (p.y0 + p.y1) / 2
        for k, (bx0, by0, bx1, by1) in enumerate(blocks):
            if bx0 <= cx <= bx1 and by0 <= cy <= by1:
                return k
        return -1

    where = [block_of(p) for p in major]
    parent = list(range(len(major)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    max_gap = 6.0 * th

    def fit(a: _Piece, b: _Piece) -> float | None:
        """How well b continues a's line to the right (lower is better), or None if it cannot."""
        if b.x0 - a.x1 > max_gap or b.x0 - a.x1 < -0.5 * min(a.x1 - a.x0, b.x1 - b.x0):
            return None
        # the bodies of the letters (middle half of the ink) must sit at about the same height;
        # a little slope is allowed, growing with the distance between the two pieces
        centre_gap = abs((a.c0 + a.c1) - (b.c0 + b.c1)) / 2
        if centre_gap > 0.5 * th + 0.16 * max(0, b.x0 - a.x1):
            return None
        return max(0, b.x0 - a.x1) + 3.0 * centre_gap

    # Each piece links only to its best continuation on the right, and is the best continuation of at
    # most one piece on its left: one wrong pairing can no longer merge two whole lines.
    best_right: dict[int, tuple[float, int]] = {}
    for i, a in enumerate(major):
        for j in range(i + 1, len(major)):
            b = major[j]
            if b.x0 - a.x1 > max_gap:
                break
            if where[i] != where[j]:
                continue
            score = fit(a, b)
            if score is not None and (i not in best_right or score < best_right[i][0]):
                best_right[i] = (score, j)
    taken: dict[int, tuple[float, int]] = {}
    for i, (score, j) in best_right.items():
        if j not in taken or score < taken[j][0]:
            taken[j] = (score, i)
    for j, (_score, i) in taken.items():
        parent[find(j)] = find(i)
    groups: dict[int, list[_Piece]] = {}
    for k, p in enumerate(major):
        groups.setdefault(find(k), []).append(p)
    lines = list(groups.values())
    for p in minor:  # attach to the line whose body is nearest above/below and that spans it horizontally
        best, dist = None, 1.2 * th
        for ln in lines:
            lx0, lx1 = min(q.x0 for q in ln) - 0.5 * th, max(q.x1 for q in ln) + 0.5 * th
            if not lx0 <= (p.x0 + p.x1) / 2 <= lx1:
                continue
            ly0, ly1 = min(q.y0 for q in ln), max(q.y1 for q in ln)
            d = max(0, ly0 - p.y1, p.y0 - ly1)
            if d < dist:
                best, dist = ln, d
        if best is not None:
            best.append(p)
    out = []
    for ln in lines:
        ln.sort(key=lambda q: q.x0)
        runs = _merge_runs([(q.x0, q.x1) for q in ln], max_gap=0)
        line_h = max(q.y1 for q in ln) - min(q.y0 for q in ln)
        word_runs = _merge_runs(runs, max_gap=word_gap_threshold(runs, int(max(th, min(line_h, 1.6 * th)))))
        words = []
        for wx0, wx1 in word_runs:
            members = [q for q in ln if q.x0 < wx1 and q.x1 > wx0]
            words.append((wx0, min(q.y0 for q in members), wx1, max(q.y1 for q in members)))
        out.append(words)
    return out
