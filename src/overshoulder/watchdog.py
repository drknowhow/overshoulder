"""The watchdog: the person's side of the desk. A separate process the MCP server cannot stop.

- Panic hotkey (Ctrl+Alt+P by default): revokes the active lease and pauses until an operator resumes.
- Physical mouse or keyboard input while an agent drives pauses driving; it resumes by itself after a
  few idle seconds. Low-level hooks tell physical from injected input (LLKHF_INJECTED/LLMHF_INJECTED),
  so the agent's own SendInput never trips it.
- An outline around the active scope (green driving, amber paused), excluded from screen capture so
  it never appears in recordings.

It talks to the daemon over the operator endpoints, and registers its own pid as protected so agents
can never click or type into its windows.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from overshoulder.gateway.lease import HUMAN_PAUSE as HUMAN_REASON  # noqa: E402

# -- decisions (pure, unit-tested) ---------------------------------------------------------------

@dataclass
class WatchdogState:
    idle_resume_s: float = 5.0
    last_physical: float = 0.0
    paused_by_us: bool = False

    def on_physical_input(self, now: float) -> None:
        self.last_physical = now

    def decide(self, now: float, status: dict | None) -> str | None:
        """Return "pause", "resume" or None for the current daemon status."""
        if not status:
            return None
        paused = status.get("paused")
        active = status.get("active")
        recent = now - self.last_physical < self.idle_resume_s
        if active and not paused and recent:
            self.paused_by_us = True
            return "pause"
        if paused == HUMAN_REASON and self.paused_by_us and not recent:
            self.paused_by_us = False
            return "resume"
        if paused and paused != HUMAN_REASON:
            self.paused_by_us = False  # someone else (panic, operator) owns this pause
        return None


# -- daemon client -------------------------------------------------------------------------------

class Operator:
    def __init__(self, port: int, key: str):
        self.base = f"http://127.0.0.1:{port}/operator"
        self.key = key

    def _req(self, path: str, body: dict | None = None, method: str = "POST") -> dict | None:
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(f"{self.base}/{path}", data=data, method=method,
                                     headers={"x-overshoulder-operator": self.key,
                                              "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None  # daemon down or restarting; the next poll retries

    def status(self) -> dict | None:
        return self._req("status", method="GET")

    def panic(self, reason: str) -> dict | None:
        return self._req("panic", {"reason": reason})

    def pause(self) -> dict | None:
        return self._req("pause", {"reason": HUMAN_REASON})

    def resume_human(self) -> dict | None:
        return self._req("resume", {"only_if": HUMAN_REASON})

    def protect(self, pid: int) -> dict | None:
        return self._req("protect", {"pid": pid})


# -- low-level hooks and hotkey (runs on its own thread with a message loop) ----------------------

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
WM_HOTKEY = 0x0312
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_NOREPEAT = 0x1, 0x2, 0x4, 0x4000

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
kernel32.GetModuleHandleW.restype = wt.HMODULE


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def parse_hotkey(spec: str) -> tuple[int, int]:
    from overshoulder.driver.input import vk_for
    mods, vk = 0, None
    for part in spec.lower().split("+"):
        part = part.strip()
        if part in ("ctrl", "control"):
            mods |= MOD_CONTROL
        elif part == "alt":
            mods |= MOD_ALT
        elif part == "shift":
            mods |= MOD_SHIFT
        else:
            vk = vk_for(part)
    if vk is None or not mods:
        raise ValueError(f"panic hotkey needs a modifier and a key, got {spec!r}")
    return mods | MOD_NOREPEAT, vk


def input_thread(on_physical, on_panic, hotkey: str, ready: threading.Event, errors: list) -> None:
    """Install hooks + hotkey and pump messages. Callbacks must return quickly."""

    @HOOKPROC
    def kb_proc(n, w, l_):
        if n == 0 and not ctypes.cast(l_, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.flags & LLKHF_INJECTED:
            on_physical()
        return user32.CallNextHookEx(None, n, w, l_)

    @HOOKPROC
    def ms_proc(n, w, l_):
        if n == 0 and not ctypes.cast(l_, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.flags & LLMHF_INJECTED:
            on_physical()
        return user32.CallNextHookEx(None, n, w, l_)

    hmod = kernel32.GetModuleHandleW(None)
    hooks = [user32.SetWindowsHookExW(WH_KEYBOARD_LL, kb_proc, hmod, 0),
             user32.SetWindowsHookExW(WH_MOUSE_LL, ms_proc, hmod, 0)]
    mods, vk = parse_hotkey(hotkey)
    if not all(hooks):
        errors.append("could not install input hooks")
    if not user32.RegisterHotKey(None, 1, mods, vk):
        errors.append(f"could not register panic hotkey {hotkey} (in use by another app?)")
    ready.set()
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        if msg.message == WM_HOTKEY:
            on_panic()
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


# -- outline overlay (tkinter, main thread) -------------------------------------------------------

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT, WS_EX_TOPMOST = 0x80000, 0x20, 0x8
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x80, 0x8000000
WDA_EXCLUDEFROMCAPTURE = 0x11
KEY = "#ff00fe"  # transparent colour key
BORDER = 4


def _click_through(tk_window) -> None:
    hwnd = user32.GetParent(tk_window.winfo_id()) or tk_window.winfo_id()
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOPMOST
                          | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE)
    user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)


def run(port: int | None = None, hotkey: str = "ctrl+alt+p", idle_resume_s: float = 5.0) -> None:
    import tkinter as tk

    from overshoulder.server import DEFAULT_PORT, ensure_operator_key

    op = Operator(port or DEFAULT_PORT, ensure_operator_key())
    state = WatchdogState(idle_resume_s=idle_resume_s)
    lock = threading.Lock()

    def on_physical():
        with lock:
            state.on_physical_input(time.monotonic())

    def on_panic():
        threading.Thread(target=op.panic, args=(f"panic hotkey {hotkey}",), daemon=True).start()

    ready, errors = threading.Event(), []
    threading.Thread(target=input_thread, args=(on_physical, on_panic, hotkey, ready, errors),
                     daemon=True).start()
    ready.wait(5)
    for e in errors:
        print(f"watchdog: {e}", flush=True)

    root = tk.Tk()
    root.withdraw()
    frame = tk.Toplevel(root)
    frame.overrideredirect(True)
    frame.attributes("-topmost", True)
    frame.configure(bg=KEY)
    frame.attributes("-transparentcolor", KEY)
    canvas = tk.Canvas(frame, bg=KEY, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    frame.withdraw()
    frame.update_idletasks()
    _click_through(frame)
    shown = {"geom": None, "protected": False}

    def poll():
        status = op.status()
        if status is not None and not shown["protected"]:
            shown["protected"] = op.protect(os.getpid()) is not None
        if status is None:
            shown["protected"] = False  # daemon restarted or down; re-register when it is back
        with lock:
            decision = state.decide(time.monotonic(), status)
        if decision == "pause":
            op.pause()
        elif decision == "resume":
            op.resume_human()
        draw(status)
        root.after(250, poll)

    def draw(status):
        active = (status or {}).get("active")
        bounds = (active or {}).get("bounds")
        if not bounds:
            if shown["geom"]:
                frame.withdraw()
                shown["geom"] = None
            return
        left, top, right, bottom = bounds
        x, y = left - BORDER - 1, top - BORDER - 1
        w, h = right - left + 2 * BORDER + 2, bottom - top + 2 * BORDER + 2
        geom = f"{w}x{h}+{x}+{y}"
        colour = "#f5a623" if status.get("paused") else "#22c55e"
        if status.get("paused") and status["paused"] != HUMAN_REASON:
            colour = "#ef4444"
        if geom != shown["geom"]:
            frame.geometry(geom)
            frame.deiconify()
            _click_through(frame)
            shown["geom"] = geom
        canvas.delete("all")
        # Drawn just outside the scope, so even a capture that ignored the affinity flag would miss it.
        canvas.create_rectangle(BORDER // 2, BORDER // 2, w - BORDER // 2, h - BORDER // 2,
                                outline=colour, width=BORDER)

    print(f"watchdog: panic hotkey {hotkey}; auto-pause on physical input; daemon port {port or DEFAULT_PORT}",
          flush=True)
    root.after(250, poll)
    root.mainloop()
