import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image  # noqa: E402

from overshoulder import render as R  # noqa: E402
from overshoulder import zoom as Z  # noqa: E402

SIZE = (800, 600)


def _row(action, t, **kw):
    return {"event": "action", "action": action, "ok": True, "t_act": t, "ts": t + 0.4, "ms": 400, **kw}


def _click(t, rect):
    return _row("click", t, target={"kind": "element", "name": "B", "rect": list(rect)})


def _stroke(t, x, y, tx, ty):
    return _row("drag", t, target={"kind": "point", "x": x, "y": y}, to={"kind": "point", "x": tx, "y": ty})


def _width(crop):
    return crop[2] - crop[0]


def test_a_small_target_gets_a_view_and_a_large_one_does_not():
    small = R.build_steps([_click(1.0, (100, 100, 140, 120))], 0.0)[0]
    big = R.build_steps([_click(1.0, (0, 0, 700, 500))], 0.0)[0]
    v = Z.view_for(small, SIZE)
    assert v is not None and v.w == SIZE[0] / Z.ZOOM_MAX and (v.cx, v.cy) == (120, 110)
    assert Z.view_for(big, SIZE) is None


def test_the_camera_closes_in_as_the_step_happens_and_opens_out_after():
    steps = R.build_steps([_click(5.0, (380, 280, 420, 320))], 0.0)
    keys = Z.plan(steps, SIZE)
    s = steps[0]
    assert Z.camera_at(s.t - Z.ZOOM_IN_S - 0.1, keys, SIZE, (0, 0)) is None
    mid = Z.camera_at(s.t - Z.ZOOM_IN_S / 2, keys, SIZE, (0, 0))
    assert SIZE[0] / Z.ZOOM_MAX < _width(mid) < SIZE[0]
    assert _width(Z.camera_at(s.t, keys, SIZE, (0, 0))) == pytest.approx(SIZE[0] / Z.ZOOM_MAX)
    assert _width(Z.camera_at(s.end - 0.01, keys, SIZE, (0, 0))) == pytest.approx(SIZE[0] / Z.ZOOM_MAX)
    assert Z.camera_at(s.end + Z.ZOOM_OUT_S + 0.01, keys, SIZE, (0, 0)) is None


def test_the_view_stays_inside_the_frame_and_keeps_its_aspect():
    steps = R.build_steps([_click(1.0, (0, 0, 20, 20))], 0.0)
    crop = Z.camera_at(1.0, Z.plan(steps, SIZE), SIZE, (0, 0))
    assert crop[0] == 0 and crop[1] == 0
    assert (crop[2] - crop[0]) / (crop[3] - crop[1]) == pytest.approx(SIZE[0] / SIZE[1])


def test_close_steps_pan_without_opening_out_and_far_steps_open_out():
    close = R.build_steps([_click(1.0, (100, 100, 120, 120)), _click(2.0, (600, 400, 620, 420))], 0.0)
    keys = Z.plan(close, SIZE)
    assert all(Z.camera_at(t / 10, keys, SIZE, (0, 0)) is not None for t in range(10, 21))
    far = R.build_steps([_click(1.0, (100, 100, 120, 120)), _click(9.0, (600, 400, 620, 420))], 0.0)
    keys = Z.plan(far, SIZE)
    assert Z.camera_at(6.0, keys, SIZE, (0, 0)) is None


def test_typing_right_after_a_click_stays_zoomed_on_the_field():
    rows = [_click(1.0, (100, 100, 160, 120)), _row("type", 1.8, params={"text": "hi"})]
    steps = R.build_steps(rows, 0.0)
    keys = Z.plan(steps, SIZE)
    assert Z.camera_at(1.8, keys, SIZE, (0, 0)) == Z.camera_at(1.0, keys, SIZE, (0, 0))


def test_a_view_follows_a_window_that_moved():
    steps = R.build_steps([_click(1.0, (1400, 1300, 1420, 1320))], 0.0)
    keys = Z.plan(steps, SIZE)
    a = Z.camera_at(1.0, keys, SIZE, (1000, 1000))
    b = Z.camera_at(1.0, keys, SIZE, (1100, 1000))
    assert b[0] == pytest.approx(a[0] - 100)


def test_a_steps_own_annotations_stay_inside_its_view():
    steps = R.build_steps([_click(1.0, (500, 400, 520, 420))], 0.0)
    plain = Z.camera_at(1.0, Z.plan(steps, SIZE), SIZE, (0, 0))
    wide = Z.camera_at(1.0, Z.plan(steps, SIZE, {1: [(720, 520, 760, 560)]}), SIZE, (0, 0))
    assert plain[2] < 760
    assert wide is not None and wide[2] >= 760 and wide[3] >= 560


def test_keyboard_only_recordings_never_zoom():
    steps = R.build_steps([_row("hotkey", 1.0, params={"combo": "ctrl+s"})], 0.0)
    assert Z.plan(steps, SIZE) == []


def test_annotations_are_drawn_after_the_zoom_at_their_normal_size():
    img = Image.new("RGB", SIZE, (255, 255, 255))
    steps = R.build_steps([_click(1.0, (380, 280, 420, 320))], 0.0)
    fonts = {"badge": R._font(18), "caption": R._font(26)}
    view = Z.camera_at(1.0, Z.plan(steps, SIZE), SIZE, (0, 0))
    out = R.annotate(img, 1.0, (0, 0), None, steps, fonts, view=view)
    # Magnified 2x around its centre, the target's top edge moves from y=280 to y=260; the highlight
    # sits 4 px outside it.
    assert out.getpixel((400, 257)) != (255, 255, 255)
    border = [out.getpixel((400, y)) != (255, 255, 255) for y in range(246, 272)]
    assert 3 <= sum(border) <= 5  # a 4 px outline, not one scaled up to 8 px


def test_strokes_on_a_canvas_never_move_the_camera():
    rows = [_stroke(1.0 + 3 * i, 100 + 40 * i, 300, 140 + 40 * i, 320) for i in range(6)]
    assert Z.plan(R.build_steps(rows, 0.0), SIZE) == []


def test_one_click_on_a_control_between_strokes_does_not_zoom_in_and_out():
    rows = [_stroke(1.0, 100, 300, 300, 400), _stroke(4.0, 300, 400, 500, 300), _click(7.0, (20, 20, 40, 40)),
            _stroke(10.0, 500, 300, 700, 400)]
    assert Z.plan(R.build_steps(rows, 0.0), SIZE) == []


def test_steps_far_apart_on_the_clock_but_close_in_the_video_do_not_open_out():
    steps = R.build_steps([_click(1.0, (100, 100, 120, 120)), _click(9.0, (600, 400, 620, 420))], 0.0)
    keys = Z.plan(steps, SIZE, seen=R.seen_time(steps, 12.0, [], tight=True))
    assert all(Z.camera_at(t / 10, keys, SIZE, (0, 0)) is not None for t in range(10, 91))
    assert Z.camera_at(6.0, Z.plan(steps, SIZE), SIZE, (0, 0)) is None  # real-time pacing: a pause to sit through


def test_the_camera_holds_while_the_next_target_is_in_view_and_comes_back_after_a_short_visit():
    rows = [_click(1.0, (100, 100, 120, 120)), _click(2.0, (130, 100, 150, 120)), _click(3.0, (600, 450, 620, 470)),
            _click(4.0, (110, 130, 130, 150))]
    shots = Z.shots(R.build_steps(rows, 0.0), SIZE)
    assert [len(s.steps) for s in shots] == [2, 1, 1]
    assert shots[2].view == shots[0].view


def test_seen_time_counts_only_what_the_video_keeps():
    steps = R.build_steps([_click(1.0, (100, 100, 120, 120)), _click(9.0, (600, 400, 620, 420))], 0.0)
    assert R.seen_time(steps, 12.0, [], tight=True)(3.2, 9.0) == pytest.approx(1.5)
    assert R.seen_time(steps, 12.0, [], tight=False)(3.2, 9.0) == pytest.approx(5.8)
    assert R.seen_time(steps, 12.0, [(8.0, 8.5)], tight=True)(3.2, 9.0) == pytest.approx(1.0)


def test_render_rejects_an_unknown_zoom(tmp_path):
    with pytest.raises(ValueError):
        R.render(tmp_path, actions=[], zoom="max")
