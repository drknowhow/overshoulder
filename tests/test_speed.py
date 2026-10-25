import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import render as R  # noqa: E402
from overshoulder import shotcut as sc  # noqa: E402
from overshoulder import speed as S  # noqa: E402
from overshoulder.layer import Layer, LayerError, validate  # noqa: E402
from tests.rechelpers import ORIGIN, make_recording, video_frames  # noqa: E402
from tests.test_shotcut import FULL, MELT, needs_ffmpeg, needs_melt, props  # noqa: E402


def _row(action, t, **kw):
    return {"event": "action", "action": action, "ok": True, "t_act": t, "ts": t + 0.2, "ms": 200, **kw}


def _dot(t, x=100, y=100):
    return _row("click", t, target={"kind": "point", "x": x, "y": y})


def _button(t, name="Red"):
    return _row("click", t, target={"kind": "element", "name": name, "rect": [10, 10, 40, 40]})


def _steps(rows):
    return R.build_steps(rows, 0.0)


# -- which parts play faster ---------------------------------------------------------------------------

def test_a_run_of_alike_steps_plays_its_first_two_at_normal_speed_and_the_rest_fast():
    steps = _steps([_dot(1.0 + 3 * i) for i in range(6)])
    [span] = S.auto_spans(steps, set())
    assert (span.t0, span.t1) == (steps[2].start, steps[5].end)
    assert span.factor == S.AUTO_FACTOR and span.auto and span.label == "3–6. Click here"


def test_a_later_run_of_the_same_step_keeps_only_its_first_and_a_short_run_keeps_all():
    rows = [_dot(1.0 + 3 * i) for i in range(4)] + [_button(13.0)] + [_dot(16.0 + 3 * i) for i in range(4)]
    rows += [_button(28.0, "Blue")] + [_dot(31.0 + 3 * i) for i in range(3)]
    steps = _steps(rows)
    spans = S.auto_spans(steps, set())
    assert [(s.t0, s.t1) for s in spans] == [(steps[2].start, steps[3].end), (steps[6].start, steps[8].end)]


def test_a_step_the_layer_singles_out_breaks_the_run():
    steps = _steps([_dot(1.0 + 3 * i) for i in range(8)])
    [span] = S.auto_spans(steps, {steps[3].id})  # 1-3 is too short a run; 5-8 is the first full one
    assert (span.t0, span.t1, span.label) == (steps[6].start, steps[7].end, "7–8. Click here")
    assert S.auto_spans(steps, {steps[2].id, steps[5].id}) == []


def test_layer_items_win_and_factor_1_keeps_normal_speed():
    steps = _steps([_dot(1.0 + 3 * i) for i in range(6)])
    auto = S.auto_spans(steps, set())[0]
    items = [{"kind": "speed", "t0": auto.t0, "t1": auto.t0 + 2.0, "factor": 1},
             {"kind": "speed", "step": steps[0].id, "factor": 8}]
    spans = S.plan(steps, items, True, set())
    assert [(s.t0, s.t1, s.factor, s.auto) for s in spans] == [
        (steps[0].start, steps[0].end, 8, False), (auto.t0 + 2.0, auto.t1, S.AUTO_FACTOR, True)]
    assert S.plan(steps, items, False, set()) == [spans[0]]  # speed="off": the layer's items only
    later = [{"kind": "speed", "t0": 0.0, "t1": 30.0, "factor": 2}, {"kind": "speed", "t0": 0.0, "t1": 30.0}]
    assert [s.factor for s in S.plan(steps, later, False, set())] == [S.AUTO_FACTOR]  # the later item wins


def test_a_fast_part_keeps_every_factor_th_frame_and_says_what_it_saved():
    spans = [S.Span(2.0, 4.0, 4)]
    assert [i for i in range(10, 30) if S.keep(i, i / 10, spans)] == [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20,
                                                                      24, 28]
    assert S.saved(0.0, 10.0, spans) == pytest.approx(1.5)
    seen = R.seen_time([], 10.0, [], tight=False, fast=spans)
    assert seen(1.0, 5.0) == pytest.approx(2.5)


def test_a_speed_item_needs_a_time_or_a_step_and_a_sensible_factor():
    assert validate({"kind": "speed", "t0": 1, "t1": 2})["kind"] == "speed"
    assert validate({"kind": "speed", "step": 3, "factor": 1})["factor"] == 1
    for bad in ({"kind": "speed", "t0": 1}, {"kind": "speed", "t0": 1, "t1": 2, "factor": 17},
                {"kind": "speed", "step": 3, "factor": True}, {"kind": "speed", "step": 3, "factor": 2.5}):
        with pytest.raises(LayerError):
            validate(bad)


# -- the Shotcut project ------------------------------------------------------------------------------

def test_a_fast_part_is_one_timewarp_clip_counted_in_sped_up_frames(tmp_path):
    t = sc.Timeline()
    for i in (0, 1, 2):
        t.frame(i, FULL)
    for i in (4, 8, 12):
        t.frame(i, FULL, 4)
    t.frame(13, FULL)
    assert [(s.first, s.length, s.speed) for s in t.segments] == [(0, 3, 1), (4, 3, 4), (13, 1, 1)]
    root = ET.parse(sc.write(tmp_path / "tutorial.mlt", size=(320, 312), fps=10, timeline=t, clean_frames=40,
                             narration=False)).getroot()
    fast = props(next(p for p in root.findall("producer") if p.get("id") == "clip1"))
    assert fast["mlt_service"] == "timewarp" and fast["resource"] == "4:edit/clean.mp4"
    assert fast["warp_resource"] == "edit/clean.mp4" and fast["length"] == "10"
    rec = [(e.get("producer"), e.get("in"), e.get("out"))
           for e in root.find("playlist[@id='recording']").findall("entry")]
    assert rec == [("clip0", "0", "2"), ("clip1", "1", "3"), ("clip2", "13", "13")]


# -- rendering ---------------------------------------------------------------------------------------

def _dots(n=8, every=0.8):
    return [_dot(1001.0 + every * i, ORIGIN[0] + 60 + 20 * i, ORIGIN[1] + 120) for i in range(n)]


@needs_ffmpeg
def test_render_plays_a_run_faster_with_a_chip_and_lists_it(tmp_path):
    rec = make_recording(tmp_path, seconds=10.0, actions=_dots() + [_button(1007.6)])
    off = R.render(rec, gif=False, speed="off", out_dir=tmp_path / "off")
    out = R.render(rec, gif=False)
    assert "faster" not in off and out["faster"][0]["from"] == "auto" and out["faster"][0]["factor"] == 4
    assert out["faster_saved_s"] > 1.0 and out["frames"] < off["frames"]
    steps = json.loads((rec / "tutorial" / "steps.json").read_text(encoding="utf-8"))
    assert [s.get("speed") for s in steps] == [None, None, 4, 4, 4, 4, 4, 4, None]
    frames = video_frames(Path(out["video"]), (320, 240 + R.STRIP))
    chip = [(lambda p: p[2] > 180 and p[0] < 90)(f.getpixel((24, 240 + R.STRIP // 2))) for f in frames]
    assert not chip[0] and any(chip) and not chip[-1]  # the chip shows only while the run plays fast


@needs_ffmpeg
def test_the_layer_can_speed_up_a_part_with_no_steps_in_real_time_pacing(tmp_path):
    rec = make_recording(tmp_path, seconds=6.0, actions=[_button(1001.0)])
    base = R.render(rec, gif=False, tight=False)
    Layer.for_recording(rec).add({"kind": "speed", "t0": 3.5, "t1": 5.5, "factor": 4})
    out = R.render(rec, gif=False, tight=False)
    assert base["frames"] - out["frames"] == 15 and out["faster"] == [
        {"t0": 3.5, "t1": 5.5, "factor": 4, "from": "layer"}]


@needs_melt
def test_melt_renders_a_sped_up_project_like_the_tutorial(tmp_path):
    rec = make_recording(tmp_path, seconds=8.0, actions=_dots())
    out = R.render(rec, gif=False, mlt=True)
    assert out["faster"]
    melt = tmp_path / "melt.mp4"
    subprocess.run([MELT, "-silent", out["project"], "-consumer", f"avformat:{melt}", "vcodec=libx264", "crf=12",
                    "pix_fmt=yuv420p"], capture_output=True, check=True, cwd=tmp_path.anchor)
    size = (320, 240 + R.STRIP)
    assert len(video_frames(melt, size)) == len(video_frames(Path(out["video"]), size)) == out["frames"]
    stats = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(melt), "-i", out["video"], "-lavfi",
                            "[0:v][1:v]psnr=stats_file=-", "-f", "null", "-"], capture_output=True, text=True,
                           check=True).stdout
    psnr = [float(x.split(":")[1]) for line in stats.splitlines() for x in line.split() if x.startswith("psnr_avg:")]
    assert len(psnr) == out["frames"] and min(psnr) > 30, min(psnr)

