"""Keystroke captions: the keys an agent pressed, or the text it typed, over the bottom of the video.

A hotkey shows as keycaps (Ctrl + S). Typed text appears as it was typed, over the time the action
took; text the gateway logged as redacted shows as dots, one per character, so a tutorial never shows
more than the log does.
"""

from __future__ import annotations

from PIL import ImageDraw, ImageFont

NAMES = {
    "ctrl": "Ctrl", "control": "Ctrl", "shift": "Shift", "alt": "Alt", "win": "Win", "cmd": "Win",
    "enter": "Enter", "return": "Enter", "esc": "Esc", "escape": "Esc", "tab": "Tab", "space": "Space",
    "backspace": "Backspace", "delete": "Del", "del": "Del", "insert": "Ins", "ins": "Ins",
    "up": "↑", "down": "↓", "left": "←", "right": "→", "home": "Home", "end": "End",
    "pgup": "PgUp", "pageup": "PgUp", "pgdn": "PgDn", "pagedown": "PgDn",
}
PANEL = (17, 24, 39, 215)
CAP_FILL, CAP_EDGE, CAP_TEXT = (249, 250, 251, 255), (156, 163, 175, 255), (17, 24, 39, 255)
MARGIN = 14  # from the bottom and sides of the video
PAD = 10  # inside the panel
TYPE_MIN_S, TYPE_MAX_S = 0.3, 1.5  # typed text appears over the action's own duration, within these


def keycaps(combo: str) -> list[str]:
    """'ctrl+shift+s' -> ['Ctrl', 'Shift', 'S']."""
    caps = []
    for part in str(combo).split("+"):
        k = part.strip()
        if not k:
            continue
        low = k.lower()
        caps.append(NAMES.get(low) or (k.upper() if len(k) == 1 or (low[0] == "f" and low[1:].isdigit()) else
                                       k.capitalize()))
    return caps


def typed_text(text) -> str | None:
    """What a typing caption shows: the text itself, or a dot per character when the log redacted it."""
    if isinstance(text, str):
        return text.replace("\r\n", "\n").replace("\n", " ↵ ").replace("\t", " ⇥ ")
    if isinstance(text, dict) and isinstance(text.get("len"), int):
        return "•" * text["len"]
    return None


def typing_seconds(row: dict) -> float:
    return min(max(row.get("ms", 0) / 1000, TYPE_MIN_S), TYPE_MAX_S)


def _font(fonts: dict, size: int) -> ImageFont.ImageFont:
    key = f"keys{size}"
    if key not in fonts:
        fonts[key] = fonts["make"](size)
    return fonts[key]


def draw(d: ImageDraw.ImageDraw, size: tuple[int, int], step, t: float, fonts: dict) -> None:
    """Draw step's keystroke caption, if it has one and has started, at the bottom centre of a
    `size` video. fonts["make"](px) makes a font."""
    if t < step.t or not (step.keys or step.typed):
        return
    width, height = size
    room = width - 2 * MARGIN - 2 * PAD
    if step.keys:
        for px in (22, 20, 18, 16, 14):
            font = _font(fonts, px)
            plus = d.textlength(" + ", font=font)
            caps = [max(d.textlength(k, font=font) + 16, px * 1.6) for k in step.keys]
            if sum(caps) + plus * (len(caps) - 1) <= room:
                break
        cap_h = px + 14
        total = sum(caps) + plus * (len(caps) - 1)
        x = (width - total) / 2
        bottom = height - MARGIN
        top = bottom - cap_h - 2 * PAD
        d.rounded_rectangle((x - PAD, top, x + total + PAD, bottom), radius=12, fill=PANEL)
        y = top + PAD
        for i, (label, w) in enumerate(zip(step.keys, caps, strict=True)):
            d.rounded_rectangle((x, y + 3, x + w, y + cap_h), radius=6, fill=CAP_EDGE)  # the key's lower edge
            d.rounded_rectangle((x, y, x + w, y + cap_h - 3), radius=6, fill=CAP_FILL, outline=CAP_EDGE)
            d.text((x + w / 2, y + (cap_h - 3) / 2), label, font=font, fill=CAP_TEXT, anchor="mm")
            x += w
            if i < len(step.keys) - 1:
                d.text((x + plus / 2, y + cap_h / 2), "+", font=font, fill=(255, 255, 255, 255), anchor="mm")
                x += plus
        return
    font = _font(fonts, 22)
    k = min(1.0, (t - step.t) / step.typing_s) if step.typing_s > 0 else 1.0
    so_far = step.typed[:round(len(step.typed) * k)]
    caret = 6 if k < 1.0 else 0  # drawn as a bar: the caret glyphs are missing from Segoe UI Bold
    shown = so_far
    while shown and d.textlength("…" + shown, font=font) + caret > room:
        shown = shown[1:]  # keep the end of long text in view, where the typing is
    text = ("…" if shown != so_far else "") + shown
    tw = max(d.textlength(text, font=font) + caret, 40)
    bottom = height - MARGIN
    top = bottom - 22 - 14 - 2 * PAD
    x = (width - tw) / 2
    d.rounded_rectangle((x - PAD - 6, top, x + tw + PAD + 6, bottom), radius=12, fill=PANEL)
    d.text((x, (top + bottom) / 2), text, font=font, fill=(255, 255, 255, 255), anchor="lm")
    if caret:
        cx = x + d.textlength(text, font=font) + 3
        d.rectangle((cx, top + PAD + 2, cx + 1, bottom - PAD - 2), fill=(255, 255, 255, 255))
