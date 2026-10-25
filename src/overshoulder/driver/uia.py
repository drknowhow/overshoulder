"""UI Automation access through pywinauto: scoped observation, element refs, patterns.

Elements are handed to callers as short refs ("e12"). A ref resolves back to the same UIA element
for the life of the cache; acting on it re-reads its rect and identity first so the gateway can
refuse a target that moved or vanished.
"""

from __future__ import annotations

import itertools
import re
import threading
from dataclasses import dataclass

from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect

try:  # pywinauto is Windows-only; import lazily-safe for type checkers on other platforms
    from pywinauto.uia_element_info import UIAElementInfo
except ImportError:  # pragma: no cover
    UIAElementInfo = None


def _rect(info) -> Rect:
    r = info.rectangle
    return Rect(r.left, r.top, r.right, r.bottom)


def _is_password(info) -> bool:
    try:
        return bool(info.element.CurrentIsPassword)
    except Exception:
        return False


@dataclass
class Element:
    ref: str
    info: object  # UIAElementInfo
    root_hwnd: int
    depth: int


class ElementCache:
    """Ref -> live UIA element. One per lease; cleared when the lease changes."""

    def __init__(self, limit: int = 5000):
        self._lock = threading.Lock()
        self._by_ref: dict[str, Element] = {}
        self._by_runtime: dict[tuple, str] = {}
        self._ids = itertools.count(1)
        self.limit = limit

    def clear(self) -> None:
        with self._lock:
            self._by_ref.clear()
            self._by_runtime.clear()

    def put(self, info, root_hwnd: int, depth: int) -> Element:
        with self._lock:
            try:
                rid = tuple(info.runtime_id or ())
            except Exception:
                rid = ()
            if rid and rid in self._by_runtime:
                el = self._by_ref[self._by_runtime[rid]]
                el.info = info
                return el
            if len(self._by_ref) >= self.limit:
                self._by_ref.clear()
                self._by_runtime.clear()
            ref = f"e{next(self._ids)}"
            el = Element(ref, info, root_hwnd, depth)
            self._by_ref[ref] = el
            if rid:
                self._by_runtime[rid] = ref
            return el

    def get(self, ref: str) -> Element | None:
        with self._lock:
            return self._by_ref.get(ref)


def describe_all(elements: list[Element]) -> list[dict]:
    """Describe elements, dropping any that vanish mid-read (apps rebuild their UI while loading)."""
    out = []
    for el in elements:
        try:
            out.append(describe(el))
        except Exception:
            continue
    return out


def describe(el: Element) -> dict:
    info = el.info
    r = _rect(info)
    d = {"ref": el.ref, "name": info.name or "", "type": info.control_type or "",
         "rect": [r.left, r.top, r.right, r.bottom], "depth": el.depth}
    if info.automation_id:
        d["auto_id"] = info.automation_id
    if not info.enabled:
        d["enabled"] = False
    if _is_password(info):
        d["password"] = True
    return d


def roots_for_window(hwnd: int) -> list[int]:
    """The scope window plus top-level dialogs/popups it owns, topmost first."""
    owned = [x.hwnd for x in w.top_windows() if x.hwnd != hwnd and w.root_owner(x.hwnd) == hwnd]
    return [*owned, hwnd]


def walk(cache: ElementCache, root_hwnd: int, max_nodes: int = 400, max_depth: int = 25,
         only_named: bool = False) -> list[Element]:
    """Breadth-first walk under a top-level window, skipping offscreen elements."""
    root = UIAElementInfo(root_hwnd)
    out: list[Element] = [cache.put(root, root_hwnd, 0)]
    frontier = [(root, 0)]
    while frontier and len(out) < max_nodes:
        nxt = []
        for info, depth in frontier:
            if depth >= max_depth:
                continue
            try:
                kids = info.children()
            except Exception:
                continue
            for k in kids:
                try:
                    if k.element.CurrentIsOffscreen:
                        continue
                except Exception:
                    continue
                el = cache.put(k, root_hwnd, depth + 1)
                if not only_named or k.name:
                    out.append(el)
                nxt.append((k, depth + 1))
                if len(out) >= max_nodes:
                    break
            if len(out) >= max_nodes:
                break
        frontier = nxt
    return out


def by_automation_id(cache: ElementCache, root_hwnd: int, auto_id: str) -> list[Element]:
    """Elements under a top-level window with this automation id, found by UI Automation's own FindAll
    instead of a walk, which reads each element over COM. Offscreen ones are skipped, as walk skips them.
    Their depth is not counted (1)."""
    from pywinauto.uia_defines import IUIA
    iuia = IUIA()
    cond = iuia.iuia.CreatePropertyCondition(iuia.UIA_dll.UIA_AutomationIdPropertyId, auto_id)
    found = UIAElementInfo(root_hwnd).element.FindAll(iuia.tree_scope["descendants"], cond)
    out = []
    for i in range(found.Length if found else 0):
        e = found.GetElement(i)
        try:
            if e.CurrentIsOffscreen:
                continue
        except Exception:
            continue
        out.append(cache.put(UIAElementInfo(e), root_hwnd, 1))
    return out


def find(elements: list[Element], name: str | None = None, name_re: str | None = None,
         control_type: str | None = None, auto_id: str | None = None) -> list[Element]:
    rx = re.compile(name_re, re.IGNORECASE) if name_re else None
    hits = []
    for el in elements:
        info = el.info
        if name is not None and (info.name or "") != name:
            continue
        if rx and not rx.search(info.name or ""):
            continue
        if control_type and (info.control_type or "").lower() != control_type.lower():
            continue
        if auto_id and info.automation_id != auto_id:
            continue
        hits.append(el)
    return hits


def element_state(el: Element) -> dict | None:
    """Fresh identity + rect, or None if the element no longer exists."""
    info = el.info
    try:
        r = _rect(info)
        return {"rect": r, "name": info.name or "", "type": info.control_type or "",
                "auto_id": info.automation_id or "", "pid": info.process_id,
                "password": _is_password(info), "enabled": bool(info.enabled)}
    except Exception:
        return None


# -- patterns ------------------------------------------------------------------------------------

def _wrapper(el: Element):
    from pywinauto.controls.uiawrapper import UIAWrapper
    return UIAWrapper(el.info)


# These go through the pattern interfaces, not the control-type wrapper's methods: a wrapper picked for
# the control type (a ListItem, say) may not have the method, while a missing pattern always raises
# NoPatternInterfaceError, which the ladder turns into a refusal.
def invoke(el: Element) -> None:
    _wrapper(el).iface_invoke.Invoke()


def toggle(el: Element) -> str:
    iface = _wrapper(el).iface_toggle
    iface.Toggle()
    return str(iface.CurrentToggleState)


def select(el: Element) -> None:
    _wrapper(el).iface_selection_item.Select()


def set_value(el: Element, value: str) -> None:
    _wrapper(el).iface_value.SetValue(value)


def set_range_value(el: Element, value: float) -> float:
    """Set a slider's or spinner's number through RangeValue and return what it reads back. A number
    outside its range raises ValueError naming the range."""
    rv = _wrapper(el).iface_range_value
    lo, hi = rv.CurrentMinimum, rv.CurrentMaximum
    if not lo <= value <= hi:
        raise ValueError(f"{value:g} is outside this control's range, {lo:g} to {hi:g}")
    rv.SetValue(value)
    return rv.CurrentValue


def focus(el: Element) -> None:
    el.info.element.SetFocus()


def activate(hwnd: int) -> None:
    """Bring a top-level window to the front via UIA (no synthetic Alt press)."""
    UIAElementInfo(hwnd).element.SetFocus()


def keys_of(info) -> tuple[str, str]:
    """(accelerator key, access key) as UI Automation reports them, e.g. ("Ctrl+S", "Alt, F")."""
    try:
        e = info.element
        return e.CurrentAcceleratorKey or "", e.CurrentAccessKey or ""
    except Exception:
        return "", ""


def read(el: Element) -> dict:
    """Name, plus value or document text when the element exposes them."""
    info = el.info
    out = {"name": info.name or ""}
    wr = _wrapper(el)
    try:
        out["value"] = wr.iface_value.CurrentValue
    except Exception:
        pass
    if "value" not in out:
        try:
            out["value"] = wr.iface_range_value.CurrentValue  # a slider or spinner: a number
        except Exception:
            pass
    if "value" not in out:
        try:
            out["text"] = wr.iface_text.DocumentRange.GetText(-1)
        except Exception:
            pass
    try:
        out["toggle"] = str(wr.iface_toggle.CurrentToggleState)
    except Exception:
        pass
    return out


def _iuia():
    from pywinauto.uia_defines import IUIA
    return IUIA().iuia


def _probe(elem) -> dict | None:
    if elem is None:
        return None
    try:
        info = UIAElementInfo(elem)
        return {"name": info.name or "", "type": info.control_type or "", "pid": info.process_id,
                "password": bool(elem.CurrentIsPassword)}
    except Exception:
        return None


def info_for_handle(hwnd: int) -> dict | None:
    """Name, control type, pid and IsPassword of the UIA element behind a window handle."""
    try:
        return _probe(UIAElementInfo(hwnd).element)
    except Exception:
        return None


def any_password(root_hwnd: int, max_nodes: int = 3000) -> bool | None:
    """Whether any password field exists in a window's tree (None if the tree cannot be read)."""
    try:
        for root in roots_for_window(root_hwnd):
            for el in walk(ElementCache(), root, max_nodes=max_nodes):
                if _is_password(el.info):
                    return True
        return False
    except Exception:
        return None


def element_in_window_at(root_hwnd: int, x: int, y: int, max_nodes: int = 3000) -> dict | None:
    """The smallest element of a window's own tree containing (x, y), even if the window is covered."""
    best, best_area = None, None
    try:
        for root in roots_for_window(root_hwnd):
            for el in walk(ElementCache(), root, max_nodes=max_nodes):
                try:
                    r = _rect(el.info)
                except Exception:
                    continue
                if el.depth == 0 or not r.contains(x, y):
                    continue
                area = r.width * r.height
                if best_area is None or area < best_area:
                    best, best_area = el, area
    except Exception:
        return None
    if best is None:
        return None
    info = best.info
    return {"name": info.name or "", "type": info.control_type or "", "pid": info.process_id,
            "password": _is_password(info)}


def focused_info() -> dict | None:
    """Name, control type, pid and IsPassword of the element with keyboard focus; None if unknown."""
    try:
        return _probe(_iuia().GetFocusedElement())
    except Exception:
        return None


def info_at_point(x: int, y: int) -> dict | None:
    """The UIA element under a screen point; None if unknown (e.g. canvas apps)."""
    try:
        from ctypes.wintypes import POINT
        return _probe(_iuia().ElementFromPoint(POINT(x, y)))
    except Exception:
        return None
