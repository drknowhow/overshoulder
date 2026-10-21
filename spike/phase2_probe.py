"""Follow-up probes for the phase 2 spike (run after phase2_background.py left gaps).

1. Notepad in the background: UIA tree size, File > Save through UIA menus, read-back via TextPattern.
2. VS Code in the background: list Edit elements, set the editor text through UIA, WM_CHAR after
   focusing the editor element through UIA.
Same measurement rules as phase2_background.py (sentinel in front, injected-input hook).
"""

import json
import os
import time
from pathlib import Path

import phase2_background as S
from overshoulder.driver import uia
from overshoulder.driver import windows as w


def text_of(hwnd):
    el = S.find_el(hwnd, control_type="Document")
    if el is None:
        return None
    try:
        return uia._wrapper(el).iface_text.DocumentRange.GetText(-1)
    except Exception as e:
        return f"<no TextPattern: {type(e).__name__}>"


def notepad():
    path = S.TMP / "probe_notes.txt"
    path.write_text("hello\n", encoding="utf-8")
    S.launch_noactivate(["notepad.exe", str(path)])
    win = S.wait_window(r"probe_notes.*Notepad")
    time.sleep(1.5)
    S.force_foreground(S.SENTINEL)
    h = win.hwnd
    cache = uia.ElementCache()
    els = uia.walk(cache, h, max_nodes=4000)
    S.ROWS.append({"app": "notepad", "action": "probe", "method": f"UIA walk in background: {len(els)} nodes",
                   "effect": len(els) > 20})
    print("background walk nodes:", len(els), "text:", repr(text_of(h))[:60], flush=True)
    editor = S.child_by_class(h, "RichEditD2DPT")
    S.post_chars(editor, "typed ")
    time.sleep(0.4)

    def menu_save():
        file_menu = S.find_el(h, name="File", control_type="MenuItem")
        wr = uia._wrapper(file_menu)
        try:
            wr.expand()
        except Exception:
            wr.invoke()
        time.sleep(1.0)
        save = S.find_el(h, name="Save", control_type="MenuItem")
        if save is None:
            for x in S.all_titled_windows():
                if w.pid_of(x) == w.pid_of(h):
                    save = S.find_el(x, name="Save", control_type="MenuItem")
                    if save:
                        break
        print("  save item:", save and save.info.name, flush=True)
        uia._wrapper(save).invoke()

    S.measure("notepad", "save", "UIA menu File>Save", menu_save,
              lambda: ("typed" in path.read_text(encoding="utf-8"), path.read_text(encoding="utf-8")[:30]),
              settle=1.5)
    S.measure("notepad", "read", "TextPattern in background", lambda: None,
              lambda: ("typed" in (text_of(h) or ""), repr(text_of(h))[:40]))
    return h


def vscode():
    exe = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"))
    data = S.TMP / "vsc2"
    (data / "User").mkdir(parents=True, exist_ok=True)
    (data / "User" / "settings.json").write_text(json.dumps({
        "editor.accessibilitySupport": "on", "files.autoSave": "afterDelay", "files.autoSaveDelay": 300,
        "workbench.startupEditor": "none", "chat.commandCenter.enabled": False,
        "workbench.secondarySideBar.defaultVisibility": "hidden"}), encoding="utf-8")
    f = S.TMP / "probe.py"
    f.write_text("", encoding="utf-8")
    S.launch_noactivate([str(exe), "--user-data-dir", str(data), "--extensions-dir", str(S.TMP / "vscx2"),
                         "--disable-extensions", "--new-window", "--force-renderer-accessibility", str(f)])
    win = S.wait_window(r"probe\.py", timeout=45)
    time.sleep(4.0)
    S.force_foreground(S.SENTINEL)
    h = win.hwnd
    cache = uia.ElementCache()
    els = uia.walk(cache, h, max_nodes=6000)
    edits = [(e.ref, e.info.name[:70], e.info.control_type) for e in els if e.info.control_type == "Edit"]
    print("vscode edits:", json.dumps(edits, indent=0)[:900], flush=True)
    editor = next((e for e in els if e.info.control_type == "Edit" and "chat" not in (e.info.name or "").lower()
                   and "search" not in (e.info.name or "").lower()), None)
    print("  chosen editor:", editor and editor.info.name, flush=True)

    def on_disk(s):
        return lambda: (s in f.read_text(encoding="utf-8", errors="replace"),
                        f.read_text(encoding="utf-8", errors="replace")[-40:])

    S.measure("vscode", "type", "UIA ValuePattern on editor", lambda: uia.set_value(editor, "a = 1"),
              on_disk("a = 1"), settle=1.5)
    rw = S.child_by_class(h, "Chrome_RenderWidgetHostHWND")
    S.measure("vscode", "type", "UIA focus editor + WM_CHAR",
              lambda: (uia.focus(editor), time.sleep(0.3), S.post_chars(rw, "b = 2")), on_disk("b = 2"),
              settle=1.5)
    return h


def main():
    S.COUNTER = S.InjectedCounter()
    S.SENTINEL = S.start_sentinel()
    S.force_foreground(S.SENTINEL)
    opened = []
    try:
        for fn in (notepad, vscode):
            try:
                opened.append(fn())
            except Exception as e:
                print(f"{fn.__name__} crashed: {e!r}", flush=True)
    finally:
        for h in opened:
            S.close_window(h)
        (S.OUT / "phase2_probe.json").write_text(json.dumps(S.ROWS, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
