"""Coordinate frames and invertible transforms, shared by every module.

Frames (see docs/architecture.md): original → working (EXIF) → processed (rotation +
perspective) → recognition (resize/offset) / crop. All maps are 3×3 homographies.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

import numpy as np

Point = tuple[float, float]


@dataclass(frozen=True)
class Box:
    """Axis-aligned box in a named frame with that frame's dimensions."""

    x: float
    y: float
    w: float
    h: float

    def corners(self) -> list[Point]:
        return [(self.x, self.y), (self.x + self.w, self.y), (self.x + self.w, self.y + self.h), (self.x, self.y + self.h)]

    def clamp(self, width: float, height: float) -> "Box":
        x0, y0 = max(0.0, self.x), max(0.0, self.y)
        x1, y1 = min(width, self.x + self.w), min(height, self.y + self.h)
        return Box(x0, y0, max(0.0, x1 - x0), max(0.0, y1 - y0))

    def pad(self, px: float) -> "Box":
        return Box(self.x - px, self.y - px, self.w + 2 * px, self.h + 2 * px)

    def union(self, other: "Box") -> "Box":
        x0, y0 = min(self.x, other.x), min(self.y, other.y)
        x1, y1 = max(self.x + self.w, other.x + other.w), max(self.y + self.h, other.y + other.h)
        return Box(x0, y0, x1 - x0, y1 - y0)

    def intersection_area(self, other: "Box") -> float:
        w = min(self.x + self.w, other.x + other.w) - max(self.x, other.x)
        h = min(self.y + self.h, other.y + other.h) - max(self.y, other.y)
        return max(0.0, w) * max(0.0, h)

    @staticmethod
    def bounding(points: Iterable[Point]) -> "Box":
        pts = list(points)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return Box(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


class Homography:
    """Projective map `dst = H · src` on homogeneous points."""

    __slots__ = ("m",)

    def __init__(self, matrix: Sequence[Sequence[float]] | np.ndarray):
        m = np.asarray(matrix, dtype=float)
        if m.shape != (3, 3):
            raise ValueError("homography must be 3×3")
        if abs(m[2, 2]) > 1e-12:
            m = m / m[2, 2]
        self.m = m

    @classmethod
    def identity(cls) -> "Homography":
        return cls(np.eye(3))

    @classmethod
    def scale_offset(cls, sx: float, sy: float, tx: float = 0.0, ty: float = 0.0) -> "Homography":
        """`dst = src * s + t`."""
        return cls([[sx, 0, tx], [0, sy, ty], [0, 0, 1]])

    @classmethod
    def rotation(cls, degrees: float, src_w: float, src_h: float) -> tuple["Homography", int, int]:
        """Rotate counter-clockwise about the centre, expanding the canvas; returns (H, new_w, new_h)."""
        a = math.radians(degrees)
        c, s = math.cos(a), math.sin(a)
        rot = np.array([[c, s, 0], [-s, c, 0], [0, 0, 1]], dtype=float)
        to_origin = np.array([[1, 0, -src_w / 2], [0, 1, -src_h / 2], [0, 0, 1]], dtype=float)
        corners = Homography(rot @ to_origin).apply_many([(0, 0), (src_w, 0), (src_w, src_h), (0, src_h)])
        bb = Box.bounding(corners)
        new_w, new_h = int(round(bb.w)), int(round(bb.h))
        back = np.array([[1, 0, new_w / 2], [0, 1, new_h / 2], [0, 0, 1]], dtype=float)
        return cls(back @ rot @ to_origin), new_w, new_h

    @classmethod
    def from_points(cls, src: Sequence[Point], dst: Sequence[Point]) -> "Homography":
        """Exact homography mapping four source points onto four destination points."""
        if len(src) != 4 or len(dst) != 4:
            raise ValueError("need exactly four point pairs")
        rows, rhs = [], []
        for (x, y), (u, v) in zip(src, dst):
            rows.append([x, y, 1, 0, 0, 0, -u * x, -u * y])
            rows.append([0, 0, 0, x, y, 1, -v * x, -v * y])
            rhs += [u, v]
        a = np.array(rows, dtype=float)
        if abs(np.linalg.det(a)) < 1e-9:
            raise ValueError("degenerate corner configuration")
        h = np.linalg.solve(a, np.array(rhs, dtype=float))
        return cls(np.append(h, 1.0).reshape(3, 3))

    def compose(self, then: "Homography") -> "Homography":
        """Apply `self` first, then `then`."""
        return Homography(then.m @ self.m)

    def inverse(self) -> "Homography":
        return Homography(np.linalg.inv(self.m))

    def apply(self, p: Point) -> Point:
        x, y, w = self.m @ np.array([p[0], p[1], 1.0])
        return (float(x / w), float(y / w))

    def apply_many(self, pts: Iterable[Point]) -> list[Point]:
        return [self.apply(p) for p in pts]

    def map_box(self, box: Box) -> Box:
        """Bounding box of the mapped corners (exact for scale/offset maps)."""
        return Box.bounding(self.apply_many(box.corners()))

    def pil_perspective_coeffs(self) -> tuple[float, ...]:
        """Coefficients for `PIL.Image.transform(..., Image.PERSPECTIVE)`, which maps output → input.

        `self` must map output (destination) pixels to input (source) pixels.
        """
        m = self.m / self.m[2, 2]
        return (m[0, 0], m[0, 1], m[0, 2], m[1, 0], m[1, 1], m[1, 2], m[2, 0], m[2, 1])

    def to_list(self) -> list[list[float]]:
        return [[float(v) for v in row] for row in self.m]

    def is_close(self, other: "Homography", tol: float = 1e-6) -> bool:
        return bool(np.allclose(self.m, other.m, atol=tol))


# EXIF orientation tags 1–8 as maps from original pixel coords to oriented coords.
def exif_homography(orientation: int, w: int, h: int) -> tuple[Homography, int, int]:
    """Map original (pre-EXIF) coordinates to working coordinates; returns (H, working_w, working_h)."""
    table: dict[int, tuple[list[list[float]], bool]] = {
        1: ([[1, 0, 0], [0, 1, 0], [0, 0, 1]], False),
        2: ([[-1, 0, w], [0, 1, 0], [0, 0, 1]], False),
        3: ([[-1, 0, w], [0, -1, h], [0, 0, 1]], False),
        4: ([[1, 0, 0], [0, -1, h], [0, 0, 1]], False),
        5: ([[0, 1, 0], [1, 0, 0], [0, 0, 1]], True),
        6: ([[0, -1, h], [1, 0, 0], [0, 0, 1]], True),
        7: ([[0, -1, h], [-1, 0, w], [0, 0, 1]], True),
        8: ([[0, 1, 0], [-1, 0, w], [0, 0, 1]], True),
    }
    matrix, swaps = table.get(orientation, table[1])
    return Homography(matrix), (h if swaps else w), (w if swaps else h)


def processing_homography(processing: dict, working_w: int, working_h: int) -> tuple[Homography, int, int]:
    """Working → processed map for a processing spec; returns (H, processed_w, processed_h).

    `processing` keys: `corners` (four working-frame points TL, TR, BR, BL, optional) and
    `rotation` (degrees counter-clockwise, applied after perspective, optional).
    """
    h = Homography.identity()
    out_w, out_h = working_w, working_h
    corners = processing.get("corners")
    if corners:
        tl, tr, br, bl = [tuple(map(float, c)) for c in corners]
        out_w = int(round(max(_dist(tl, tr), _dist(bl, br))))
        out_h = int(round(max(_dist(tl, bl), _dist(tr, br))))
        if out_w < 8 or out_h < 8:
            raise ValueError("page corners enclose too small an area")
        h = Homography.from_points([tl, tr, br, bl], [(0, 0), (out_w, 0), (out_w, out_h), (0, out_h)])
    rotation = float(processing.get("rotation") or 0.0) % 360
    if rotation:
        rot, out_w, out_h = Homography.rotation(rotation, out_w, out_h)
        h = h.compose(rot)
    return h, out_w, out_h


def _dist(a: Point, b: Point) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def remap_box(box: Box, old_to_working: Homography, working_to_new: Homography, new_w: float, new_h: float) -> Box:
    """Move a box from one processed revision to another through the stable working frame."""
    mapped = Box.bounding(working_to_new.apply_many(old_to_working.apply_many(box.corners())))
    return mapped.clamp(new_w, new_h)


def fit_within(w: int, h: int, max_long_edge: int, max_pixels: int) -> float:
    """Largest scale ≤ 1 so that the image fits both limits."""
    scale = min(1.0, max_long_edge / max(w, h), math.sqrt(max_pixels / max(1, w * h)))
    return scale
