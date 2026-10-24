"""Controls found by how they look, for apps that expose no UI Automation inside their windows.

Text refs (t1, t2...) are words read off the scope's own pixels by OCR (driver.ocr). A ref keeps where
the text was, relative to the scope's top-left corner, and the pixels it covered then. Before an action
those pixels are compared with the scope's pixels now: if the text there changed or the window's
content moved, the ref is stale and has to be found again, the way an element ref goes stale when its
element is gone. A text ref takes mouse actions only: click, double_click, right_click and drag.
"""

from __future__ import annotations

import itertools
import re
import threading
import time
from dataclasses import dataclass

from PIL import Image

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
_REF = re.compile(r"t\d+")


def is_found_ref(ref: str | None) -> bool:
    return bool(ref) and bool(_REF.fullmatch(ref))


@dataclass
class Found:
    ref: str
    text: str
    rel: Rect  # the box, relative to the scope's top-left corner when it was read
    hwnd: int | None  # the window the box is in
    pixels: Image.Image  # the padded box's pixels when it was read
    source: str = "ocr"

    def describe(self, origin: Rect) -> dict:
        r = self.rel
        return {"ref": self.ref, "name": self.text, "text": self.text, "source": self.source,
                "rect": [origin.left + r.left, origin.top + r.top, origin.left + r.right, origin.top + r.bottom]}


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

    def add(self, text: str, rel: Rect, hwnd: int | None, img: Image.Image, source: str = "ocr") -> Found:
        with self._lock:
            if len(self._by_ref) >= self.limit:
                self._by_ref.pop(next(iter(self._by_ref)))
            f = Found(f"t{next(self._ids)}", text, rel, hwnd, padded(img, rel), source)
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


def still_there(f: Found, img: Image.Image) -> bool:
    now = padded(img, f.rel)
    return now.size == f.pixels.size and capture.changed_fraction(now, f.pixels) <= STALE_SHARE
