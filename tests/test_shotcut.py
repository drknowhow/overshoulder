import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import render as R  # noqa: E402
from overshoulder import shotcut as sc  # noqa: E402
from overshoulder.layer import Layer  # noqa: E402
from tests.rechelpers import HAVE_FFMPEG, make_recording, red_pixels, video_frames  # noqa: E402
from tests.test_narrate import FakeVoice  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG or not shutil.which("ffprobe"), reason="needs ffmpeg and ffprobe")
def _mlt_melt() -> str | None:
    """MLT's melt, not another program of the same name (GitHub's Windows runners put WiX's melt on PATH)."""
    for path in (os.environ.get("OVERSHOULDER_MELT"), shutil.which("melt"),
                 str(Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Shotcut" / "melt.exe")):
        if path and Path(path).is_file():
            try:
                out = subprocess.run([path, "-version"], capture_output=True, text=True, timeout=20)
            except (OSError, subprocess.SubprocessError):
                continue
            if "mltframework" in out.stdout + out.stderr:
                return path
    return None


MELT = _mlt_melt()
needs_melt = pytest.mark.skipif(not HAVE_FFMPEG or MELT is None, reason="needs MLT's melt (Shotcut)")
FULL = (0.0, 0.0, 320.0, 312.0)


def probe(path, entries):
    out = subprocess.run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0", "-show_entries",
                          f"stream={entries}", "-of", "json", str(path)], capture_output=True, text=True, check=True)
    return json.loads(out.stdout)["streams"][0]


def props(el):
    return {p.get("name"): p.text for p in el.findall("property")}


# -- the timeline ------------------------------------------------------------------------------------

def test_consecutive_frames_make_one_clip_and_a_jump_starts_the_next():
    t = sc.Timeline()
    for i in (0, 1, 2, 7, 8):
        t.frame(i, FULL)
    assert [(s.kind, s.first, s.length) for s in t.segments] == [("clip", 0, 3), ("clip", 7, 2)]
    assert t.frames == 5


def test_holding_a_frame_makes_one_still_per_hold():
    t = sc.Timeline("edit-2")
    t.frame(4, FULL)
    first = t.held(4, FULL)
    assert first is not None and first.image == "edit-2/hold-01.png"
    assert t.held(4, FULL) is None  # the same hold goes on
    t.frame(5, FULL)
    assert t.held(5, FULL).image == "edit-2/hold-02.png"
    assert [(s.kind, s.length) for s in t.segments] == [("clip", 1), ("hold", 2), ("clip", 1), ("hold", 1)]
    assert t.frames == 5


def test_the_zoom_rect_puts_the_crop_over_the_whole_picture():
    assert sc.zoom_rect(None, (320, 240), 312) == FULL
    x, y, w, h = sc.zoom_rect((80.0, 60.0, 240.0, 180.0), (320, 240), 312)  # the middle half, so 2x
    assert (x, y, w, h) == (-160.0, -120.0, 640.0, 624.0)


def test_keyframes_mark_every_moving_frame_and_only_the_ends_of_a_still_run():
    still, moving = FULL, [(-float(i), 0.0, 320.0 + i, 312.0) for i in range(1, 4)]
    keys = sc.keyframes([still, still, still, *moving, moving[-1], moving[-1]])
    assert [k.split("=")[0] for k in keys.split(";")] == ["0", "2", "3", "4", "5", "7"]
    assert keys.startswith("0=0 0 320 312 1;") and "-0 " not in keys


def test_marker_times_are_clock_times():
    assert sc.clock(0, 10) == "00:00:00.000"
    assert sc.clock(37, 30) == "00:00:01.233"
    assert sc.clock(36005, 10) == "01:00:00.500"


# -- the project file --------------------------------------------------------------------------------

def _project(tmp_path, narration=True):
    t = sc.Timeline()
    for i in range(5):
        t.frame(i, FULL)
    t.marker(2, "1. Click Go")
    zoomed = [(-float(i), -float(i), 320.0 + 2 * i, 312.0 + 2 * i) for i in range(4)]
    for i, r in zip(range(10, 14), zoomed, strict=True):
        t.frame(i, r)
    t.held(13, zoomed[-1])
    t.held(13, zoomed[-1])
    return sc.write(tmp_path / "tutorial.mlt", size=(320, 312), fps=10, timeline=t, clean_frames=40,
                    narration=narration)


def test_the_project_has_the_recording_the_annotations_and_the_narration(tmp_path):
    root = ET.parse(_project(tmp_path)).getroot()
    assert root.get("title") == sc.TITLE and root.get("root") is None  # paths resolve next to the file
    tracks = {props(p).get("shotcut:name"): p for p in root.findall("playlist")}
    assert set(tracks) == {None, "Recording", "Annotations", "Narration"}
    rec = [(e.get("producer"), e.get("in"), e.get("out")) for e in tracks["Recording"].findall("entry")]
    assert rec == [("clip0", "0", "4"), ("clip1", "10", "13"), ("hold2", "0", "1")]
    assert props(tracks["Narration"])["hide"] == "1"
    resources = [props(p)["resource"] for p in root.findall("producer")]
    assert resources[1:] == ["edit/clean.mp4", "edit/clean.mp4", "edit/hold-01.png", "edit/overlay.mov",
                             "narration.wav"]
    assert all(":" not in r and not r.startswith("/") for r in resources[1:])
    marks = root.find("tractor/properties[@name='shotcut:markers']")
    assert [props(m) for m in marks] == [{"text": "1. Click Go", "start": "00:00:00.200", "end": "00:00:00.200",
                                          "color": "#2563EB"}]


def test_a_zoomed_clip_keys_its_filter_from_the_clip_start_inside_a_whole_file_producer(tmp_path):
    root = ET.parse(_project(tmp_path)).getroot()
    prods = {p.get("id"): p for p in root.findall("producer")}
    assert prods["clip0"].find("filter") is None  # never zoomed: no filter to clutter the clip
    clip = prods["clip1"]
    assert (clip.get("in"), clip.get("out")) == ("0", "39")  # the whole clean file, like Shotcut
    f = clip.find("filter")
    assert (f.get("in"), f.get("out")) == ("10", "13")
    p = props(f)
    assert p["mlt_service"] == "affine" and p["shotcut:filter"] == "affineSizePosition"
    assert p["transition.rect"].split(";")[0] == "0=0 0 320 312 1"
    assert p["transition.rect"].split(";")[-1] == "3=-3 -3 326 318 1"
    hold = prods["hold2"]
    assert props(hold)["mlt_service"] == "qimage" and props(hold.find("filter"))["transition.rect"] == \
        "0=-3 -3 326 318 1;1=-3 -3 326 318 1"


def test_a_silent_tutorial_has_no_narration_track(tmp_path):
    root = ET.parse(_project(tmp_path, narration=False)).getroot()
    names = [props(p).get("shotcut:name") for p in root.findall("playlist")]
    assert "Narration" not in names and len(root.findall("tractor/track")) == 3


def test_a_project_saved_in_shotcut_is_kept_and_the_next_render_gets_new_names(tmp_path):
    assert sc.names(tmp_path) == (tmp_path / "tutorial.mlt", "edit")
    _project(tmp_path)
    assert sc.names(tmp_path) == (tmp_path / "tutorial.mlt", "edit")  # ours: written over
    edited = tmp_path / "tutorial.mlt"
    edited.write_text(edited.read_text(encoding="utf-8").replace(sc.TITLE, "Shotcut version 26.8.1"),
                      encoding="utf-8")
    assert sc.names(tmp_path) == (tmp_path / "tutorial-2.mlt", "edit-2")


# -- rendering ---------------------------------------------------------------------------------------

@needs_ffmpeg
def test_render_writes_the_project_and_media_that_match_the_tutorial(tmp_path):
    rec = make_recording(tmp_path)
    Layer.for_recording(rec).add({"kind": "redact", "rect": [20, 20, 120, 60], "style": "solid"})
    out = R.render(rec, gif=False, narrate=True, engine=FakeVoice(1.5), mlt=True)
    tut = rec / "tutorial"
    assert out["project"] == str(tut / "tutorial.mlt") and out["project_media"] == str(tut / "edit")
    clean, over = tut / "edit" / "clean.mp4", tut / "edit" / "overlay.mov"
    assert int(probe(clean, "nb_read_frames")["nb_read_frames"]) == 40  # every source frame
    o = probe(over, "nb_read_frames,pix_fmt,width,height")
    assert o["pix_fmt"] == "argb" and int(o["nb_read_frames"]) == out["frames"]
    assert (o["width"], o["height"]) == (320, 240 + R.STRIP)
    assert out["held_s"] > 0 and sorted(p.name for p in (tut / "edit").glob("hold-*.png"))
    root = ET.parse(tut / "tutorial.mlt").getroot()
    used = [props(p)["resource"] for p in root.findall("producer")][1:]
    assert all((tut / r).is_file() for r in used)
    entries = root.find("playlist[@id='recording']").findall("entry")
    assert sum(int(e.get("out")) - int(e.get("in")) + 1 for e in entries) == out["frames"]


@needs_ffmpeg
def test_the_clean_track_is_redacted_so_hiding_the_annotations_shows_no_secret(tmp_path):
    rec = make_recording(tmp_path)
    Layer.for_recording(rec).add({"kind": "redact", "rect": [20, 20, 120, 60], "style": "solid"})
    R.render(rec, gif=False, mlt=True)
    frames = video_frames(rec / "tutorial" / "edit" / "clean.mp4", (320, 240 + R.STRIP))
    assert frames and all(red_pixels(f.crop((20, 20, 120, 60))) == 0 for f in frames)
    assert all(red_pixels(p) == 0 for p in (rec / "tutorial" / "edit").glob("hold-*.png"))


@needs_ffmpeg
def test_a_render_after_the_person_saved_in_shotcut_leaves_their_project_alone(tmp_path):
    rec = make_recording(tmp_path)
    R.render(rec, gif=False, mlt=True)
    edited = rec / "tutorial" / "tutorial.mlt"
    text = edited.read_text(encoding="utf-8").replace(sc.TITLE, "Shotcut version 26.8.1")
    edited.write_text(text, encoding="utf-8")
    before = (rec / "tutorial" / "edit" / "overlay.mov").read_bytes()
    out = R.render(rec, gif=False, mlt=True, zoom="off")
    assert out["project"].endswith("tutorial-2.mlt") and out["project_media"].endswith("edit-2")
    assert edited.read_text(encoding="utf-8") == text
    assert (rec / "tutorial" / "edit" / "overlay.mov").read_bytes() == before


@needs_melt
def test_melt_renders_the_project_like_the_tutorial(tmp_path):
    """Every frame, not the average: an average SSIM over 0.95 once passed a render that never zoomed."""
    rec = make_recording(tmp_path, seconds=6.0)
    Layer.for_recording(rec).add({"kind": "redact", "rect": [20, 20, 120, 60], "style": "blur"})
    out = R.render(rec, gif=False, narrate=True, engine=FakeVoice(2.0), mlt=True)
    melt = tmp_path / "melt.mp4"
    subprocess.run([MELT, "-silent", out["project"], "-consumer", f"avformat:{melt}", "vcodec=libx264", "crf=12",
                    "pix_fmt=yuv420p", "acodec=aac"], capture_output=True, check=True, cwd=tmp_path.anchor)
    size = (320, 240 + R.STRIP)
    ours, theirs = video_frames(Path(out["video"]), size), video_frames(melt, size)
    assert len(ours) == len(theirs) == out["frames"]
    stats = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(melt), "-i", out["video"], "-lavfi",
                            "[0:v][1:v]psnr=stats_file=-", "-f", "null", "-"], capture_output=True, text=True,
                           check=True).stdout
    psnr = [float(x.split(":")[1]) for line in stats.splitlines() for x in line.split() if x.startswith("psnr_avg:")]
    assert len(psnr) == out["frames"] and min(psnr) > 30, min(psnr)
    audio = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "a", "-show_entries", "stream=codec_name",
                            "-of", "csv=p=0", str(melt)], capture_output=True, text=True).stdout.strip()
    assert audio == "aac"
