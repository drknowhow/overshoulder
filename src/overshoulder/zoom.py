"""Zoom a tutorial where it makes controls readable, and otherwise keep the camera still.

The camera is a crop of the frame, scaled back up to full size. The steps are grouped into shots, each
shown in one view, and the camera moves only between shots. A step stays in the shot before it when it
follows closely and that view already shows it: its target is inside a zoomed view, or the view is the
whole frame and the target is not a control to read. Zooming is for reading controls, so only a step
aimed at an element or at found text closes in by itself. A step aimed at bare x/y coordinates, which
is how agents work on a canvas, is shown in the view it finds, so strokes on a canvas are drawn without
the camera moving. A step with no target, such as typing or a hotkey, stays in the shot before it, so
typing stays zoomed on the field it goes into.

Two rules keep a short visit elsewhere (a shot shown less than MIN_SHOT_S, such as picking a colour
between strokes) from moving the camera twice: the step after it goes back to the view before it when
that view shows it, and a short zoom between two whole-frame shots is not made at all.

The camera closes in on a shot while the agent's pointer glides to its first target (ZOOM_IN_S,
arriving as the step happens) and moves straight from one shot to the next when they are close. It
opens out to the whole frame between two zoomed shots only for a pause the viewer actually sits
through: "close" is measured in the time the video shows (`seen`), since tight timing cuts the idle time
between an agent's actions.

Views are kept in screen pixels and turned into frame pixels per frame, so a window that moves during
the recording is followed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

ZOOM_MAX = 2.0  # never magnify more than this
MIN_ZOOM = 1.25  # a smaller zoom is not worth the motion: show the whole frame
FILL = 0.45  # the target takes at most this share of the view's width and height
POINT_BOX = 80  # a point target counts as a box this many pixels across
ZOOM_IN_S, ZOOM_OUT_S = 0.6, 0.4  # ZOOM_OUT_S fits inside render.PAD_AFTER, so tight cuts land zoomed out
FULL_MIN_S = 0.5  # open out between two zoomed shots only if the whole frame would then show this long
RUN_GAP_S = 5.0  # steps this close in the video are one stretch of work: the camera never opens out between them
MIN_SHOT_S = 3.0  # a shot shown for less than this is a short visit (one click shows for about render.HOLD_S)

Crop = tuple[float, float, float, float]  # left, top, right, bottom in frame pixels
Seen = Callable[[float, float], float]  # seconds of source time a..b that the video shows


@dataclass(frozen=True)
class View:
    cx: float  # centre, screen pixels
    cy: float
    w: float  # crop width in frame pixels; the height follows the frame's aspect


@dataclass
class Shot:
    steps: list
    view: View | None  # None: the whole frame

    @property
    def t(self) -> float:
        return self.steps[0].t

    @property
    def end(self) -> float:
        return self.steps[-1].end


def _boxes(step, extra: list | None) -> list:
    boxes = list(extra or [])
    if step.rect:
        boxes.append(step.rect)
    elif step.point:
        x, y = step.point
        boxes.append((x - POINT_BOX / 2, y - POINT_BOX / 2, x + POINT_BOX / 2, y + POINT_BOX / 2))
    return boxes


def view_of(boxes: list, size: tuple[int, int]) -> View | None:
    """The view that frames all of `boxes` (screen pixels), or None when that is too big to be worth a
    zoom."""
    if not boxes:
        return None
    width, height = size
    left, top = min(b[0] for b in boxes), min(b[1] for b in boxes)
    right, bottom = max(b[2] for b in boxes), max(b[3] for b in boxes)
    w = max((right - left) / FILL, (bottom - top) / FILL * width / height, width / ZOOM_MAX)
    if width / w < MIN_ZOOM:
        return None
    return View((left + right) / 2, (top + bottom) / 2, w)


def view_for(step, size: tuple[int, int], extra: list | None = None) -> View | None:
    """The view that frames a step's target and any `extra` rects (screen pixels, such as the step's
    own annotations), or None when that is too big to be worth a zoom."""
    return view_of(_boxes(step, extra), size)


def _own(step, boxes: list, size: tuple[int, int]) -> View | None:
    """The view a step closes in on by itself: only a control to read (an element or found text, which
    have a rect) gets one."""
    return view_of(boxes, size) if step.rect else None


def _inside(boxes: list, v: View, size: tuple[int, int]) -> bool:
    """Whether every box is inside the view. The view is taken before it is kept inside the frame, so
    near the frame's edge this can say no when the shown crop would hold the box."""
    width, height = size
    h = v.w * height / width
    left, top, right, bottom = v.cx - v.w / 2, v.cy - h / 2, v.cx + v.w / 2, v.cy + h / 2
    return all(left <= b[0] and top <= b[1] and b[2] <= right and b[3] <= bottom for b in boxes)


def _shows(v: View | None, step, boxes: list, size: tuple[int, int]) -> bool:
    """Whether view v shows a step well enough to keep it."""
    if not boxes:
        return True
    if v is None:
        return _own(step, boxes, size) is None
    return _inside(boxes, v, size)


def _real_time(a: float, b: float) -> float:
    return max(0.0, b - a)


def shots(steps: list, size: tuple[int, int], extra: dict | None = None, seen: Seen | None = None) -> list[Shot]:
    """The steps grouped into shots, each shown in one view (see the module docstring)."""
    seen = seen or _real_time
    extra = extra or {}

    def close(a: Shot, t: float) -> bool:
        return seen(a.end, t) <= RUN_GAP_S

    def short(a: Shot) -> bool:
        return seen(a.t, a.end) < MIN_SHOT_S

    out: list[Shot] = []
    for s in steps:
        boxes = _boxes(s, extra.get(s.id))
        last = out[-1] if out else None
        if last is not None and close(last, s.t):
            if _shows(last.view, s, boxes, size):
                last.steps.append(s)
                continue
            before = out[-2] if len(out) > 1 else None
            if (before is not None and close(before, last.t) and short(last)
                    and _shows(before.view, s, boxes, size)):
                out.append(Shot([s], before.view))  # back from a short visit to the view it left
                continue
        out.append(Shot([s], _own(s, boxes, size)))
    for a, b, c in zip(out, out[1:], out[2:], strict=False):
        if (b.view is not None and a.view is None and c.view is None and close(a, b.t) and close(b, c.t)
                and short(b)):
            b.view = None
    return out


def plan(steps: list, size: tuple[int, int], extra: dict | None = None,
         seen: Seen | None = None) -> list[tuple[float, View | None]]:
    """Camera keyframes (time, view), times in seconds from the recording start; None is the whole
    frame. Between two keyframes the camera eases from one to the other. extra maps a step id to more
    rects its view must show. seen(a, b) says how much of a..b the video shows (default: all of it)."""
    seen = seen or _real_time
    keys: list[tuple[float, View | None]] = []
    cur: View | None = None  # what the camera holds after the last keyframe
    prev: Shot | None = None
    for sh in shots(steps, size, extra, seen):
        v = sh.view
        if v != cur:
            arrive_from = sh.t - (ZOOM_IN_S if v is not None else 0)
            if (prev is not None and cur is not None and seen(prev.end, sh.t) > RUN_GAP_S
                    and seen(prev.end + ZOOM_OUT_S, arrive_from) >= FULL_MIN_S):
                # A pause the viewer sits through: open out to the whole frame, then close in on the next shot.
                keys += [(prev.end, cur), (prev.end + ZOOM_OUT_S, None)]
                if v is not None:
                    keys += [(sh.t - ZOOM_IN_S, None), (sh.t, v)]
            else:
                # From the whole frame, or close together: move straight to the new view.
                leave = sh.t - ZOOM_IN_S if prev is None else max(prev.steps[-1].t, sh.t - ZOOM_IN_S)
                keys += [(leave, cur), (sh.t, v)]
            cur = v
        prev = sh
    if cur is not None and prev is not None:
        keys += [(prev.end, cur), (prev.end + ZOOM_OUT_S, None)]
    return keys


def _crop(v: View | None, size: tuple[int, int], origin: tuple[int, int]) -> Crop:
    width, height = size
    if v is None:
        return 0.0, 0.0, float(width), float(height)
    w = min(v.w, width)
    h = w * height / width
    left = min(max(v.cx - origin[0] - w / 2, 0.0), width - w)
    top = min(max(v.cy - origin[1] - h / 2, 0.0), height - h)
    return left, top, left + w, top + h


def camera_at(t: float, keys: list[tuple[float, View | None]], size: tuple[int, int],
              origin: tuple[int, int]) -> Crop | None:
    """The crop at time t for a frame whose top-left is `origin` on screen; None for the whole frame."""
    if not keys:
        return None
    if t <= keys[0][0]:
        a = b = keys[0][1]
        k = 0.0
    elif t >= keys[-1][0]:
        a = b = keys[-1][1]
        k = 0.0
    else:
        i = max(j for j, (kt, _) in enumerate(keys) if kt <= t)
        (t0, a), (t1, b) = keys[i], keys[i + 1]
        k = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
        k = k * k * (3 - 2 * k)  # ease in and out
    if a is None and b is None:
        return None
    ca, cb = _crop(a, size, origin), _crop(b, size, origin)
    crop = tuple(p + (q - p) * k for p, q in zip(ca, cb, strict=True))
    full = (0.0, 0.0, float(size[0]), float(size[1]))
    if all(abs(p - q) < 0.5 for p, q in zip(crop, full, strict=True)):
        return None
    return crop
