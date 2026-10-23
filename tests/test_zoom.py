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


def test_render_rejects_an_unknown_zoom(tmp_path):
    with pytest.raises(ValueError):
        R.render(tmp_path, actions=[], zoom="max")
