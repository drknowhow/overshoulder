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


def test_never_mode_refuses_what_only_real_input_could_do(desk, tmp_path):
    """Input mode "never": even method="auto" may not fall back to the person's mouse or foreground."""
    _no_running("CalculatorApp.exe")
    from overshoulder import settings as st
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.gateway.errors import NeedsRealInput
    from overshoulder.service import Service
    s = Service(gateway_for_tests(tmp_path, settings_source=lambda: st.Settings(input_mode="never")),
                shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("never-test")
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
            # A posted click cannot reach a UWP core window, and the mode forbids the real one.
            with pytest.raises(NeedsRealInput):
                s.act(token, lease, "click", x=x, y=y, method="auto")
            with pytest.raises(NeedsRealInput):
                s.act(token, lease, "activate", method="auto")
        codes = [r["error"]["code"] for r in s.gw.log.tail(10) if r.get("ok") is False]
        assert codes == ["needs_real_input", "needs_real_input"]
    finally:
        _close(hwnd)


def test_borrow_mode_waits_for_the_person_then_gives_back_cursor_and_window(desk, tmp_path):
    """Input mode "borrow": a click only real input can do waits until the person has been idle, brings
    the window forward, clicks, then puts the cursor and the person's window back."""
    _no_running("CalculatorApp.exe")
    from overshoulder import settings as st
    from overshoulder.driver import input as inp
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service
    s = Service(gateway_for_tests(tmp_path, settings_source=lambda: st.Settings(input_mode="borrow",
                                                                                borrow_idle_s=2)),
                shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("borrow-test")
    H, _, sentinel = desk
    H.launch_noactivate(["calc.exe"])
    hwnd = _wait_window(r"^Calculator$")
    time.sleep(5.0)
    try:
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        s.act(token, lease, "invoke", ref=s.find(token, lease, auto_id="clearButton")[0]["ref"],
              method="background")
        nine = s.find(token, lease, auto_id="num9Button")[0]
        x = (nine["rect"][0] + nine["rect"][2]) // 2
        y = (nine["rect"][1] + nine["rect"][3]) // 2
        assert H.take_foreground(sentinel)
        cursor_before = H.cursor()
        person = H.simulate_person(2.5)
        deadline = time.time() + 5
        while inp.person_idle_s() > 0.5 and time.time() < deadline:
            time.sleep(0.05)
        assert inp.person_idle_s() <= 0.5, "the stand-in person never started"
        t0 = time.monotonic()
        out = s.act(token, lease, "click", x=x, y=y, method="auto")
        took = time.monotonic() - t0
        person.wait(10)
        shown = s.read(token, lease, s.find(token, lease, auto_id="CalculatorResults")[0]["ref"])["name"]
        evidence = {
            "what": "borrow mode: a click on Calculator's 9 (posted clicks cannot reach a UWP core window), "
                    "while another process stood in for the person by pressing F24 every 0.2 s for 2.5 s",
            "input_mode": "borrow", "borrow_idle_s": 2, "person_active_for_s": 2.5,
            "act_took_s": round(took, 2), "method": out["result"]["method"], "borrow": out["result"]["borrow"],
            "attempts": out["result"]["attempts"],
            "cursor_before": list(cursor_before), "cursor_after": list(H.cursor()),
            "persons_window_in_front_after": H.user32.GetForegroundWindow() == sentinel,
            "calculator_shows": shown}
        if os.environ.get("OVERSHOULDER_EVIDENCE_DIR"):
            out_dir = Path(os.environ["OVERSHOULDER_EVIDENCE_DIR"])
            out_dir.mkdir(parents=True, exist_ok=True)
            (out_dir / "borrow_calculator.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
        borrow = out["result"]["borrow"]
        assert out["result"]["method"] == "real_click"
        assert borrow["waited_s"] >= 3.0, evidence  # 2.5 s of the person, then 2 s idle
        assert borrow["brought_forward"] and borrow["restored"] == {"cursor": True, "foreground": True}, evidence
        assert evidence["persons_window_in_front_after"] and H.cursor() == cursor_before, evidence
        assert shown.strip().endswith("9"), shown
    finally:
        _close(hwnd)
