"""Synthetic, non-personal fixture pages. Rendered from a system font: NOT real handwriting.

They exercise the pipeline (import, detection, crops, export); they say nothing about
recognition or generation quality on real handwriting.
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FIXTURE_LINES = ["the quick brown fox", "jumps over the lazy dog", "becuase it was late", "Meet me at 4pm"]


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size)


def page(lines: list[str] | None = None, size: tuple[int, int] = (1200, 900), ruling: bool = True, seed_shift: int = 0) -> Image.Image:
    lines = FIXTURE_LINES if lines is None else lines
    img = Image.new("RGB", size, (246, 243, 236))
    draw = ImageDraw.Draw(img)
    font = _font(44)
    top = 120
    for i, text in enumerate(lines):
        y = top + i * 150
        if ruling:
            draw.line([(40, y + 62), (size[0] - 40, y + 62)], fill=(170, 190, 215), width=2)
        draw.text((90 + seed_shift, y), text, fill=(35, 38, 60), font=font)
    return img


def encode(img: Image.Image, fmt: str = "PNG", exif_orientation: int | None = None) -> bytes:
    buf = io.BytesIO()
    if exif_orientation:
        exif = Image.Exif()
        exif[0x0112] = exif_orientation
        img.save(buf, fmt, exif=exif.tobytes())
    else:
        img.save(buf, fmt)
    return buf.getvalue()


def zip_bytes(entries: dict[str, bytes], symlinks: tuple[str, ...] = ()) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
        for name in symlinks:
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o120777 << 16)
            zf.writestr(info, "/etc/passwd")
    return buf.getvalue()


def write_demo_set(folder: Path) -> list[Path]:
    """A small labelled demo set for manual trials and screenshots."""
    folder.mkdir(parents=True, exist_ok=True)
    paths = []
    sets = [FIXTURE_LINES, ["notes for monday", "read chapter three", "ask about the essay"], ["shopping list", "eggs, milk, bread", "call Sam back"]]
    for i, lines in enumerate(sets):
        p = folder / f"fixture-page-{i + 1}.png"
        p.write_bytes(encode(page(lines, seed_shift=i * 7)))
        paths.append(p)
    return paths
