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
    from pywinauto import Desktop

    path = tmp_path / "overshoulder_input_test.txt"
    path.write_text("", encoding="utf-8")
    __import__("bghelpers").wait_no_window("Notepad")
    subprocess.Popen(["notepad.exe", str(path)])
    win = Desktop(backend="uia").window(title_re=r".*overshoulder_input_test.*Notepad")
    win.wait("ready", timeout=20)
    win.set_focus()
    time.sleep(0.8)
    yield win, path
    import bghelpers
    bghelpers.close_discarding(win.handle)


def test_type_text_and_hotkey_save_literal_text(notepad):
    from overshoulder.driver import input as inp

    win, path = notepad
    doc = win.child_window(control_type="Document").wrapper_object()
    r = doc.rectangle()
    inp.click(r.left + 40, r.top + 20)
    text = "print('a + b') {x} ~%^"
    inp.type_text(win.handle, text)
    inp.hotkey(win.handle, "ctrl+s")
    time.sleep(1.0)
    assert path.read_text(encoding="utf-8").strip() == text


def test_type_text_refuses_when_target_is_not_foreground(notepad):
    from overshoulder.driver import input as inp

    with pytest.raises(inp.FocusLost):
        inp.type_text(0x1, "should not be typed")


def test_person_watch_ignores_our_own_injected_input():
    from overshoulder.driver import input as inp
    from overshoulder.driver.person import PersonWatch
    with PersonWatch() as watch:
        assert watch.ok, "low-level hooks could not be installed"
        inp.send([inp._key(0x87), inp._key(0x87, flags=inp.KEYEVENTF_KEYUP)])  # F24: no app uses it
        time.sleep(0.3)
        assert not watch.seen
