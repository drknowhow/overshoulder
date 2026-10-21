"""Live: agents act on background windows without the person's mouse, keyboard or foreground.

Each test runs with a sentinel window in front (standing in for the person's own work) and asserts,
besides the action's effect, that no input was injected, the sentinel kept the foreground, and the
real cursor did not move. Uses method="background", so real input would raise instead of running.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]

VSCODE = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"))


@pytest.fixture(scope="module")
def desk():
    import bghelpers as H
    counter = H.InjectedCounter()
    sentinel = H.start_sentinel()
    return H, counter, sentinel


@pytest.fixture
def svc(tmp_path):
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service
    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    return s, s.gw.registry.add("bg-test")


def _wait_window(title_re, timeout=45):
    import ctypes
    import re

    from overshoulder.driver import windows as w
    user32 = ctypes.windll.user32
    end = time.time() + timeout
    while time.time() < end:
        found = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def cb(h, _, found=found):
            if user32.IsWindowVisible(h) and re.search(title_re, w.title(h)):
                found.append(h)
            return True

        user32.EnumWindows(cb, 0)
        if found:
            return found[0]
        time.sleep(0.25)
    raise AssertionError(f"no window {title_re}")


class Quiet:
    """Asserts nothing touched the person's input or foreground during the block."""

    def __init__(self, desk):
        self.H, self.counter, self.sentinel = desk

    def __enter__(self):
        assert self.H.take_foreground(self.sentinel), "sentinel could not take the foreground"
        self.counter.injected = 0
        self.cur = self.H.cursor()
        return self

    def __exit__(self, exc_type, *a):
        if exc_type:
            return False
        fg = self.H.user32.GetForegroundWindow()
        assert self.counter.injected == 0, f"{self.counter.injected} injected input events"
        assert fg == self.sentinel, "the person's foreground window changed"
        assert self.H.cursor() == self.cur, "the real cursor moved"
        return False


def _close(hwnd):
    import bghelpers
    bghelpers.close_discarding(hwnd)


def _no_running(*names):
    out = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower()
    for n in names:
        if n.lower() in out:
            pytest.skip(f"{n} is already running; the test would touch the person's window")


def test_notepad_type_and_save_in_the_background(desk, svc, tmp_path):
    _no_running("notepad.exe")
    s, token = svc
    H = desk[0]
    path = tmp_path / "bg_notes.txt"
    path.write_text("first line\n", encoding="utf-8")
    __import__("bghelpers").wait_no_window("Notepad")
    H.launch_noactivate(["notepad.exe", str(path)])
    hwnd = _wait_window(r"bg_notes.*Notepad")
    time.sleep(1.5)
    try:
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        with Quiet(desk):
            doc = s.find(token, lease, control_type="Document")[0]["ref"]
            out = s.act(token, lease, "type", ref=doc, text="typed in the background (1+1) ", method="background")
            assert out["result"]["method"] in ("posted_chars", "replace_selection")
            save = s.act(token, lease, "hotkey", combo="ctrl+s", method="background")
            assert save["result"]["method"] == "posted_keys"
        time.sleep(1.0)
        assert "typed in the background (1+1)" in path.read_text(encoding="utf-8")
    finally:
        _close(hwnd)


def test_calculator_in_the_background(desk, svc):
    _no_running("CalculatorApp.exe")
    s, token = svc
    H = desk[0]
    H.launch_noactivate(["calc.exe"])
    hwnd = _wait_window(r"^Calculator$")
    time.sleep(5.0)  # UWP apps take the foreground at launch; Quiet() takes it back before acting
    try:
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        with Quiet(desk):
            clear = s.find(token, lease, auto_id="clearButton")[0]["ref"]
            out = s.act(token, lease, "invoke", ref=clear, method="background")
            # UIA Invoke on XAML apps grabs the foreground; the guard must have put it back
            assert out["result"].get("focus", {}).get("restored", True)
            s.act(token, lease, "type", text="7*6=", method="background")
        shown = s.read(token, lease, s.find(token, lease, auto_id="CalculatorResults")[0]["ref"])
        assert shown["name"].strip().endswith("42"), shown
    finally:
        _close(hwnd)


def test_character_map_in_the_background(desk, svc):
    _no_running("charmap.exe")
    s, token = svc
    H = desk[0]
    proc = H.launch_noactivate(["charmap.exe"])
    hwnd = _wait_window(r"^Character Map$")
    time.sleep(1.0)
    try:
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        with Quiet(desk):
            box = s.find(token, lease, control_type="Edit")[0]["ref"]
            s.act(token, lease, "set_value", ref=box, value="bg", method="background")
            select = s.find(token, lease, name="Select", control_type="Button")[0]["ref"]
            out = s.act(token, lease, "click", ref=select, method="background")
            assert out["result"]["method"] == "uia_invoke"
        value = s.read(token, lease, box)["value"].strip()
        assert value.endswith("bg") and len(value) == 3, value
    finally:
        if proc.poll() is None:
            proc.terminate()


@pytest.mark.skipif(not VSCODE.exists(), reason="VS Code not installed")
def test_vscode_typing_in_the_background(desk, svc, tmp_path):
    s, token = svc
    H = desk[0]
    data = tmp_path / "vsc"
    (data / "User").mkdir(parents=True)
    (data / "User" / "settings.json").write_text(json.dumps({
        "editor.accessibilitySupport": "on", "files.autoSave": "afterDelay", "files.autoSaveDelay": 300,
        "workbench.startupEditor": "none"}), encoding="utf-8")
    f = tmp_path / "bg_code.py"
    f.write_text("", encoding="utf-8")
    H.launch_noactivate([str(VSCODE), "--user-data-dir", str(data), "--extensions-dir", str(tmp_path / "x"),
                         "--disable-extensions", "--new-window", "--force-renderer-accessibility", str(f)])
    hwnd = _wait_window(r"bg_code\.py", timeout=60)
    time.sleep(4.0)
    try:
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        from overshoulder.actions import NeedsRealInput
        with Quiet(desk):
            # Chromium in the background does not reliably commit posted typing (measured), so background
            # mode must refuse rather than report a success that did not happen.
            editor = s.find(token, lease, name="bg_code.py", control_type="Edit")[0]["ref"]
            with pytest.raises(NeedsRealInput):
                s.act(token, lease, "type", ref=editor, text="answer = 6 * 7", method="background")
            # Clicks still work in the background: open the Explorer panel by its activity-bar tab.
            tab = s.find(token, lease, name_re="^Explorer", control_type="TabItem")[0]["ref"]
            out = s.act(token, lease, "click", ref=tab, method="background")
            assert out["result"]["background"]
        assert f.read_text(encoding="utf-8") == ""
    finally:
        _close(hwnd)


def test_background_method_refuses_instead_of_using_real_input(desk, svc):
    """A UWP core window ignores posted clicks; with method=background that must refuse, not click."""
    _no_running("CalculatorApp.exe")
    from overshoulder.actions import NeedsRealInput
    s, token = svc
    H = desk[0]
    H.launch_noactivate(["calc.exe"])
    hwnd = _wait_window(r"^Calculator$")
    time.sleep(5.0)
    try:
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        nine = s.find(token, lease, auto_id="num9Button")[0]
        x = (nine["rect"][0] + nine["rect"][2]) // 2
        y = (nine["rect"][1] + nine["rect"][3]) // 2
        with Quiet(desk):
            with pytest.raises(NeedsRealInput):
                s.act(token, lease, "click", x=x, y=y, method="background")
    finally:
        _close(hwnd)
