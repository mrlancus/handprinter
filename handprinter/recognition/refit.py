"""Re-cut a line's word boxes so they match the recognised text.

Detection guesses words from gaps alone, so it splits wide words and joins close ones. Once the
recogniser says a line holds N words, the line's ink can be cut at its N-1 widest gaps instead.
This only ever replaces untouched machine word boxes, and the result is still a proposal.
"""

from __future__ import annotations

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
    clear: bool  # every cut is clearly wider than every gap kept inside a word


def segment_by_count(mask: np.ndarray, n: int) -> Segmentation | None:
    """Cut a line's ink mask into exactly `n` words at its `n - 1` widest gaps.

    Returns None when the ink has fewer separate pieces than words (words written touching).
    """
    if n < 1 or mask.size == 0:
        return None
    h = mask.shape[0]
    min_ink = max(3, int(0.002 * h * h))
    runs = [(a, b) for a, b in detect._runs(mask.any(axis=0), min_len=1) if int(mask[:, a:b].sum()) >= min_ink]
    if len(runs) < n:
        return None
    gaps = sorted(((runs[i + 1][0] - runs[i][1], i) for i in range(len(runs) - 1)), reverse=True)
    chosen, kept = gaps[: n - 1], gaps[n - 1:]
    clear = not chosen or not kept or min(g for g, _ in chosen) >= CLEAR_RATIO * max(1, kept[0][0])
    cut_after = sorted(i for _, i in chosen)
    boxes, start = [], 0
    for end in cut_after + [len(runs) - 1]:
        x0, x1 = runs[start][0], runs[end][1]
        rows = np.flatnonzero(mask[:, x0:x1].any(axis=1))
        boxes.append((int(x0), int(rows[0]), int(x1), int(rows[-1]) + 1))
        start = end + 1
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
            mask[mask.mean(axis=1) > 0.35] = False  # printed ruling crossing the line
            for b in omitted:
                ox0, oy0 = max(0, int(b.x) - x0), max(0, int(b.y) - y0)
                ox1, oy1 = int(np.ceil(b.x + b.w)) - x0, int(np.ceil(b.y + b.h)) - y0
                if ox1 > 0 and oy1 > 0:
                    mask[oy0:oy1, ox0:ox1] = False
        return mask, x0, y0


def refit_words(ink: LineInk, source: dict[str, Any], line_box: Box, n_words: int, pad: float = 4.0) -> tuple[list[Box], bool] | None:
    """Word boxes (processed pixels) for a line known to hold `n_words` words, and whether every cut was clear."""
    mask, ox, oy = ink.mask(source, line_box)
    seg = segment_by_count(mask, n_words)
    if seg is None:
        return None
    boxes = [Box(ox + x0 - pad, oy + y0 - pad, x1 - x0 + 2 * pad, y1 - y0 + 2 * pad).clamp(source["processed_w"], source["processed_h"])
             for x0, y0, x1, y1 in seg.boxes]
    return boxes, seg.clear
