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
    return (gray < local - 18) & (gray < 215)


def detect_layout(img: Image.Image) -> Detection:
    scale = min(1.0, WORK_EDGE / max(img.size))
    small = img.convert("L").resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)
    gray = np.asarray(small, dtype=np.float32)
    mask = ink_mask(gray)
    h, w = mask.shape

    row_fill = mask.mean(axis=1)
    ruling_rows = row_fill > 0.35
    rulings = [Box(0, y0, w, y1 - y0) for y0, y1 in _runs(ruling_rows, min_len=1)]
    clean = mask.copy()
    clean[ruling_rows] = False

    profile = _smooth(clean.sum(axis=1).astype(float), max(1, h // 300))
    threshold = max(1.0, 0.04 * float(profile.max(initial=0)))
    bands = _merge_runs(_runs(profile > threshold, min_len=max(3, h // 200)), max_gap=max(2, h // 250))

    lines: list[DetectedLine] = []
    for y0, y1 in bands:
        band = clean[y0:y1]
        cols = band.any(axis=0)
        if cols.sum() < 3:
            continue
        line_h = y1 - y0
        ink_runs = _runs(cols, min_len=1)
        segments = _merge_runs(ink_runs, max_gap=word_gap_threshold(ink_runs, line_h))
        words = []
        for x0, x1 in segments:
            ys_ink = np.nonzero(band[:, x0:x1].any(axis=1))[0]
            if len(ys_ink) == 0 or (x1 - x0) * len(ys_ink) < 12:
                continue
            words.append(Box(x0, y0 + ys_ink[0], x1 - x0, ys_ink[-1] - ys_ink[0] + 1))
        if not words:
            continue
        line_box = words[0]
        for wb in words[1:]:
            line_box = line_box.union(wb)
        overlaps = any(line_box.intersection_area(r.pad(2)) > 0 for r in rulings)
        lines.append(DetectedLine(line_box, words, overlaps))

    inv = 1 / scale
    pad = 4 * inv

    def up(b: Box) -> Box:
        return Box(b.x * inv - pad, b.y * inv - pad, b.w * inv + 2 * pad, b.h * inv + 2 * pad).clamp(img.width, img.height)

    return Detection(
        lines=[DetectedLine(up(line.box), [up(wb) for wb in line.words], line.overlaps_ruling) for line in lines],
        rulings=[Box(0, r.y * inv, img.width, max(1.0, r.h * inv)) for r in rulings],
    )


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
