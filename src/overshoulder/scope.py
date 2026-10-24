"""Scopes: the one unit of permission. A session acts only inside the scope it leased.

A window scope is bound to a specific window *and* the process instance that owns it, so a new
window that happens to reuse the handle, or a restarted app with the same title, is not in scope.
A screen scope is one monitor or all of them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from overshoulder.appid import AppIdentity
from overshoulder.driver import display
from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect


class ScopeError(RuntimeError):
    code = "scope_error"


class ScopeStale(ScopeError):
    """The bound window closed, or its handle now belongs to a different process instance."""
    code = "scope_stale"


class OutOfScope(ScopeError):
    """A point, rect or window is outside the leased scope."""
    code = "out_of_scope"


class ScopeNotFound(ScopeError):
    code = "scope_not_found"


class Scope(Protocol):
    kind: str

    def describe(self) -> dict: ...
    def bounds(self) -> Rect: ...
    def check_alive(self) -> None: ...
    def owns_window(self, hwnd: int) -> bool: ...


def require_point(scope: Scope, x: int, y: int) -> None:
    scope.check_alive()
    contains = getattr(scope, "contains", None)
    inside = contains(x, y) if contains else scope.bounds().contains(x, y)
    if not inside:
        raise OutOfScope(f"point ({x}, {y}) is outside the {scope.kind} scope {scope.bounds()}")


def require_rect(scope: Scope, rect: Rect) -> None:
    scope.check_alive()
    b = scope.bounds()
    if not b.contains_rect(rect):
        raise OutOfScope(f"rect {rect} is not fully inside the {scope.kind} scope {b}")


def require_foreground_in_scope(scope: Scope, hwnd: int | None = None) -> int:
    """The foreground window must belong to the scope; returns it. Used before every keystroke."""
    scope.check_alive()
    fg = w.foreground() if hwnd is None else hwnd
    if not fg or not scope.owns_window(fg):
        raise OutOfScope(f"foreground window {fg:#x} is not in the {scope.kind} scope")
    return fg


@dataclass(frozen=True)
class WindowBinding:
    hwnd: int
    pid: int  # the app's pid (for UWP, the hosted app, not ApplicationFrameHost)
    owner_pid: int  # the pid that owns the top-level hwnd
    created: int  # owner process creation time, FILETIME ticks
    process: str
    class_name: str
    title: str
    app: AppIdentity | None = None  # which app this is, for its profile (overshoulder.profiles)


def _app_identity(pid: int, image: str) -> AppIdentity | None:
    from overshoulder.driver import appinfo
    try:
        return appinfo.read(pid, image)
    except OSError:
        return None  # a hint for profiles; the scope binds without it


def bind_window(hwnd: int) -> WindowBinding:
    if not w.is_window(hwnd):
        raise ScopeNotFound(f"no window {hwnd:#x}")
    owner_pid = w.pid_of(hwnd)
    ident = w.process_identity(owner_pid)
    if ident is None:
        raise ScopeNotFound(f"window {hwnd:#x} has no live owning process")
    app_pid = w.hosted_app_pid(hwnd) or owner_pid
    app_ident = w.process_identity(app_pid) or ident
    return WindowBinding(hwnd, app_pid, owner_pid, ident[0], app_ident[1].rsplit("\\", 1)[-1],
                         w.class_name(hwnd), w.title(hwnd), _app_identity(app_pid, app_ident[1]))


class WindowScope:
    kind = "window"

    def __init__(self, binding: WindowBinding):
        self.binding = binding

    def describe(self) -> dict:
        b = self.binding
        out = {"kind": "window", "hwnd": b.hwnd, "pid": b.pid, "process": b.process,
               "class": b.class_name, "title": b.title}
        if b.app:
            out["app"] = b.app.describe()
        return out

    def check_alive(self) -> None:
        b = self.binding
        if not w.is_window(b.hwnd):
            raise ScopeStale(f"window {b.hwnd:#x} ({b.title!r}) is gone")
        if w.pid_of(b.hwnd) != b.owner_pid:
            raise ScopeStale(f"window handle {b.hwnd:#x} now belongs to another process")
        ident = w.process_identity(b.owner_pid)
        if ident is None or ident[0] != b.created:
            raise ScopeStale(f"process {b.owner_pid} restarted since the scope was bound")

    def bounds(self) -> Rect:
        return w.frame_bounds(self.binding.hwnd)

    def owns_window(self, hwnd: int) -> bool:
        """The bound window itself, or a dialog/popup of the same app whose owner chain leads to it."""
        if hwnd == self.binding.hwnd:
            return True
        return (w.root_owner(hwnd) == self.binding.hwnd
                and w.pid_of(hwnd) in (self.binding.pid, self.binding.owner_pid))


class ScreenScope:
    kind = "screen"

    def __init__(self, monitor: int | None):
        """monitor: 1-based index, or None for all monitors."""
        self.monitor = monitor
        mons = display.monitors()
        if monitor is not None and not any(m.index == monitor for m in mons):
            raise ScopeNotFound(f"no monitor {monitor}; have {[m.index for m in mons]}")

    def describe(self) -> dict:
        return {"kind": "screen", "monitor": "all" if self.monitor is None else self.monitor}

    def _rects(self) -> list[Rect]:
        mons = display.monitors()
        picked = [m for m in mons if self.monitor is None or m.index == self.monitor]
        return [Rect(m.left, m.top, m.right, m.bottom) for m in picked]

    def check_alive(self) -> None:
        if not self._rects():
            raise ScopeStale(f"monitor {self.monitor} is no longer connected")

    def bounds(self) -> Rect:
        rs = self._rects()
        if not rs:
            raise ScopeStale(f"monitor {self.monitor} is no longer connected")
        return Rect(min(r.left for r in rs), min(r.top for r in rs),
                    max(r.right for r in rs), max(r.bottom for r in rs))

    def contains(self, x: int, y: int) -> bool:
        return any(r.contains(x, y) for r in self._rects())

    def owns_window(self, hwnd: int) -> bool:
        """A window whose visible frame is centred on the scope's monitor(s)."""
        f = w.frame_bounds(hwnd)
        return self.contains(*f.center())


_SPEC = re.compile(r"^(window|screen):(.+)$")


def parse_scope(spec: str) -> Scope:
    """Build a scope from 'window:<hwnd>' (decimal or 0x hex), 'screen:<n>' or 'screen:all'."""
    m = _SPEC.match(spec.strip().lower())
    if not m:
        raise ValueError(f"bad scope spec {spec!r}; use window:<hwnd>, screen:<n> or screen:all")
    kind, arg = m.groups()
    if kind == "screen":
        return ScreenScope(None if arg == "all" else int(arg))
    return WindowScope(bind_window(int(arg, 0)))


def find_windows(title_re: str | None = None, process: str | None = None) -> list[w.WindowInfo]:
    """Candidate windows to scope to, filtered by title regex and/or process image name."""
    rx = re.compile(title_re, re.IGNORECASE) if title_re else None
    out = []
    for info in w.top_windows():
        if rx and not rx.search(info.title):
            continue
        if process and info.process.lower() != process.lower():
            continue
        out.append(info)
    return out
