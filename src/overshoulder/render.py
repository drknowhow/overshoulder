"""Turn a clean recording + action timeline into a tutorial: MP4, per-step PNGs and steps.md.

Everything is drawn here, after the fact, from the recording's tracks: a pointer, click ripples,
numbered step badges, a highlight around each target, and a caption bar. The video also zooms in on
each step's target and back out (zoom.py). The recording itself never contains overlays, so a
tutorial can be re-rendered with different styling or edits.

The pointer is the agent's by default: it glides to each step's target just before the step. Agents
mostly act in the background, where the real cursor belongs to the person and is somewhere else; the
real cursor track is still recorded and can be drawn instead (pointer="real").
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from overshoulder import keys as kc
from overshoulder import layer as lay
from overshoulder import narrate as nr
from overshoulder import zoom as zm

ACCENT = (37, 99, 235)  # badge / highlight
RIPPLE = (250, 204, 21)
CAPTION_BG = (17, 24, 39, 215)
STEP_ACTIONS = {"click", "double_click", "right_click", "invoke", "toggle", "select", "set_value", "type",
                "hotkey", "activate", "focus"}
# Actions a viewer would do with a click: they get a ripple where the pointer lands.
PRESS_ACTIONS = {"click", "double_click", "right_click", "invoke", "toggle", "select"}
POINTERS = ("agent", "real", "none")
ZOOMS = ("auto", "off")
LEAD_S, HOLD_S, RIPPLE_S = 0.5, 2.2, 0.6
GLIDE_S = 0.5  # the agent's pointer moves to a step's target over this long, ending at the step
STRIP = 72  # caption strip under the video, so captions never cover the app
# Tight timing keeps frames this close to a step and drops the idle time between agent actions.
PAD_BEFORE, PAD_AFTER, EDGE_S = 0.6, 0.4, 1.0
GIF_WIDTH, GIF_FPS = 640, 10  # GIFs are for READMEs and chat: capped so they stay a few MB


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
    keys: list[str] | None = None  # a hotkey's keycaps
    typed: str | None = None  # typed text as a caption may show it
    typing_s: float = 0.0  # how long the typed text takes to appear
    id: int = 0  # stable number from the recording; n is the number shown, after hidden steps go


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
        params = row.get("params") or {}
        steps.append(Step(len(steps) + 1, round(t, 3), row["action"], _caption(row), rect, point,
                          aim=point or center,
                          keys=kc.keycaps(params.get("combo", "")) if row["action"] == "hotkey" else None,
                          typed=kc.typed_text(params.get("text")) if row["action"] == "type" else None,
                          typing_s=kc.typing_seconds(row) if row["action"] == "type" else 0.0,
                          id=len(steps) + 1))
    return _pace(steps)


def _pace(steps: list[Step]) -> list[Step]:
    """Set when each step's annotations show, so neighbours never overlap."""
    for i, st in enumerate(steps):
        nxt = steps[i + 1].t if i + 1 < len(steps) else None
        st.end = min(st.t + HOLD_S, nxt - 0.05) if nxt is not None else st.t + HOLD_S
        st.start = max(st.t - LEAD_S, steps[i - 1].end if i else st.t - LEAD_S)
    return steps


def edit_steps(steps: list[Step], edits: dict[int, dict], cuts: list[tuple[float, float]]) -> list[Step]:
    """Apply a layer's step edits: drop hidden steps and steps inside a cut, replace captions, and
    number what is left from 1."""
    kept = []
    for s in steps:
        e = edits.get(s.id, {})
        if e.get("hidden") or lay.in_cut(s.t, cuts):
            continue
        kept.append(replace(s, n=len(kept) + 1, caption=e.get("caption", s.caption)))
    return _pace(kept)


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
             steps: list[Step], fonts: dict, view: zm.Crop | None = None, keys: bool = True,
             items: list[dict] | None = None) -> Image.Image:
    """Draw every annotation active at time t onto a frame whose top-left is `origin` on screen.

    view: a crop of the frame (frame pixels) to scale up to full size before drawing, so the zoom
    magnifies the app but not the annotations. keys: draw keystroke captions. items: layer items
    showing now, in screen pixels (layer.active); redactions are applied to the pixels before the
    zoom, so no output can show what they cover. Returns the frame with a caption strip of STRIP
    pixels added underneath.
    """
    ox, oy = origin
    items = items or []
    if any(x["kind"] == "redact" for x in items):
        img = lay.redact(img.copy(), items, lambda x, y: (x - ox, y - oy))
    if view is not None:
        img = img.resize(img.size, Image.Resampling.BICUBIC, box=view)
        sx, sy = img.width / (view[2] - view[0]), img.height / (view[3] - view[1])

    def at(x: float, y: float) -> tuple[float, float]:
        """Screen pixels to output pixels."""
        if view is None:
            return x - ox, y - oy
        return (x - ox - view[0]) * sx, (y - oy - view[1]) * sy

    base = img.convert("RGBA")
    layer = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    active = [s for s in steps if s.start <= t < s.end]
    fonts.setdefault("make", _font)
    lay.draw_spotlights(d, base.size, items, at)

    for s in active:
        if s.rect:
            (lf, tp), (r, b) = at(s.rect[0], s.rect[1]), at(s.rect[2], s.rect[3])
            d.rounded_rectangle((lf - 4, tp - 4, r + 4, b + 4), radius=8, outline=ACCENT + (255,), width=4)
            bx, by = max(lf - 16, 4), max(tp - 16, 4)
        elif s.point:
            px, py = at(*s.point)
            bx, by = px + 14, py - 34
        else:
            bx = by = None  # keyboard step with no target: the caption carries it
        if bx is not None:
            d.ellipse((bx, by, bx + 32, by + 32), fill=ACCENT + (255,), outline=(255, 255, 255, 255), width=2)
            d.text((bx + 16, by + 16), str(s.n), font=fonts["badge"], fill=(255, 255, 255, 255), anchor="mm")
        if s.point and 0 <= t - s.t <= RIPPLE_S:
            k = (t - s.t) / RIPPLE_S
            rad = 10 + 30 * k
            px, py = at(*s.point)
            d.ellipse((px - rad, py - rad, px + rad, py + rad), outline=RIPPLE + (int(255 * (1 - k)),), width=4)

    lay.draw(d, items, at, fonts)

    if cursor:
        cx, cy = at(*cursor)
        if -40 < cx < base.width + 40 and -40 < cy < base.height + 40:
            d.ellipse((cx - 22, cy - 22, cx + 22, cy + 22), fill=RIPPLE + (70,))
            d.polygon(_cursor_shape(cx, cy, 1.3), fill=(255, 255, 255, 255), outline=(0, 0, 0, 255))

    if keys and active:
        kc.draw(d, base.size, active[-1], t, fonts)

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
           tight: bool = True, pointer: str = "agent", zoom: str = "auto", keys: bool = True,
           gif: bool = True, narrate: bool = False, voice: str | None = None, engine: nr.Engine | None = None,
           outside_audio: bool = False) -> dict:
    """tight=True cuts the idle time between steps; tight=False keeps real-time pacing. pointer: "agent"
    (the agent's own track), "real" (the recorded cursor) or "none". zoom: "auto" closes in on each
    step's target in the video (stills always show the whole frame), "off" never zooms. keys: show
    hotkeys as keycaps and typed text as it is typed. gif: also write tutorial.gif.

    narrate: speak each step (narrate.py) from the moment its caption appears, hold the video on the
    step until the line ends, and add the voice to tutorial.mp4 as an AAC track, with narration.wav
    and narration.srt beside it. voice: part of a Windows voice's name. engine: another TTS in place
    of the Windows voice (narrate.command; the CLI's --tts-cmd). outside_audio: let the layer's audio
    files live outside the recording directory (a person's CLI render, never an agent's)."""
    if pointer not in POINTERS:
        raise ValueError(f"pointer must be one of {POINTERS}")
    if zoom not in ZOOMS:
        raise ValueError(f"zoom must be one of {ZOOMS}")
    rec = Path(rec_dir)
    out = Path(out_dir or rec / "tutorial")
    out.mkdir(parents=True, exist_ok=True)
    meta = json.loads((rec / "meta.json").read_text(encoding="utf-8"))
    if actions is None:
        actions = json.loads((rec / "actions.json").read_text(encoding="utf-8"))
    layer = lay.Layer.for_recording(rec)
    all_steps = build_steps(actions, meta["start"])
    cuts = layer.cuts()
    steps = edit_steps(all_steps, layer.step_edits(), cuts)
    drawn = layer.drawn()
    track = _frames_track(rec)
    clean_w, clean_h = meta["size"]
    crop = layer.crop((clean_w, clean_h))
    cl, ct = (crop[0], crop[1]) if crop else (0, 0)
    width, height = (crop[2] - crop[0], crop[3] - crop[1]) if crop else (clean_w, clean_h)
    fps = meta["fps"]
    fonts = {"badge": _font(18), "caption": _font(26), "make": _font}
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    for old in out.glob("step-*.png"):
        old.unlink()  # a re-render with hidden or cut steps must not leave their old stills behind
    for old in ("narration.srt", "narration.wav"):
        (out / old).unlink(missing_ok=True)
    lines: dict[int, tuple[str, bytes]] = {}
    if narrate:
        with tempfile.TemporaryDirectory(prefix="overshoulder-say-") as work:
            lines = nr.prepare(steps, layer.step_edits(), Path(work), engine or nr.sapi(voice), rec, outside_audio)

    dec = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(rec / "video.mp4"),
                            "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], stdout=subprocess.PIPE)
    enc = subprocess.Popen([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo", "-pix_fmt",
                            "rgb24", "-s", f"{width}x{height + STRIP}", "-r", str(fps), "-i", "-", "-c:v",
                            "libx264",
                            "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
                            str(out / "tutorial.mp4")], stdin=subprocess.PIPE)
    shots_due = {s.n: still_time(s) for s in steps}
    by_n = {s.n: s for s in steps}
    windows = {s.id: (s.start, s.end) for s in steps}
    saved: dict[int, str] = {}
    frame_bytes = clean_w * clean_h * 3
    last_t = track[-1]["t"] if track else 0.0
    b0 = track[0]["bounds"] if track else [0, 0, clean_w, clean_h]
    home = (b0[0] + cl + width // 2, b0[1] + ct + height // 2)
    extra: dict[int, list] = {}  # a step's own annotations must stay in its zoomed view
    for x in drawn:
        s = next((s for s in steps if s.id == x.get("step")), None)
        if s is not None and x["kind"] not in ("redact", "spotlight"):
            ox, oy = _frame_at(track, s.t)["bounds"][:2]
            b = lay.bbox(x)
            extra.setdefault(s.id, []).append((b[0] + ox, b[1] + oy, b[2] + ox, b[3] + oy))
    camera = zm.plan(steps, (width, height), extra) if zoom == "auto" else []

    def pointer_for(fr: dict, t: float):
        if pointer == "agent":
            return pointer_at(t, steps, home)
        return fr.get("cursor") if pointer == "real" else None

    to_say = [s for s in steps if s.id in lines]
    # A line holds its step on the last frame before the pointer and the camera set off for the next one.
    move_s = max(GLIDE_S, zm.ZOOM_IN_S)
    hold_at = {s.id: max(s.t, min(s.end, nxt.t - move_s)) if nxt else s.end
               for s, nxt in zip(steps, [*steps[1:], None], strict=True)}
    cues: list[tuple[float, float, str]] = []  # output seconds
    placed: list[tuple[float, bytes]] = []
    hold: tuple[float, float] | None = None  # (source time to hold at, output time its line ends)
    last = b""  # the last frame written, repeated while a line finishes
    i = written = held = 0

    def freeze(until: float) -> None:
        nonlocal written, held
        while last and written / fps < until:
            enc.stdin.write(last)
            written += 1
            held += 1

    def speak_before(t: float) -> None:
        """Before writing the frame at source time t: hold the previous step until its line has ended,
        then start the line of every step whose caption shows from t."""
        nonlocal hold
        while True:
            if hold and t >= hold[0]:
                freeze(hold[1])
                hold = None
            if not to_say or to_say[0].start > t:
                return
            s = to_say.pop(0)
            text, clip = lines[s.id]
            at = written / fps
            cues.append((at, at + nr.seconds(clip), text))
            placed.append((at, clip))
            hold = (hold_at[s.id], at + nr.seconds(clip) + nr.GAP_S)

    try:
        while True:
            raw = dec.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            fr = track[min(i, len(track) - 1)] if track else {"t": i / fps, "bounds": [0, 0, clean_w, clean_h],
                                                               "cursor": None}
            t = fr["t"]
            img = Image.frombytes("RGB", (clean_w, clean_h), raw)
            if crop:
                img = img.crop(crop)
            frame_origin = (fr["bounds"][0], fr["bounds"][1])  # where the clean frame's (0, 0) is on screen
            origin = (frame_origin[0] + cl, frame_origin[1] + ct)
            if (not tight or keep_frame(t, steps, last_t)) and not lay.in_cut(t, cuts):
                speak_before(t)
                framed = annotate(img, t, origin, pointer_for(fr, t), steps, fonts,
                                  view=zm.camera_at(t, camera, (width, height), origin), keys=keys,
                                  items=lay.active(drawn, t, windows, frame_origin))
                last = framed.tobytes()
                enc.stdin.write(last)
                written += 1
            for n, due in shots_due.items():
                if n not in saved and t >= due:
                    path = out / f"step-{n:02d}.png"
                    # Each still shows its own step only, even if the video already moved on.
                    solo = replace(by_n[n], start=t - 1, end=t + 1)
                    annotate(img, t, origin, pointer_for(fr, t), [solo], fonts, keys=keys,
                             items=lay.active(drawn, t, windows, frame_origin, still_of=solo.id)).save(path)
                    saved[n] = path.name
            i += 1
        if hold:
            freeze(hold[1])  # the last line may run past the end of the recording
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
    shown = {s.id: s for s in steps}
    (out / "steps.json").write_text(json.dumps(
        [_step_record(s, shown.get(s.id), track, saved) for s in all_steps], indent=1), encoding="utf-8")
    result = {"video": str(out / "tutorial.mp4"), "frames": written, "source_frames": i, "steps": len(steps),
              "images": len(saved), "seconds": round(written / fps, 1), "markdown": str(out / "steps.md"),
              "steps_json": str(out / "steps.json"), "annotations": len(layer.items())}
    if cues:
        nr.write_track(out / "narration.wav", placed, written / fps)
        nr.write_srt(out / "narration.srt", cues)
        nr.mux(out / "tutorial.mp4", out / "narration.wav", out / "tutorial.narrated.mp4")
        os.replace(out / "tutorial.narrated.mp4", out / "tutorial.mp4")
        result.update(narration=str(out / "narration.srt"), narration_audio=str(out / "narration.wav"),
                      lines=len(cues), held_s=round(held / fps, 1))
    (out / "tutorial.gif").unlink(missing_ok=True)
    if gif and written:
        result.update(export_gif(out / "tutorial.mp4", out / "tutorial.gif", fps=min(GIF_FPS, fps)))
    return result


def export_gif(video: Path, gif: Path, width: int = GIF_WIDTH, fps: int = GIF_FPS) -> dict:
    """Write an animated GIF of a video, no wider than `width`, with a palette made for that video."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found on PATH")
    vf = (f"fps={fps},scale='min({width},iw)':-1:flags=lanczos,split[a][b];"
          "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(video), "-vf", vf, "-loop", "0",
                    str(gif)], check=True)
    return {"gif": str(gif), "gif_bytes": gif.stat().st_size}


def render_image(image: Path, out: Path | None = None) -> dict:
    """Apply a screenshot's layer (<name>.annotations.json) and write <name>.annotated.png. Step and
    time fields mean nothing for one image: every drawn item and redaction shows."""
    image = Path(image)
    layer = lay.Layer.for_image(image)
    items = layer.drawn()
    img = lay.redact(Image.open(image).convert("RGB"), items, lambda x, y: (x, y))
    crop = layer.crop(img.size)
    cl, ct = (crop[0], crop[1]) if crop else (0, 0)
    if crop:
        img = img.crop(crop)
    base = img.convert("RGBA")
    over = Image.new("RGBA", base.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(over)

    def at(x: float, y: float) -> tuple[float, float]:
        return x - cl, y - ct

    lay.draw_spotlights(d, base.size, items, at)
    lay.draw(d, items, at, {"make": _font})
    path = Path(out or image.with_name(image.stem + ".annotated.png"))
    Image.alpha_composite(base, over).convert("RGB").save(path)
    return {"image": str(path), "annotations": len(items), "size": list(base.size)}


def _frame_at(track: list[dict], t: float) -> dict:
    return min(track, key=lambda f: abs(f["t"] - t)) if track else {"bounds": [0, 0, 0, 0]}


def _step_record(s: Step, shown: Step | None, track: list[dict], saved: dict[int, str]) -> dict:
    """One step as steps.json lists it, with its target in clean-frame pixels (the layer's
    coordinates) so annotations can be placed against it."""
    ox, oy = _frame_at(track, s.t)["bounds"][:2]
    return {"id": s.id, "n": shown.n if shown else None, "t": s.t, "action": s.action,
            "caption": shown.caption if shown else s.caption, "hidden": shown is None,
            "rect": [s.rect[0] - ox, s.rect[1] - oy, s.rect[2] - ox, s.rect[3] - oy] if s.rect else None,
            "point": [s.point[0] - ox, s.point[1] - oy] if s.point else None,
            "still": saved.get(shown.n) if shown else None}
