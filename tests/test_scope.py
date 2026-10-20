import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import scope as sc  # noqa: E402
from overshoulder.driver import windows as w  # noqa: E402
from overshoulder.driver.display import Monitor  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402


def test_rect_contains_is_half_open():
    r = Rect(0, 0, 10, 10)
    assert r.contains(0, 0) and r.contains(9, 9)
    assert not r.contains(10, 5) and not r.contains(5, 10)


def test_rect_intersect_of_disjoint_rects_is_empty():
    assert Rect(0, 0, 5, 5).intersect(Rect(6, 6, 9, 9)).empty


def test_parse_scope_rejects_unknown_kinds():
    with pytest.raises(ValueError):
        sc.parse_scope("app:notepad")


def _binding(**kw):
    base = dict(hwnd=0x10, pid=7, owner_pid=7, created=123, process="x.exe", class_name="C", title="T")
    return sc.WindowBinding(**{**base, **kw})


FRAME = Rect(100, 100, 500, 400)


def _fake_window(monkeypatch, *, alive=True, owner_pid=7, created=123, frame=FRAME,
                 foreground=0x10, owners=None):
    monkeypatch.setattr(w, "is_window", lambda h: alive)
    monkeypatch.setattr(w, "pid_of", lambda h: owner_pid)
    monkeypatch.setattr(w, "process_identity", lambda pid: (created, "C:\\x.exe"))
    monkeypatch.setattr(w, "frame_bounds", lambda h: frame)
    monkeypatch.setattr(w, "foreground", lambda: foreground)
    monkeypatch.setattr(w, "root_owner", lambda h: (owners or {}).get(h, h))


def test_window_scope_goes_stale_when_window_closes(monkeypatch):
    _fake_window(monkeypatch, alive=False)
    with pytest.raises(sc.ScopeStale):
        sc.WindowScope(_binding()).check_alive()


def test_window_scope_goes_stale_when_handle_is_reused_by_another_process(monkeypatch):
    _fake_window(monkeypatch, owner_pid=99)
    with pytest.raises(sc.ScopeStale):
        sc.WindowScope(_binding()).check_alive()


def test_window_scope_goes_stale_when_process_restarts_with_same_pid(monkeypatch):
    _fake_window(monkeypatch, created=456)
    with pytest.raises(sc.ScopeStale):
        sc.WindowScope(_binding()).check_alive()


def test_require_point_rejects_points_outside_the_window_frame(monkeypatch):
    _fake_window(monkeypatch)
    scope = sc.WindowScope(_binding())
    sc.require_point(scope, 100, 100)
    with pytest.raises(sc.OutOfScope):
        sc.require_point(scope, 99, 200)


def test_require_rect_needs_the_whole_rect_inside(monkeypatch):
    _fake_window(monkeypatch)
    scope = sc.WindowScope(_binding())
    sc.require_rect(scope, Rect(110, 110, 200, 200))
    with pytest.raises(sc.OutOfScope):
        sc.require_rect(scope, Rect(450, 110, 520, 200))


def test_window_scope_refuses_a_foreign_process_window_owned_by_it(monkeypatch):
    _fake_window(monkeypatch, owners={0x40: 0x10})
    monkeypatch.setattr(w, "pid_of", lambda h: 555 if h == 0x40 else 7)
    assert not sc.WindowScope(_binding()).owns_window(0x40)


def test_screen_scope_point_in_a_monitor_gap_is_refused(monkeypatch):
    mons = [Monitor(1, 0, 0, 1920, 1080, 96, True), Monitor(2, 1920, 200, 3840, 1280, 96, False)]
    monkeypatch.setattr(sc.display, "monitors", lambda: mons)
    with pytest.raises(sc.OutOfScope):
        sc.require_point(sc.ScreenScope(None), 2000, 50)


def test_window_scope_owns_its_dialogs_but_not_other_windows(monkeypatch):
    _fake_window(monkeypatch, owners={0x20: 0x10, 0x30: 0x30})
    scope = sc.WindowScope(_binding())
    assert scope.owns_window(0x10)
    assert scope.owns_window(0x20)
    assert not scope.owns_window(0x30)


def test_keystrokes_refused_when_foreground_left_the_scope(monkeypatch):
    _fake_window(monkeypatch, foreground=0x30, owners={0x30: 0x30})
    with pytest.raises(sc.OutOfScope):
        sc.require_foreground_in_scope(sc.WindowScope(_binding()))


def _fake_monitors(monkeypatch):
    mons = [Monitor(1, 0, 0, 1920, 1080, 120, True), Monitor(2, 1920, 0, 3840, 1080, 96, False)]
    monkeypatch.setattr(sc.display, "monitors", lambda: mons)


def test_screen_scope_for_one_monitor_excludes_the_other(monkeypatch):
    _fake_monitors(monkeypatch)
    one = sc.ScreenScope(1)
    sc.require_point(one, 1919, 500)
    with pytest.raises(sc.OutOfScope):
        sc.require_point(one, 1920, 500)


def test_screen_scope_all_spans_every_monitor(monkeypatch):
    _fake_monitors(monkeypatch)
    assert sc.ScreenScope(None).bounds() == Rect(0, 0, 3840, 1080)


def test_screen_scope_rejects_a_missing_monitor(monkeypatch):
    _fake_monitors(monkeypatch)
    with pytest.raises(sc.ScopeNotFound):
        sc.ScreenScope(3)


def test_screen_scope_owns_windows_centred_on_its_monitor(monkeypatch):
    _fake_monitors(monkeypatch)
    monkeypatch.setattr(w, "frame_bounds", lambda h: Rect(1900, 10, 2400, 300))
    assert not sc.ScreenScope(1).owns_window(0x1)
    assert sc.ScreenScope(2).owns_window(0x1)
