import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]


@pytest.fixture
def notepad(tmp_path):
    from overshoulder.driver.display import enable_dpi_awareness
    enable_dpi_awareness()
    from overshoulder import scope as sc

    path = tmp_path / "overshoulder_scope_test.txt"
    path.write_text("", encoding="utf-8")
    __import__("bghelpers").wait_no_window("Notepad")
    subprocess.Popen(["notepad.exe", str(path)])
    found = []
    for _ in range(80):
        found = sc.find_windows(title_re="overshoulder_scope_test")
        if found:
            break
        time.sleep(0.25)
    assert found, "notepad window did not appear"
    yield found[0], path
    import bghelpers
    bghelpers.close_discarding(found[0].hwnd)


def test_find_windows_reports_the_real_process_for_a_packaged_app():
    from overshoulder import scope as sc
    proc = subprocess.Popen(["calc.exe"])
    try:
        hits = []
        for _ in range(80):
            hits = sc.find_windows(title_re="^Calculator$")
            if hits:
                break
            time.sleep(0.25)
        assert hits
        b = sc.bind_window(hits[0].hwnd)
        assert b.class_name == "ApplicationFrameWindow"
        assert b.process.lower() == "calculatorapp.exe"
        assert b.pid != b.owner_pid
    finally:
        from pywinauto import Desktop
        for h in hits:
            try:
                Desktop(backend="uia").window(handle=h.hwnd).close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.terminate()


def test_window_scope_bounds_exclude_invisible_border(notepad):
    from overshoulder import scope as sc
    from overshoulder.driver import windows as w

    info, _ = notepad
    scope = sc.parse_scope(f"window:{info.hwnd}")
    frame, outer = scope.bounds(), w.window_rect(info.hwnd)
    assert outer.contains_rect(frame)
    assert frame.width < outer.width


def test_window_scope_owns_its_save_dialog_and_goes_stale_on_close(notepad):
    from pywinauto import Desktop

    from overshoulder import scope as sc
    from overshoulder.driver import input as inp
    from overshoulder.driver import windows as w

    info, _ = notepad
    scope = sc.parse_scope(f"window:{info.hwnd}")
    Desktop(backend="uia").window(handle=info.hwnd).set_focus()
    time.sleep(0.5)
    sc.require_foreground_in_scope(scope)
    inp.hotkey(info.hwnd, "ctrl+shift+s")
    dialog = 0
    for _ in range(40):
        dialog = w.foreground()
        if dialog and dialog != info.hwnd:
            break
        time.sleep(0.25)
    assert dialog and dialog != info.hwnd, "Save As dialog did not take focus"
    assert scope.owns_window(dialog)
    from pywinauto import Desktop as _D
    for b in _D(backend="uia").window(handle=dialog).descendants(control_type="Button"):
        if b.window_text() == "Cancel":
            b.invoke()
            break
    time.sleep(0.5)
    Desktop(backend="uia").window(handle=info.hwnd).close()
    time.sleep(1.0)
    with pytest.raises(sc.ScopeStale):
        scope.check_alive()
