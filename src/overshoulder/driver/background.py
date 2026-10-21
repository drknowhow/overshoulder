"""Acting on a window without the person's mouse, keyboard or foreground (see docs/SPIKE-phase2.md).

Everything here talks to the target window's own thread: posted mouse and character messages,
edit-control messages, and hotkeys whose modifiers are set in the target thread's key state. Nothing
goes through SendInput, so the real cursor never moves and the person's foreground window keeps
keyboard focus. `ForegroundGuard` puts the foreground back if an app grabs it anyway.

The caller (the gateway) decides whether a target may be touched; this module only does it.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from collections.abc import Callable
from dataclasses import dataclass

from overshoulder.driver import windows as w

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

WM_SETTEXT = 0x000C
WM_KEYDOWN, WM_KEYUP, WM_CHAR, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0100, 0x0101, 0x0102, 0x0104, 0x0105
WM_MOUSEMOVE, WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0200, 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP = 0x0204, 0x0205
EM_REPLACESEL, BM_CLICK = 0x00C2, 0x00F5
MK_LBUTTON, MK_RBUTTON = 0x0001, 0x0002
SMTO_ABORTIFHUNG = 0x0002
CWP_SKIPINVISIBLE, CWP_SKIPTRANSPARENT = 0x0001, 0x0004
GA_ROOT = 2
GWL_STYLE = -16
ES_PASSWORD = 0x0020
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12

user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.PostMessageW.restype = wt.BOOL
user32.SendMessageTimeoutW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, ctypes.c_void_p, wt.UINT, wt.UINT,
                                       ctypes.POINTER(ctypes.c_size_t)]
user32.ScreenToClient.argtypes = [wt.HWND, ctypes.POINTER(wt.POINT)]
user32.ChildWindowFromPointEx.argtypes = [wt.HWND, wt.POINT, wt.UINT]
user32.ChildWindowFromPointEx.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetAncestor.restype = wt.HWND
user32.GetForegroundWindow.restype = wt.HWND
user32.SetForegroundWindow.argtypes = [wt.HWND]
user32.BringWindowToTop.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]
user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]

# Window classes whose input does not come through posted mouse messages (UWP/XAML core windows).
NO_POSTED_MOUSE = frozenset({"Windows.UI.Core.CoreWindow", "ApplicationFrameInputSinkWindow"})
CHROMIUM = frozenset({"Chrome_RenderWidgetHostHWND", "Chrome_WidgetWin_1", "Intermediate D3D Window"})
EDIT_CLASSES = ("edit", "richedit")


class BackgroundUnsupported(RuntimeError):
    """This method cannot reach that window; the caller should try the next rung."""
    code = "background_unsupported"


def root_of(hwnd: int) -> int:
    return user32.GetAncestor(hwnd, GA_ROOT) or 0


def thread_of(hwnd: int) -> int:
    return user32.GetWindowThreadProcessId(hwnd, None)


class GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("flags", wt.DWORD), ("hwndActive", wt.HWND), ("hwndFocus", wt.HWND),
                ("hwndCapture", wt.HWND), ("hwndMenuOwner", wt.HWND), ("hwndMoveSize", wt.HWND),
                ("hwndCaret", wt.HWND), ("rcCaret", wt.RECT)]


def thread_focus(top_hwnd: int) -> int:
    """The window that has keyboard focus inside top_hwnd's own thread (kept even in the background)."""
    gti = GUITHREADINFO(cbSize=ctypes.sizeof(GUITHREADINFO))
    if not user32.GetGUIThreadInfo(thread_of(top_hwnd), ctypes.byref(gti)):
        return 0
    return gti.hwndFocus or 0


def _first_child(top: int, cls: str) -> int:
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if w.class_name(h) == cls and user32.IsWindowVisible(h):
            found.append(h)
            return False
        return True

    user32.EnumChildWindows(top, cb, 0)
    return found[0] if found else 0


def input_window(top: int) -> int:
    """Where keyboard messages for a top-level window should go, even while it is in the background.

    UWP apps take input in their CoreWindow (hosted by ApplicationFrameWindow, in another process);
    Chromium/Electron apps in their render widget; everything else in its thread's focus window.
    """
    cls = w.class_name(top)
    if cls == "ApplicationFrameWindow":
        return _first_child(top, "Windows.UI.Core.CoreWindow") or top
    if cls == "Chrome_WidgetWin_1":
        focus = thread_focus(top)
        if focus and w.class_name(focus) == "Chrome_RenderWidgetHostHWND":
            return focus
        return _first_child(top, "Chrome_RenderWidgetHostHWND") or top
    focus = thread_focus(top)
    if focus and focus != top:
        return focus
    # A background window's thread may report the top-level itself as focused (seen on Win11 Notepad);
    # keys sent there go nowhere, so use the window's edit control when it has exactly one visible.
    edits = _visible_edit_children(top)
    return edits[0] if len(edits) == 1 else (focus or top)


def _visible_edit_children(top: int) -> list[int]:
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        if user32.IsWindowVisible(h) and is_edit_class(h):
            found.append(h)
        return True

    user32.EnumChildWindows(top, cb, 0)
    return found


def is_edit_class(hwnd: int) -> bool:
    return w.class_name(hwnd).lower().startswith(EDIT_CLASSES)


def is_password_edit(hwnd: int) -> bool:
    return is_edit_class(hwnd) and bool(user32.GetWindowLongW(hwnd, GWL_STYLE) & ES_PASSWORD)


def deepest_child_at(top: int, x: int, y: int) -> int:
    """The innermost visible child of `top` under a screen point, even when `top` is covered."""
    h = top
    for _ in range(32):
        pt = wt.POINT(x, y)
        user32.ScreenToClient(h, ctypes.byref(pt))
        c = user32.ChildWindowFromPointEx(h, pt, CWP_SKIPINVISIBLE | CWP_SKIPTRANSPARENT)
        if not c or c == h:
            return h
        h = c
    return h


def _client_lparam(hwnd: int, x: int, y: int) -> int:
    pt = wt.POINT(x, y)
    user32.ScreenToClient(hwnd, ctypes.byref(pt))
    return ((pt.y & 0xFFFF) << 16) | (pt.x & 0xFFFF)


def _post(hwnd: int, msg: int, wparam: int, lparam: int) -> None:
    if not user32.PostMessageW(hwnd, msg, wparam, lparam):
        raise BackgroundUnsupported(f"PostMessage to {hwnd:#x} failed (window gone or blocked)")


def post_click(top: int, x: int, y: int, button: str = "left", count: int = 1) -> int:
    """Post a click to whatever child of `top` sits under (x, y). Returns the child that got it."""
    target = deepest_child_at(top, x, y)
    if w.class_name(target) in NO_POSTED_MOUSE:
        raise BackgroundUnsupported(f"{w.class_name(target)} ignores posted mouse input")
    lp = _client_lparam(target, x, y)
    down, up, mk = (WM_RBUTTONDOWN, WM_RBUTTONUP, MK_RBUTTON) if button == "right" else \
        (WM_LBUTTONDOWN, WM_LBUTTONUP, MK_LBUTTON)
    _post(target, WM_MOUSEMOVE, 0, lp)
    _post(target, down, mk, lp)
    _post(target, up, 0, lp)
    if count == 2 and button == "left":
        _post(target, WM_LBUTTONDBLCLK, MK_LBUTTON, lp)
        _post(target, WM_LBUTTONUP, 0, lp)
    return target


def is_chromium(hwnd: int) -> bool:
    return w.class_name(hwnd) in CHROMIUM


def post_text(hwnd: int, text: str, abort: Callable[[], object] | None = None, delay: float = 0.004) -> None:
    """Post WM_CHAR for each UTF-16 unit; newlines become Enter (\\r)."""
    from overshoulder.driver.input import utf16_units
    for ch in text.replace("\r\n", "\n"):
        if abort is not None:
            abort()
        for unit in ([0x0D] if ch == "\n" else utf16_units(ch)):
            _post(hwnd, WM_CHAR, unit, 1)
        if delay:
            time.sleep(delay)


def _send_text(hwnd: int, msg: int, wparam: int, text: str) -> None:
    buf = ctypes.create_unicode_buffer(text)
    res = ctypes.c_size_t()
    if not user32.SendMessageTimeoutW(hwnd, msg, wparam, ctypes.cast(buf, ctypes.c_void_p), SMTO_ABORTIFHUNG, 3000,
                                      ctypes.byref(res)):
        raise BackgroundUnsupported(f"window {hwnd:#x} did not answer")


def replace_selection(hwnd: int, text: str) -> None:
    """EM_REPLACESEL: insert at the caret of a Win32 edit / rich edit, as one undoable change."""
    if not is_edit_class(hwnd):
        raise BackgroundUnsupported(f"{w.class_name(hwnd)} is not an edit control")
    _send_text(hwnd, EM_REPLACESEL, 1, text)


def set_text(hwnd: int, text: str) -> None:
    if not is_edit_class(hwnd):
        raise BackgroundUnsupported(f"{w.class_name(hwnd)} is not an edit control")
    _send_text(hwnd, WM_SETTEXT, 0, text)


def click_button(hwnd: int) -> None:
    res = ctypes.c_size_t()
    if w.class_name(hwnd) != "Button" or not user32.SendMessageTimeoutW(hwnd, BM_CLICK, 0, None, SMTO_ABORTIFHUNG,
                                                                         3000, ctypes.byref(res)):
        raise BackgroundUnsupported("not a Win32 button")


_MOD_VKS = {VK_SHIFT, VK_CONTROL, VK_MENU, 0x5B, 0x5C}


def post_hotkey(hwnd: int, combo: str) -> None:
    """Press a combo like "ctrl+s" in the target's thread only.

    Apps read Ctrl/Shift/Alt from their thread's key state, which posted modifier messages do not set.
    So this attaches to the target thread, sets those keys in its state, posts the key, then puts the
    state back. The person's own foreground thread is never attached, so their keyboard is unaffected.
    """
    from overshoulder.driver.input import vk_for
    vks = [vk_for(p) for p in combo.split("+") if p.strip()]
    mods = [v for v in vks if v in _MOD_VKS]
    keys = [v for v in vks if v not in _MOD_VKS]
    if not keys:
        raise ValueError(f"hotkey {combo!r} has no key besides modifiers")
    alt = VK_MENU in mods
    me, them = kernel32.GetCurrentThreadId(), thread_of(hwnd)
    _ensure_queue()
    if not user32.AttachThreadInput(me, them, True):
        raise BackgroundUnsupported("could not share the target thread's key state")
    try:
        state = (ctypes.c_ubyte * 256)()
        user32.GetKeyboardState(state)
        saved = bytes(state)
        for m in mods:
            state[m] = 0x80
        user32.SetKeyboardState(state)
        for k in keys:
            _post(hwnd, WM_SYSKEYDOWN if alt else WM_KEYDOWN, k, 1 | (0x20000000 if alt else 0))
            _post(hwnd, WM_SYSKEYUP if alt else WM_KEYUP, k, 0xC0000001 | (0x20000000 if alt else 0))
        time.sleep(0.25)  # let the target read the state while it is set
        user32.SetKeyboardState((ctypes.c_ubyte * 256)(*saved))
    finally:
        user32.AttachThreadInput(me, them, False)


# -- keeping the person's foreground --------------------------------------------------------------

class LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.UINT), ("dwTime", wt.DWORD)]


def last_input_tick() -> int:
    li = LASTINPUTINFO(cbSize=ctypes.sizeof(LASTINPUTINFO))
    user32.GetLastInputInfo(ctypes.byref(li))
    return li.dwTime


def cursor_pos() -> tuple[int, int]:
    p = wt.POINT()
    user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


class _MSG(ctypes.Structure):
    _fields_ = [("hwnd", wt.HWND), ("message", wt.UINT), ("wParam", wt.WPARAM), ("lParam", wt.LPARAM),
                ("time", wt.DWORD), ("pt", wt.POINT)]


def _ensure_queue() -> None:
    # AttachThreadInput fails for a thread that has no message queue yet; peeking creates one.
    user32.PeekMessageW(ctypes.byref(_MSG()), None, 0, 0, 0)


def set_foreground(hwnd: int) -> bool:
    """Give `hwnd` the foreground without synthetic input (shares input state with the current owner)."""
    fg = user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    _ensure_queue()
    fg_tid = thread_of(fg) if fg else 0
    # Attach the thread that owns hwnd first. Attaching an unrelated thread (ours) instead leaves the
    # previous owner unable to take posted keyboard input afterwards (measured on VS Code).
    for mine in (thread_of(hwnd), kernel32.GetCurrentThreadId()):
        attached = bool(fg_tid) and mine != fg_tid and bool(user32.AttachThreadInput(mine, fg_tid, True))
        try:
            user32.BringWindowToTop(hwnd)
            user32.SetForegroundWindow(hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(mine, fg_tid, False)
        if user32.GetForegroundWindow() == hwnd:
            return True
    return False


@dataclass
class GuardResult:
    stolen: bool = False
    restored: bool = False
    person_active: bool = False


class ForegroundGuard:
    """Remember the foreground; if an action hands it to the target, give it back.

    It does not restore when the person used the mouse or keyboard during the action: then the
    change may be theirs.
    """

    def __init__(self, scope_owns: Callable[[int], bool]):
        self.scope_owns = scope_owns
        self.result = GuardResult()

    def __enter__(self) -> ForegroundGuard:
        self.fg = user32.GetForegroundWindow() or 0
        self.tick = last_input_tick()
        return self

    def __exit__(self, *exc) -> None:
        now = user32.GetForegroundWindow() or 0
        if now == self.fg or not now or not self.scope_owns(root_of(now) or now):
            return
        self.result.stolen = True
        if last_input_tick() != self.tick:
            self.result.person_active = True
            return
        if self.fg and w.is_window(self.fg):
            time.sleep(0.05)
            self.result.restored = set_foreground(self.fg)
