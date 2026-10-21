"""Screenshots of a rect in physical pixels, and a change measure for verifying actions."""

from __future__ import annotations

from pathlib import Path

import mss
from PIL import Image, ImageChops

from overshoulder.driver.windows import Rect


def grab(rect: Rect) -> Image.Image:
    with mss.MSS() if hasattr(mss, "MSS") else mss.mss() as s:
        shot = s.grab({"left": rect.left, "top": rect.top, "width": rect.width, "height": rect.height})
        return Image.frombytes("RGB", shot.size, shot.rgb)


def save(img: Image.Image, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    return path


def window_image(hwnd: int, rect: Rect | None = None) -> Image.Image | None:
    """The window's own pixels (PrintWindow), optionally cropped to a screen rect; None if unavailable."""
    from overshoulder.recorder import print_window_bgra

    got = print_window_bgra(hwnd)
    if got is None:
        return None
    raw, outer = got
    img = Image.frombuffer("RGBA", (outer.width, outer.height), raw, "raw", "BGRA", 0, 1).convert("RGB")
    if rect is not None:
        box = (max(rect.left - outer.left, 0), max(rect.top - outer.top, 0),
               min(rect.right - outer.left, outer.width), min(rect.bottom - outer.top, outer.height))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        img = img.crop(box)
    return img


def changed_fraction(a: Image.Image, b: Image.Image, threshold: int = 24) -> float:
    """Share of pixels whose greyscale difference exceeds threshold (0..1). Size change counts as 1."""
    if a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > threshold else 0)
    return diff.histogram()[255] / (a.size[0] * a.size[1])
