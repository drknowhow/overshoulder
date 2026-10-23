"""Narrowing: limits that make a session do less than its scope allows.

Two sources, merged into one set of limits per lease:
  - the caller's own, asked for in take_scope. They last for that lease; taking a new scope on the same
    lease can add to them but never take them away;
  - the person's standing limits for a client (`overshoulder client limit NAME ...`). They apply to every
    lease that client takes, and no agent call can lift them.

  read_only       every act is refused; observe, find, read, screenshot and recording still work.
  no_typing       type, set_value and hotkey are refused.
  deny_processes  windows of these programs (image names, e.g. "chrome.exe") are not in scope: a window
                  scope on one is refused, acts on them are refused, observe/find/windows leave them out,
                  and screenshots and recordings of a screen scope black them out.

Process names are matched case-insensitively; "chrome" means "chrome.exe".
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect
from overshoulder.gateway.errors import Narrowed

TYPING_ACTIONS = frozenset({"type", "set_value", "hotkey"})
LIMIT_KEYS = ("read_only", "no_typing", "deny_processes")


def process_key(name: str) -> str:
    n = str(name or "").strip().lower().rsplit("\\", 1)[-1]
    return n if not n or "." in n else n + ".exe"


@dataclass(frozen=True)
class Limits:
    read_only: bool = False
    no_typing: bool = False
    deny_processes: frozenset[str] = frozenset()

    @classmethod
    def make(cls, read_only: bool = False, no_typing: bool = False,
             deny_processes: Iterable[str] | None = None) -> Limits:
        return cls(bool(read_only), bool(no_typing),
                   frozenset(p for p in (process_key(x) for x in deny_processes or ()) if p))

    @classmethod
    def from_dict(cls, d: dict | None) -> Limits:
        d = d or {}
        return cls.make(d.get("read_only", False), d.get("no_typing", False), d.get("deny_processes"))

    def to_dict(self) -> dict:
        return {"read_only": self.read_only, "no_typing": self.no_typing,
                "deny_processes": sorted(self.deny_processes)}

    def merge(self, other: Limits) -> Limits:
        """Both sets at once: a limit set in either holds."""
        return Limits(self.read_only or other.read_only, self.no_typing or other.no_typing,
                      self.deny_processes | other.deny_processes)

    def __bool__(self) -> bool:
        return self.read_only or self.no_typing or bool(self.deny_processes)

    def denies(self, process: str) -> bool:
        return bool(process) and process_key(process) in self.deny_processes

    def describe(self) -> str:
        parts = (["read-only"] if self.read_only else []) + (["no typing"] if self.no_typing else [])
        if self.deny_processes:
            parts.append("hides " + ", ".join(sorted(self.deny_processes)))
        return "; ".join(parts) or "none"


@dataclass(frozen=True)
class LeaseLimits:
    """The limits on one lease, keeping who set each, so a refusal can say whose limit it is."""
    person: Limits = Limits()
    session: Limits = Limits()

    @property
    def effective(self) -> Limits:
        return self.person.merge(self.session)

    def set_by(self, key: str, process: str = "") -> str:
        if key == "deny_processes":
            return "person" if self.person.denies(process) else "session"
        return "person" if getattr(self.person, key) else "session"

    def to_dict(self) -> dict:
        return {**self.effective.to_dict(), "set_by_person": self.person.to_dict()}

    def refuse(self, key: str, message: str, process: str = "") -> Narrowed:
        by = self.set_by(key, process)
        who = "the person set this limit for your client" if by == "person" else \
            "you asked for this limit in take_scope"
        return Narrowed(f"{message} ({who})", limit=key, set_by=by)

    def check_action(self, action: str) -> None:
        """read_only and no_typing, before anything else about the action is looked at."""
        eff = self.effective
        if eff.read_only:
            raise self.refuse("read_only", f"this session is read-only, so {action} is refused")
        if eff.no_typing and action in TYPING_ACTIONS:
            raise self.refuse("no_typing", f"this session may not type, so {action} is refused")

    def check_process(self, process: str, what: str) -> None:
        if self.effective.denies(process):
            raise self.refuse("deny_processes", f"{what} belongs to {process_key(process)}, which this session "
                                                f"may not touch", process)


class ProcessNames:
    """Image names for pids and windows, cached briefly (a pid can be reused, so not for long)."""

    def __init__(self, ttl_s: float = 5.0):
        self.ttl_s = ttl_s
        self._cache: dict[int, tuple[float, str]] = {}

    def of_pid(self, pid: int) -> str:
        now = time.monotonic()
        hit = self._cache.get(pid)
        if hit and now - hit[0] < self.ttl_s:
            return hit[1]
        ident = w.process_identity(pid) if pid else None
        name = process_key(ident[1]) if ident and ident[1] else ""
        if len(self._cache) > 512:
            self._cache.clear()
        self._cache[pid] = (now, name)
        return name

    def of_window(self, hwnd: int) -> list[str]:
        """The window's own process and, for a UWP frame, the app it hosts."""
        pids = {w.pid_of(hwnd)}
        hosted = w.hosted_app_pid(hwnd)
        if hosted:
            pids.add(hosted)
        return [n for n in (self.of_pid(p) for p in pids) if n]


PROCESS_NAMES = ProcessNames()


class NarrowedScope:
    """A lease's scope with its limits attached. Everything not about limits is the wrapped scope's.

    owns_window() is what every scope check goes through (hit tests, background targets, the keyboard
    focus rule, observe and find), so a denied program's window is out of scope everywhere at once; the
    gateway checks deny_processes first so the refusal says why. `source` gives the lease's limits as they
    are now, so a limit the person adds while a lease is held applies at the next check.
    """

    def __init__(self, inner, source: Callable[[], LeaseLimits],
                 window_processes: Callable[[int], list[str]] | None = None,
                 visible_windows: Callable[[], list[tuple[int, Rect]]] | None = None):
        self.inner = inner
        self.source = source
        self._window_processes = window_processes or PROCESS_NAMES.of_window
        self._visible_windows = visible_windows or w.visible_windows

    def __getattr__(self, name):
        return getattr(self.inner, name)

    @property
    def limits(self) -> LeaseLimits:
        return self.source()

    def denied_window(self, hwnd: int, deny: Limits | None = None) -> str:
        """The denied program a window belongs to, or ""."""
        deny = deny if deny is not None else self.limits.effective
        if not deny.deny_processes:
            return ""
        for p in self._window_processes(hwnd):
            if deny.denies(p):
                return p
        return ""

    def owns_window(self, hwnd: int) -> bool:
        return self.inner.owns_window(hwnd) and not self.denied_window(hwnd)

    def describe(self) -> dict:
        d = self.inner.describe()
        hidden = self.limits.effective.deny_processes
        return {**d, "hidden": sorted(hidden)} if hidden else d

    def hidden_rects(self) -> list[Rect]:
        """Screen rects of every visible window of a denied program (menus and dialogs too). Each is
        blacked out whole, even where another window covers part of it: hiding a little too much is the
        safe way to be wrong."""
        deny = self.limits.effective
        if not deny.deny_processes:
            return []
        return [r for h, r in self._visible_windows() if not r.empty and self.denied_window(h, deny)]


def hidden_rects(scope) -> list[Rect]:
    fn = getattr(scope, "hidden_rects", None)
    return fn() if fn else []


def black_out(img, origin: Rect, rects: list[Rect]):
    """Paint rects (screen pixels) black on an image of the screen area starting at origin."""
    if not rects:
        return img
    from PIL import ImageDraw
    d = ImageDraw.Draw(img)
    for r in rects:
        box = (r.left - origin.left, r.top - origin.top, r.right - origin.left - 1, r.bottom - origin.top - 1)
        if box[2] >= 0 and box[3] >= 0 and box[0] < img.width and box[1] < img.height:
            d.rectangle(box, fill=(0, 0, 0))
    return img
