"""Live: drags on the real desktop. A Tk canvas box moves with posted messages (the person's mouse and
foreground untouched); a file moves into a folder in Explorer, which only takes drags from the real
mouse, through a borrow of the person's mouse."""

import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]

from test_desktop_background import Quiet, _wait_window, desk, svc  # noqa: E402, F401


def _pythonw() -> str:
    w = Path(sys.executable).with_name("pythonw.exe")
    return str(w if w.exists() else sys.executable)


def _read(path: Path, key: str, timeout: float = 15.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get(key):
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError(f"{path.name} never had {key}")


def _launch(args):
    import subprocess
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 4  # SW_SHOWNOACTIVATE
    return subprocess.Popen(args, startupinfo=si, creationflags=0x08000000)  # CREATE_NO_WINDOW: no console


def test_a_winforms_box_drags_in_the_background(desk, svc, tmp_path):  # noqa: F811
    s, token = svc
    out = tmp_path / "drag.json"
    proc = _launch(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    str(Path(__file__).with_name("dragapp.ps1")), str(out)])
    try:
        hwnd = _wait_window(r"^Overshoulder drag test$")
        ready = _read(out, "ready")
        (x0, y0), (x1, y1) = ready["start"], ready["drop"]
        time.sleep(1.0)  # WinForms may take the foreground as it opens; Quiet() takes it back first
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        with Quiet(desk):
            r = s.act(token, lease, "drag", x=x0, y=y0, to_x=x1, to_y=y1, method="background")
        assert r["result"]["method"] == "posted_drag" and r["result"]["background"]
        done = _read(out, "released")
        assert done["moves"] > 3
        assert done["box"][:2] == [30 + x1 - x0, 30 + y1 - y0], done  # it went exactly where it was dropped
        assert r["verify"]["changed"]
        s.release(token, lease)
    finally:
        proc.terminate()


def test_tk_follows_the_real_cursor_so_a_background_drag_refuses(desk, svc, tmp_path):  # noqa: F811
    """Tk polls the real cursor while a button is held; a posted drag would drop at the person's cursor."""
    from overshoulder.actions import NeedsRealInput
    s, token = svc
    out = tmp_path / "drag.json"
    proc = _launch([_pythonw(), str(Path(__file__).with_name("dragapp.py")), str(out)])
    try:
        hwnd = _wait_window(r"^Overshoulder drag test$")
        ready = _read(out, "ready")
        (x0, y0), (x1, y1) = ready["start"], ready["drop"]
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        with Quiet(desk):
            with pytest.raises(NeedsRealInput):
                s.act(token, lease, "drag", x=x0, y=y0, to_x=x1, to_y=y1, method="background")
        time.sleep(0.5)
        assert "released" not in json.loads(out.read_text(encoding="utf-8"))
        s.release(token, lease)
    finally:
        proc.terminate()


def _explorer_windows() -> set[int]:
    from overshoulder.driver import windows as w
    return {x.hwnd for x in w.top_windows() if w.class_name(x.hwnd) == "CabinetWClass"}


def test_a_file_drags_into_a_folder_in_explorer_through_a_borrow(tmp_path):
    import subprocess

    from overshoulder import settings as st
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service
    root = tmp_path / "overshoulder_drag_files"
    (root / "Inbox").mkdir(parents=True)
    (root / "drag_me.txt").write_text("moved by a drag", encoding="utf-8")
    before = _explorer_windows()
    subprocess.Popen(["explorer.exe", str(root)])
    hwnd = _wait_window(r"^overshoulder_drag_files")
    if hwnd in before or hwnd not in _explorer_windows():
        pytest.skip("Explorer opened the folder in a window that was already open; not touching it")
    s = Service(gateway_for_tests(tmp_path, settings_source=lambda: st.Settings(input_mode="borrow")),
                shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("drag-test")
    try:
        time.sleep(2.0)
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        end = time.time() + 15
        while True:  # the item view fills in after the window shows; extensions may be hidden
            items = s.find(token, lease, name_re=r"^drag_me(\.txt)?$", control_type="ListItem")
            folders = s.find(token, lease, name="Inbox", control_type="ListItem")
            if (items and folders) or time.time() > end:
                break
            time.sleep(0.5)
        item, folder = items[0]["ref"], folders[0]["ref"]
        r = None
        for _ in range(3):  # the borrow waits for the person to be idle; give it a few tries
            try:
                r = s.act(token, lease, "drag", ref=item, to_ref=folder)
                break
            except Exception as e:
                if getattr(e, "code", "") != "person_active":
                    raise
        assert r is not None, "the person stayed active for every try"
        assert r["result"]["method"] == "real_drag" and not r["result"]["background"]
        assert r["result"]["borrow"]["mode"] == "borrow"
        assert r["result"]["borrow"]["restored"] == {"cursor": True, "foreground": True}, r["result"]["borrow"]
        end = time.time() + 10
        while time.time() < end and not (root / "Inbox" / "drag_me.txt").exists():
            time.sleep(0.2)
        assert (root / "Inbox" / "drag_me.txt").exists() and not (root / "drag_me.txt").exists()
        s.release(token, lease)
    finally:
        import bghelpers
        bghelpers.close_discarding(hwnd)
