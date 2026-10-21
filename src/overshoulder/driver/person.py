"""Telling the person's mouse and keyboard apart from ours.

Low-level hooks see every input event with a flag on injected ones (LLKHF_INJECTED / LLMHF_INJECTED), so
they separate the person from SendInput exactly. Windows' last-input clock cannot do that while we are
typing, because our own keys keep resetting it. The watchdog runs these hooks all the time; the gateway
opens a PersonWatch only while it borrows the person's input for one action.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import threading

WH_KEYBOARD_LL, WH_MOUSE_LL = 13, 14
LLKHF_INJECTED, LLMHF_INJECTED = 0x10, 0x01
WM_QUIT = 0x0012

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
LRESULT = ctypes.c_ssize_t
HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wt.WPARAM, wt.LPARAM)
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, wt.HINSTANCE, wt.DWORD]
user32.SetWindowsHookExW.restype = wt.HHOOK
user32.CallNextHookEx.argtypes = [wt.HHOOK, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = LRESULT
user32.UnhookWindowsHookEx.argtypes = [wt.HHOOK]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
kernel32.GetModuleHandleW.restype = wt.HMODULE


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wt.POINT), ("mouseData", wt.DWORD), ("flags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


def physical_key(lparam) -> bool:
    return not ctypes.cast(lparam, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents.flags & LLKHF_INJECTED


def physical_mouse(lparam) -> bool:
    return not ctypes.cast(lparam, ctypes.POINTER(MSLLHOOKSTRUCT)).contents.flags & LLMHF_INJECTED


class PersonWatch:
    """While open, `seen` turns true on any physical (not injected) mouse or keyboard event.

    The hooks live on their own thread with a message loop, for as long as the watch is open. `ok` is
    false if they could not be installed; then only the watchdog's pause can tell.
    """

    def __init__(self):
        self.seen = False
        self.ok = False
        self._tid = 0
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> PersonWatch:
        self._thread = threading.Thread(target=self._run, name="person-watch", daemon=True)
        self._thread.start()
        self._ready.wait(2.0)
        return self

    def __exit__(self, *exc) -> bool:
        if self._tid:
            user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(2.0)
        return False

    def _run(self) -> None:
        @HOOKPROC
        def keys(n, wparam, lparam):
            if n == 0 and physical_key(lparam):
                self.seen = True
            return user32.CallNextHookEx(None, n, wparam, lparam)

        @HOOKPROC
        def mouse(n, wparam, lparam):
            if n == 0 and physical_mouse(lparam):
                self.seen = True
            return user32.CallNextHookEx(None, n, wparam, lparam)

        msg = wt.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # the queue must exist before WM_QUIT is posted
        self._tid = kernel32.GetCurrentThreadId()
        hmod = kernel32.GetModuleHandleW(None)
        hooks = [user32.SetWindowsHookExW(WH_KEYBOARD_LL, keys, hmod, 0),
                 user32.SetWindowsHookExW(WH_MOUSE_LL, mouse, hmod, 0)]
        self.ok = all(hooks)
        self._ready.set()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            for h in hooks:
                if h:
                    user32.UnhookWindowsHookEx(h)
