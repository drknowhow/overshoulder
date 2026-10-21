"""The method ladder: do each action without the person's input when the app allows it.

Rungs, per action (docs/SPIKE-phase2.md has the measurements behind them):
  press a control   UIA Invoke/Toggle/Select/Expand  -> posted click -> real click
  click a point     posted click (one retry for Chromium, which can swallow the first) -> real click
  type              WM_CHAR posted to the scope's input window -> EM_REPLACESEL -> real typing
                    (Chromium/Electron windows in the background go straight to real typing)
  set a value       UIA ValuePattern -> WM_SETTEXT
  hotkey            keys posted with modifiers in the target thread's key state -> (real, only if asked)

The method argument picks how far down the ladder an action may go:
  "background"  never the person's mouse or keyboard; fail instead
  "auto"        background first, real input only when a rung reports it cannot reach the window or
                read-back proves the background attempt did nothing
  "input"       real input straight away
Real input is never used to retry something that may already have happened (a hotkey, or a click
whose effect is just not visible), so nothing is done twice.

Every rung asks the gateway first: p["background_target"](hwnd) before messaging a window, and
p["real_input"]() before SendInput. A ForegroundGuard wraps each attempt and puts the person's
foreground window back if the app grabs it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from overshoulder.driver import background as bg
from overshoulder.driver import capture, uia
from overshoulder.driver import input as inp
from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect

METHODS = ("auto", "background", "input")
PRESSABLE = {"button", "menuitem", "hyperlink", "splitbutton", "checkbox", "radiobutton", "tabitem", "listitem",
             "treeitem"}
REGION = 90  # half-size of the area checked for a visible change around a posted click
# Posted input is processed asynchronously (Chromium hands it to a renderer process), so "nothing
# arrived" is only concluded after read-back stays unchanged this long. Concluding it early would
# make "auto" type the text a second time with real keys.
LANDED_WAIT_S = 2.0


class NeedsRealInput(RuntimeError):
    """No background method reached the window and the caller did not allow real input."""
    code = "needs_real_input"


@dataclass
class Outcome:
    method: str = ""
    background: bool = True
    attempts: list[dict] = field(default_factory=list)
    focus: dict = field(default_factory=dict)
    note: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {"method": self.method, "background": self.background, "attempts": self.attempts}
        if self.focus:
            d["focus"] = self.focus
        if self.note:
            d["note"] = self.note
        d.update(self.extra)
        return d


class Ladder:
    def __init__(self, scope, p: dict, method: str = "auto"):
        if method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        self.scope = scope
        self.p = p
        self.method = method
        self.out = Outcome()

    # -- shared pieces ---------------------------------------------------------------------------

    def _guarded(self, fn):
        # Whether an app may take the foreground depends on Windows' state at that moment (which
        # process is in front, which one got the last input), so the same UIA call can bring an app
        # forward in one run and not the next. Nothing is refused for it: the guard puts the person's
        # window back and the outcome reports what happened.
        with bg.ForegroundGuard(self._owns) as guard:
            result = fn()
        if guard.result.stolen:
            self.out.focus = {"stolen": True, "restored": guard.result.restored,
                              "person_active": guard.result.person_active}
        return result

    def _owns(self, hwnd: int) -> bool:
        try:
            return self.scope.owns_window(hwnd)
        except Exception:
            return False

    def _attempt(self, name: str, ok: bool, detail: str = "") -> None:
        self.out.attempts.append({"method": name, "ok": ok, **({"detail": detail} if detail else {})})

    def _done(self, name: str, background: bool = True) -> Outcome:
        self.out.method, self.out.background = name, background
        return self.out

    def _real_allowed(self) -> bool:
        return self.method in ("auto", "input")

    def _no_background_left(self, why: str) -> None:
        if not self._real_allowed():
            raise NeedsRealInput(f"{why}; this needs the person's mouse or keyboard (method='background')")

    def _scope_in_front(self) -> bool:
        fg = w.foreground()
        try:
            return bool(fg) and self.scope.owns_window(bg.root_of(fg) or fg)
        except Exception:
            return False

    def top_at(self, x: int, y: int) -> int:
        """The scope's top-level window (or owned dialog) under a point, ignoring what covers it."""
        binding = getattr(self.scope, "binding", None)
        if binding is not None:
            for h in uia.roots_for_window(binding.hwnd):
                if w.frame_bounds(h).contains(x, y):
                    return h
            return binding.hwnd
        return w.window_at(x, y)

    def focus_window(self, top: int) -> int:
        return bg.input_window(top)

    # -- actions ---------------------------------------------------------------------------------

    def press(self, el, target, button: str = "left", count: int = 1) -> Outcome:
        """click / double_click / right_click."""
        kind = (target.control_type or "").lower()
        if self.method != "input" and el is not None and button == "left" and count == 1 and kind in PRESSABLE:
            for name, fn in (("uia_invoke", uia.invoke), ("uia_toggle", uia.toggle), ("uia_select", uia.select)):
                try:
                    self.p["abort"]()
                    self._guarded(lambda fn=fn: fn(el))
                    self._attempt(name, True)
                    return self._done(name)
                except Exception as e:  # pattern not supported: next rung
                    if getattr(e, "code", None) in ("lease_revoked", "paused", "needs_real_input"):
                        raise
                    self._attempt(name, False, type(e).__name__)
        x, y = target.point()
        if self.method != "input":
            top = self.top_at(x, y)
            child = bg.deepest_child_at(top, x, y)
            try:
                self.p["background_target"](child)
                region = Rect(x - REGION, y - REGION, x + REGION, y + REGION)
                for attempt in (1, 2):
                    self.p["abort"]()
                    before = capture.window_image(top, region)
                    self._guarded(lambda: bg.post_click(top, x, y, button, count))
                    time.sleep(0.25)
                    after = capture.window_image(top, region)
                    seen = before is not None and after is not None and \
                        capture.changed_fraction(before, after) > 0.002
                    self._attempt("posted_click", True, "visible change" if seen else "no visible change")
                    if seen or attempt == 2 or not bg.is_chromium(child):
                        break  # Chromium may swallow the first click after inactivity; others never retried
                if not seen:
                    self.out.note = ("no visible change; if nothing happened, the app may ignore posted clicks: "
                                     "retry with method='input'")
                return self._done("posted_click")
            except bg.BackgroundUnsupported as e:
                self._attempt("posted_click", False, str(e))
                self._no_background_left(str(e))
        self.p["real_input"]()
        inp.click(x, y, button=button, count=count)
        self._attempt("real_click", True)
        return self._done("real_click", background=False)

    def type_text(self, el, text: str) -> Outcome:
        if self.method == "input":
            return self._real_type(el, text)
        top = el.root_hwnd if el is not None else getattr(getattr(self.scope, "binding", None), "hwnd", 0)
        hwnd = 0
        if el is not None:
            native = getattr(el.info, "handle", None) or 0
            if native and bg.is_edit_class(native):
                hwnd = native
            else:
                # The field must hold focus inside its window, or the keys would land in another field.
                try:
                    self._guarded(lambda: uia.focus(el))
                    time.sleep(0.15)
                except NeedsRealInput:
                    raise
                except Exception as e:  # not focusable through UIA; keys go to the window's own focus
                    self._attempt("uia_focus", False, type(e).__name__)
        if not hwnd:
            focus = self.p.get("scope_focus") or {}
            hwnd = (focus.get("hwnd") if el is None else 0) or (self.focus_window(top) if top else 0)
        if not hwnd and getattr(self.scope, "binding", None) is None:
            hwnd = self.focus_window(w.foreground())  # screen scope: the foreground app's focus
        if not hwnd:
            self._no_background_left("no window to send keys to")
            return self._real_type(el, text)
        self.p["background_target"](hwnd, typing=True)
        if bg.is_chromium(hwnd) and not self._scope_in_front():
            # Measured on VS Code: once a Chromium window has been active and lost the foreground, posted
            # characters reach its input field but are not committed to the document, and read-back of
            # that field cannot tell. Not trusted, so this goes to real input (or is refused).
            self._attempt("posted_chars", False, "Chromium window in the background")
            self._no_background_left("Chromium/Electron apps do not reliably accept posted typing in the background")
            return self._real_type(el, text)
        before = self._read(el, hwnd)
        try:
            self._guarded(lambda: bg.post_text(hwnd, text, abort=self.p["abort"]))
        except bg.BackgroundUnsupported as e:
            self._attempt("posted_chars", False, str(e))
            self._no_background_left(str(e))
            return self._real_type(el, text)
        landed = self._wait_landed(el, hwnd, before, text)
        self._attempt("posted_chars", landed is not False, _landed_detail(landed))
        if landed is not False:
            if landed is None:
                self.out.note = ("typed, but the text could not be read back to confirm it; check the result "
                                 "(observe/read), and pass ref of the field next time")
            return self._done("posted_chars")
        # Read-back proves nothing arrived, so a second method cannot double-type.
        if bg.is_edit_class(hwnd):
            try:
                self._guarded(lambda: bg.replace_selection(hwnd, text))
                landed = self._wait_landed(el, hwnd, before, text)
                self._attempt("replace_selection", landed is not False, _landed_detail(landed))
                if landed is not False:
                    return self._done("replace_selection")
            except bg.BackgroundUnsupported as e:
                self._attempt("replace_selection", False, str(e))
        self._no_background_left("the app did not accept posted characters")
        return self._real_type(el, text)

    def _real_type(self, el, text: str) -> Outcome:
        if el is not None:
            uia.focus(el)
            time.sleep(0.15)
        fg = self.p["real_input"]()
        inp.type_text(fg, text, abort=self.p["abort"])
        self._attempt("real_typing", True)
        return self._done("real_typing", background=False)

    def set_value(self, el, value: str) -> Outcome:
        try:
            self._guarded(lambda: uia.set_value(el, value))
            self._attempt("uia_value", True)
            return self._done("uia_value")
        except Exception as e:
            self._attempt("uia_value", False, type(e).__name__)
        native = getattr(el.info, "handle", None) or 0
        if native and bg.is_edit_class(native):
            self.p["background_target"](native, typing=True)
            self._guarded(lambda: bg.set_text(native, value))
            self._attempt("set_text_message", True)
            return self._done("set_text_message")
        raise NeedsRealInput("this control takes no value through UI Automation or edit messages; "
                             "focus it and use type instead")

    def hotkey(self, combo: str) -> Outcome:
        if self.method != "input":
            binding = getattr(self.scope, "binding", None)
            top = binding.hwnd if binding is not None else w.foreground()
            hwnd = self.focus_window(top)
            try:
                self.p["background_target"](hwnd)
                self.p["abort"]()
                self._guarded(lambda: bg.post_hotkey(hwnd, combo))
                self._attempt("posted_keys", True)
                self.out.note = "posted to the app's own thread; if nothing happened, retry with method='input'"
                return self._done("posted_keys")
            except bg.BackgroundUnsupported as e:
                self._attempt("posted_keys", False, str(e))
                self._no_background_left(str(e))
        fg = self.p["real_input"]()
        inp.hotkey(fg, combo)
        self._attempt("real_keys", True)
        return self._done("real_keys", background=False)

    def pattern(self, name: str, fn) -> Outcome:
        """invoke / toggle / select / focus through UI Automation, with the foreground guard."""
        result = self._guarded(fn)
        self._attempt(name, True)
        out = self._done(name)
        if result is not None:
            out.extra["result"] = result
        return out

    # -- read-back -------------------------------------------------------------------------------

    def _wait_landed(self, el, hwnd: int, before: str | None, text: str) -> bool | None:
        deadline = time.monotonic() + LANDED_WAIT_S
        while True:
            time.sleep(0.15)
            landed = _landed(before, self._read(el, hwnd), text)
            if landed is not False or time.monotonic() >= deadline:
                return landed

    @staticmethod
    def _read(el, hwnd: int) -> str | None:
        """Text of the target, for proving whether typing landed; None if it cannot be read."""
        try:
            if el is not None:
                got = uia.read(el)
            else:
                from pywinauto.uia_element_info import UIAElementInfo
                got = uia.read(uia.Element("", UIAElementInfo(hwnd), hwnd, 0))
            parts = [x for x in (got.get("text"), got.get("value")) if isinstance(x, str)]
            # An empty document reads as "", which is real read-back, not "unreadable".
            return "\n".join(parts) if parts else None
        except Exception:
            return None


def _landed(before: str | None, after: str | None, text: str) -> bool | None:
    """True if the text arrived, False if read-back proves it did not, None if it cannot be told."""
    if before is None or after is None:
        return None
    needle = text.replace("\r\n", "\n").replace("\n", "\r")[-40:] or text[-40:]
    if needle and after.count(needle) > before.count(needle):
        return True
    plain = text[-40:]
    if plain and after.count(plain) > before.count(plain):
        return True
    return False if after == before else None


def _landed_detail(landed: bool | None) -> str:
    return {True: "read back", False: "read-back shows nothing arrived", None: "could not read back"}[landed]
