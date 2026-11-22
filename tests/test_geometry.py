"""Spec §4 geometry and lineage: invertible frame mappings, round trips, crops from the working image."""

from __future__ import annotations

import math

import numpy as np
import pytest
from PIL import Image

from handprinter import imaging
from handprinter.geometry import Box, Homography, exif_homography, processing_homography, remap_box


@pytest.mark.parametrize("orientation", range(1, 9))
def test_exif_homography_matches_pillow_pixels(orientation):
    w, h = 7, 4
    img = Image.new("RGB", (w, h), "black")
    img.putpixel((5, 1), (255, 0, 0))
    oriented = imaging.orient(imaging.Decoded(img, orientation))
    hom, ww, wh = exif_homography(orientation, w, h)
    assert (ww, wh) == oriented.size
    # pixel centre (5.5, 1.5) must land on the red pixel's centre
    x, y = hom.apply((5.5, 1.5))
    assert oriented.getpixel((int(math.floor(x)), int(math.floor(y)))) == (255, 0, 0)
    back = hom.inverse().apply((x, y))
    assert back == pytest.approx((5.5, 1.5))


def test_homography_from_points_round_trip():
    src = [(10, 20), (410, 35), (420, 330), (5, 300)]
    dst = [(0, 0), (400, 0), (400, 300), (0, 300)]
    h = Homography.from_points(src, dst)
    for s, d in zip(src, dst):
        assert h.apply(s) == pytest.approx(d, abs=1e-6)
    for p in [(100, 100), (250.5, 77.25)]:
        assert h.inverse().apply(h.apply(p)) == pytest.approx(p, abs=1e-6)


def test_processing_rotation_then_back():
    h, w2, h2 = processing_homography({"rotation": 90}, 300, 200)
    assert (w2, h2) == (200, 300)
    for p in [(0, 0), (300, 200), (123, 45)]:
        assert h.inverse().apply(h.apply(p)) == pytest.approx(p, abs=1e-6)


def test_recognition_resize_round_trip():
    # processed box → recognition image (scaled + offset) → back to processed
    context = Box(200, 400, 1600, 120)
    scale = 1568 / 1600
    to_source = Homography.scale_offset(1 / scale, 1 / scale, context.x, context.y)
    target = Box(250, 410, 900, 90)
    in_image = to_source.inverse().map_box(target)
    back = to_source.map_box(in_image)
    assert (back.x, back.y, back.w, back.h) == pytest.approx((target.x, target.y, target.w, target.h), abs=1e-6)


def test_crop_is_cut_from_working_image_through_perspective():
    # A working image with a marker; perspective-correct it, then a crop around the
    # marker's processed position must contain the marker.
    working = Image.new("RGB", (400, 300), "white")
    for x in range(198, 203):
        for y in range(148, 153):
            working.putpixel((x, y), (0, 0, 0))
    corners = [[20, 10], [390, 25], [380, 290], [10, 280]]
    h, _, _ = processing_homography({"corners": corners}, 400, 300)
    px, py = h.apply((200.5, 150.5))
    crop = imaging.cut_crop(working, h, Box(px - 10, py - 10, 20, 20))
    arr = np.asarray(crop.convert("L"))
    assert arr.min() < 60 and arr[10, 10] < 128


def test_identity_crop_is_byte_exact():
    working = Image.effect_noise((50, 40), 60).convert("RGB")
    crop = imaging.cut_crop(working, Homography.identity(), Box(5, 6, 20, 10))
    assert crop.tobytes() == working.crop((5, 6, 25, 16)).tobytes()


def test_remap_box_through_working_frame():
    old, _, _ = processing_homography({}, 400, 300)
    new, nw, nh = processing_homography({"rotation": 180}, 400, 300)
    box = Box(10, 20, 30, 40)
    moved = remap_box(box, old.inverse(), new, nw, nh)
    assert (moved.x, moved.y, moved.w, moved.h) == pytest.approx((360, 240, 30, 40), abs=1e-6)
