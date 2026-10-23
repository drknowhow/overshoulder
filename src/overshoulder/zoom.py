"""Zoom a tutorial to each step's target and back out, so small controls are readable.

The camera is a crop of the frame, scaled back up to full size. It shows the whole frame until the
agent's pointer heads for a step's target, closes in on the target while the pointer glides there
(ZOOM_IN_S, arriving as the step happens), holds through the step and opens back out afterwards
(ZOOM_OUT_S). Steps close together move straight from one target to the next. A step with no target
of its own, such as typing or a hotkey, keeps the previous step's view when it follows closely, so
typing stays zoomed on the field it goes into.

Views are kept in screen pixels and turned into frame pixels per frame, so a window that moves during
the recording is followed.
"""

from __future__ import annotations

from dataclasses import dataclass

ZOOM_MAX = 2.0  # never magnify more than this
MIN_ZOOM = 1.25  # a smaller zoom is not worth the motion: show the whole frame
FILL = 0.45  # the target takes at most this share of the view's width and height
POINT_BOX = 80  # a point target counts as a box this many pixels across
ZOOM_IN_S, ZOOM_OUT_S = 0.6, 0.4  # ZOOM_OUT_S fits inside render.PAD_AFTER, so tight cuts land zoomed out
FULL_MIN_S = 0.5  # open out between two steps only if the whole frame would then show this long
FOLLOW_S = 1.0  # a step without a target keeps the previous view if it starts this soon after it

Crop = tuple[float, float, float, float]  # left, top, right, bottom in frame pixels


@dataclass(frozen=True)
class View:
    cx: float  # centre, screen pixels
    cy: float
    w: float  # crop width in frame pixels; the height follows the frame's aspect


def view_for(step, size: tuple[int, int]) -> View | None:
    """The view that frames a step's target, or None when the target is too big to be worth a zoom."""
    width, height = size
    if step.rect:
        left, top, right, bottom = step.rect
    elif step.point:
        x, y = step.point
        left, top, right, bottom = x - POINT_BOX / 2, y - POINT_BOX / 2, x + POINT_BOX / 2, y + POINT_BOX / 2
    else:
        return None
    w = max((right - left) / FILL, (bottom - top) / FILL * width / height, width / ZOOM_MAX)
    if width / w < MIN_ZOOM:
        return None
    return View((left + right) / 2, (top + bottom) / 2, w)


def plan(steps: list, size: tuple[int, int]) -> list[tuple[float, View | None]]:
    """Camera keyframes (time, view), times in seconds from the recording start; None is the whole
    frame. Between two keyframes the camera eases from one to the other."""
    keys: list[tuple[float, View | None]] = []
    cur: View | None = None  # what the camera holds after the last keyframe
    prev = None
    for s in steps:
        if s.rect or s.point:
            v = view_for(s, size)
        elif prev is not None and s.t - prev.end <= FOLLOW_S:
            v = cur
        else:
            v = None
        if v != cur:
            arrive_from = s.t - (ZOOM_IN_S if v is not None else 0)
            if prev is not None and cur is not None and arrive_from - (prev.end + ZOOM_OUT_S) >= FULL_MIN_S:
                # Far apart: open out to the whole frame, then close in on the next target.
                keys += [(prev.end, cur), (prev.end + ZOOM_OUT_S, None)]
                if v is not None:
                    keys += [(s.t - ZOOM_IN_S, None), (s.t, v)]
            else:
                # From the whole frame, or close together: move straight to the new view.
                leave = s.t - ZOOM_IN_S if prev is None else max(prev.t, s.t - ZOOM_IN_S)
                keys += [(leave, cur), (s.t, v)]
            cur = v
        prev = s
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
