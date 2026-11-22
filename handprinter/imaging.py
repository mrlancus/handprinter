"""Image decoding, content sniffing, hashing, quality signals and rendering helpers."""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter

from handprinter.config import MAX_IMAGE_PIXELS
from handprinter.geometry import Box, Homography, processing_homography

Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS

THUMB_EDGE = 480
DISPLAY_EDGE = 2000

_HEIF_BRANDS = {b"heic", b"heix", b"heim", b"heis", b"hevc", b"hevx", b"mif1", b"msf1", b"heif"}


class UnsupportedInput(ValueError):
    """The content is not something we accept, or an optional decoder is missing."""


def sniff(head: bytes) -> str | None:
    """Detect the content type from magic bytes. Returns a short kind or None."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(head) >= 12 and head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith((b"PK\x03\x04", b"PK\x05\x06")):
        return "zip"
    if head.startswith(b"%PDF-"):
        return "pdf"
    if len(head) >= 12 and head[4:8] == b"ftyp" and head[8:12] in _HEIF_BRANDS:
        return "heic"
    return None


MIME = {"jpeg": "image/jpeg", "png": "image/png", "webp": "image/webp", "heic": "image/heic", "pdf": "application/pdf"}
EXT = {"jpeg": "jpg", "png": "png", "webp": "webp", "heic": "heic", "pdf": "pdf"}


def heif_available() -> bool:
    try:
        import pillow_heif  # noqa: F401
    except ImportError:
        return False
    return True


def pdf_available() -> bool:
    try:
        import pypdfium2  # noqa: F401
    except ImportError:
        return False
    return True


@dataclass
class Decoded:
    """One decoded page: the pixels in the original frame plus its EXIF orientation."""

    image: Image.Image
    orientation: int
    page_index: int | None = None


def decode_image(data: bytes, kind: str) -> Decoded:
    if kind == "heic":
        if not heif_available():
            raise UnsupportedInput("HEIC/HEIF needs the optional decoder: pip install -e \".[heic]\"")
        import pillow_heif

        pillow_heif.register_heif_opener()
    try:
        img = Image.open(io.BytesIO(data))
        w, h = img.size
        if w * h > MAX_IMAGE_PIXELS:
            raise UnsupportedInput(f"image has {w * h:,} pixels (limit {MAX_IMAGE_PIXELS:,})")
        orientation = int(img.getexif().get(0x0112, 1) or 1)
        img.load()
    except UnsupportedInput:
        raise
    except Image.DecompressionBombError as exc:
        raise UnsupportedInput(f"image too large: {exc}") from exc
    except Exception as exc:  # Pillow raises many types for corrupt data
        raise UnsupportedInput(f"cannot decode image: {exc.__class__.__name__}") from exc
    if orientation not in range(1, 9):
        orientation = 1
    return Decoded(image=_to_rgb(img), orientation=orientation)


def render_pdf(data: bytes, max_pages: int, dpi: int = 200) -> list[Decoded]:
    if not pdf_available():
        raise UnsupportedInput("PDF pages need the optional renderer: pip install -e \".[pdf]\"")
    import pypdfium2 as pdfium

    try:
        doc = pdfium.PdfDocument(data)
    except Exception as exc:
        raise UnsupportedInput(f"cannot open PDF: {exc.__class__.__name__}") from exc
    try:
        if len(doc) > max_pages:
            raise UnsupportedInput(f"PDF has {len(doc)} pages (limit {max_pages})")
        pages = []
        for index in range(len(doc)):
            page = doc[index]
            pw, ph = page.get_size()
            scale = dpi / 72
            if pw * ph * scale * scale > MAX_IMAGE_PIXELS:
                scale = (MAX_IMAGE_PIXELS / (pw * ph)) ** 0.5
            pages.append(Decoded(image=_to_rgb(page.render(scale=scale).to_pil()), orientation=1, page_index=index))
        return pages
    finally:
        doc.close()


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode == "RGB":
        return img
    if img.mode in ("RGBA", "LA", "P"):
        rgba = img.convert("RGBA")
        canvas = Image.new("RGB", rgba.size, (255, 255, 255))
        canvas.paste(rgba, mask=rgba.split()[-1])
        return canvas
    return img.convert("RGB")


_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def orient(decoded: Decoded) -> Image.Image:
    """Working image: EXIF orientation applied (same table as Pillow's exif_transpose), nothing else."""
    op = _TRANSPOSE.get(decoded.orientation)
    return decoded.image.transpose(op) if op is not None else decoded.image


def dhash(img: Image.Image) -> str:
    small = np.asarray(img.convert("L").resize((9, 8), Image.Resampling.LANCZOS), dtype=np.int16)
    bits = (small[:, 1:] > small[:, :-1]).flatten()
    value = 0
    for bit in bits:
        value = (value << 1) | int(bit)
    return f"{value:016x}"


def hamming(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def quality_flags(img: Image.Image) -> list[str]:
    """Fallible photographic signals. They prompt a human look; they prove nothing."""
    gray = np.asarray(_scaled(img.convert("L"), 1000), dtype=np.float32)
    flags = []
    lap = gray[1:-1, 1:-1] * 4 - gray[:-2, 1:-1] - gray[2:, 1:-1] - gray[1:-1, :-2] - gray[1:-1, 2:]
    if lap.var() < 40:
        flags.append("possible_blur")
    if (gray >= 250).mean() > 0.04 and (gray >= 250).mean() < 0.9:
        flags.append("possible_glare")
    h, w = gray.shape
    blocks = [gray[i * h // 4:(i + 1) * h // 4, j * w // 4:(j + 1) * w // 4] for i in range(4) for j in range(4)]
    paper = [float(np.percentile(b, 90)) for b in blocks]
    if max(paper) - min(paper) > 70:
        flags.append("possible_shadow")
    dark = gray < 110
    border = max(2, min(h, w) // 100)
    edges = [dark[:border].mean(), dark[-border:].mean(), dark[:, :border].mean(), dark[:, -border:].mean()]
    if max(edges) > 0.02 and dark.mean() < 0.3:
        flags.append("possible_clipped_writing")
    return flags


def _scaled(img: Image.Image, edge: int) -> Image.Image:
    scale = edge / max(img.size)
    if scale >= 1:
        return img
    return img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.Resampling.LANCZOS)


def render_processed(working: Image.Image, processing: dict) -> tuple[Image.Image, Homography]:
    """Apply rotation/perspective (geometry only). Returns the image and the working→processed map."""
    h, out_w, out_h = processing_homography(processing, working.width, working.height)
    if h.is_close(Homography.identity()):
        return working, h
    coeffs = h.inverse().pil_perspective_coeffs()
    out = working.transform((out_w, out_h), Image.Transform.PERSPECTIVE, coeffs, Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))
    return out, h


def normalise_lighting(img: Image.Image) -> Image.Image:
    """Divide by a blurred paper estimate. Display/detection aid only; never used for training crops."""
    gray = img.convert("L")
    radius = max(8, max(gray.size) // 40)
    background = gray.filter(ImageFilter.MaxFilter(5)).filter(ImageFilter.GaussianBlur(radius))
    g = np.asarray(gray, dtype=np.float32)
    bg = np.maximum(np.asarray(background, dtype=np.float32), 1.0)
    flat = np.clip(g / bg * 245.0, 0, 255).astype(np.uint8)
    return Image.fromarray(flat, "L").convert("RGB")


def save_preview(img: Image.Image, path: Path, edge: int) -> None:
    _scaled(img, edge).save(path, "JPEG", quality=86, optimize=True)


def cut_crop(working: Image.Image, working_to_processed: Homography, box: Box) -> Image.Image:
    """Cut a box given in processed coordinates from the full-resolution working image.

    Pure geometry (no lighting changes): output pixel (u, v) samples working at
    H⁻¹ · (u + box.x, v + box.y).
    """
    w, h = max(1, round(box.w)), max(1, round(box.h))
    out_to_processed = Homography.scale_offset(1, 1, box.x, box.y)
    out_to_working = out_to_processed.compose(working_to_processed.inverse())
    if out_to_working.is_close(Homography.scale_offset(1, 1, box.x, box.y)) and float(box.x).is_integer() and float(box.y).is_integer():
        return working.crop((int(box.x), int(box.y), int(box.x) + w, int(box.y) + h))
    return working.transform((w, h), Image.Transform.PERSPECTIVE, out_to_working.pil_perspective_coeffs(), Image.Resampling.BICUBIC, fillcolor=(255, 255, 255))


def png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=False, compress_level=6)
    return buf.getvalue()
