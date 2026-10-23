import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image  # noqa: E402

from overshoulder import keys as K  # noqa: E402
from overshoulder import render as R  # noqa: E402

WHITE = (255, 255, 255)


def _row(action, t, **kw):
    return {"event": "action", "action": action, "ok": True, "t_act": t, "ts": t + 0.4, "ms": 400, **kw}


def _fonts():
    return {"badge": R._font(18), "caption": R._font(26), "make": R._font}


def _panel_width(img, y):
    xs = [x for x in range(img.width) if img.getpixel((x, y)) != WHITE]
    return (max(xs) - min(xs)) if xs else 0


def test_hotkeys_become_keycaps():
    assert K.keycaps("ctrl+shift+s") == ["Ctrl", "Shift", "S"]
    assert K.keycaps("alt+f4") == ["Alt", "F4"]
    assert K.keycaps("enter") == ["Enter"]
    assert K.keycaps("ctrl+left") == ["Ctrl", "←"]


def test_redacted_typing_shows_one_dot_per_character_and_never_the_text():
    assert K.typed_text({"redacted": True, "len": 4}) == "•" * 4
    assert K.typed_text("a\nb") == "a ↵ b"
    assert K.typed_text(None) is None


def test_steps_carry_their_keystrokes():
    rows = [_row("hotkey", 1.0, params={"combo": "ctrl+s"}), _row("type", 2.0, params={"text": "hi"}),
            _row("click", 3.0, target={"kind": "point", "x": 5, "y": 5})]
    a, b, c = R.build_steps(rows, 0.0)
    assert a.keys == ["Ctrl", "S"] and a.typed is None
    assert b.typed == "hi" and b.typing_s == pytest.approx(0.4)
    assert c.keys is None and c.typed is None


def test_keycaps_appear_when_the_keys_are_pressed_and_not_before():
    img = Image.new("RGB", (400, 300), WHITE)
    steps = R.build_steps([_row("hotkey", 1.0, params={"combo": "ctrl+s"})], 0.0)
    before = R.annotate(img, 0.9, (0, 0), None, steps, _fonts())
    during = R.annotate(img, 1.1, (0, 0), None, steps, _fonts())
    off = R.annotate(img, 1.1, (0, 0), None, steps, _fonts(), keys=False)
    y = 300 - K.MARGIN - 10
    assert _panel_width(before.crop((0, 0, 400, 300)), y) == 0
    assert _panel_width(during.crop((0, 0, 400, 300)), y) > 80
    assert _panel_width(off.crop((0, 0, 400, 300)), y) == 0


def test_typed_text_grows_while_it_is_typed():
    img = Image.new("RGB", (600, 300), WHITE)
    steps = R.build_steps([_row("type", 1.0, ms=1000, params={"text": "hello there world"})], 0.0)
    y = 300 - K.MARGIN - 10
    early = _panel_width(R.annotate(img, 1.2, (0, 0), None, steps, _fonts()).crop((0, 0, 600, 300)), y)
    late = _panel_width(R.annotate(img, 2.0, (0, 0), None, steps, _fonts()).crop((0, 0, 600, 300)), y)
    assert 0 < early < late


def test_long_typed_text_stays_inside_a_narrow_video():
    img = Image.new("RGB", (240, 300), WHITE)
    steps = R.build_steps([_row("type", 1.0, params={"text": "a very long sentence " * 8})], 0.0)
    out = R.annotate(img, 3.0, (0, 0), None, steps, _fonts()).crop((0, 0, 240, 300))
    y = 300 - K.MARGIN - 10
    assert out.getpixel((0, y)) == WHITE and out.getpixel((239, y)) == WHITE
