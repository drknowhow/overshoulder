"""Turn a clean recording + action timeline into a tutorial: MP4, per-step PNGs and steps.md.

Everything is drawn here, after the fact, from the recording's tracks: a pointer, click ripples,
numbered step badges, a highlight around each target, and a caption bar. The recording itself never
contains overlays, so a tutorial can be re-rendered with different styling or edits.

The pointer is the agent's by default: it glides to each step's target just before the step. Agents
mostly act in the background, where the real cursor belongs to the person and is somewhere else; the
real cursor track is still recorded and can be drawn instead (pointer="real").
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ACCENT = (37, 99, 235)  # badge / highlight
RIPPLE = (250, 204, 21)
CAPTION_BG = (17, 24, 39, 215)
STEP_ACTIONS = {"click", "double_click", "right_click", "invoke", "toggle", "select", "set_value", "type",
                "hotkey", "activate", "focus"}
# Actions a viewer would do with a click: they get a ripple where the pointer lands.
PRESS_ACTIONS = {"click", "double_click", "right_click", "invoke", "toggle", "select"}
POINTERS = ("agent", "real", "none")
LEAD_S, HOLD_S, RIPPLE_S = 0.5, 2.2, 0.6
GLIDE_S = 0.5  # the agent's pointer moves to a step's target over this long, ending at the step
STRIP = 72  # caption strip under the video, so captions never cover the app
# Tight timing keeps frames this close to a step and drops the idle time between agent actions.
PAD_BEFORE, PAD_AFTER, EDGE_S = 0.6, 0.4, 1.0


@dataclass
class Step:
    n: int
    t: float  # seconds from recording start
    action: str
    caption: str
    rect: tuple[int, int, int, int] | None  # screen pixels
    point: tuple[int, int] | None
    start: float = 0.0  # when its annotations appear ...
    end: float = 0.0  # ... and disappear; never overlapping the next step
    aim: tuple[int, int] | None = None  # where the agent's pointer goes for this step


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("segoeuib.ttf", "segoeui.ttf", "arial.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _caption(row: dict) -> str:
    a, p, t = row["action"], row.get("params") or {}, row.get("target") or {}
    name = t.get("name") or t.get("control_type") or ""
    label = f'"{name}"' if name else "here"
    text = p.get("text")
    shown = text if isinstance(text, str) else (f"{text['len']} characters" if isinstance(text, dict) else "")
    if a in ("click", "invoke", "select"):
        return f"Click {label}"
    if a == "double_click":
        return f"Double-click {label}"
    if a == "right_click":
        return f"Right-click {label}"
    if a == "toggle":
        return f"Toggle {label}"
    if a == "type":
        return f'Type "{shown}"' if isinstance(text, str) else f"Type {shown}"
    if a == "set_value":
        v = p.get("value")
        return f'Enter "{v}" in {label}' if isinstance(v, str) else f"Fill in {label}"
    if a == "hotkey":
        return "Press " + "+".join(k.strip().capitalize() for k in str(p.get("combo", "")).split("+"))
    if a == "activate":
        return "Bring the window to the front"
    if a == "focus":
        return f"Select {label}"
    return a


def build_steps(actions: list[dict], start: float) -> list[Step]:
    steps = []
    for row in actions:
        if not row.get("ok") or row.get("action") not in STEP_ACTIONS:
            continue
        t = (row.get("t_act") or (row["ts"] - row.get("ms", 0) / 1000)) - start
        target = row.get("target") or {}
        rect = tuple(target["rect"]) if target.get("rect") else None
        if rect and (rect[2] <= rect[0] or rect[3] <= rect[1]):
            rect = None  # hidden element (e.g. Chromium's editor input): nothing on screen to point at
        center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2) if rect else None
        point = (target["x"], target["y"]) if target.get("kind") == "point" else None
        if point is None and center and row["action"] in PRESS_ACTIONS:
            point = center
        steps.append(Step(len(steps) + 1, round(t, 3), row["action"], _caption(row), rect, point,
                          aim=point or center))
    for i, st in enumerate(steps):
        nxt = steps[i + 1].t if i + 1 < len(steps) else None
        st.end = min(st.t + HOLD_S, nxt - 0.05) if nxt is not None else st.t + HOLD_S
        st.start = max(st.t - LEAD_S, steps[i - 1].end if i else st.t - LEAD_S)
    return steps


def pointer_at(t: float, steps: list[Step], home: tuple[int, int]) -> tuple[int, int] | None:
    """Where the agent's pointer is at time t. It rests at `home` (the middle of the first frame) until
    the first step with a target, glides to each target over GLIDE_S, arriving as the step happens, and
    rests there until the next one. None when no step has a target."""
    aimed = [s for s in steps if s.aim]
    if not aimed:
        return None
    prev, prev_t = home, None
    for s in aimed:
        if t < s.t:
            start = s.t - GLIDE_S if prev_t is None else max(prev_t, s.t - GLIDE_S)
            k = 1.0 if s.t <= start else (t - start) / (s.t - start)
            if k <= 0:
                return prev
            k = min(k, 1.0)
            k = k * k * (3 - 2 * k)  # ease in and out
            return round(prev[0] + (s.aim[0] - prev[0]) * k), round(prev[1] + (s.aim[1] - prev[1]) * k)
        prev, prev_t = s.aim, s.t
    return prev


def still_time(step: Step) -> float:
    """When to grab a step's still: after the action took effect, before the next step starts."""
    return max(step.t, min(step.t + 0.8, step.end - 0.1))


def _frames_track(rec: Path) -> list[dict]:
    return [json.loads(x) for x in (rec / "frames.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]


def _cursor_shape(x: int, y: int, s: float = 1.0) -> list[tuple[float, float]]:
    pts = [(0, 0), (0, 17), (4.5, 13), (7.5, 20), (10, 19), (7, 12), (13, 12)]
    return [(x + px * s, y + py * s) for px, py in pts]


def _fit(d: ImageDraw.ImageDraw, text: str, max_w: float, size: int) -> tuple[str, ImageFont.ImageFont]:
    while size > 15:
        font = _font(size)
        if d.textlength(text, font=font) <= max_w:
            return text, font
        size -= 2
    font = _font(16)
    while text and d.textlength(text + "...", font=font) > max_w:
        text = text[:-1]
    return text + "...", font


def annotate(img: Image.Image, t: float, origin: tuple[int, int], cursor: tuple[int, int] | None,
             steps: list[Step], fonts: dict) -> Image.Image:
    """Draw every annotation active at time t onto a frame whose top-left is `origin` on screen.

    Returns the frame with a caption strip of STRIP pixels added underneath.
    """
    ox, oy = origin
    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    active = [s for s in steps if s.start <= t < s.end]

    for s in active:
        if s.rect:
            lf, tp, r, b = s.rect[0] - ox, s.rect[1] - oy, s.rect[2] - ox, s.rect[3] - oy
            d.rounded_rectangle((lf - 4, tp - 4, r + 4, b + 4), radius=8, outline=ACCENT + (255,), width=4)
            bx, by = max(lf - 16, 4), max(tp - 16, 4)
        elif s.point:
            bx, by = s.point[0] - ox + 14, s.point[1] - oy - 34
        else:
            bx = by = None  # keyboard step with no target: the caption carries it
        if bx is not None:
            d.ellipse((bx, by, bx + 32, by + 32), fill=ACCENT + (255,), outline=(255, 255, 255, 255), width=2)
            d.text((bx + 16, by + 16), str(s.n), font=fonts["badge"], fill=(255, 255, 255, 255), anchor="mm")
        if s.point and 0 <= t - s.t <= RIPPLE_S:
            k = (t - s.t) / RIPPLE_S
            rad = 10 + 30 * k
            px, py = s.point[0] - ox, s.point[1] - oy
            d.ellipse((px - rad, py - rad, px + rad, py + rad), outline=RIPPLE + (int(255 * (1 - k)),), width=4)

    if cursor:
        cx, cy = cursor[0] - ox, cursor[1] - oy
        if -40 < cx < base.width + 40 and -40 < cy < base.height + 40:
            d.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=RIPPLE + (70,))
            d.polygon(_cursor_shape(cx, cy, 1.3), fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))

    framed = Image.alpha_composite(base, layer).convert("RGB")
    out = Image.new("RGB", (base.width, base.height + STRIP), CAPTION_BG[:3])
    out.paste(framed, (0, 0))
    if active:
        s = active[-1]
        sd = ImageDraw.Draw(out)
        text, font = _fit(sd, f"{s.n}. {s.caption}", base.width - 32, 26)
        sd.text((base.width / 2, base.height + STRIP / 2), text, font=font, fill=(255, 255, 255), anchor="mm")
    return out


def keep_frame(t: float, steps: list[Step], last_t: float) -> bool:
    """Tight timing: keep the opening and closing second and the frames around each step."""
    if t <= EDGE_S or t >= last_t - EDGE_S:
        return True
    return any(s.start - PAD_BEFORE <= t <= s.end + PAD_AFTER for s in steps)


def render(rec_dir: Path, actions: list[dict] | None = None, out_dir: Path | None = None,
           tight: bool = True, pointer: str = "agent") -> dict:
    """tight=True cuts the idle time between steps; tight=False keeps real-time pacing. pointer: "agent"
    (the agent's own track), "real" (the recorded cursor) or "none"."""
    if pointer not in POINTERS:
        raise ValueError(f"pointer must be one of {POINTERS}")
    rec = Path(rec_dir)
    out = Path(out_dir or rec / "tutorial")
    out.mkdir(parents=True, exist_ok=True)
    meta = json.loads((rec / "meta.json").read_text(encoding="utf-8"))
    if actions is None:
        actions = json.loads((rec / "actions.json").read_text(encoding="utf-8"))
    steps = build_steps(actions, meta["start"])
    track = _frames_track(rec)
    width, height = meta["size"]
    fps = meta["fps"]
    fonts = {"badge": _font(18), "caption": _font(26)}
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")

    dec = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(rec / "video.mp4"),
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt",
                            "rgb24", "-s", f"{width}x{height + STRIP}", "-r", str(fps), "-i", "-", "-c:v",
                            "libx264",
                            "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                            str(out / "tutorial.mp4")], stdin=subprocess.PIPE)
    shots_due = {s.n: still_time(s) for s in steps}
    by_n = {s.n: s for s in steps}
    saved: dict[int, str] = {}
    frame_bytes = width * height * 3
    last_t = track[-1]["t"] if track else 0.0
    b0 = track[0]["bounds"] if track else [0, 0, width, height]
    home = ((b0[0] + b0[2]) // 2, (b0[1] + b0[3]) // 2)

    def pointer_for(fr: dict, t: float):
        if pointer == "agent":
            return pointer_at(t, steps, home)
        return fr.get("cursor") if pointer == "real" else None

    i = written = 0
    try:
        while True:
            raw = dec.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            fr = track[min(i, len(track) - 1)] if track else {"t": i / fps, "bounds": [0, 0, width, height],
                                                               "cursor": None}
            t = fr["t"]
            img = Image.frombytes("RGB", (width, height), raw)
            if not tight or keep_frame(t, steps, last_t):
                framed = annotate(img, t, (fr["bounds"][0], fr["bounds"][1]), pointer_for(fr, t), steps, fonts)
                enc.stdin.write(framed.tobytes())
                written += 1
            for n, due in shots_due.items():
                if n not in saved and t >= due:
                    path = out / f"step-{n:02d}.png"
                    # Each still shows its own step only, even if the video already moved on.
                    one = by_n[n]
                    solo = Step(one.n, one.t, one.action, one.caption, one.rect, one.point, t - 1, t + 1)
                    annotate(img, t, (fr["bounds"][0], fr["bounds"][1]), pointer_for(fr, t), [solo],
                             fonts).save(path)
                    saved[n] = path.name
            i += 1
    finally:
        dec.stdout.close()
        dec.wait()
        enc.stdin.close()
        enc.wait()

    lines = ["# Steps", ""]
    for s in steps:
        lines.append(f"{s.n}. {s.caption}")
        if s.n in saved:
            lines.append(f"\n   ![Step {s.n}]({saved[s.n]})\n")
    (out / "steps.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"video": str(out / "tutorial.mp4"), "frames": written, "source_frames": i, "steps": len(steps),
            "images": len(saved), "seconds": round(written / fps, 1), "markdown": str(out / "steps.md")}
