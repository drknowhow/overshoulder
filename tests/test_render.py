import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image  # noqa: E402

from overshoulder import render as R  # noqa: E402


def _row(action, t, ok=True, **kw):
    return {"event": "action", "action": action, "ok": ok, "t_act": t, "ts": t + 0.4, "ms": 400, **kw}


def test_build_steps_numbers_only_successful_visible_actions():
    rows = [_row("observe", 1.0), _row("click", 2.0, target={"kind": "point", "x": 50, "y": 60}),
            _row("type", 3.0, ok=False), _row("hotkey", 4.0, params={"combo": "ctrl+s"})]
    steps = R.build_steps(rows, start=0.0)
    assert [(s.n, s.action) for s in steps] == [(1, "click"), (2, "hotkey")]
    assert steps[0].point == (50, 60)
    assert steps[1].caption == "Press Ctrl+S"


def test_captions_never_reveal_redacted_text():
    row = _row("type", 1.0, params={"text": {"redacted": True, "len": 9}})
    assert R.build_steps([row], 0.0)[0].caption == "Type 9 characters"
    row = _row("type", 1.0, params={"text": "hello"})
    assert R.build_steps([row], 0.0)[0].caption == 'Type "hello"'


def test_element_click_ripples_at_the_element_centre():
    row = _row("click", 1.0, target={"kind": "element", "name": "OK", "rect": [10, 10, 30, 50]})
    s = R.build_steps([row], 0.0)[0]
    assert s.point == (20, 30) and s.caption == 'Click "OK"'


def test_annotate_draws_badge_and_caption_only_while_a_step_is_active():
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    steps = R.build_steps([_row("click", 2.0, target={"kind": "element", "name": "OK",
                                                      "rect": [100, 100, 160, 130]})], 0.0)
    fonts = {"badge": R._font(18), "caption": R._font(26)}
    quiet = R.annotate(img, 10.0, (0, 0), None, steps, fonts)
    busy = R.annotate(img, 2.1, (0, 0), None, steps, fonts)
    assert quiet.tobytes() == img.tobytes()
    assert busy.getpixel((130, 97)) != (255, 255, 255)  # highlight border, top edge
    row = [busy.getpixel((x, 262)) for x in range(busy.width)]
    assert sum(px[0] < 80 for px in row) > 100  # dark caption bar across the bottom


def test_annotate_translates_screen_coordinates_by_the_frame_origin():
    img = Image.new("RGB", (200, 200), (255, 255, 255))
    steps = R.build_steps([_row("click", 1.0, target={"kind": "point", "x": 1050, "y": 2050})], 0.0)
    fonts = {"badge": R._font(18), "caption": R._font(26)}
    out = R.annotate(img, 1.1, (1000, 2000), None, steps, fonts)
    assert out.getpixel((50 + 13, 50)) != (255, 255, 255)  # ripple ring near (50, 50)


def test_a_step_annotation_ends_before_the_next_step_starts():
    rows = [_row("click", 1.0, target={"kind": "point", "x": 5, "y": 5}),
            _row("type", 1.6, params={"text": "x"}), _row("hotkey", 6.0, params={"combo": "ctrl+s"})]
    a, b, c = R.build_steps(rows, 0.0)
    assert a.end <= b.t and b.start >= a.end
    assert c.end == c.t + R.HOLD_S
    assert a.t <= R.still_time(a) < b.t


def test_keyboard_step_without_target_draws_no_badge():
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    steps = R.build_steps([_row("hotkey", 1.0, params={"combo": "ctrl+s"})], 0.0)
    out = R.annotate(img, 1.1, (0, 0), None, steps, {"badge": R._font(18), "caption": R._font(26)})
    assert all(out.getpixel((x, y)) == (255, 255, 255) for x in range(0, 60) for y in range(0, 60))
