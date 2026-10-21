"""Helpers for live background tests: a sentinel window that holds the foreground, an injected-input
counter, and launching apps without activating them."""

import ctypes
import ctypes.wintypes as wt
import subprocess
import threading
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.GetForegroundWindow.restype = wt.HWND


class InjectedCounter:
    """Counts injected vs physical mouse/keyboard events with low-level hooks on their own thread."""

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


def start_sentinel() -> int:
    import tkinter as tk
    box, ready = {}, threading.Event()

    def run():
        root = tk.Tk()
        root.title("Overshoulder test sentinel")
        root.geometry("380x80+40+40")
        tk.Label(root, text="Overshoulder background test: this window keeps the focus.").pack(expand=True)
        root.update()
        box["hwnd"] = user32.GetParent(root.winfo_id()) or root.winfo_id()
        ready.set()
        root.mainloop()

    threading.Thread(target=run, daemon=True).start()
    ready.wait(5)
    return box["hwnd"]


def take_foreground(hwnd: int) -> bool:
    from overshoulder.driver.background import set_foreground
    ok = set_foreground(hwnd)
    time.sleep(0.3)
    return ok


def launch_noactivate(args):
    si = subprocess.STARTUPINFO()
    si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 4  # SW_SHOWNOACTIVATE
    return subprocess.Popen(args, startupinfo=si)


def cursor():
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def wait_no_window(title_part: str, timeout: float = 15.0) -> None:
    """Wait until no window title contains `title_part` (e.g. a previous test's Notepad closing), so a
    new file does not open as a tab inside it."""
    from overshoulder.driver import windows as w
    end = time.time() + timeout
    while time.time() < end:
        if not any(title_part in x.title for x in w.top_windows()):
            return
        time.sleep(0.25)


def close_discarding(hwnd: int, attempts: int = 6) -> None:
    """Close a window we opened; answer "Don't save" to a save prompt; repeat for remaining tabs."""
    from pywinauto import Desktop

    from overshoulder.driver import windows as w
    for _ in range(attempts):
        if not w.is_window(hwnd):
            return
        win = Desktop(backend="uia").window(handle=hwnd)
        try:  # a modal Save As dialog blocks closing: cancel it first
            for dlg in win.descendants(control_type="Window"):
                if dlg.window_text().lower().startswith("save as"):
                    for b in dlg.descendants(control_type="Button"):
                        if b.window_text() == "Cancel":
                            b.invoke()
                            time.sleep(0.8)
                            break
        except Exception:
            pass
        try:
            win.close()
        except Exception:
            user32.PostMessageW(hwnd, 0x0010, 0, 0)  # WM_CLOSE
        time.sleep(1.0)
        if not w.is_window(hwnd):
            return
        try:
            for b in win.descendants(control_type="Button"):
                if b.window_text().replace("\u2019", "'") in ("Don't save", "Don't Save", "OK"):
                    b.invoke()
                    break
        except Exception:
            pass
        time.sleep(0.8)


PERSON_SCRIPT = r"""
import ctypes, sys, time
from ctypes import wintypes as wt
class KI(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]
class MI(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]
class U(ctypes.Union):
    _fields_ = [("ki", KI), ("mi", MI)]
class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", U)]
end = time.time() + float(sys.argv[1])
while time.time() < end:
    for flags in (0, 2):
        i = INPUT(type=1, ki=KI(0x87, 0, flags, 0, 0))
        ctypes.windll.user32.SendInput(1, ctypes.byref(i), ctypes.sizeof(INPUT))
    time.sleep(0.2)
"""


def simulate_person(seconds: float):
    """Another process presses F24 (a key no app uses) every 0.2 s. Windows' last-input clock counts it as
    the person's input, because it is not ours; low-level hooks see it as injected."""
    import sys
    return subprocess.Popen([sys.executable, "-c", PERSON_SCRIPT, str(seconds)])
