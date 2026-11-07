"""Password fields are painted black in every picture: screenshots, OCR and vision input, approval
pictures, recordings and the phone's screenshot."""

import sys
import time
import types

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_gateway import FakeScope  # noqa: E402

from overshoulder import locate, phone  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway.limits import (  # noqa: E402
    CaptureRefused,
    LeaseLimits,
    Limits,
    NarrowedScope,
    capture_rects,
)
from overshoulder.recorder import Recording  # noqa: E402

WHITE, BLACK = (255, 255, 255), (0, 0, 0)
FIELD = Rect(10, 10, 30, 20)


def window_scope(fields=None, roots=(0x21, 0x10), visible=()):
    inner = FakeScope(owned=(0x10, 0x21))
    inner.binding = types.SimpleNamespace(hwnd=0x10, pid=1)
    fields = {0x10: [FIELD], 0x21: []} if fields is None else fields
    return NarrowedScope(inner, lambda: LeaseLimits(), window_processes=lambda h: ["notepad.exe"],
                         visible_windows=lambda: list(visible), password_fields=fields.get,
                         window_roots=lambda h: list(roots))


def screen_scope(fields, windows, deny=()):
    inner = FakeScope()
    inner.kind = "screen"
    lim = LeaseLimits(session=Limits.make(deny_processes=list(deny))) if deny else LeaseLimits()
    procs = {0x20: ["chrome.exe"]}
    return NarrowedScope(inner, lambda: lim, window_processes=lambda h: procs.get(h, ["notepad.exe"]),
                         visible_windows=lambda: windows, password_fields=fields.get)


def test_a_window_scope_hides_password_fields_in_its_window_and_its_dialogs():
    s = window_scope({0x10: [FIELD], 0x21: [Rect(40, 40, 60, 50)]})
    assert s.secret_rects() == [Rect(40, 40, 60, 50), FIELD]
    # a denied program's window elsewhere on screen is not in a window's own pixels
    assert capture_rects(s) == [Rect(40, 40, 60, 50), FIELD]


def test_a_window_whose_tree_cannot_be_read_is_not_captured():
    with pytest.raises(CaptureRefused):
        window_scope({0x10: None, 0x21: []}).secret_rects()


def test_an_unreadable_dialog_is_blacked_out_whole(monkeypatch):
    from overshoulder.driver import windows as w
    monkeypatch.setattr(w, "frame_bounds", lambda h: Rect(5, 5, 50, 50))
    assert window_scope({0x10: [], 0x21: None}).secret_rects() == [Rect(5, 5, 50, 50)]


def test_a_screen_scope_checks_every_window_on_its_area_and_hides_unreadable_ones_whole():
    windows = [(0x10, Rect(0, 0, 60, 60)), (0x11, Rect(50, 50, 90, 90)), (0x12, Rect(500, 500, 600, 600)),
               (0x20, Rect(70, 0, 100, 30))]
    asked = []

    def fields(h):
        asked.append(h)
        return {0x10: [FIELD], 0x11: None, 0x20: []}.get(h, [])

    s = screen_scope(types.SimpleNamespace(get=fields), windows, deny=["chrome"])
    got = capture_rects(s)
    assert 0x12 not in asked  # off the scope's area
    assert got == [Rect(70, 0, 100, 30), FIELD, Rect(50, 50, 90, 90)]  # denied window, field, unreadable


def _white(w_, h_):
    from PIL import Image
    return Image.new("RGB", (w_, h_), WHITE)


def test_every_locate_capture_paints_the_field_black(monkeypatch):
    from overshoulder.driver import capture
    monkeypatch.setattr(capture, "window_image", lambda hwnd, rect=None: _white(rect.width, rect.height))
    monkeypatch.setattr(capture, "grab", lambda rect: _white(rect.width, rect.height))
    for s in (window_scope(), screen_scope({0x10: [FIELD]}, [(0x10, Rect(0, 0, 100, 100))])):
        img = locate._capture(s, s.bounds())
        assert img.getpixel((15, 15)) == BLACK and img.getpixel((50, 50)) == WHITE, s.inner.kind


def test_the_screenshot_tool_and_comparisons_paint_the_field_black(monkeypatch, tmp_path):
    from overshoulder.driver import capture
    from overshoulder.service import Service
    monkeypatch.setattr(capture, "window_image", lambda hwnd, rect=None: _white(rect.width, rect.height))
    monkeypatch.setattr(capture, "save", lambda img, path: img)
    svc = Service.__new__(Service)
    svc.shots_dir = tmp_path
    s = window_scope()
    assert svc._shot(s, "shot").getpixel((15, 15)) == BLACK
    assert Service._snap(s).getpixel((15, 15)) == BLACK
    assert Service._snap(window_scope({0x10: None, 0x21: []})) is None  # no picture, comparison skipped


def test_window_recording_frames_are_blacked_out_and_an_unreadable_window_is_not_recorded(monkeypatch, tmp_path):
    import overshoulder.recorder as R
    raw = bytes([255, 255, 255, 255]) * (100 * 100)
    monkeypatch.setattr(R, "print_window_bgra", lambda hwnd: (raw, Rect(0, 0, 100, 100)))

    class Pipe:
        data = b""

        def write(self, b):
            Pipe.data += b

    rec = Recording(window_scope(), tmp_path)
    rec.size = (100, 100)
    rec._looks = [(0.0, 0.0, capture_rects(rec.scope))]
    rec._proc = types.SimpleNamespace(stdin=Pipe())
    assert rec._write_window_frame(Rect(0, 0, 100, 100))
    rec._looks.append((time.perf_counter() + 1, time.perf_counter() + 1, capture_rects(rec.scope)))
    assert rec._flush()
    at = lambda x, y: Pipe.data[(y * 100 + x) * 4:(y * 100 + x) * 4 + 3]  # noqa: E731
    assert at(15, 15) == b"\0\0\0" and at(50, 50) == b"\xff\xff\xff"
    with pytest.raises(CaptureRefused):
        Recording(window_scope({0x10: None, 0x21: []}), tmp_path).start()


def test_the_phone_screenshot_hides_password_fields_or_sends_none(monkeypatch):
    from PIL import Image

    from overshoulder.driver import capture, uia
    from overshoulder.driver import windows as w
    monkeypatch.setattr(w, "frame_bounds", lambda h: Rect(0, 0, 100, 100))
    monkeypatch.setattr(capture, "window_image", lambda hwnd, rect=None: _white(rect.width, rect.height))
    monkeypatch.setattr(uia, "roots_for_window", lambda h: [h])
    monkeypatch.setattr(uia, "password_rects", lambda h: [FIELD])
    import io
    img = Image.open(io.BytesIO(phone._screenshot(0x10)))
    assert img.convert("RGB").getpixel((15, 15)) == BLACK
    monkeypatch.setattr(uia, "password_rects", lambda h: None)
    assert phone._screenshot(0x10) is None
