import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import settings as st  # noqa: E402
from overshoulder import waits as W  # noqa: E402
from overshoulder.driver.windows import Rect, WindowInfo  # noqa: E402


def fast():
    return st.Settings(poll_interval_ms=100)


def test_file_wait_fires_when_the_file_appears(tmp_path):
    target = tmp_path / "report.pdf"
    threading.Timer(0.3, lambda: target.write_text("x")).start()
    out = W.wait([{"kind": "file", "path": str(target)}], fast(), timeout_s=5)
    assert out["fired"] and "appeared" in out["detail"]


def test_file_wait_for_an_existing_file_waits_for_a_change(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("a")
    assert not W.wait([{"kind": "file", "path": str(target)}], fast(), timeout_s=0.4)["fired"]
    threading.Timer(0.3, lambda: (time.sleep(0.05), target.write_text("bb"))).start()
    out = W.wait([{"kind": "file", "path": str(target), "event": "modified"}], fast(), timeout_s=5)
    assert out["fired"]


def test_wait_times_out_without_firing(tmp_path):
    out = W.wait([{"kind": "file", "path": str(tmp_path / "never")}], fast(), timeout_s=0.3)
    assert out == {"fired": False, "timeout_s": 0.3}


def test_timeout_is_capped_by_settings(tmp_path, monkeypatch):
    s = st.Settings(poll_interval_ms=100, max_wait_minutes=1)
    monkeypatch.setattr(W.time, "sleep", lambda x: None)
    clock = iter(range(0, 10_000, 30))
    monkeypatch.setattr(W.time, "monotonic", lambda: next(clock))
    out = W.wait([{"kind": "file", "path": str(tmp_path / "never")}], s, timeout_s=99999)
    assert out["timeout_s"] == 60


def _win(hwnd, title, process="app.exe"):
    return WindowInfo(hwnd, title, "C", 1, process, Rect(0, 0, 10, 10))


def test_window_appears_ignores_windows_open_before_the_wait(monkeypatch):
    shown = [[_win(1, "Save As")], [_win(1, "Save As")], [_win(1, "Save As"), _win(2, "Save As")]]
    monkeypatch.setattr(W, "_windows", lambda: shown.pop(0) if len(shown) > 1 else shown[0])
    out = W.wait([{"kind": "window_appears", "title_re": "Save As"}], fast(), timeout_s=5)
    assert out["fired"] and "Save As" in out["detail"]


def test_any_condition_fires_the_wait(tmp_path, monkeypatch):
    monkeypatch.setattr(W, "_windows", lambda: [])
    target = tmp_path / "done.flag"
    threading.Timer(0.2, lambda: target.write_text("x")).start()
    out = W.wait([{"kind": "window_appears", "title_re": "Nope"}, {"kind": "file", "path": str(target)}],
                 fast(), timeout_s=5)
    assert out["fired"] and out["condition"] == 1


def test_switched_off_detection_refuses_to_wait():
    with pytest.raises(W.WaitError):
        W.Waiter([{"kind": "file", "path": "x"}], st.Settings(waits_enabled=False))


def test_switched_off_kind_is_refused():
    with pytest.raises(W.WaitError):
        W.Waiter([{"kind": "file", "path": "x"}], st.Settings(wait_kinds=["window_appears"]))


def test_process_allow_list_hides_other_apps(monkeypatch):
    monkeypatch.setattr(W, "_windows", lambda: [_win(5, "Secret", "bank.exe")])
    s = st.Settings(wait_processes=["notepad.exe"])
    with pytest.raises(W.WaitError):  # the window exists, but the wait may not see it
        W.Waiter([{"kind": "window_closes", "title_re": "Secret"}], s)


def test_window_closes_needs_an_existing_window(monkeypatch):
    monkeypatch.setattr(W, "_windows", lambda: [])
    with pytest.raises(W.WaitError):
        W.Waiter([{"kind": "window_closes", "title_re": "Installer"}], st.Settings())


def test_settings_round_trip_and_clamp(tmp_path):
    p = tmp_path / "s.json"
    st.save(st.Settings(poll_interval_ms=5, max_wait_minutes=99999, wait_kinds=["file", "bogus"],
                        wait_processes=[" Chrome.EXE ", ""]), p)
    s = st.load(p)
    assert s.poll_interval_ms == 100 and s.max_wait_minutes == 1440
    assert s.wait_kinds == ["file"] and s.wait_processes == ["chrome.exe"]


def test_settings_form_parsing():
    from overshoulder.settings_page import settings_from_form
    s = settings_from_form({"waits_enabled": "on", "kind_file": "on", "poll_interval_ms": "250",
                            "max_wait_minutes": "abc", "wait_processes": "notepad.exe; chrome.exe"})
    assert s.waits_enabled and s.wait_kinds == ["file"] and s.poll_interval_ms == 250
    assert s.max_wait_minutes == 60 and s.wait_processes == ["chrome.exe", "notepad.exe"]
