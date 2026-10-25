"""Play routine stretches of a tutorial faster, so viewers spend their time on the steps that teach.

A span of the recording plays `factor` times faster: the render keeps every factor-th frame there and
shows a fast-forward chip in the caption strip. Spans come from two places:

- The layer. A speed item {t0, t1, factor?} or {step, factor?} speeds that part up; factor 1 keeps it at
  normal speed, which also stops the automatic speed-up there. Layer items win over automatic spans,
  and a later item wins over an earlier one.
- The render's speed="auto". A run of RUN_MIN or more alike steps in a row (the same action and caption,
  such as strokes on a canvas or clicks on the same button) plays its first step at normal speed, or its
  first two the first time that caption runs, and the rest AUTO_FACTOR times faster. A step the layer
  singles out (a drawn item, a new caption, narration text or audio) or that has a spoken line breaks a
  run: it always plays at normal speed.

Frames are kept where their index is a multiple of the factor, so a sped-up clip in the Shotcut project
starts on a whole frame of the sped-up source (shotcut.py).
"""

from __future__ import annotations

from dataclasses import dataclass, replace

AUTO_FACTOR = 4  # also the factor of a speed item that names none (the largest is layer.MAX_FACTOR)
RUN_MIN = 4  # alike steps in a row before the rest of the run plays fast


@dataclass(frozen=True)
class Span:
    t0: float  # seconds from the recording start
    t1: float
    factor: int
    label: str | None = None  # the caption strip's text while it plays, e.g. "8–11. Drag along the arrow"
    auto: bool = False


def auto_spans(steps: list, pinned: set[int]) -> list[Span]:
    """The fast part of every run of alike steps (see the module docstring). pinned: ids of steps that
    must play at normal speed."""
    out: list[Span] = []
    ran: set[tuple[str, str]] = set()
    i = 0
    while i < len(steps):
        j = i
        if steps[i].id not in pinned:
            while (j + 1 < len(steps) and steps[j + 1].id not in pinned
                   and (steps[j + 1].action, steps[j + 1].caption) == (steps[i].action, steps[i].caption)):
                j += 1
        run = steps[i:j + 1]
        if len(run) >= RUN_MIN:
            key = (run[0].action, run[0].caption)
            fast = run[1 if key in ran else 2:]
            ran.add(key)
            out.append(Span(fast[0].start, fast[-1].end, AUTO_FACTOR,
                            f"{fast[0].n}–{fast[-1].n}. {run[0].caption}", auto=True))
        i = j + 1
    return out


def layer_spans(items: list[dict], steps: list) -> list[Span]:
    """The layer's speed items as spans, in file order. A step item covers the time its step shows; one
    whose step is hidden or cut covers nothing."""
    by_id = {s.id: s for s in steps}
    out = []
    for x in items:
        if x["kind"] != "speed":
            continue
        f = x.get("factor", AUTO_FACTOR)
        if "step" in x:
            s = by_id.get(x["step"])
            if s is not None:
                out.append(Span(s.start, s.end, f))
        else:
            out.append(Span(x["t0"], x["t1"], f))
    return out


def _minus(s: Span, others: list[Span]) -> list[Span]:
    """The parts of s that none of `others` covers."""
    parts = [(s.t0, s.t1)]
    for o in others:
        left = []
        for a, b in parts:
            if o.t1 <= a or o.t0 >= b:
                left.append((a, b))
                continue
            if a < o.t0:
                left.append((a, o.t0))
            if o.t1 < b:
                left.append((o.t1, b))
        parts = left
    return [replace(s, t0=a, t1=b) for a, b in parts if b > a]


def plan(steps: list, items: list[dict], auto: bool, pinned: set[int]) -> list[Span]:
    """The spans that play faster, in time order and never overlapping."""
    order = list(reversed(layer_spans(items, steps)))  # highest priority first
    if auto:
        order += auto_spans(steps, pinned)
    placed: list[Span] = []
    for s in order:
        placed += _minus(s, placed)
    return sorted((s for s in placed if s.factor > 1), key=lambda s: s.t0)


def span_at(t: float, spans: list[Span]) -> Span | None:
    return next((s for s in spans if s.t0 <= t < s.t1), None)


def factor_at(t: float, spans: list[Span]) -> int:
    s = span_at(t, spans)
    return s.factor if s else 1


def keep(i: int, t: float, spans: list[Span]) -> bool:
    """Whether source frame i, at t seconds, is shown: in a span, only every factor-th frame is."""
    return i % factor_at(t, spans) == 0


def saved(a: float, b: float, spans: list[Span]) -> float:
    """Seconds of a..b that speeding up takes out of the video."""
    return sum(max(0.0, min(b, s.t1) - max(a, s.t0)) * (1 - 1 / s.factor) for s in spans)
