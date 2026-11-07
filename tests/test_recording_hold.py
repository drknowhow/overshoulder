"""A recording holds each frame until a look for password fields has begun after it, and blacks it out
with what the looks before and after it found; a frame grabbed while a field moved is blacked out whole."""

import sys
import time
import types

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_gateway import FakeScope  # noqa: E402

import overshoulder.recorder as R  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.recorder import Recording, held_rects  # noqa: E402

AREA = Rect(0, 0, 100, 20)
BLACK, WHITE = b"\0\0\0", b"\xff\xff\xff"


class Pipe:
    def __init__(self):
        self.data = b""

    def write(self, b):
        self.data += b

    def close(self):
        pass


def recording(tmp_path, monkeypatch):
    """A window recording of a white 100x20 window, with nothing looked at yet."""
    raw = b"\xff" * (100 * 20 * 4)
    monkeypatch.setattr(R, "print_window_bgra", lambda hwnd: (raw, AREA))
    scope = FakeScope()
    scope.binding = types.SimpleNamespace(hwnd=0x10, pid=1)
    rec = Recording(scope, tmp_path)
    rec.size = (100, 20)
    rec._proc = types.SimpleNamespace(stdin=Pipe(), wait=lambda timeout=None: 0)
    return rec


def tick() -> float:
    """perf_counter once it has moved on, so a look never shares a time with a grab."""
    t = time.perf_counter()
    while (now := time.perf_counter()) == t:
        pass
    return now


def look(rec, rects):
    now = tick()
    rec._looks.append((now, now, rects))
    tick()


def px(rec, x, y, frame=0):
    at = (frame * 100 * 20 + y * 100 + x) * 4
    return rec._proc.stdin.data[at:at + 3]


def test_fields_that_stayed_put_are_blacked_out_where_they_are():
    f = Rect(10, 5, 20, 15)
    assert held_rects([f], [f], AREA) == [f]
    assert held_rects([], [], AREA) == []


def test_a_field_that_moved_came_or_went_blacks_out_the_whole_frame():
    f = Rect(10, 5, 20, 15)
    assert held_rects([f], [Rect(60, 5, 70, 15)], AREA) == [AREA]
    assert held_rects([], [f], AREA) == [AREA]
    assert held_rects([f], [], AREA) == [AREA]


def test_a_frame_waits_for_the_next_look_and_is_covered_between_the_two(monkeypatch, tmp_path):
    rec = recording(tmp_path, monkeypatch)
    look(rec, [Rect(10, 5, 20, 15)])
    assert rec._write_window_frame(AREA)
    assert rec._flush() and rec._proc.stdin.data == b""  # nothing reaches the encoder yet
    look(rec, [Rect(60, 5, 70, 15)])  # the field moved while the frame was held
    assert rec._flush() and rec.frames == 1
    # The field was somewhere unknown when the frame was grabbed, so none of the frame is shown.
    assert px(rec, 15, 10) == px(rec, 40, 10) == px(rec, 90, 10) == BLACK


def test_a_look_that_finished_during_the_grab_does_not_count_as_the_one_before(monkeypatch, tmp_path):
    rec = recording(tmp_path, monkeypatch)
    look(rec, [Rect(10, 5, 20, 15)])
    grab = R.print_window_bgra

    def slow(hwnd):  # a look finishes, with the field elsewhere, while the window is being grabbed
        look(rec, [Rect(60, 5, 70, 15)])
        return grab(hwnd)

    monkeypatch.setattr(R, "print_window_bgra", slow)
    assert rec._write_window_frame(AREA)
    look(rec, [Rect(60, 5, 70, 15)])
    assert rec._flush()
    assert px(rec, 15, 10) == px(rec, 40, 10) == BLACK


def test_a_frame_no_look_reaches_in_time_stops_the_recording_unsent(monkeypatch, tmp_path):
    rec = recording(tmp_path, monkeypatch)
    long_ago = time.perf_counter() - 2 * R.HOLD_MAX_S
    rec._looks.append((long_ago, long_ago, []))
    assert rec._write_window_frame(AREA)
    rec._held[0]["ended"] -= R.HOLD_MAX_S + 1  # held this long, and no look has begun since
    assert not rec._flush()
    assert rec._proc.stdin.data == b"" and rec._held == [] and "not checked in time" in rec.error


def test_stop_looks_once_more_for_the_last_frames_and_drops_them_if_it_cannot(monkeypatch, tmp_path):
    rec = recording(tmp_path, monkeypatch)
    look(rec, [])
    monkeypatch.setattr(R, "capture_rects", lambda scope: [Rect(10, 5, 20, 15)])
    assert rec._write_window_frame(AREA)
    rec.stop()
    assert rec.frames == 1 and px(rec, 15, 10) == BLACK

    rec = recording(tmp_path / "b", monkeypatch)
    (tmp_path / "b").mkdir()
    look(rec, [])

    def fail(scope):
        raise RuntimeError("controls unreadable")

    monkeypatch.setattr(R, "capture_rects", fail)
    assert rec._write_window_frame(AREA)
    out = rec.stop()
    assert rec.frames == 0 and rec._proc.stdin.data == b"" and "controls unreadable" in out["error"]
