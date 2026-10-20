"""End-to-end: the agent-facing Service drives unrelated real apps through the real gateway checks."""

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]

VSCODE = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"))


@pytest.fixture
def svc(tmp_path):
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("test-agent")
    yield s, token


def _wait_window(title_re, timeout=25):
    from overshoulder import scope as sc
    end = time.time() + timeout
    while time.time() < end:
        hits = sc.find_windows(title_re=title_re)
        if hits:
            return hits[0]
        time.sleep(0.25)
    raise AssertionError(f"no window matching {title_re!r}")


def _close(hwnd):
    from pywinauto import Desktop
    try:
        Desktop(backend="uia").window(handle=hwnd).close()
    except Exception:
        pass


def _focus(hwnd):
    from pywinauto import Desktop
    Desktop(backend="uia").window(handle=hwnd).set_focus()
    time.sleep(0.5)


def _one(hits):
    assert hits, "element not found"
    return hits[0]["ref"]


def test_calculator_by_uia_patterns_only(svc):
    s, token = svc
    subprocess.Popen(["calc.exe"])
    win = _wait_window("^Calculator$")
    try:
        lease = s.acquire(token, f"window:{win.hwnd}")["lease_id"]
        s.observe(token, lease)
        for aid in ("clearButton", "num7Button", "plusButton", "num8Button", "equalButton"):
            s.act(token, lease, "invoke", ref=_one(s.find(token, lease, auto_id=aid)))
        shown = s.read(token, lease, _one(s.find(token, lease, auto_id="CalculatorResults")))
        assert shown["name"].strip().endswith("15")
        s.release(token, lease)
    finally:
        _close(win.hwnd)


def test_notepad_type_into_element_and_save(svc, tmp_path):
    s, token = svc
    path = tmp_path / "svc_notepad.txt"
    path.write_text("", encoding="utf-8")
    subprocess.Popen(["notepad.exe", str(path)])
    win = _wait_window("svc_notepad")
    try:
        _focus(win.hwnd)
        lease = s.acquire(token, f"window:{win.hwnd}")["lease_id"]
        doc = _one(s.find(token, lease, control_type="Document"))
        out = s.act(token, lease, "type", ref=doc, text="print('hi') {x} + ~",
                    expect={"text_contains": "print('hi') {x} + ~"})
        assert out["verify"]["changed"]
        s.act(token, lease, "hotkey", combo="ctrl+s")
        time.sleep(0.8)
        assert path.read_text(encoding="utf-8").strip() == "print('hi') {x} + ~"
        log = s.gw.log.path.read_text(encoding="utf-8")
        assert "print('hi')" not in log, "typed text must be redacted without log_text"
    finally:
        _close(win.hwnd)


def test_charmap_set_value_and_click_by_ref(svc):
    s, token = svc
    proc = subprocess.Popen(["charmap.exe"])
    win = _wait_window("^Character Map$")
    try:
        _focus(win.hwnd)
        lease = s.acquire(token, f"window:{win.hwnd}")["lease_id"]
        box = _one(s.find(token, lease, control_type="Edit"))
        s.act(token, lease, "set_value", ref=box, value="xyz", expect={"text_contains": "xyz"})
        select = _one(s.find(token, lease, name="Select", control_type="Button"))
        out = s.act(token, lease, "click", ref=select)
        assert out["ok"]
        # Select inserts the grid's selected character at the caret, which set_value left at 0.
        value = s.read(token, lease, box)["value"]
        assert "xyz" in value and len(value.strip()) == 4
    finally:
        if proc.poll() is None:
            proc.terminate()


@pytest.mark.skipif(not VSCODE.exists(), reason="VS Code not installed")
def test_vscode_point_click_type_and_save(svc, tmp_path):
    s, token = svc
    f = tmp_path / "svc_vscode.py"
    f.write_text("", encoding="utf-8")
    subprocess.Popen([str(VSCODE), "--user-data-dir", str(tmp_path / "d"), "--extensions-dir",
                      str(tmp_path / "x"), "--disable-extensions", "--skip-welcome", "--new-window", str(f)])
    win = _wait_window("svc_vscode.py", timeout=40)
    try:
        time.sleep(2.0)
        _focus(win.hwnd)
        lease = s.acquire(token, f"window:{win.hwnd}")["lease_id"]
        b = s.observe(token, lease, max_nodes=50)["bounds"]
        x, y = (b[0] + b[2]) // 2, (b[1] + b[3]) // 2
        s.act(token, lease, "click", x=x, y=y)
        s.act(token, lease, "type", text="x = (1 + 2)  # overshoulder")
        s.act(token, lease, "hotkey", combo="ctrl+s")
        time.sleep(1.0)
        assert f.read_text(encoding="utf-8").strip() == "x = (1 + 2)  # overshoulder"
    finally:
        _close(win.hwnd)


def test_keystrokes_refused_when_another_app_has_focus(svc, tmp_path):
    from overshoulder import scope as sc

    s, token = svc
    path = tmp_path / "svc_focus.txt"
    path.write_text("", encoding="utf-8")
    subprocess.Popen(["notepad.exe", str(path)])
    note = _wait_window("svc_focus")
    subprocess.Popen(["calc.exe"])
    calc = _wait_window("^Calculator$")
    try:
        lease = s.acquire(token, f"window:{note.hwnd}")["lease_id"]
        _focus(calc.hwnd)
        with pytest.raises(sc.OutOfScope):
            s.act(token, lease, "type", text="should not appear")
        b = s.observe(token, lease, max_nodes=5)["bounds"]
        with pytest.raises(sc.OutOfScope):
            s.act(token, lease, "click", x=b[2] + 5, y=b[1] + 5)
        refusals = [r for r in s.gw.log.tail(10) if r.get("ok") is False]
        assert {r["error"]["code"] for r in refusals} == {"out_of_scope"}
    finally:
        _close(calc.hwnd)
        _close(note.hwnd)
