"""Editable annotations: a layer file over a clean recording or a screenshot, applied at render time.

A recording keeps its layer in annotations.json next to video.mp4; a screenshot keeps it next to the
image as <name>.annotations.json. The layer is a list of items:

  box        rect, text?, color?          an outlined box, optionally labelled
  arrow      tail, head, color?           an arrow pointing at head
  text       at, text, size?, color?      a label on a dark pill, top-left at `at`
  badge      at, text?, color?            a round badge centred at `at`
  spotlight  rect                         dims everything outside rect
  redact     rect, style?                 blur (default), pixelate or solid; applied to every output
  step       step, caption?, hidden?, narration?, audio?   edits one step of the tutorial
  cut        t0, t1                       drops that part of the recording (and the steps in it)
  crop       rect                         crops every output to rect (one per layer)

Coordinates are pixels of the clean recording (video.mp4) or of the original image, whatever crop
or zoom the output uses; steps.json gives every step's rect in the same pixels. A drawn item shows
during one step (`step`: the step's id in steps.json), between t0 and t1 seconds from the recording
start, or throughout when it has neither. Every item has an `id`; edit or remove items by id.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from PIL import Image, ImageColor, ImageDraw, ImageFilter

from overshoulder import brand

VERSION = 1
DRAWN = ("box", "arrow", "text", "badge", "spotlight")
KINDS = DRAWN + ("redact", "step", "cut", "crop")
STYLES = ("blur", "pixelate", "solid")
DEFAULT_COLOR = brand.AGENT_BLUE
REQUIRED = {"box": ("rect",), "arrow": ("tail", "head"), "text": ("at", "text"), "badge": ("at",),
            "spotlight": ("rect",), "redact": ("rect",), "step": ("step",), "cut": ("t0", "t1"),
            "crop": ("rect",)}
ALLOWED = {"id", "kind", "note", "step", "t0", "t1", "rect", "tail", "head", "at", "text", "size", "color",
           "width", "style", "caption", "hidden", "narration", "audio"}


class LayerError(ValueError):
    pass


def _numbers(v, n: int, name: str) -> list[float]:
    if not (isinstance(v, list | tuple) and len(v) == n and all(isinstance(x, int | float) for x in v)):
        raise LayerError(f"{name} must be {n} numbers")
    return [float(x) for x in v]


def validate(item: dict) -> dict:
    """Check one item and return it normalised; raises LayerError naming what is wrong."""
    if not isinstance(item, dict):
        raise LayerError("an item must be an object")
    kind = item.get("kind")
    if kind not in KINDS:
        raise LayerError(f"kind must be one of {', '.join(KINDS)}")
    unknown = set(item) - ALLOWED
    if unknown:
        raise LayerError(f"unknown field(s) {', '.join(sorted(unknown))}")
    missing = [k for k in REQUIRED[kind] if item.get(k) is None]
    if missing:
        raise LayerError(f"{kind} needs {', '.join(missing)}")
    out = dict(item)
    if "rect" in out:
        r = _numbers(out["rect"], 4, "rect")
        if r[2] <= r[0] or r[3] <= r[1]:
            raise LayerError("rect is [left, top, right, bottom] with right > left and bottom > top")
        out["rect"] = r
    for k in ("tail", "head", "at"):
        if k in out:
            out[k] = _numbers(out[k], 2, k)
    for k in ("t0", "t1"):
        if k in out and not isinstance(out[k], int | float):
            raise LayerError(f"{k} must be seconds from the recording start")
    if "t0" in out and "t1" in out and out["t1"] <= out["t0"]:
        raise LayerError("t1 must be after t0")
    if "step" in out and not (isinstance(out["step"], int) and out["step"] >= 1):
        raise LayerError("step must be a step id from steps.json (1, 2, ...)")
    if "color" in out:
        try:
            ImageColor.getrgb(out["color"])
        except (ValueError, AttributeError) as e:
            raise LayerError(f"color {out['color']!r} is not a colour") from e
    if "style" in out and out["style"] not in STYLES:
        raise LayerError(f"style must be one of {', '.join(STYLES)}")
    if "hidden" in out and not isinstance(out["hidden"], bool):
        raise LayerError("hidden must be true or false")
    for k in ("text", "caption", "narration", "note", "audio"):
        if k in out and not isinstance(out[k], str):
            raise LayerError(f"{k} must be a string")
    for k in ("size", "width"):
        if k in out and not (isinstance(out[k], int) and 6 <= out[k] <= 200):
            raise LayerError(f"{k} must be a whole number of pixels between 6 and 200")
    return out


class Layer:
    """The annotation layer stored at `path`. Reads are fresh on every call, so edits made by another
    process (an agent, the CLI, a person with an editor) are picked up by the next render."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def for_recording(cls, rec_dir: Path) -> Layer:
        return cls(Path(rec_dir) / "annotations.json")

    @classmethod
    def for_image(cls, image: Path) -> Layer:
        image = Path(image)
        return cls(image.with_name(image.stem + ".annotations.json"))

    # -- storage -------------------------------------------------------------------------------

    def items(self) -> list[dict]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            raise LayerError(f"{self.path.name} is not valid JSON: {e}") from e
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise LayerError(f'{self.path.name} must be {{"version": 1, "items": [...]}}')
        out = []
        for i, item in enumerate(items):
            try:
                out.append(validate(item))
            except LayerError as e:
                raise LayerError(f"{self.path.name} item {i + 1} ({item.get('id', 'no id')}): {e}") from e
        if sum(1 for x in out if x["kind"] == "crop") > 1:
            raise LayerError(f"{self.path.name} has more than one crop")
        return out

    def _save(self, items: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"version": VERSION, "items": items}, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)

    def add(self, item: dict) -> dict:
        items = self.items()
        item = validate({k: v for k, v in item.items() if k != "id"})
        if item["kind"] == "crop" and any(x["kind"] == "crop" for x in items):
            raise LayerError("the layer already has a crop; edit or remove it")
        if item["kind"] == "step":
            for x in items:
                if x["kind"] == "step" and x["step"] == item["step"]:
                    return self.edit(x["id"], {k: v for k, v in item.items() if k != "kind"})
        used = [int(x["id"][1:]) for x in items if str(x.get("id", ""))[1:].isdigit()]
        item = {"id": f"a{max(used, default=0) + 1}", **item}
        self._save(items + [item])
        return item

    def edit(self, item_id: str, changes: dict) -> dict:
        """Merge changes into one item; a change to None removes that field."""
        if "id" in changes or "kind" in changes:
            raise LayerError("an item's id and kind cannot change; remove it and add a new one")
        items = self.items()
        for i, x in enumerate(items):
            if x.get("id") == item_id:
                merged = {k: v for k, v in {**x, **changes}.items() if v is not None}
                items[i] = validate(merged)
                self._save(items)
                return items[i]
        raise LayerError(f"no item {item_id!r}")

    def remove(self, item_id: str) -> bool:
        items = self.items()
        kept = [x for x in items if x.get("id") != item_id]
        if len(kept) == len(items):
            return False
        self._save(kept)
        return True

    # -- what a render needs -------------------------------------------------------------------

    def crop(self, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
        """The crop clamped to the frame, with an even width and height (the encoder needs that)."""
        c = next((x["rect"] for x in self.items() if x["kind"] == "crop"), None)
        if c is None:
            return None
        left, top = max(0, int(c[0])), max(0, int(c[1]))
        right, bottom = min(size[0], int(c[2])), min(size[1], int(c[3]))
        right -= (right - left) % 2
        bottom -= (bottom - top) % 2
        if right - left < 16 or bottom - top < 16:
            raise LayerError("the crop leaves less than 16x16 pixels of the frame")
        return left, top, right, bottom

    def cuts(self) -> list[tuple[float, float]]:
        return [(x["t0"], x["t1"]) for x in self.items() if x["kind"] == "cut"]

    def step_edits(self) -> dict[int, dict]:
        return {x["step"]: x for x in self.items() if x["kind"] == "step"}

    def drawn(self) -> list[dict]:
        """The items drawn onto frames: redactions first, then the rest in file order."""
        items = [x for x in self.items() if x["kind"] in DRAWN or x["kind"] == "redact"]
        return sorted(items, key=lambda x: x["kind"] != "redact")


def bbox(item: dict) -> tuple[float, float, float, float]:
    """Roughly the area a drawn item covers, in its own coordinates (text size is estimated)."""
    if "rect" in item:
        return tuple(item["rect"])
    if "tail" in item:
        (x0, y0), (x1, y1) = item["tail"], item["head"]
        return min(x0, x1) - 10, min(y0, y1) - 10, max(x0, x1) + 10, max(y0, y1) + 10
    x, y = item["at"]
    if item["kind"] == "badge":
        r = item.get("size", 32) / 2
        return x - r, y - r, x + r, y + r
    size = item.get("size", 22)
    return x, y, x + 0.6 * size * len(item.get("text", "")) + 16, y + size + 10


def in_cut(t: float, cuts: list[tuple[float, float]]) -> bool:
    return any(t0 <= t < t1 for t0, t1 in cuts)


def active(items: list[dict], t: float, windows: dict[int, tuple[float, float]], offset: tuple[float, float],
           still_of: int | None = None) -> list[dict]:
    """The items showing at time t, moved by `offset` (a recording's coordinates are frame pixels;
    adding the frame's origin puts them in screen pixels, like the steps). windows maps a step id to
    its (start, end); still_of: drawing the still of that step, so only its own step items show."""
    out = []
    for x in items:
        if "step" in x:
            if still_of is not None:
                if x["step"] != still_of:
                    continue
            else:
                w = windows.get(x["step"])
                if w is None or not (w[0] <= t < w[1]):
                    continue
        if "t0" in x and t < x["t0"]:
            continue
        if "t1" in x and t >= x["t1"]:
            continue
        moved = dict(x)
        for k in ("rect", "tail", "head", "at"):
            if k in x:
                v = x[k]
                moved[k] = [v[i] + offset[i % 2] for i in range(len(v))]
        out.append(moved)
    return out


def redact(img: Image.Image, items: list[dict], to_img) -> Image.Image:
    """Apply every redaction in items to img (in place for RGB images); to_img maps an item's (x, y)
    to image pixels."""
    for x in items:
        if x["kind"] != "redact":
            continue
        (lf, t), (r, b) = to_img(x["rect"][0], x["rect"][1]), to_img(x["rect"][2], x["rect"][3])
        box = (max(0, round(lf)), max(0, round(t)), min(img.width, round(r)), min(img.height, round(b)))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        style = x.get("style", "blur")
        region = img.crop(box)
        if style == "solid":
            region = Image.new(img.mode, region.size, (55, 65, 81))
        elif style == "pixelate":
            block = max(8, min(region.size) // 4)
            small = region.resize((max(1, region.width // block), max(1, region.height // block)),
                                  Image.Resampling.BOX)
            region = small.resize(region.size, Image.Resampling.NEAREST)
        else:
            # Downscale to about three blocks across the short side, so no glyph's shape survives, then
            # blur the blocks smooth.
            block = max(12, min(region.size) // 3)
            small = region.resize((max(1, region.width // block), max(1, region.height // block)),
                                  Image.Resampling.BOX)
            region = small.resize(region.size, Image.Resampling.BILINEAR).filter(ImageFilter.GaussianBlur(block / 2))
        img.paste(region, box[:2])
    return img


def _rgb(x: dict) -> tuple[int, int, int]:
    return ImageColor.getrgb(x.get("color", DEFAULT_COLOR))[:3]


def draw_spotlights(d: ImageDraw.ImageDraw, size: tuple[int, int], items: list[dict], at) -> None:
    """Dim everything outside each spotlight. Draw these first: clearing the hole clears the layer."""
    spots = [x for x in items if x["kind"] == "spotlight"]
    if not spots:
        return
    d.rectangle((0, 0, size[0], size[1]), fill=(0, 0, 0, 120))
    for x in spots:
        (lf, t), (r, b) = at(x["rect"][0], x["rect"][1]), at(x["rect"][2], x["rect"][3])
        d.rectangle((lf, t, r, b), fill=(0, 0, 0, 0))


def draw(d: ImageDraw.ImageDraw, items: list[dict], at, fonts: dict) -> None:
    """Draw boxes, arrows, text and badges; at maps an item's (x, y) to output pixels."""
    make = fonts["make"]
    for x in items:
        kind, color = x["kind"], _rgb(x) + (255,)
        if kind == "box":
            (lf, t), (r, b) = at(x["rect"][0], x["rect"][1]), at(x["rect"][2], x["rect"][3])
            d.rounded_rectangle((lf, t, r, b), radius=6, outline=color, width=x.get("width", 4))
            if x.get("text"):
                _pill(d, (lf, t - 6), x["text"], make(x.get("size", 18)), color, anchor="lb")
        elif kind == "arrow":
            (x0, y0), (x1, y1) = at(*x["tail"]), at(*x["head"])
            _arrow(d, (x0, y0), (x1, y1), color, x.get("width", 5))
            if x.get("text"):
                _pill(d, (x0, y0), x["text"], make(x.get("size", 18)), color, anchor="mm")
        elif kind == "text":
            _pill(d, at(*x["at"]), x["text"], make(x.get("size", 22)), (17, 24, 39, 225), anchor="lt",
                  fg=(255, 255, 255, 255) if "color" not in x else color)
        elif kind == "badge":
            cx, cy = at(*x["at"])
            rad = x.get("size", 32) / 2
            d.ellipse((cx - rad, cy - rad, cx + rad, cy + rad), fill=color, outline=(255, 255, 255, 255), width=2)
            if x.get("text"):
                d.text((cx, cy), x["text"], font=make(max(10, int(rad * 1.1))), fill=(255, 255, 255, 255),
                       anchor="mm")


def _pill(d, xy, text, font, bg, anchor="lt", fg=(255, 255, 255, 255)) -> None:
    lf, t, r, b = d.textbbox(xy, text, font=font, anchor=anchor)
    d.rounded_rectangle((lf - 8, t - 5, r + 8, b + 5), radius=8, fill=bg)
    d.text(xy, text, font=font, fill=fg, anchor=anchor)


def _arrow(d, tail, head, color, width) -> None:
    import math

    (x0, y0), (x1, y1) = tail, head
    ang = math.atan2(y1 - y0, x1 - x0)
    size = 6 + width * 3
    back = (x1 - size * math.cos(ang), y1 - size * math.sin(ang))
    d.line((x0, y0, back[0], back[1]), fill=color, width=width)
    left = (x1 - size * math.cos(ang - 0.45), y1 - size * math.sin(ang - 0.45))
    right = (x1 - size * math.cos(ang + 0.45), y1 - size * math.sin(ang + 0.45))
    d.polygon([(x1, y1), left, right], fill=color)
