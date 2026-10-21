"""After VS Code loses activation, can posted messages make it accept background typing again?

Each variant posts its focus messages, then WM_CHAR, and checks read-back and the foreground.
"""

import json
import os
import tempfile
import time
from pathlib import Path

import phase2_background as S
from overshoulder.driver import background as bg

WM_SETFOCUS, WM_ACTIVATE, WM_NCACTIVATE = 0x0007, 0x0006, 0x0086


def value(ed):
    return S.uia.read(ed).get("value", "")


def main():
    S.COUNTER = S.InjectedCounter()
    S.SENTINEL = S.start_sentinel()
    tmp = Path(tempfile.mkdtemp(prefix="os_vsrefocus_"))
    data = tmp / "vsc"
    (data / "User").mkdir(parents=True)
    (data / "User" / "settings.json").write_text(json.dumps({
        "editor.accessibilitySupport": "on", "workbench.startupEditor": "none"}), encoding="utf-8")
    f = tmp / "refocus.py"
    f.write_text("", encoding="utf-8")
    exe = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"))
    S.launch_noactivate([str(exe), "--user-data-dir", str(data), "--extensions-dir", str(tmp / "x"),
                         "--disable-extensions", "--new-window", "--force-renderer-accessibility", str(f)])
    win = S.wait_window(r"refocus\.py", timeout=60)
    time.sleep(4.0)
    print("fg after launch:", S.w.title(S.user32.GetForegroundWindow()), flush=True)
    for _ in range(5):
        if bg.set_foreground(S.SENTINEL) or S.force_foreground(S.SENTINEL):
            break
        time.sleep(0.5)
    time.sleep(1.0)
    print('handover ok:', S.user32.GetForegroundWindow() == S.SENTINEL, flush=True)
    h = win.hwnd
    ed = S.find_el(h, name="refocus.py", control_type="Edit")
    rw = bg.input_window(h)
    WM_KILLFOCUS = 0x0008
    variants = [
        ("nothing (inactive, as left)", lambda: None),
        ("posted WM_SETFOCUS to render widget", lambda: S.user32.PostMessageW(rw, WM_SETFOCUS, 0, 0)),
        ("posted WM_ACTIVATE(active)+WM_SETFOCUS", lambda: (S.user32.PostMessageW(h, WM_ACTIVATE, 1, 0),
                                                              S.user32.PostMessageW(rw, WM_SETFOCUS, 0, 0))),
        ("sent WM_SETFOCUS to render widget", lambda: S.user32.SendMessageW(rw, WM_SETFOCUS, 0, 0)),
    ]
    for i, (name, prep) in enumerate(variants):
        token = f"v{i}x"
        before = value(ed)
        if S.user32.GetForegroundWindow() != S.SENTINEL:
            bg.set_foreground(S.SENTINEL); time.sleep(0.5)
        S.COUNTER.reset()
        prep()
        time.sleep(0.4)
        bg.post_text(rw, token)
        time.sleep(1.2)
        after = value(ed)
        print(f"[{name}] landed={token in after and token not in before} fg_is_sentinel="
              f"{S.user32.GetForegroundWindow() == S.SENTINEL} injected={S.COUNTER.injected} value={after!r}",
              flush=True)
    S.close_window(h)


if __name__ == "__main__":
    main()
