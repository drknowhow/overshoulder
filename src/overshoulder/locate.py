"""Controls found by how they look, for apps that expose no UI Automation inside their windows.

Text refs (t1, t2...) are words read off the scope's own pixels by OCR (driver.ocr). A ref keeps where
the text was, relative to the scope's top-left corner, and the pixels it covered then. Before an action
those pixels are compared with the scope's pixels now: if the text there changed or the window's
content moved, the ref is stale and has to be found again, the way an element ref goes stale when its
element is gone. A text ref takes mouse actions only: click, double_click, right_click and drag.

Vision refs (v1, v2...) are boxes a vision model drew around a described control (vision.py). They are
kept and checked the same way; their name is the description the agent gave. A vision ref is confirmed
when something on screen agrees with the description: the text read inside its box, or the name of the
control UI Automation reports at its centre. An action on an unconfirmed one asks the person, showing
them the box (picture()).
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import dataclass

from PIL import Image, ImageDraw

from overshoulder.driver import capture
from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect
from overshoulder.gateway.errors import GatewayError
from overshoulder.gateway.limits import black_out, hidden_rects

MOUSE_ACTIONS = frozenset({"click", "double_click", "right_click", "drag"})
# A ref is stale once more than this share of its box's pixels has changed.
STALE_SHARE = 0.05
# Pixels around a box that are kept with it, so a change right at the edge of the text counts.
PAD = 2
# scope_pixels: two captures this close count as the same picture (a caret blinking is about 0.0001).
SETTLED_SHARE = 0.001
SETTLE_TRIES = 4
SETTLE_S = 0.05
_REF = re.compile(r"[tv]\d+")
# dhash: two pictures whose hashes differ in at most this many of 64 bits count as the same picture.
DHASH_NEAR = 6


def dhash(img: Image.Image) -> str:
    """A 64-bit difference hash of a picture: in a 9x8 grey copy, whether each pixel is brighter than the
    one to its right. Close pictures give close hashes, and nothing the picture shows can be read back
    from one, so a profile can keep it."""
    g = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (g[row * 9 + col] > g[row * 9 + col + 1])
    return f"{bits:016x}"


def same_picture(a: str, b: str) -> bool:
    try:
        return bin(int(a, 16) ^ int(b, 16)).count("1") <= DHASH_NEAR
    except ValueError:
        return False


def is_found_ref(ref: str | None) -> bool:
    return bool(ref) and bool(_REF.fullmatch(ref))


@dataclass
class Found:
    ref: str
    text: str  # the words read, or for a vision ref the description
    rel: Rect  # the box, relative to the scope's top-left corner when it was read
    hwnd: int | None  # the window the box is in
    pixels: Image.Image  # the padded box's pixels when it was read
    source: str = "ocr"
    confirmed_by: str = ""  # a vision ref: what on screen agreed with the description, or ""

    def describe(self, origin: Rect) -> dict:
        r = self.rel
        out = {"ref": self.ref, "name": self.text, "source": self.source,
               "text" if self.source == "ocr" else "description": self.text,
               "rect": [origin.left + r.left, origin.top + r.top, origin.left + r.right, origin.top + r.bottom]}
        if self.source == "vision":
            out["confirmed"] = bool(self.confirmed_by)
            out["confirmed_by"] = self.confirmed_by or "nothing on screen; an action on it asks the person"
        return out


class FoundCache:
    """Ref -> Found. One per lease; cleared when the lease changes, like uia.ElementCache."""

    def __init__(self, limit: int = 2000):
        self._lock = threading.Lock()
        self._by_ref: dict[str, Found] = {}
        self._ids = itertools.count(1)
        self.limit = limit

    def clear(self) -> None:
        with self._lock:
            self._by_ref.clear()

    def add(self, text: str, rel: Rect, hwnd: int | None, img: Image.Image, source: str = "ocr",
            confirmed_by: str = "") -> Found:
        with self._lock:
            if len(self._by_ref) >= self.limit:
                self._by_ref.pop(next(iter(self._by_ref)))
            prefix = "v" if source == "vision" else "t"
            f = Found(f"{prefix}{next(self._ids)}", text, rel, hwnd, padded(img, rel), source, confirmed_by)
            self._by_ref[f.ref] = f
            return f

    def get(self, ref: str) -> Found | None:
        with self._lock:
            return self._by_ref.get(ref)


def padded(img: Image.Image, rel: Rect) -> Image.Image:
    return img.crop((max(rel.left - PAD, 0), max(rel.top - PAD, 0),
                     min(rel.right + PAD, img.width), min(rel.bottom + PAD, img.height)))


def scope_pixels(scope, tries: int = SETTLE_TRIES) -> tuple[Image.Image, Rect]:
    """What the scope shows and where its top-left corner is on screen. A window scope gives the window's
    own pixels; a screen scope gives the screen with the windows of programs the session may not see
    painted black, so their text is never read.

    A window that paints straight to the screen can be caught halfway through a repaint (measured: one
    capture in six of a WinForms window showed a button's fill without its border or label, and OCR then
    missed the label). So captures are taken until two in a row agree, up to `tries`; content that never
    settles, such as a video, gives the last one.
    """
    bounds = scope.bounds()
    img = _capture(scope, bounds)
    for _ in range(tries - 1):
        time.sleep(SETTLE_S)
        again = _capture(scope, bounds)
        if again.size == img.size and capture.changed_fraction(again, img) <= SETTLED_SHARE:
            return again, bounds
        img = again
    return img, bounds


def _capture(scope, bounds: Rect) -> Image.Image:
    binding = getattr(scope, "binding", None)
    if binding is not None:
        img = capture.window_image(binding.hwnd, bounds)
        if img is None:
            raise GatewayError("could not capture the window's own pixels; try again")
        return img
    return black_out(capture.grab(bounds), bounds, hidden_rects(scope))


def window_of(scope, origin: Rect, rel: Rect) -> int | None:
    binding = getattr(scope, "binding", None)
    if binding is not None:
        return binding.hwnd
    cx, cy = origin.left + (rel.left + rel.right) // 2, origin.top + (rel.top + rel.bottom) // 2
    return w.window_at(cx, cy) or None


# label_at reads this much of the scope around a point (screen pixels), and takes the nearest phrase
# that lies within LABEL_REACH line heights of the point across and LABEL_REACH_DOWN up or down: a
# painted button's label sits in its middle with a margin around it.
LABEL_REGION = (640, 240)
LABEL_REACH = 4.0
LABEL_REACH_DOWN = 1.5


def label_at(scope, x: int, y: int) -> str:
    """The text a click at (x, y) would land on or beside, read with OCR, or "" if there is none."""
    from overshoulder.driver import ocr

    if not ocr.available():
        return ""
    img, origin = scope_pixels(scope)
    px, py = x - origin.left, y - origin.top
    half_w, half_h = LABEL_REGION[0] // 2, LABEL_REGION[1] // 2
    left, top = max(px - half_w, 0), max(py - half_h, 0)
    right, bottom = min(px + half_w, img.width), min(py + half_h, img.height)
    if right <= left or bottom <= top:
        return ""
    return nearest_label(ocr.merged(ocr.scan(img.crop((left, top, right, bottom)))), px - left, py - top)


def nearest_label(phrases, x: int, y: int) -> str:
    """The phrase nearest (x, y), measured in its own line heights, if within reach."""
    best, best_d = "", 1.0
    for ph in phrases:
        b, h = ph.box, max(ph.box.height, 1)
        dx = max(b.left - x, 0, x - b.right) / (LABEL_REACH * h)
        dy = max(b.top - y, 0, y - b.bottom) / (LABEL_REACH_DOWN * h)
        if max(dx, dy) <= best_d:
            best, best_d = ph.text, max(dx, dy)
    return best


# picture(): at most this much of the scope around the box, at the scope's own scale, so a button on a
# big screen stays legible in the approval box and on a phone.
PICTURE_SIZE = (900, 600)
PICTURE_COLOR = (37, 99, 235)  # brand.AGENT_BLUE: the agent's highlights


def picture(scope, rect: Rect) -> bytes:
    """A PNG of the scope around `rect` (screen pixels) with the rect outlined and a cross where a click
    lands: what an approval for an action on a vision ref shows the person."""
    import io

    img, origin = scope_pixels(scope)
    rel = Rect(rect.left - origin.left, rect.top - origin.top, rect.right - origin.left, rect.bottom - origin.top)
    cx, cy = rel.center()
    w_, h_ = min(PICTURE_SIZE[0], img.width), min(PICTURE_SIZE[1], img.height)
    left = max(0, min(cx - w_ // 2, img.width - w_))
    top = max(0, min(cy - h_ // 2, img.height - h_))
    img = img.convert("RGB").crop((left, top, left + w_, top + h_))
    d = ImageDraw.Draw(img)
    box = (rel.left - left, rel.top - top, rel.right - left - 1, rel.bottom - top - 1)
    d.rectangle(box, outline=(255, 255, 255), width=5)
    d.rectangle(box, outline=PICTURE_COLOR, width=3)
    x, y = cx - left, cy - top
    for a, b in (((x - 8, y), (x + 8, y)), ((x, y - 8), (x, y + 8))):
        d.line((a, b), fill=(255, 255, 255), width=4)
        d.line((a, b), fill=PICTURE_COLOR, width=2)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def still_there(f: Found, img: Image.Image) -> bool:
    now = padded(img, f.rel)
    return now.size == f.pixels.size and capture.changed_fraction(now, f.pixels) <= STALE_SHARE
