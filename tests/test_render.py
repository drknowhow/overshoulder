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
    assert quiet.size == busy.size == (400, 300 + R.STRIP)
    assert quiet.crop((0, 0, 400, 300)).tobytes() == img.tobytes()
    assert busy.getpixel((130, 97)) != (255, 255, 255)  # highlight border, top edge
    strip_quiet = quiet.crop((0, 300, 400, 300 + R.STRIP)).tobytes()
    strip_busy = busy.crop((0, 300, 400, 300 + R.STRIP)).tobytes()
    assert strip_quiet != strip_busy  # the caption is drawn in the strip, not over the app


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


def test_long_captions_shrink_or_truncate_to_fit_a_narrow_window():
    img = Image.new("RGB", (240, 200), (255, 255, 255))
    steps = R.build_steps([_row("type", 1.0, params={"text": "a very long sentence " * 6})], 0.0)
    out = R.annotate(img, 1.1, (0, 0), None, steps, {"badge": R._font(18), "caption": R._font(26)})
    strip = out.crop((0, 200, 240, 200 + R.STRIP))
    assert strip.getpixel((0, R.STRIP // 2)) == R.CAPTION_BG[:3]  # text did not run off the left edge
    assert strip.getpixel((239, R.STRIP // 2)) == R.CAPTION_BG[:3]


def test_tight_timing_drops_idle_frames_between_steps():
    rows = [_row("click", 2.0, target={"kind": "point", "x": 1, "y": 1}),
            _row("click", 30.0, target={"kind": "point", "x": 1, "y": 1})]
    steps = R.build_steps(rows, 0.0)
    assert R.keep_frame(0.5, steps, 40.0) and R.keep_frame(39.5, steps, 40.0)
    assert R.keep_frame(2.5, steps, 40.0) and R.keep_frame(30.1, steps, 40.0)
    assert not R.keep_frame(15.0, steps, 40.0)


def test_the_agents_pointer_glides_to_each_target_and_rests_there():
    rows = [_row("click", 2.0, target={"kind": "point", "x": 100, "y": 100}),
            _row("invoke", 5.0, target={"kind": "element", "name": "OK", "rect": [200, 300, 240, 320]}),
            _row("hotkey", 6.0, params={"combo": "ctrl+s"})]
    steps = R.build_steps(rows, 0.0)
    home = (0, 0)
    assert R.pointer_at(0.0, steps, home) == home  # rests at home until the first glide
    assert R.pointer_at(2.0 - R.GLIDE_S / 2, steps, home) == (50, 50)  # halfway there
    assert R.pointer_at(2.0, steps, home) == (100, 100)  # arrives as the step happens
    assert R.pointer_at(4.0, steps, home) == (100, 100)  # rests until the next glide
    assert R.pointer_at(5.0, steps, home) == (220, 310)  # an element step aims at its centre
    assert R.pointer_at(9.0, steps, home) == (220, 310)  # a keyboard step does not move it


def test_close_steps_glide_only_after_the_previous_step():
    rows = [_row("click", 1.0, target={"kind": "point", "x": 0, "y": 0}),
            _row("click", 1.2, target={"kind": "point", "x": 100, "y": 0})]
    steps = R.build_steps(rows, 0.0)
    assert R.pointer_at(1.0, steps, (0, 0)) == (0, 0)
    assert R.pointer_at(1.1, steps, (0, 0)) == (50, 0)


def test_no_target_means_no_pointer_and_hidden_elements_are_not_aimed_at():
    rows = [_row("hotkey", 1.0, params={"combo": "ctrl+s"}),
            _row("type", 2.0, target={"kind": "element", "name": "editor", "rect": [10, 10, 10, 10]})]
    steps = R.build_steps(rows, 0.0)
    assert [s.aim for s in steps] == [None, None]
    assert R.pointer_at(3.0, steps, (5, 5)) is None


def test_invoke_ripples_like_a_click_at_the_controls_centre():
    row = _row("invoke", 1.0, target={"kind": "element", "name": "Save", "rect": [10, 10, 30, 50]})
    s = R.build_steps([row], 0.0)[0]
    assert s.point == (20, 30) and s.aim == (20, 30)


def test_render_rejects_an_unknown_pointer_style(tmp_path):
    with pytest.raises(ValueError):
        R.render(tmp_path, actions=[], pointer="laser")
