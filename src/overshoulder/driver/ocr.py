"""Text on screen, read with the OCR engine built into Windows (Windows.Media.Ocr).

It is for apps that paint their own controls, where UI Automation sees one canvas and nothing in it.
Words come back with boxes in the image's pixels. `phrases` groups them into the runs of text a person
reads as one label: Windows OCR puts buttons that share a baseline on one line, so a line is split
wherever the gap between two words is wider than the text is tall.

One reading takes three passes, because each finds words the others miss (measured 2026-10-23 on a
window of painted buttons and on Character Map):
- the image at twice its size, which finds small and greyed-out text;
- the image at its own size, which found a label the doubled one missed;
- the image at twice its size with long straight edges painted out. Windows OCR skips text inside a row
  of bordered boxes (three painted buttons gave none of their labels), and reads it once the borders
  are gone.
"""

from __future__ import annotations

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from PIL import Image, ImageChops, ImageFilter, ImageOps

from overshoulder.driver.windows import Rect

PASSES = (("plain", 2), ("plain", 1), ("unlined", 2))
# A gap between words wider than this many line heights starts a new phrase.
PHRASE_GAP = 1.0
# Straight edges at least this long (about twice it, at the image's own size) are painted out by unlined.
EDGE_RUN = 16
# Windows.Media.Ocr refuses images with a side longer than its MaxImageDimension (10000 on Windows 11).
MAX_SIDE = 9000

_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="overshoulder-ocr")
_engine = None


@dataclass(frozen=True)
class Word:
    text: str
    box: Rect


@dataclass(frozen=True)
class Phrase:
    words: tuple[Word, ...]

    @property
    def text(self) -> str:
        return " ".join(w.text for w in self.words)

    @property
    def box(self) -> Rect:
        return union(w.box for w in self.words)


def union(boxes) -> Rect:
    boxes = list(boxes)
    return Rect(min(b.left for b in boxes), min(b.top for b in boxes),
                max(b.right for b in boxes), max(b.bottom for b in boxes))


def overlaps(a: Rect, b: Rect, share: float = 0.5) -> bool:
    """Whether the overlap covers at least `share` of the smaller box."""
    i = a.intersect(b)
    small = min(a.width * a.height, b.width * b.height)
    return small > 0 and i.width * i.height >= share * small


def _engine_or_none():
    global _engine
    if _engine is None:
        try:
            from winrt.windows.media.ocr import OcrEngine
            _engine = OcrEngine.try_create_from_user_profile_languages() or False
        except Exception:  # winrt missing, or no language with OCR installed
            _engine = False
    return _engine or None


def available() -> bool:
    """Whether Windows OCR can run here: the winrt packages are installed and a language has OCR."""
    try:
        return _pool.submit(_engine_or_none).result(timeout=30) is not None
    except Exception:
        return False


async def _recognize(img: Image.Image, scale: int) -> list[list[Word]]:
    from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
    from winrt.windows.storage.streams import DataWriter

    engine = _engine_or_none()
    if engine is None:
        raise RuntimeError("Windows OCR is not available (winrt packages missing or no OCR language)")
    big = img.convert("RGBA")
    if scale != 1:
        big = big.resize((img.width * scale, img.height * scale), Image.Resampling.LANCZOS)
    dw = DataWriter()
    dw.write_bytes(big.tobytes("raw", "BGRA"))
    bmp = SoftwareBitmap.create_copy_from_buffer(dw.detach_buffer(), BitmapPixelFormat.BGRA8, big.width,
                                                 big.height)
    result = await engine.recognize_async(bmp)
    lines = []
    for line in result.lines:
        words = []
        for wd in line.words:
            if "�" in wd.text:  # a glyph the engine could not map to a character
                continue
            r = wd.bounding_rect
            words.append(Word(wd.text, Rect(int(r.x // scale), int(r.y // scale),
                                            int(-(-(r.x + r.width) // scale)),
                                            int(-(-(r.y + r.height) // scale)))))
        if words:
            lines.append(words)
    return lines


def read(img: Image.Image, scale: int = 2) -> list[list[Word]]:
    """One OCR pass: lines of words, boxes in the image's own pixels.

    Runs on one worker thread with its own event loop, so it can be called from any thread, including
    one that is running an event loop already. An image too big for the engine is read in tiles.
    """
    if max(img.width, img.height) * scale <= MAX_SIDE:
        return _pool.submit(lambda: asyncio.run(_recognize(img, scale))).result(timeout=60)
    tile, overlap = MAX_SIDE // scale, 200
    lines: list[list[Word]] = []
    for top in range(0, img.height, tile - overlap):
        for left in range(0, img.width, tile - overlap):
            part = img.crop((left, top, min(left + tile, img.width), min(top + tile, img.height)))
            for line in _pool.submit(lambda p=part: asyncio.run(_recognize(p, scale))).result(timeout=60):
                lines.append([Word(w.text, Rect(w.box.left + left, w.box.top + top, w.box.right + left,
                                                w.box.bottom + top)) for w in line])
    return lines


def _runs(edges: Image.Image, horizontal: bool) -> Image.Image:
    """Where `edges` (0/255) has unbroken runs of EDGE_RUN pixels along rows (or columns)."""
    k, (w, h) = EDGE_RUN, edges.size
    out = Image.new("L", edges.size, 0)
    for off in (0, k // 2):
        part = edges.crop((off, 0, w, h) if horizontal else (0, off, w, h))
        n = max(1, (part.width if horizontal else part.height) // k)
        small = part.resize((n, h) if horizontal else (w, n), Image.Resampling.BOX)
        full = small.point(lambda v: 255 if v >= 240 else 0).resize(
            (n * k, h) if horizontal else (w, n * k), Image.Resampling.NEAREST)
        layer = Image.new("L", edges.size, 0)
        layer.paste(full, (off, 0) if horizontal else (0, off))
        out = ImageChops.lighter(out, layer)
    return out


def unlined(img: Image.Image, threshold: int = 40) -> Image.Image:
    """The image with long straight edges (box borders, rules) painted over with what surrounds them."""
    g = ImageOps.grayscale(img)
    dy = ImageChops.difference(g, ImageChops.offset(g, 0, 1)).point(lambda v: 255 if v > threshold else 0)
    dx = ImageChops.difference(g, ImageChops.offset(g, 1, 0)).point(lambda v: 255 if v > threshold else 0)
    mask = ImageChops.lighter(_runs(dy, True), _runs(dx, False)).filter(ImageFilter.MaxFilter(5))
    rgb = img.convert("RGB")
    return Image.composite(rgb.filter(ImageFilter.MedianFilter(7)), rgb, mask)


def scan(img: Image.Image) -> list[list[Phrase]]:
    """The phrases each of PASSES found in the image."""
    out, clean = [], None
    for how, scale in PASSES:
        if how == "unlined":
            clean = clean or unlined(img)
        out.append(phrases(read(clean if how == "unlined" else img, scale)))
    return out


def merged(passes: list[list[Phrase]]) -> list[Phrase]:
    """One list from several passes: a phrase is kept unless an earlier one covers the same place.
    Sorted top to bottom, then left to right."""
    kept: list[Phrase] = []
    for found in passes:
        for ph in found:
            if not any(overlaps(ph.box, k.box) for k in kept):
                kept.append(ph)
    return sorted(kept, key=lambda p: (p.box.top // 8, p.box.left))


def phrases(lines: list[list[Word]]) -> list[Phrase]:
    """OCR lines split into phrases at gaps wider than PHRASE_GAP line heights."""
    out = []
    for line in lines:
        words = sorted(line, key=lambda w: w.box.left)
        height = max(w.box.height for w in words)
        run = [words[0]]
        for prev, cur in zip(words, words[1:], strict=False):
            if cur.box.left - prev.box.right > PHRASE_GAP * height:
                out.append(Phrase(tuple(run)))
                run = []
            run.append(cur)
        out.append(Phrase(tuple(run)))
    return out


def _norm(s: str) -> str:
    return re.sub(r"^\W+|\W+$", "", s).casefold()


def find(passes: list[list[Phrase]], text: str) -> list[Phrase]:
    """The runs of words that read `text` (case and surrounding punctuation ignored), each as a phrase
    of just those words, so "Save" in a line "Save Open Delete" gets the box of "Save" alone. Hits from
    later passes that cover an earlier hit's place are dropped."""
    want = [_norm(t) for t in text.split() if _norm(t)]
    if not want:
        return []
    per_pass = []
    for found in passes:
        hits = []
        for ph in found:
            words = [_norm(w.text) for w in ph.words]
            for i in range(len(words) - len(want) + 1):
                if words[i:i + len(want)] == want:
                    hits.append(Phrase(ph.words[i:i + len(want)]))
        per_pass.append(hits)
    return merged(per_pass)
