"""DPI awareness and monitor geometry, in physical pixels."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
from dataclasses import dataclass

_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
_MDT_EFFECTIVE_DPI = 0
_dpi_enabled = False


def enable_dpi_awareness() -> bool:
    """Make this process per-monitor DPI aware (v2) so UIA rects, capture and SendInput share pixels.

    Must run before any UI library creates a window or queries geometry. Returns True if this call
    (or an earlier one) succeeded. Idempotent.
    """
    global _dpi_enabled
    if _dpi_enabled:
        return True
    ok = bool(ctypes.windll.user32.SetProcessDpiAwarenessContext(
        ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))
    # Fails with ERROR_ACCESS_DENIED when awareness was already set (e.g. by a manifest); that is fine
    # only if the existing context is per-monitor v2.
    if not ok:
        current = ctypes.windll.user32.GetThreadDpiAwarenessContext()
        ok = bool(ctypes.windll.user32.AreDpiAwarenessContextsEqual(
            ctypes.c_void_p(current), ctypes.c_void_p(_DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)))
    _dpi_enabled = ok
    return ok


@dataclass(frozen=True)
class Monitor:
    index: int  # 1-based, in enumeration order
    left: int
    top: int
    right: int
    bottom: int
    dpi: int
    primary: bool

    @property
    def width(self) -> int:
        return self.right - self.left

    @property
    def height(self) -> int:
        return self.bottom - self.top

    @property
    def scale(self) -> float:
        return self.dpi / 96


_MONITORINFOF_PRIMARY = 1


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", wt.RECT), ("rcWork", wt.RECT), ("dwFlags", wt.DWORD)]


def monitors() -> list[Monitor]:
    """All monitors with their physical-pixel rects and effective DPI."""
    user32 = ctypes.windll.user32
    shcore = ctypes.windll.shcore
    found: list[Monitor] = []

    @ctypes.WINFUNCTYPE(ctypes.c_int, wt.HMONITOR, wt.HDC, ctypes.POINTER(wt.RECT), wt.LPARAM)
    def _cb(hmon, _hdc, rect, _lp):
        dx, dy = ctypes.c_uint(), ctypes.c_uint()
        dpi = 96 if shcore.GetDpiForMonitor(hmon, _MDT_EFFECTIVE_DPI, ctypes.byref(dx), ctypes.byref(dy)) \
            else dx.value
        info = _MONITORINFO(cbSize=ctypes.sizeof(_MONITORINFO))
        user32.GetMonitorInfoW(hmon, ctypes.byref(info))
        r = rect.contents
        found.append(Monitor(len(found) + 1, r.left, r.top, r.right, r.bottom, dpi,
                             bool(info.dwFlags & _MONITORINFOF_PRIMARY)))
        return 1

    user32.EnumDisplayMonitors(None, None, _cb, 0)
    return found
