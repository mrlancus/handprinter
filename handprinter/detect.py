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

    row_fill = mask.mean(axis=1)
    ruling_rows = row_fill > 0.35
    rulings = [Box(0, y0, w, y1 - y0) for y0, y1 in _runs(ruling_rows, min_len=1)]
    clean = mask.copy()
    clean[ruling_rows] = False
    # Margin lines, table borders and other long vertical rules: ink in most of a column's height.
    vertical = vertical_rule_columns(clean)
    clean[:, vertical] = False
    vertical_rulings = [Box(x0, 0, x1 - x0, h) for x0, x1 in _runs(vertical, min_len=1)]

    # Columns, headings and separate notes first (recursive XY cut), then lines inside each block,
    # so a line never runs across two columns or picks up a neighbouring note.
    blocks = xy_blocks(clean)
    per_block = []
    for bx0, by0, bx1, by1 in blocks:
        sub = clean[by0:by1, bx0:bx1]
        bh = by1 - by0
        profile = _smooth(sub.sum(axis=1).astype(float), max(1, h // 400))
        threshold = max(1.0, 0.06 * float(profile.max(initial=0)))
        bands = _merge_runs(_runs(profile > threshold, min_len=max(2, h // 300)), max_gap=max(1, h // 400))
        per_block.append((bx0, by0, bx1, by1, profile, [(by0 + a, by0 + b) for a, b in bands if b - a >= 2], bh))
    all_heights = [b - a for *_, bands, _bh in per_block for a, b in bands]
    typical = float(np.median(all_heights)) if len(all_heights) >= 3 else None

    lines: list[DetectedLine] = []
    for bx0, by0, bx1, by1, profile, bands, _bh in per_block:
        shifted = [(a - by0, b - by0) for a, b in bands]
        split, notes = split_touching_bands(shifted, profile, typical)
        bands = [(by0 + a, by0 + b) for a, b in split]
        band_notes = {(by0 + a, by0 + b): v for (a, b), v in notes.items()}
        for index, (y0, y1) in enumerate(bands):
            # Tails and loops (g, y, f, capitals) are often too thin to count towards the band itself:
            # words may grow into the space halfway to the neighbouring lines to keep them whole.
            lo = by0 if index == 0 else (bands[index - 1][1] + y0) // 2
            hi = by1 if index == len(bands) - 1 else (y1 + bands[index + 1][0]) // 2
            band = clean[y0:y1, bx0:bx1]
            cols = band.any(axis=0)
            if cols.sum() < 3:
                continue
            line_h = y1 - y0
            ink_runs = _runs(cols, min_len=1)
            segments = _merge_runs(ink_runs, max_gap=word_gap_threshold(ink_runs, line_h))
            words = []
            for sx0, sx1 in segments:
                ys_ink = np.nonzero(band[:, sx0:sx1].any(axis=1))[0]
                if len(ys_ink) == 0 or (sx1 - sx0) * len(ys_ink) < 12:
                    continue
                x0, x1 = bx0 + sx0, bx0 + sx1
                top, bottom = _grow_vertically(clean, ruling_rows, x0, x1, y0 + int(ys_ink[0]), y0 + int(ys_ink[-1]) + 1, lo, hi)
                words.append(Box(x0, top, x1 - x0, bottom - top))
            if not words:
                continue
            line_box = words[0]
            for wb in words[1:]:
                line_box = line_box.union(wb)
            overlaps = any(line_box.intersection_area(r.pad(2)) > 0 for r in rulings)
            edge = 2  # px at work scale: writing that runs to the photo edge is probably cut off
            at_edge = frozenset(i for i, wb in enumerate(words) if wb.x <= edge or wb.x + wb.w >= w - edge or wb.y <= edge or wb.y + wb.h >= h - edge)
            note = band_notes.get((y0, y1), "")
            lines.append(DetectedLine(line_box, words, overlaps, note == "unsplit", note == "split", at_edge))
    lines.sort(key=lambda line: (line.box.y, line.box.x))

    inv = 1 / scale
    pad = 4 * inv

    def up(b: Box) -> Box:
        return Box(b.x * inv - pad, b.y * inv - pad, b.w * inv + 2 * pad, b.h * inv + 2 * pad).clamp(img.width, img.height)

    return Detection(
        lines=[DetectedLine(up(line.box), [up(wb) for wb in line.words], line.overlaps_ruling, line.may_hold_several,
                            line.touches_neighbour, line.words_at_edge) for line in lines],
        rulings=[Box(0, r.y * inv, img.width, max(1.0, r.h * inv)) for r in rulings]
        + [Box(r.x * inv, 0, max(1.0, r.w * inv), img.height) for r in vertical_rulings],
    )


def split_touching_bands(
    bands: list[tuple[int, int]], profile: np.ndarray, typical: float | None = None,
) -> tuple[list[tuple[int, int]], dict[tuple[int, int], str]]:
    """Split bands that are much taller than the page's typical line: lines whose tails touch merge into one band.

    Each tall band is cut into round(height / typical) lines at the emptiest row near each expected
    boundary. Returns the new bands and notes: "split" for parts of a cut band, "unsplit" for a tall
    band with no clear place to cut (so a person checks it).
    """
    notes: dict[tuple[int, int], str] = {}
    if typical is None:
        if len(bands) < 3:
            return bands, notes
        typical = float(np.median([b - a for a, b in bands]))
    out: list[tuple[int, int]] = []
    for a, b in bands:
        height = b - a
        k = round(height / typical) if typical > 0 else 1
        if height < 1.6 * typical or k < 2:
            out.append((a, b))
            continue
        inside = profile[a:b]
        busy = float(np.median(inside)) or 1.0
        cuts, clean_cuts = [], True
        for j in range(1, k):
            centre = a + round(j * height / k)
            lo, hi = max(a + 1, centre - round(0.35 * typical)), min(b - 1, centre + round(0.35 * typical))
            if hi <= lo:
                clean_cuts = False
                break
            y = lo + int(np.argmin(profile[lo:hi]))
            clean_cuts = clean_cuts and profile[y] < 0.35 * busy
            cuts.append(y)
        if not cuts or not clean_cuts:
            out.append((a, b))
            notes[(a, b)] = "unsplit"
            continue
        edges = [a, *cuts, b]
        for top, bottom in zip(edges, edges[1:]):
            out.append((top, bottom))
            notes[(top, bottom)] = "split"
    return out, notes


def xy_blocks(mask: np.ndarray, min_gutter: int | None = None, min_gap: int | None = None) -> list[tuple[int, int, int, int]]:
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
            if gutters and (y1 - y0) >= 3 * min_gap:
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


def _grow_vertically(clean: np.ndarray, ruling_rows: np.ndarray, x0: int, x1: int, top: int, bottom: int, lo: int, hi: int) -> tuple[int, int]:
    """Extend a word's rows while its own columns still hold ink, up to the limits [lo, hi).

    Printed ruling rows were blanked from `clean`; a stroke crossing one continues if there is ink
    on the far side of the ruling.
    """
    def inked(y: int, step: int) -> bool:
        if clean[y, x0:x1].any():
            return True
        if not ruling_rows[y]:
            return False
        beyond = y
        while lo <= beyond < hi and ruling_rows[beyond]:
            beyond += step
        return lo <= beyond < hi and bool(clean[beyond, x0:x1].any())

    while top > lo and inked(top - 1, -1):
        top -= 1
    while bottom < hi and inked(bottom, 1):
        bottom += 1
    return top, bottom


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
            merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged
