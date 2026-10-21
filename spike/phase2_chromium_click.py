"""Do Chromium/Electron windows accept posted mouse clicks while in the background? (VS Code)"""

import json
import os
import time
from pathlib import Path

import phase2_background as S


def main():
    S.COUNTER = S.InjectedCounter()
    S.SENTINEL = S.start_sentinel()
    S.force_foreground(S.SENTINEL)
    exe = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"))
    data = S.TMP / "vsc3"
    (data / "User").mkdir(parents=True, exist_ok=True)
    (data / "User" / "settings.json").write_text(json.dumps({"workbench.startupEditor": "none"}), encoding="utf-8")
    f = S.TMP / "click.py"
    f.write_text("", encoding="utf-8")
    S.launch_noactivate([str(exe), "--user-data-dir", str(data), "--extensions-dir", str(S.TMP / "vscx3"),
                         "--disable-extensions", "--new-window", "--force-renderer-accessibility", str(f)])
    win = S.wait_window(r"click\.py", timeout=45)
    time.sleep(4.0)
    S.force_foreground(S.SENTINEL)
    h = win.hwnd
    tab = S.find_el(h, name_re=r"^Explorer", control_type="TabItem") or S.find_el(h, name_re=r"^Search",
                                                                                   control_type="TabItem")
    print("tab:", tab and tab.info.name, flush=True)
    r = S.uia.element_state(tab)["rect"]

    def panel_open():
        return S.find_el(h, name="Open Folder", control_type="Button") is not None

    for attempt in ("first", "second"):
        was = panel_open()
        S.measure("vscode", "click", f"posted click on activity tab ({attempt})",
                  lambda: S.post_click(h, *r.center()),
                  lambda was=was: (panel_open() != was, f"Explorer panel open: {was} -> {panel_open()}"),
                  settle=1.0)
    S.close_window(h)
    (S.OUT / "phase2_chromium.json").write_text(json.dumps(S.ROWS, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
