"""Top-level window queries via Win32: enumeration, identity, bounds and ownership."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
dwmapi = ctypes.windll.dwmapi

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
GA_ROOTOWNER = 3
GW_OWNER = 4

user32.GetForegroundWindow.restype = wt.HWND
user32.GetAncestor.restype = wt.HWND
user32.GetAncestor.argtypes = [wt.HWND, wt.UINT]
user32.GetWindow.restype = wt.HWND
user32.GetWindow.argtypes = [wt.HWND, wt.UINT]
user32.IsWindow.argtypes = [wt.HWND]
user32.IsWindowVisible.argtypes = [wt.HWND]
user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wt.HWND]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetProcessTimes.argtypes = [wt.HANDLE] + [ctypes.c_void_p] * 4
kernel32.QueryFullProcessImageNameW.argtypes = [wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)]
dwmapi.DwmGetWindowAttribute.argtypes = [wt.HWND, wt.DWORD, ctypes.c_void_p, wt.DWORD]


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def empty(self) -> bool:
        return self.width <= 0 or self.height <= 0

    def contains(self, x: int, y: int) -> bool:
        return self.left <= x < self.right and self.top <= y < self.bottom

    def contains_rect(self, other: Rect) -> bool:
        return (self.left <= other.left and self.top <= other.top
                and other.right <= self.right and other.bottom <= self.bottom)

    def intersect(self, other: Rect) -> Rect:
        r = Rect(max(self.left, other.left), max(self.top, other.top),
                 min(self.right, other.right), min(self.bottom, other.bottom))
        return r if not r.empty else Rect(0, 0, 0, 0)

    def center(self) -> tuple[int, int]:
        return (self.left + self.width // 2, self.top + self.height // 2)


def is_window(hwnd: int) -> bool:
    return bool(user32.IsWindow(hwnd))


def pid_of(hwnd: int) -> int:
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def window_rect(hwnd: int) -> Rect:
    r = wt.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return Rect(r.left, r.top, r.right, r.bottom)


def frame_bounds(hwnd: int) -> Rect:
    """Visible frame without the invisible resize border Windows 10/11 adds around windows."""
    r = wt.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)):
        return window_rect(hwnd)
    return Rect(r.left, r.top, r.right, r.bottom)


def is_cloaked(hwnd: int) -> bool:
    v = wt.DWORD()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v)):
        return False
    return bool(v.value)


def root_owner(hwnd: int) -> int:
    return user32.GetAncestor(hwnd, GA_ROOTOWNER) or 0


def foreground() -> int:
    return user32.GetForegroundWindow() or 0


class _FILETIME(ctypes.Structure):
    _fields_ = [("lo", wt.DWORD), ("hi", wt.DWORD)]


def process_identity(pid: int) -> tuple[int, str] | None:
    """(creation time as FILETIME ticks, image path) for pid, or None if the process is gone."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        c, e, k, u = _FILETIME(), _FILETIME(), _FILETIME(), _FILETIME()
        times = [ctypes.byref(t) for t in (c, e, k, u)]
        if not kernel32.GetProcessTimes(h, *times):
            return None
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        path = buf.value if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) else ""
        return ((c.hi << 32) | c.lo, path)
    finally:
        kernel32.CloseHandle(h)


def _enum_children(hwnd: int) -> list[int]:
    out: list[int] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(h, _lp):
        out.append(h)
        return True

    user32.EnumChildWindows(hwnd, _cb, 0)
    return out


def hosted_app_pid(hwnd: int) -> int | None:
    """For a UWP window hosted by ApplicationFrameHost, the pid of the app's CoreWindow."""
    if class_name(hwnd) != "ApplicationFrameWindow":
        return None
    host = pid_of(hwnd)
    for child in _enum_children(hwnd):
        if class_name(child) == "Windows.UI.Core.CoreWindow" and pid_of(child) != host:
            return pid_of(child)
    return None


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    pid: int
    process: str  # image file name, e.g. "notepad.exe"
    bounds: Rect


def top_windows() -> list[WindowInfo]:
    """Visible, uncloaked, titled top-level windows in z-order (topmost first)."""
    out: list[WindowInfo] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(h, _lp):
        if user32.IsWindowVisible(h) and not is_cloaked(h) and not user32.GetWindow(h, GW_OWNER):
            t = title(h)
            if t:
                pid = hosted_app_pid(h) or pid_of(h)
                ident = process_identity(pid)
                exe = ident[1].rsplit("\\", 1)[-1] if ident and ident[1] else ""
                out.append(WindowInfo(h, t, class_name(h), pid, exe, frame_bounds(h)))
        return True

    user32.EnumWindows(_cb, 0)
    return out


def visible_windows() -> list[tuple[int, Rect]]:
    """Every visible, uncloaked top-level window with its frame, owned and untitled ones too (menus,
    dialogs, tooltips), in z-order. top_windows() lists what an agent can pick; this is what is on screen."""
    out: list[tuple[int, Rect]] = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(h, _lp):
        if user32.IsWindowVisible(h) and not is_cloaked(h):
            out.append((h, frame_bounds(h)))
        return True

    user32.EnumWindows(_cb, 0)
    return out


GA_ROOT = 2
user32.WindowFromPoint.restype = wt.HWND
user32.WindowFromPoint.argtypes = [wt.POINT]


def window_at(x: int, y: int) -> int:
    """Top-level window that would receive a click at (x, y), or 0."""
    h = user32.WindowFromPoint(wt.POINT(x, y))
    return (user32.GetAncestor(h, GA_ROOT) or 0) if h else 0
