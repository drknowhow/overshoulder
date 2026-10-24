"""The "Made with Overshoulder" mark on what the renderer makes for people to watch or read.

- tutorial.mp4, tutorial.gif and the step PNGs: the mark and the words at the right end of the dark
  caption strip, so it never covers the app (render.annotate);
- an annotated screenshot: a thin strip under the picture carries it (render.render_image);
- steps.md: a last line;
- the Shotcut project: a track of its own above the annotations (shotcut.py).

The files also say it in their metadata: the MP4 comment tag, a PNG text chunk and a GIF comment.
Screenshots an agent takes stay clean, because agents and OCR read them.

The visible mark is on for a new install, and the Tutorials setting "Show Made with Overshoulder" turns it
off for free. visible() is the one place that reads that setting. The metadata stays either way.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFont
from PIL.PngImagePlugin import PngInfo

from overshoulder import __version__, brand

TEXT = f"Made with {brand.NAME}"
URL = "https://overshoulder.yepgent.com"
TILE = 28  # the mark's size in the strip, in pixels
WORDS = 18  # the words' size
GAP, PAD = 8, 16  # between the mark and the words; from the strip's right end
WORDS_FROM = 640  # narrower frames show the mark without the words, to leave the caption room
WORDS_FILL = (255, 255, 255, 158)  # 62% white on INK
SHOT_STRIP = 40  # the strip under an annotated screenshot
_POINTER = [(0, 0), (0, 22), (5.2, 16.9), (8.6, 24.6), (12.2, 23), (8.9, 15.4), (16, 15.4)]  # brand.POINTER_PATH


_seen: tuple[tuple, bool] | None = None  # ((settings file, its mtime and size), show_mark)


def visible() -> bool:
    """Whether the visible mark is drawn: the person's show_mark setting, on unless they turned it off.
    Called for every frame, so the file is read again only when it changes."""
    global _seen
    from overshoulder import settings as st
    p = st.settings_path()
    try:
        s = p.stat()
        stamp = (str(p), s.st_mtime_ns, s.st_size)  # the size too: two saves can land in one clock tick
    except OSError:
        stamp = (str(p), None, None)
    if _seen is None or _seen[0] != stamp:
        _seen = (stamp, bool(st.load_or_default(p).show_mark))
    return _seen[1]


def comment() -> str:
    return f"{TEXT} {__version__} {URL}"


@lru_cache(maxsize=8)
def tile(size: int = TILE, ring: bool = True) -> Image.Image:
    """brand.MARK_SVG as an RGBA image `size` pixels square. ring: a faint white edge, so the INK tile
    stands out on the INK strip."""
    k = 4  # drawn 4x and scaled down, for smooth edges
    s = size * k
    u = s / 64  # one unit of the SVG's 64-unit view box
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    ink = ImageColor.getrgb(brand.INK)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=15 * u, fill=ink + (255,),
                        outline=(255, 255, 255, 64) if ring else None, width=max(1, round(2 * u)) if ring else 0)

    def pointer(tx: float, ty: float) -> list[tuple[float, float]]:
        return [((tx + 1.3 * x) * u, (ty + 1.3 * y) * u) for x, y in _POINTER]

    d.polygon(pointer(17, 10), fill=ImageColor.getrgb(brand.MARK_BLUE) + (255,))
    d.polygon(pointer(28, 21), fill=(255, 255, 255, 255), outline=ink + (255,), width=max(1, round(1.4 * u)))
    return img.resize((size, size), Image.Resampling.LANCZOS)


@lru_cache(maxsize=4)
def _font(size: int) -> ImageFont.ImageFont:
    try:
        f = ImageFont.truetype("SegUIVar.ttf", size)
        f.set_variation_by_axes([600])  # Segoe UI Semibold, as in the mock
        return f
    except (OSError, ValueError, AttributeError):
        pass
    for name in ("segoeuib.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _words(width: int) -> bool:
    return width >= WORDS_FROM


def footprint(width: int) -> int:
    """How many pixels at the right end of a strip `width` wide the mark takes, padding included."""
    if not visible():
        return 0
    w = PAD + TILE
    if _words(width):
        w += GAP + round(_font(WORDS).getlength(TEXT))
    return w + PAD


def draw(img: Image.Image, strip: tuple[int, int, int, int]) -> None:
    """Draw the mark at the right end of `strip` (x0, y0, x1, y1 in img's pixels), centred on its height."""
    if not visible():
        return
    x0, y0, x1, y1 = strip
    mid = (y0 + y1) / 2
    x = x1 - PAD
    over = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)
    if _words(x1 - x0):
        font = _font(WORDS)
        x -= round(font.getlength(TEXT))
        d.text((x, mid), TEXT, font=font, fill=WORDS_FILL, anchor="lm")
        x -= GAP
    x -= TILE
    over.alpha_composite(tile(), (x, round(mid - TILE / 2)))
    if img.mode == "RGBA":
        img.alpha_composite(over)
    else:
        img.paste(Image.alpha_composite(img.convert("RGBA"), over).convert(img.mode))


def layer(size: tuple[int, int], strip_h: int) -> Image.Image:
    """A transparent frame with only the mark in its bottom strip: the Shotcut project's mark track."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    draw(img, (0, size[1] - strip_h, size[0], size[1]))
    return img


def under(img: Image.Image) -> Image.Image:
    """img with a thin strip underneath that carries the mark (an annotated screenshot)."""
    if not visible():
        return img
    out = Image.new("RGB", (img.width, img.height + SHOT_STRIP), ImageColor.getrgb(brand.INK))
    out.paste(img.convert("RGB"), (0, 0))
    draw(out, (0, img.height, img.width, out.height))
    return out


def png_info() -> PngInfo:
    info = PngInfo()
    info.add_text("Software", f"{brand.NAME} {__version__}")
    info.add_text("Comment", comment())
    return info


def ffmpeg_args() -> list[str]:
    """Output options that put the comment in an MP4's metadata."""
    return ["-metadata", f"comment={comment()}"]


def markdown() -> str:
    """The last line of steps.md."""
    return f"*{TEXT}* ({URL})" if visible() else ""


def tag_gif(path: Path) -> None:
    """Put the comment in a GIF as a comment extension, right after the colour table at its start."""
    data = Path(path).read_bytes()
    if data[:6] != b"GIF89a":
        return  # comments are a GIF89a extension
    flags = data[10]
    at = 13 + (3 * (2 << (flags & 7)) if flags & 0x80 else 0)
    text = comment().encode("ascii", "replace")
    blocks = b"".join(bytes([len(text[i:i + 255])]) + text[i:i + 255] for i in range(0, len(text), 255))
    Path(path).write_bytes(data[:at] + b"\x21\xfe" + blocks + b"\x00" + data[at:])
