"""Write a tutorial as a Shotcut project (.mlt) that a person can open and finish editing.

The project holds the parts render() puts together, so a melt render of it matches tutorial.mp4:

- V1 "Recording": clips of edit/clean.mp4, the recording with its crop and redactions applied but no
  zoom and no annotations, cut where the tutorial cuts, and stills (edit/hold-NN.png) where it holds
  a step until its narration ends. Each clip carries a Size, Position & Rotate filter that follows the
  zoom, with a keyframe on every frame while the camera moves.
- V2 "Annotations": edit/overlay.mov, everything drawn on top (pointer, highlights, badges, keycaps,
  layer items and the caption strip), transparent everywhere else.
- A1 "Narration": narration.wav, when the tutorial is narrated.
- A marker at each step, named after its caption.

Paths are relative to the project file, so the tutorial folder can be moved. The clean recording is
redacted before it is written, so hiding the annotation track never shows what a redaction covers.
A project someone saved in Shotcut is never overwritten: the next render writes tutorial-2.mlt with
its media in edit-2/, and so on (names()).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

Rect = tuple[float, float, float, float]  # x, y, width, height in project pixels
TITLE = "Overshoulder tutorial"  # Shotcut replaces it when it saves, which is how names() spots an edited project


@dataclass
class Segment:
    kind: str  # "clip" (frames of clean.mp4) or "hold" (a still)
    first: int  # clip: first source frame; hold: the source frame the still shows
    length: int = 0
    rects: list[Rect] = field(default_factory=list)  # one per frame
    image: str = ""  # hold: the still, relative to the project


class Timeline:
    """The V1 edit and the markers, built one output frame at a time while render() writes them."""

    def __init__(self, edit: str = "edit") -> None:
        self.edit = edit  # the media folder, relative to the project
        self.segments: list[Segment] = []
        self.markers: list[tuple[int, str]] = []
        self.frames = 0

    def frame(self, src: int, rect: Rect) -> None:
        """The next output frame shows source frame `src`."""
        s = self.segments[-1] if self.segments else None
        if s is not None and s.kind == "clip" and s.first + s.length == src:
            s.length += 1
            s.rects.append(rect)
        else:
            self.segments.append(Segment("clip", src, 1, [rect]))
        self.frames += 1

    def held(self, src: int, rect: Rect) -> Segment | None:
        """The next output frame repeats source frame `src`. Returns the hold when this frame starts a
        new one, so the caller can write its still to `image`."""
        s = self.segments[-1] if self.segments else None
        self.frames += 1
        if s is not None and s.kind == "hold" and s.first == src:
            s.length += 1
            s.rects.append(rect)
            return None
        n = sum(1 for x in self.segments if x.kind == "hold") + 1
        s = Segment("hold", src, 1, [rect], f"{self.edit}/hold-{n:02d}.png")
        self.segments.append(s)
        return s

    def marker(self, frame: int, text: str) -> None:
        self.markers.append((frame, text))


def names(out: Path) -> tuple[Path, str]:
    """The project file and media folder to write in `out`: tutorial.mlt and edit/, unless that project
    was saved by someone else since, then tutorial-2.mlt and edit-2/, and so on."""
    for k in range(1, 1000):
        path = out / ("tutorial.mlt" if k == 1 else f"tutorial-{k}.mlt")
        if not path.exists() or ours(path):
            return path, "edit" if k == 1 else f"edit-{k}"
    raise RuntimeError(f"too many edited projects in {out}")


def ours(path: Path) -> bool:
    """Whether the project still has our title. Reads the opening tag only, without parsing the XML."""
    try:
        with open(path, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace")
    except OSError:
        return False
    m = re.search(r'<mlt\b[^>]*\btitle="([^"]*)"', head)
    return m is not None and m.group(1) == TITLE


def zoom_rect(view: tuple[float, float, float, float] | None, size: tuple[int, int], full_h: int) -> Rect:
    """Where a clean frame goes so the zoom's crop `view` (frame pixels, render.annotate's `view`) fills
    the picture above the caption strip. The clean frame is `full_h` tall: the strip is part of it."""
    w, _ = size
    if view is None:
        return 0.0, 0.0, float(w), float(full_h)
    s = w / (view[2] - view[0])
    return -view[0] * s, -view[1] * s, w * s, full_h * s


def keyframes(rects: list[Rect]) -> str:
    """An MLT animation string with a key wherever the rect changes, and at both ends; linear in
    between, which is exact because a moving camera gets a key on every frame."""
    keys = []
    for i, r in enumerate(rects):
        if i in (0, len(rects) - 1) or r != rects[i - 1] or r != rects[i + 1]:
            keys.append(f"{i}=" + " ".join(_num(v) for v in r) + " 1")
    return ";".join(keys)


def _num(v: float) -> str:
    text = f"{v:.3f}".rstrip("0").rstrip(".")
    return "0" if text == "-0" else text


def clock(frame: int, fps: int) -> str:
    ms = round(frame * 1000 / fps)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}"


def _props(el: ET.Element, values: dict) -> ET.Element:
    for k, v in values.items():
        ET.SubElement(el, "property", name=k).text = str(v)
    return el


def _producer(root: ET.Element, pid: str, resource: str, service: str, length: int, first: int, last: int,
              caption: str) -> ET.Element:
    p = ET.SubElement(root, "producer", id=pid, attrib={"in": str(first), "out": str(last)})
    _props(p, {"length": length, "eof": "pause", "resource": resource, "mlt_service": service,
               "shotcut:caption": caption})
    if service.startswith("avformat"):
        _props(p, {"seekable": 1, "shotcut:skipConvert": 1})
    return p


def write(path: Path, *, size: tuple[int, int], fps: int, timeline: Timeline, clean_frames: int,
          narration: bool) -> Path:
    """Write the project. The media must already be in place next to it: <edit>/clean.mp4
    (clean_frames long), <edit>/overlay.mov (one frame per output frame), the holds' stills and, if
    narration, narration.wav."""
    w, h = size
    frames = timeline.frames
    edit = timeline.edit
    mlt = ET.Element("mlt", LC_NUMERIC="C", version="7.0.0", title=TITLE, producer="tractor0")
    ET.SubElement(mlt, "profile", description=f"{w}x{h} {fps} fps", width=str(w), height=str(h),
                  progressive="1", sample_aspect_num="1", sample_aspect_den="1", display_aspect_num=str(w),
                  display_aspect_den=str(h), frame_rate_num=str(fps), frame_rate_den="1", colorspace="709")
    black = ET.SubElement(mlt, "producer", id="black", attrib={"in": "0", "out": str(frames - 1)})
    _props(black, {"length": frames, "eof": "pause", "resource": 0, "mlt_service": "color",
                   "mlt_image_format": "rgba", "set.test_audio": 0})

    entries = []
    for i, s in enumerate(timeline.segments):
        if s.kind == "clip":
            first, last = s.first, s.first + s.length - 1
            p = _producer(mlt, f"clip{i}", f"{edit}/clean.mp4", "avformat-novalidate", clean_frames, 0,
                          clean_frames - 1, "clean.mp4")
        else:
            first, last = 0, s.length - 1
            p = _producer(mlt, f"hold{i}", s.image, "qimage", s.length, first, last, Path(s.image).name)
        full = (0.0, 0.0, float(w), float(h))
        if any(r != full for r in s.rects):
            # Like Shotcut: the producer spans the whole file, the filter spans the clip, and the keys
            # count from the clip's start. MLT checks a filter's in/out against frames counted from the
            # producer's in point but keys it from the frame's own number, so only this layout agrees.
            f = ET.SubElement(p, "filter", id=f"zoom{i}", attrib={"in": str(first), "out": str(last)})
            _props(f, {"mlt_service": "affine", "shotcut:filter": "affineSizePosition",
                       "background": "color:#00000000", "transition.threads": 0, "transition.fill": 1,
                       "transition.distort": 0, "transition.rect": keyframes(s.rects)})
        entries.append((p.get("id"), first, last))
    _producer(mlt, "overlay", f"{edit}/overlay.mov", "avformat-novalidate", frames, 0, frames - 1, "overlay.mov")
    if narration:
        _producer(mlt, "narration", "narration.wav", "avformat-novalidate", frames, 0, frames - 1, "narration.wav")

    bg = ET.SubElement(mlt, "playlist", id="background")
    ET.SubElement(bg, "entry", producer="black", attrib={"in": "0", "out": str(frames - 1)})
    v1 = _props(ET.SubElement(mlt, "playlist", id="recording"), {"shotcut:video": 1, "shotcut:name": "Recording"})
    for pid, first, last in entries:
        ET.SubElement(v1, "entry", producer=pid, attrib={"in": str(first), "out": str(last)})
    v2 = _props(ET.SubElement(mlt, "playlist", id="annotations"),
                {"shotcut:video": 1, "shotcut:name": "Annotations"})
    ET.SubElement(v2, "entry", producer="overlay", attrib={"in": "0", "out": str(frames - 1)})
    tracks = ["background", "recording", "annotations"]
    if narration:
        a1 = _props(ET.SubElement(mlt, "playlist", id="narration_track"),
                    {"shotcut:audio": 1, "shotcut:name": "Narration", "hide": 1})
        ET.SubElement(a1, "entry", producer="narration", attrib={"in": "0", "out": str(frames - 1)})
        tracks.append("narration_track")

    tractor = ET.SubElement(mlt, "tractor", id="tractor0", attrib={"in": "0", "out": str(frames - 1)})
    _props(tractor, {"shotcut": 1, "shotcut:projectAudioChannels": 2})
    marks = ET.SubElement(tractor, "properties", name="shotcut:markers")
    for n, (frame, text) in enumerate(timeline.markers):
        m = ET.SubElement(marks, "properties", name=str(n))
        _props(m, {"text": text, "start": clock(frame, fps), "end": clock(frame, fps), "color": "#2563EB"})
    for t in tracks:
        ET.SubElement(tractor, "track", producer=t)
    for b in range(1, len(tracks)):
        _props(ET.SubElement(tractor, "transition", id=f"mix{b}"),
               {"a_track": 0, "b_track": b, "always_active": 1, "sum": 1, "mlt_service": "mix"})
    # V1 is opaque, so it needs no blend (Shotcut disables it too); the annotations go over it.
    _props(ET.SubElement(tractor, "transition", id="blend1"),
           {"a_track": 0, "b_track": 1, "threads": 0, "mlt_service": "qtblend", "disable": 1})
    _props(ET.SubElement(tractor, "transition", id="blend2"),
           {"a_track": 1, "b_track": 2, "threads": 0, "mlt_service": "qtblend", "disable": 0})

    ET.indent(mlt)
    path = Path(path)
    path.write_bytes(b"<?xml version='1.0' encoding='utf-8'?>\n" + ET.tostring(mlt, encoding="utf-8"))
    return path
