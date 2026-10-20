"""Phase 0 spike: drive unrelated windows with pywinauto + SendInput, record and verify.

Run: .venv\\Scripts\\python spike\\phase0.py [--only notepad,calc,charmap,vscode]
Writes spike/out/<target>/ (before/after PNGs, MP4, actions.jsonl) and spike/out/report.json.
Touches only windows this script launches; closes them afterwards.
"""
import ctypes
import ctypes.wintypes as wt

# Must run before any UI library import so window rects and capture share physical pixels.
ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import mss
from PIL import Image, ImageChops
from pywinauto import Desktop, mouse
from pywinauto.keyboard import send_keys

OUT = Path(__file__).parent / "out"
FFMPEG = shutil.which("ffmpeg")


def monitors():
    shcore = ctypes.windll.shcore
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    def cb(hmon, _hdc, rect, _lp):
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        shcore.GetDpiForMonitor(hmon, 0, ctypes.byref(dx), ctypes.byref(dy))
        r = rect.contents
        found.append({"rect": [r.left, r.top, r.right, r.bottom], "dpi": dx.value,
                      "scale_pct": round(dx.value / 96 * 100)})
        return 1

    user32.EnumDisplayMonitors(None, None, cb, 0)
    return found


def top_windows():
    return {w.handle: w for w in Desktop(backend="uia").windows()}


def spec(w):
    # Desktop.windows() yields wrappers; child_window() lives on WindowSpecification.
    return Desktop(backend="uia").window(handle=w.handle)


def wait_new_window(before, title_hint, timeout=20):
    end = time.time() + timeout
    while time.time() < end:
        for h, w in top_windows().items():
            if h not in before:
                try:
                    if title_hint.lower() in w.window_text().lower():
                        return spec(w)
                except Exception:
                    pass
        time.sleep(0.3)
    raise TimeoutError(f"no new window matching {title_hint!r}")


def rect_of(w):
    r = w.rectangle()
    return {"left": r.left, "top": r.top, "width": r.width(), "height": r.height()}


def grab(rect, path):
    with mss.MSS() as s:
        shot = s.grab(rect)
        img = Image.frombytes("RGB", shot.size, shot.rgb)
    img.save(path)
    return img


def changed_fraction(a, b):
    if a.size != b.size:
        return 1.0
    diff = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v > 24 else 0)
    return sum(diff.histogram()[255:]) / (a.size[0] * a.size[1])


class FocusLost(RuntimeError):
    pass


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _U)]


def _check_focus(w):
    # Keystrokes go to whatever has focus; refuse rather than type into someone else's window.
    fg = ctypes.windll.user32.GetForegroundWindow()
    if fg != w.handle:
        raise FocusLost(f"foreground is {fg:#x}, scope window is {w.handle:#x}")


def type_text(w, text):
    """Type literal text via SendInput KEYEVENTF_UNICODE (no key-syntax parsing)."""
    for ch in text:
        _check_focus(w)
        if ch == "\n":
            send_keys("{ENTER}")
            continue
        raw = ch.encode("utf-16-le")
        for unit in [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]:
            down = INPUT(type=1, ki=KEYBDINPUT(0, unit, 0x0004, 0, 0))
            up = INPUT(type=1, ki=KEYBDINPUT(0, unit, 0x0004 | 0x0002, 0, 0))
            arr = (INPUT * 2)(down, up)
            ctypes.windll.user32.SendInput(2, arr, ctypes.sizeof(INPUT))
        time.sleep(0.02)


def keys(w, combo):
    """Send a hotkey in pywinauto syntax, e.g. "^s"."""
    _check_focus(w)
    send_keys(combo)


def tree_stats(w, limit=4000):
    t0 = time.time()
    total = named = 0
    types = {}
    for d in w.descendants()[:limit]:
        total += 1
        info = d.element_info
        if info.name:
            named += 1
        types[info.control_type] = types.get(info.control_type, 0) + 1
    return {"controls": total, "named": named, "ms": round((time.time() - t0) * 1000),
            "top_types": dict(sorted(types.items(), key=lambda kv: -kv[1])[:6])}


class Recorder:
    def __init__(self, rect, path):
        # Even dimensions keep libx264 happy.
        w, h = rect["width"] // 2 * 2, rect["height"] // 2 * 2
        self.proc = subprocess.Popen(
            [FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "gdigrab", "-framerate", "15",
             "-draw_mouse", "1", "-offset_x", str(rect["left"]), "-offset_y", str(rect["top"]),
             "-video_size", f"{w}x{h}", "-i", "desktop", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", str(path)],
            stdin=subprocess.PIPE)
        time.sleep(1.0)

    def stop(self):
        time.sleep(0.8)
        self.proc.communicate(b"q", timeout=20)


class Log:
    def __init__(self, path):
        self.f = open(path, "w", encoding="utf-8")
        self.t0 = time.time()

    def __call__(self, action, **kw):
        row = {"t": round(time.time() - self.t0, 2), "action": action, **kw}
        self.f.write(json.dumps(row) + "\n")
        self.f.flush()


def run_target(name, launch, title_hint, act, cleanup):
    d = OUT / name
    d.mkdir(parents=True, exist_ok=True)
    res = {"target": name}
    before = top_windows()
    proc = launch()
    try:
        w = wait_new_window(before, title_hint)
        w.set_focus()
        time.sleep(1.2)
        res["window"] = {"title": w.window_text(), "class": w.element_info.class_name,
                         "framework": w.element_info.framework_id, "pid": w.process_id(),
                         "rect": rect_of(w), "handle": w.handle}
        res["tree"] = tree_stats(w)
        rect = rect_of(w)
        img0 = grab(rect, d / "before.png")
        rec = Recorder(rect, d / "session.mp4") if FFMPEG else None
        log = Log(d / "actions.jsonl")
        try:
            res["verify"] = act(w, log)
        finally:
            if rec:
                rec.stop()
        img1 = grab(rect_of(w), d / "after.png")
        res["pixels_changed"] = round(changed_fraction(img0, img1), 4)
        res["ok"] = bool(res["verify"].get("ok"))
    except Exception as e:
        res["ok"] = False
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        try:
            cleanup(proc)
        except Exception as e:
            res["cleanup_error"] = f"{type(e).__name__}: {e}"
    return res


# ---- targets -----------------------------------------------------------------

TMP = Path(tempfile.mkdtemp(prefix="overshoulder_spike_"))


def notepad_act(w, log):
    edit = w.child_window(control_type="Document").wrapper_object()
    edit.click_input()  # SendInput mouse
    log("click", target="Document", via="SendInput", rect=str(edit.rectangle()))
    type_text(w, "Hello from Overshoulder\nLine two")
    log("type", target="Document", text="Hello from Overshoulder\\nLine two", via="SendInput")
    time.sleep(0.5)
    try:
        text = edit.iface_text.DocumentRange.GetText(-1)
    except Exception as e:
        text = f"<TextPattern failed: {type(e).__name__}>"
    keys(w, "^s")
    log("hotkey", keys="ctrl+s")
    time.sleep(0.8)
    ok = "Hello from Overshoulder" in text
    return {"ok": ok, "read_back": text[:80], "method": "UIA TextPattern"}


def calc_act(w, log):
    for aid in ["num7Button", "plusButton", "num8Button"]:
        w.child_window(auto_id=aid, control_type="Button").wrapper_object().invoke()  # UIA, no mouse
        log("invoke", target=aid, via="UIA InvokePattern")
        time.sleep(0.25)
    eq = w.child_window(auto_id="equalButton", control_type="Button").wrapper_object()
    eq.click_input()
    log("click", target="equalButton", via="SendInput", rect=str(eq.rectangle()))
    time.sleep(0.5)
    shown = w.child_window(auto_id="CalculatorResults").wrapper_object().window_text()
    return {"ok": shown.strip().endswith("15"), "read_back": shown, "method": "UIA Name"}


def charmap_act(w, log):
    box = w.child_window(auto_id="104", control_type="Edit").wrapper_object()
    box.click_input()
    log("click", target="Characters to copy", via="SendInput")
    type_text(w, "abc")
    log("type", target="Characters to copy", text="abc", via="SendInput")
    sel = w.child_window(title="Select", control_type="Button").wrapper_object()
    sel.invoke()
    log("invoke", target="Select", via="UIA InvokePattern")
    time.sleep(0.4)
    val = box.get_value()
    return {"ok": val.startswith("abc"), "read_back": val, "method": "UIA ValuePattern"}


def vscode_act(w, log):
    # Click the middle of the window (editor area) by coordinates, then type.
    r = w.rectangle()
    x, y = r.left + r.width() // 2, r.top + r.height() // 2
    mouse.click(coords=(x, y))
    log("click", target="editor (coordinates)", via="SendInput", x=x, y=y)
    type_text(w, "print('hello from overshoulder')")
    log("type", target="editor", text="print('hello from overshoulder')", via="SendInput")
    time.sleep(0.6)
    found = None
    try:
        ed = w.child_window(control_type="Edit", found_index=0).wrapper_object()
        found = ed.window_text() or ed.get_value()
    except Exception as e:
        found = f"<no UIA edit: {type(e).__name__}>"
    keys(w, "^s")
    log("hotkey", keys="ctrl+s")
    time.sleep(0.8)
    on_disk = (TMP / "spike.py").read_text(encoding="utf-8", errors="replace")
    return {"ok": "hello from overshoulder" in on_disk, "uia_read_back": str(found)[:80],
            "file_read_back": on_disk[:80], "method": "file on disk (UIA fallback)"}


def kill(proc):
    if proc and proc.poll() is None:
        proc.terminate()


TARGETS = {
    "notepad": lambda: run_target(
        "notepad",
        lambda: subprocess.Popen(["notepad.exe", str(TMP / "notes.txt")]),
        "notes", notepad_act,
        lambda p: Desktop(backend="uia").window(title_re=".*notes.txt.*Notepad").close()),
    "calc": lambda: run_target(
        "calc", lambda: subprocess.Popen(["calc.exe"]), "Calculator", calc_act,
        lambda p: Desktop(backend="uia").window(title="Calculator").close()),
    "charmap": lambda: run_target(
        "charmap", lambda: subprocess.Popen(["charmap.exe"]), "Character Map", charmap_act, kill),
    "vscode": lambda: run_target(
        "vscode",
        lambda: subprocess.Popen([os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                                  "--user-data-dir", str(TMP / "vsc-data"), "--extensions-dir",
                                  str(TMP / "vsc-ext"), "--disable-extensions", "--skip-welcome",
                                  "--skip-release-notes", "--disable-workspace-trust", "--new-window",
                                  str(TMP / "spike.py")]),
        "spike.py", vscode_act,
        lambda p: Desktop(backend="uia").window(title_re=".*spike.py.*").close()),
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default=",".join(TARGETS))
    args = ap.parse_args()
    (TMP / "notes.txt").write_text("", encoding="utf-8")
    (TMP / "spike.py").write_text("", encoding="utf-8")
    OUT.mkdir(exist_ok=True)
    cursor = mouse.win32api.GetCursorPos() if hasattr(mouse, "win32api") else None
    report = {"python": sys.version.split()[0], "monitors": monitors(), "results": []}
    for name in args.only.split(","):
        report["results"].append(TARGETS[name]())
        time.sleep(1.0)
    if cursor:
        mouse.move(coords=cursor)
    (OUT / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
