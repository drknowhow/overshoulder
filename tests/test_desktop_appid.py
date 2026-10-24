import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]
VSCODE = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe"


def _bind_new(cmd: list[str], title_re: str):
    """Start a program, bind its window, close it; the binding outlives the window."""
    from pywinauto import Desktop

    from overshoulder import scope as sc
    __import__("bghelpers").wait_no_window(title_re.strip("^$"))
    proc = subprocess.Popen(cmd)
    hits = []
    try:
        for _ in range(80):
            hits = sc.find_windows(title_re=title_re)
            if hits:
                break
            time.sleep(0.25)
        assert hits, f"{cmd[0]} did not open a window"
        return sc.bind_window(hits[0].hwnd)
    finally:
        for h in hits:
            try:
                Desktop(backend="uia").window(handle=h.hwnd).close()
            except Exception:
                pass
        if proc.poll() is None:
            proc.terminate()
        __import__("bghelpers").wait_no_window(title_re.strip("^$"))


@pytest.mark.parametrize("cmd, title_re, app_id", [
    (["calc.exe"], "^Calculator$", "Microsoft.WindowsCalculator_8wekyb3d8bbwe"),
    (["charmap.exe"], "^Character Map$", "microsoft-corporation.charmap"),
    pytest.param([str(VSCODE), "--new-window", "--disable-extensions", "--skip-welcome", "--user-data-dir"],
                 "Visual Studio Code$", "microsoft-corporation.code",
                 marks=pytest.mark.skipif(not VSCODE.exists(), reason="VS Code is not installed for this user")),
])
def test_an_app_keeps_its_id_across_restarts(cmd, title_re, app_id):
    with tempfile.TemporaryDirectory() as first_dir, tempfile.TemporaryDirectory() as second_dir:
        # VS Code gets an empty data folder each time, so the person's own windows and settings stay out of it
        extra = cmd[-1] == "--user-data-dir"
        first = _bind_new(cmd + [first_dir] if extra else cmd, title_re)
        second = _bind_new(cmd + [second_dir] if extra else cmd, title_re)
    assert first.app is not None and first.app.id == second.app.id == app_id
    assert first.pid != second.pid
    from overshoulder.scope import WindowScope
    assert WindowScope(second).describe()["app"]["id"] == app_id
