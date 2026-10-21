"""Phase 2 spike: which actions work on a BACKGROUND window without touching the person's input?

For each (app, action, method) it records:
  effect     the action did what it should (verified by UIA read-back, the file on disk, or a
             PrintWindow before/after pixel diff, all of which work on covered windows)
  injected   synthetic input events seen by a low-level hook during the action (must be 0)
  fg_changed the foreground window changed (must be False)
  cursor     the real cursor moved (must be False)
A sentinel window owned by this script holds the foreground for the whole run, so every target app
is genuinely in the background. Apps are launched with SW_SHOWNOACTIVATE.

Run: .venv\\Scripts\\python spike\\phase2_background.py   (writes spike/out/phase2.json)
Refuses to run if Notepad, Calculator or Character Map is already open (Notepad would open our
file as a tab in the person's own window).
"""

import ctypes
import ctypes.wintypes as wt
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import overshoulder  # noqa: F401  (per-monitor v2 DPI before pywinauto)
from overshoulder.driver import uia
from overshoulder.driver import windows as w
from overshoulder.recorder import print_window_bgra

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
OUT = Path(__file__).parent / "out"
TMP = Path(tempfile.mkdtemp(prefix="os_phase2_"))

WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP = 0x0200, 0x0201, 0x0202
WM_KEYDOWN, WM_KEYUP, WM_CHAR = 0x0100, 0x0101, 0x0102
EM_REPLACESEL, BM_CLICK = 0x00C2, 0x00F5
MK_LBUTTON = 0x0001
SMTO_ABORTIFHUNG = 0x0002
VK_CONTROL = 0x11

user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_void_p, wt.UINT, wt.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.RealChildWindowFromPoint.argtypes = [wt.HWND, wt.POINT]
user32.RealChildWindowFromPoint.restype = wt.HWND
user32.ChildWindowFromPointEx.argtypes = [wt.HWND, wt.POINT, wt.UINT]
user32.ChildWindowFromPointEx.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND


# -- measurement ----------------------------------------------------------------------------------

class InjectedCounter:
    """Counts injected vs physical mouse/keyboard events with low-level hooks on a pump thread."""

    def __init__(self):
        self.injected = 0
        self.physical = 0
        ready = threading.Event()
        threading.Thread(target=self._run, args=(ready,), daemon=True).start()
        ready.wait(5)

    def _run(self, ready):
        from overshoulder.watchdog import HOOKPROC, KBDLLHOOKSTRUCT, MSLLHOOKSTRUCT

        def make(struct, flag):
            @HOOKPROC
            def proc(n, wp, lp):
                if n == 0:
                    if ctypes.cast(lp, ctypes.POINTER(struct)).contents.flags & flag:
                        self.injected += 1
                    else:
                        self.physical += 1
                return user32.CallNextHookEx(None, n, wp, lp)
            return proc

        self._kb = make(KBDLLHOOKSTRUCT, 0x10)
        self._ms = make(MSLLHOOKSTRUCT, 0x01)
        hmod = kernel32.GetModuleHandleW(None)
        user32.SetWindowsHookExW(13, self._kb, hmod, 0)
        user32.SetWindowsHookExW(14, self._ms, hmod, 0)
        ready.set()
        msg = wt.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def reset(self):
        self.injected = self.physical = 0


def cursor():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


ROWS = []
COUNTER = None


SENTINEL = None


def measure(app, action, method, fn, verify, settle=0.7):
    if SENTINEL and user32.GetForegroundWindow() != SENTINEL:
        force_foreground(SENTINEL)
    fg0, cur0 = user32.GetForegroundWindow(), cursor()
    COUNTER.reset()
    err = None
    try:
        fn()
    except Exception as e:  # spike: record every failure mode
        err = f"{type(e).__name__}: {e}"[:200]
    time.sleep(settle)
    try:
        effect, detail = verify()
    except Exception as e:
        effect, detail = False, f"verify error {type(e).__name__}: {e}"[:200]
    row = {"app": app, "action": action, "method": method, "effect": bool(effect), "detail": str(detail)[:160],
           "injected": COUNTER.injected, "physical": COUNTER.physical,
           "fg_changed": user32.GetForegroundWindow() != fg0, "cursor_moved": cursor() != cur0, "error": err}
    row["background_ok"] = row["effect"] and row["injected"] == 0 and not row["fg_changed"] and \
        not row["cursor_moved"]
    ROWS.append(row)
    mark = "OK " if row["background_ok"] else ("eff" if row["effect"] else "-- ")
    print(f"[{mark}] {app:9} {action:16} {method:26} inj={row['injected']} fg={row['fg_changed']} "
          f"cur={row['cursor_moved']} {row['detail'][:70]} {err or ''}", flush=True)
    return row


# -- sentinel: our own window holds the foreground -----------------------------------------------

def start_sentinel():
    import tkinter as tk
    box = {}
    ready = threading.Event()

    def run():
        root = tk.Tk()
        root.title("Overshoulder spike sentinel")
        root.geometry("420x90+40+40")
        tk.Label(root, text="Overshoulder background-input spike is running.\nThis window holds the focus.",
                 font=("Segoe UI", 11)).pack(expand=True)
        root.update()
        box["hwnd"] = user32.GetParent(root.winfo_id()) or root.winfo_id()
        ready.set()
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    return box["hwnd"]


def force_foreground(hwnd):
    """AttachThreadInput trick: no synthetic input, used only to set up the sentinel."""
    fg = user32.GetForegroundWindow()
    fg_tid = user32.GetWindowThreadProcessId(fg, None)
    my_tid = user32.GetWindowThreadProcessId(hwnd, None)
    user32.AttachThreadInput(my_tid, fg_tid, True)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(my_tid, fg_tid, False)
    time.sleep(0.3)
    return user32.GetForegroundWindow() == hwnd


# -- helpers --------------------------------------------------------------------------------------

def launch_noactivate(args):
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 4  # SW_SHOWNOACTIVATE
    return subprocess.Popen(args, startupinfo=si)


class _Win:
    def __init__(self, hwnd):
        self.hwnd, self.title, self.pid = hwnd, w.title(hwnd), w.pid_of(hwnd)


def all_titled_windows():
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h) and not user32.GetWindow(h, 4) and w.title(h):
            out.append(h)
        return True

    user32.EnumWindows(cb, 0)
    return out


def wait_window(title_re, timeout=45):
    end = time.time() + timeout
    while time.time() < end:
        for h in all_titled_windows():
            if re.search(title_re, w.title(h)):
                print(f"  found {w.title(h)!r} cloaked={w.is_cloaked(h)}", flush=True)
                return _Win(h)
        time.sleep(0.25)
    raise RuntimeError(f"no window {title_re}")


def children(hwnd):
    out = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        out.append(h)
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return out


def child_by_class(hwnd, cls):
    return next((h for h in children(hwnd) if w.class_name(h) == cls), None)


def deepest_child_at(top, x, y):
    """The innermost child window under a screen point (like WindowFromPoint, but inside `top`)."""
    h = top
    while True:
        pt = wt.POINT(x, y)
        user32.ScreenToClient(h, ctypes.byref(pt))
        c = user32.ChildWindowFromPointEx(h, pt, 0x0001 | 0x0004)  # skip invisible, transparent
        if not c or c == h:
            return h
        h = c


def post_click(top, x, y):
    target = deepest_child_at(top, x, y)
    pt = wt.POINT(x, y)
    user32.ScreenToClient(target, ctypes.byref(pt))
    lp = (pt.y << 16) | (pt.x & 0xFFFF)
    user32.PostMessageW(target, WM_MOUSEMOVE, 0, lp)
    user32.PostMessageW(target, WM_LBUTTONDOWN, MK_LBUTTON, lp)
    user32.PostMessageW(target, WM_LBUTTONUP, 0, lp)
    return target


def post_chars(hwnd, text):
    for ch in text:
        user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 1)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]


def thread_focus(hwnd):
    gti = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    user32.GetGUIThreadInfo(tid, ctypes.byref(gti))
    return gti.hwndFocus or 0


def send_text_msg(hwnd, msg, wparam, text):
    buf = ctypes.create_unicode_buffer(text)
    res = ctypes.c_size_t()
    ok = user32.SendMessageTimeoutW(hwnd, msg, wparam, ctypes.cast(buf, ctypes.c_void_p), SMTO_ABORTIFHUNG,
                                    2000, ctypes.byref(res))
    if not ok:
        raise RuntimeError("SendMessageTimeout failed")


def posted_hotkey_with_state(hwnd, vk_mod, vk):
    """Experimental: borrow the target thread's key state (AttachThreadInput + SetKeyboardState), post keys."""
    my_tid = kernel32.GetCurrentThreadId()
    tid = user32.GetWindowThreadProcessId(hwnd, None)
    user32.AttachThreadInput(my_tid, tid, True)
    try:
        state = (ctypes.c_ubyte * 256)()
        user32.GetKeyboardState(state)
        saved = bytes(state)
        state[vk_mod] = 0x80
        user32.SetKeyboardState(state)
        user32.PostMessageW(hwnd, WM_KEYDOWN, vk, 1)
        user32.PostMessageW(hwnd, WM_KEYUP, vk, 0xC0000001)
        time.sleep(0.4)
        restore = (ctypes.c_ubyte * 256)(*saved)
        user32.SetKeyboardState(restore)
    finally:
        user32.AttachThreadInput(my_tid, tid, False)


def pw_snapshot(hwnd):
    got = print_window_bgra(hwnd)
    return got[0] if got else b""


def changed(a, b):
    if not a or not b or len(a) != len(b):
        return True
    step = 4 * 97
    diff = sum(1 for i in range(0, len(a), step) if a[i] != b[i])
    return diff > 3


def find_el(root_hwnd, **kw):
    cache = uia.ElementCache()
    els = []
    for r in uia.roots_for_window(root_hwnd):
        els += uia.walk(cache, r, max_nodes=4000)
    hits = uia.find(els, kw.get("name"), kw.get("name_re"), kw.get("control_type"), kw.get("auto_id"))
    return hits[0] if hits else None


def doc_text(root_hwnd):
    el = find_el(root_hwnd, control_type="Document")
    return uia.read(el).get("text", "") if el else ""


def close_window(hwnd):
    from pywinauto import Desktop
    try:
        Desktop(backend="uia").window(handle=hwnd).close()
    except Exception:
        pass


# -- apps -----------------------------------------------------------------------------------------

def test_notepad():
    path = TMP / "phase2_notes.txt"
    path.write_text("start\n", encoding="utf-8")
    launch_noactivate(["notepad.exe", str(path)])
    win = wait_window(r"phase2_notes.*Notepad")
    time.sleep(1.0)
    hwnd = win.hwnd
    editor = child_by_class(hwnd, "RichEditD2DPT")
    focus = thread_focus(hwnd)
    print(f"notepad hwnd={hwnd:#x} editor={editor or 0:#x} thread_focus={focus:#x} "
          f"focus_class={w.class_name(focus) if focus else None}", flush=True)
    target = editor or focus

    measure("notepad", "type", "WM_CHAR post", lambda: post_chars(target, "A1(x){y}"),
            lambda: ("A1(x){y}" in doc_text(hwnd), doc_text(hwnd)[-30:]))
    measure("notepad", "type", "EM_REPLACESEL", lambda: send_text_msg(target, EM_REPLACESEL, 1, "B2+~"),
            lambda: ("B2+~" in doc_text(hwnd), doc_text(hwnd)[-30:]))
    doc = find_el(hwnd, control_type="Document")
    has_value = False
    try:
        uia._wrapper(doc).iface_value  # noqa: B018
        has_value = True
    except Exception:
        pass
    ROWS.append({"app": "notepad", "action": "probe", "method": "Document has ValuePattern", "effect": has_value})

    def file_has(s):
        return lambda: (s in path.read_text(encoding="utf-8", errors="replace"),
                        path.read_text(encoding="utf-8", errors="replace")[-30:])

    measure("notepad", "save", "posted ctrl+s (no state)", lambda: (
        user32.PostMessageW(target, WM_KEYDOWN, VK_CONTROL, 1),
        user32.PostMessageW(target, WM_KEYDOWN, ord("S"), 1),
        user32.PostMessageW(target, WM_KEYUP, ord("S"), 0xC0000001),
        user32.PostMessageW(target, WM_KEYUP, VK_CONTROL, 0xC0000001)), file_has("A1(x)"), settle=1.2)
    measure("notepad", "save", "posted s + borrowed ctrl state",
            lambda: posted_hotkey_with_state(target, VK_CONTROL, ord("S")), file_has("A1(x)"), settle=1.2)

    def menu_save():
        file_menu = find_el(hwnd, name="File", control_type="MenuItem")
        wr = uia._wrapper(file_menu)
        try:
            wr.expand()
        except Exception:
            wr.invoke()
        time.sleep(0.8)
        save = find_el(hwnd, name="Save", control_type="MenuItem")
        if save is None:
            # menu flyouts can be separate popup windows
            for x in w.top_windows():
                save = find_el(x.hwnd, name="Save", control_type="MenuItem") if x.pid == win.pid else None
                if save:
                    break
        uia._wrapper(save).invoke()

    post_chars(target, "C3")
    time.sleep(0.4)
    measure("notepad", "save", "UIA menu File>Save", menu_save, file_has("C3"), settle=1.5)

    def click_start_then_type():
        r = w.frame_bounds(target)
        post_click(hwnd, r.left + 6, r.top + 8)
        time.sleep(0.2)
        post_chars(target, "Z")

    measure("notepad", "click", "posted click (caret) + char", click_start_then_type,
            lambda: (doc_text(hwnd).startswith("Z"), doc_text(hwnd)[:20]))

    before = pw_snapshot(hwnd)
    measure("notepad", "capture", "PrintWindow while covered", lambda: None,
            lambda: (len(before) > 0, f"{len(before)} bytes"))
    return hwnd


def test_calc():
    launch_noactivate(["calc.exe"])
    win = wait_window(r"^Calculator$")
    time.sleep(1.5)
    hwnd = win.hwnd
    core = child_by_class(hwnd, "Windows.UI.Core.CoreWindow")

    def display():
        el = find_el(hwnd, auto_id="CalculatorResults")
        return uia.read(el)["name"] if el else ""

    def invoke(aid):
        uia.invoke(find_el(hwnd, auto_id=aid))

    measure("calc", "press buttons", "UIA Invoke",
            lambda: [invoke(a) for a in ("clearButton", "num7Button", "multiplyButton", "num6Button",
                                         "equalButton")],
            lambda: (display().strip().endswith("42"), display()))

    def post_click_button(aid):
        el = find_el(hwnd, auto_id=aid)
        r = uia.element_state(el)["rect"]
        post_click(hwnd, *r.center())

    measure("calc", "press button", "posted click",
            lambda: (invoke("clearButton"), time.sleep(0.3), post_click_button("num9Button")),
            lambda: (display().strip().endswith("9"), display()))
    measure("calc", "type digit", "WM_CHAR post to CoreWindow",
            lambda: (invoke("clearButton"), time.sleep(0.3), post_chars(core or hwnd, "5")),
            lambda: (display().strip().endswith("5"), display()))
    return hwnd


def test_charmap():
    launch_noactivate(["charmap.exe"])
    win = wait_window(r"^Character Map$")
    time.sleep(1.0)
    hwnd = win.hwnd
    edit = next((h for h in children(hwnd) if w.class_name(h).lower().startswith(("edit", "richedit"))), None)
    grid = next((h for h in children(hwnd) if "grid" in w.class_name(h).lower()), None)
    select_btn = next((h for h in children(hwnd) if w.class_name(h) == "Button" and w.title(h) == "&Select"), None)
    print(f"charmap edit={edit or 0:#x} ({w.class_name(edit) if edit else None}) grid={grid or 0:#x} "
          f"({w.class_name(grid) if grid else None}) select={select_btn or 0:#x}", flush=True)

    def value():
        el = find_el(hwnd, control_type="Edit")
        return uia.read(el).get("value", "").rstrip("\r\n") if el else ""

    measure("charmap", "set text", "UIA ValuePattern",
            lambda: uia.set_value(find_el(hwnd, control_type="Edit"), "xy"), lambda: (value() == "xy", value()))
    measure("charmap", "type", "WM_CHAR post", lambda: post_chars(edit, "q"), lambda: ("q" in value(), value()))
    measure("charmap", "set text", "WM_SETTEXT", lambda: send_text_msg(edit, 0x000C, 0, "abc"),
            lambda: (value() == "abc", value()))
    measure("charmap", "press button", "UIA Invoke Select",
            lambda: uia.invoke(find_el(hwnd, name="Select", control_type="Button")),
            lambda: (value().startswith("!") and value().endswith("abc"), value()))
    n = len(value())
    measure("charmap", "press button", "BM_CLICK",
            lambda: send_text_msg(select_btn, BM_CLICK, 0, "") if select_btn else None,
            lambda: (len(value()) == n + 1, value()))
    before_grid = pw_snapshot(hwnd)

    def click_cell():
        r = w.frame_bounds(grid)
        post_click(hwnd, r.left + int(r.width * 0.55), r.top + int(r.height * 0.3))

    measure("charmap", "click", "posted click on grid", click_cell,
            lambda: (changed(before_grid, pw_snapshot(hwnd)), "grid pixels changed"))
    return hwnd


def test_vscode():
    exe = Path(os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"))
    if not exe.exists():
        return None
    data = TMP / "vsc"
    (data / "User").mkdir(parents=True, exist_ok=True)
    (data / "User" / "settings.json").write_text(json.dumps({
        "editor.accessibilitySupport": "on", "files.autoSave": "afterDelay", "files.autoSaveDelay": 300,
        "workbench.startupEditor": "none", "security.workspace.trust.enabled": False}), encoding="utf-8")
    f = TMP / "phase2.py"
    f.write_text("", encoding="utf-8")
    launch_noactivate([str(exe), "--user-data-dir", str(data), "--extensions-dir", str(TMP / "vscx"),
                       "--disable-extensions", "--new-window", "--force-renderer-accessibility", str(f)])
    win = wait_window(r"phase2\.py", timeout=45)
    time.sleep(3.0)
    hwnd = win.hwnd
    rw = child_by_class(hwnd, "Chrome_RenderWidgetHostHWND")
    focus = thread_focus(hwnd)
    print(f"vscode hwnd={hwnd:#x} render={rw or 0:#x} thread_focus={focus:#x}", flush=True)

    def on_disk(s):
        return lambda: (s in f.read_text(encoding="utf-8", errors="replace"),
                        f.read_text(encoding="utf-8", errors="replace")[-40:])

    measure("vscode", "type", "WM_CHAR post", lambda: post_chars(rw or hwnd, "x = (1 + 2)"),
            on_disk("x = (1 + 2)"), settle=1.5)
    ed = find_el(hwnd, name_re=r"^Editor content", control_type="Edit") or find_el(hwnd, control_type="Edit")
    print(f"  vscode editor element: {ed and (ed.info.name, ed.info.control_type)}", flush=True)
    measure("vscode", "type", "UIA ValuePattern (editor)",
            lambda: uia.set_value(ed, "y = 3"), on_disk("y = 3"), settle=1.5)

    def click_editor_then_chars():
        r = uia.element_state(ed)["rect"] if ed else w.frame_bounds(hwnd)
        post_click(hwnd, r.left + 40, r.top + 10)
        time.sleep(0.3)
        post_chars(rw or hwnd, "z = 9")

    measure("vscode", "type", "posted click + WM_CHAR", click_editor_then_chars, on_disk("z = 9"), settle=1.5)
    before = pw_snapshot(hwnd)
    btn = find_el(hwnd, name_re=r"^Explorer", control_type="TabItem") or find_el(hwnd, name_re=r"^Explorer")
    measure("vscode", "press tab", "UIA select/invoke Explorer",
            lambda: (uia.select(btn) if btn else None),
            lambda: (changed(before, pw_snapshot(hwnd)), "window pixels changed"), settle=1.0)
    return hwnd


def main():
    global COUNTER
    for name in ("notepad", "CalculatorApp", "charmap"):
        r = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}.exe", "/NH"], capture_output=True, text=True)
        if f"{name}.exe".lower() in r.stdout.lower():
            sys.exit(f"{name} is already running; close it first so the spike cannot touch your windows")
    COUNTER = InjectedCounter()
    global SENTINEL
    sentinel = SENTINEL = start_sentinel()
    if not force_foreground(sentinel):
        print("warning: sentinel did not take the foreground", flush=True)
    opened = []
    try:
        for test in (test_notepad, test_calc, test_charmap, test_vscode):
            fg_before = user32.GetForegroundWindow()
            try:
                h = test()
                if h:
                    opened.append(h)
            except Exception as e:
                print(f"{test.__name__} crashed: {e!r}", flush=True)
            if user32.GetForegroundWindow() != fg_before:
                ROWS.append({"app": test.__name__, "action": "launch", "method": "SW_SHOWNOACTIVATE",
                             "effect": False, "detail": "app took the foreground at launch or during tests"})
                force_foreground(sentinel)
    finally:
        for h in opened:
            close_window(h)
        OUT.mkdir(exist_ok=True)
        (OUT / "phase2.json").write_text(json.dumps(ROWS, indent=1), encoding="utf-8")
        print(f"wrote {OUT / 'phase2.json'}; physical events during run: {COUNTER.physical}", flush=True)


if __name__ == "__main__":
    main()
