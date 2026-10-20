"""Detect that the person finished something, so a waiting agent can carry on without being told.

A wait is a list of conditions; it returns as soon as any one holds. Conditions only read the
desktop (window list, UI Automation, file system); they never click or type, and need no lease.
Each kind can be switched off on the settings page, and window/element waits can be limited to
named processes.

Condition shapes (JSON):
  {"kind": "window_appears", "title_re": "Save As", "process": "notepad.exe"}
  {"kind": "window_closes", "title_re": "Installing"}          (or "hwnd": 1234)
  {"kind": "title_changes", "title_re": "Chrome", "to_re": "Order confirmed"}  (to_re optional)
  {"kind": "element_appears", "title_re": "Setup", "name": "Finish", "control_type": "Button"}
  {"kind": "element_gone", "title_re": "Setup", "name_re": "Installing.*"}
  {"kind": "text_contains", "title_re": "Calculator", "auto_id": "CalculatorResults", "text": "42"}
  {"kind": "file", "path": "C:/Users/me/Downloads/report.pdf", "event": "exists|modified|gone"}
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from overshoulder.settings import WAIT_KINDS, Settings


class WaitError(ValueError):
    code = "wait_refused"


@dataclass
class Fired:
    index: int
    condition: dict
    detail: str


def _windows():
    from overshoulder.driver import windows as w
    return w.top_windows()


def _match_windows(cond: dict, allowed: list[str]):
    rx = re.compile(cond["title_re"], re.IGNORECASE) if cond.get("title_re") else None
    out = []
    for info in _windows():
        if allowed and info.process.lower() not in allowed:
            continue
        if cond.get("process") and info.process.lower() != cond["process"].lower():
            continue
        if cond.get("hwnd") and info.hwnd != int(cond["hwnd"]):
            continue
        if rx and not rx.search(info.title):
            continue
        out.append(info)
    return out


def _elements(win_hwnd: int, cond: dict):
    from overshoulder.driver import uia
    cache = uia.ElementCache()
    els = []
    for root in uia.roots_for_window(win_hwnd):
        els += uia.walk(cache, root, max_nodes=3000)
    return uia.find(els, cond.get("name"), cond.get("name_re"), cond.get("control_type"), cond.get("auto_id"))


class Waiter:
    def __init__(self, conditions: list[dict], settings: Settings):
        if not settings.waits_enabled:
            raise WaitError("detection is switched off on the settings page")
        if not conditions:
            raise WaitError("give at least one condition")
        self.settings = settings
        self.conditions = conditions
        self.state: list[dict] = []
        for c in conditions:
            kind = c.get("kind")
            if kind not in WAIT_KINDS:
                raise WaitError(f"unknown condition kind {kind!r}; one of {sorted(WAIT_KINDS)}")
            if kind not in settings.wait_kinds:
                raise WaitError(f"{kind} waits are switched off on the settings page")
            if kind == "file" and not c.get("path"):
                raise WaitError("file waits need a path")
            if kind in ("window_closes", "title_changes", "element_appears", "element_gone",
                        "text_contains") and not (c.get("title_re") or c.get("hwnd") or c.get("process")):
                raise WaitError(f"{kind} needs title_re, hwnd or process to pick the window")
            self.state.append(self._baseline(c))

    def _baseline(self, c: dict) -> dict:
        kind = c["kind"]
        allowed = self.settings.wait_processes
        if kind == "window_appears":
            return {"seen": {i.hwnd for i in _match_windows(c, allowed)}}
        if kind in ("window_closes", "title_changes"):
            wins = _match_windows(c, allowed)
            if not wins:
                raise WaitError(f"no window matches {c}; it has to exist when the wait starts")
            return {"hwnds": {i.hwnd for i in wins}, "titles": {i.hwnd: i.title for i in wins}}
        if kind == "file":
            p = Path(c["path"])
            return {"exists": p.exists(), "mtime": p.stat().st_mtime if p.exists() else None}
        return {}

    def check(self) -> Fired | None:
        for i, (c, st) in enumerate(zip(self.conditions, self.state, strict=True)):
            detail = self._check_one(c, st)
            if detail:
                return Fired(i, c, detail)
        return None

    def _check_one(self, c: dict, st: dict) -> str | None:
        kind = c["kind"]
        allowed = self.settings.wait_processes
        if kind == "window_appears":
            new = [i for i in _match_windows(c, allowed) if i.hwnd not in st["seen"]]
            return f"window opened: {new[0].title!r} ({new[0].process})" if new else None
        if kind == "window_closes":
            from overshoulder.driver import windows as w
            gone = [h for h in st["hwnds"] if not w.is_window(h)]
            return f"window closed: {st['titles'][gone[0]]!r}" if gone else None
        if kind == "title_changes":
            from overshoulder.driver import windows as w
            to = re.compile(c["to_re"], re.IGNORECASE) if c.get("to_re") else None
            for h, before in st["titles"].items():
                if not w.is_window(h):
                    continue
                now = w.title(h)
                if (to and to.search(now)) or (not to and now != before):
                    return f"title changed: {before!r} -> {now!r}"
            return None
        if kind == "file":
            p = Path(c["path"])
            event = c.get("event", "exists")
            if event == "exists" and p.exists() and not st["exists"]:
                return f"file appeared: {p}"
            if event == "gone" and not p.exists():
                return f"file gone: {p}"
            if event == "modified" and p.exists() and p.stat().st_mtime != st["mtime"]:
                return f"file changed: {p}"
            return None
        # element and text kinds look inside matching windows
        for win in _match_windows(c, allowed):
            try:
                hits = _elements(win.hwnd, c)
            except Exception:
                continue  # window changed mid-walk; try again next poll
            if kind == "element_appears" and hits:
                return f"{hits[0].info.control_type} {hits[0].info.name!r} appeared in {win.title!r}"
            if kind == "element_gone" and not hits:
                return f"element gone from {win.title!r}"
            if kind == "text_contains":
                from overshoulder.driver import uia
                for el in hits or []:
                    try:
                        got = uia.read(el)
                    except Exception:
                        continue
                    hay = " ".join(str(v) for v in got.values())
                    if c.get("text", "") in hay:
                        return f"{c.get('text')!r} shown in {win.title!r}"
        return None


def wait(conditions: list[dict], settings: Settings, timeout_s: float | None = None,
         on_poll=None) -> dict:
    """Block until a condition holds or the timeout passes. Returns {"fired": bool, ...}."""
    waiter = Waiter(conditions, settings)
    limit = settings.max_wait_minutes * 60
    timeout_s = limit if timeout_s is None else min(float(timeout_s), limit)
    t0 = time.monotonic()
    period = settings.poll_interval_ms / 1000
    while True:
        fired = waiter.check()
        if fired:
            if settings.beep_on_detect:
                import winsound
                winsound.MessageBeep()
            return {"fired": True, "condition": fired.index, "detail": fired.detail,
                    "after_s": round(time.monotonic() - t0, 1)}
        if time.monotonic() - t0 >= timeout_s:
            return {"fired": False, "timeout_s": timeout_s}
        if on_poll:
            on_poll()
        time.sleep(period)


def parse_conditions(text: str) -> list[dict]:
    data = json.loads(text)
    return data if isinstance(data, list) else [data]
