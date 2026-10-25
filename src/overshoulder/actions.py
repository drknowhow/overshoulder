"""The method ladder: do each action without the person's input when the app allows it.

Rungs, per action (docs/SPIKE-phase2.md has the measurements behind them):
  press a control   BM_CLICK for a Win32 button -> UIA Invoke/Toggle/Select -> posted click -> real click
  invoke            BM_CLICK for a Win32 button -> UIA Invoke -> Toggle -> Select (Paint's tool buttons
                    only toggle); never the mouse, so a control with none of them is refused
  select            UIA SelectionItem -> Invoke (Paint's colour swatches are list items that only invoke)
  click a point     posted click (one retry for Chromium, which can swallow the first) -> real click
  type              EM_REPLACESEL for edit controls (one message, so nothing arrives out of order) ->
                    WM_CHAR posted to the scope's input window -> real typing
                    (Chromium/Electron windows in the background go straight to real typing)
  set a value       WM_SETTEXT for a Win32 edit -> UIA ValuePattern -> UIA RangeValue for a number
                    (sliders and spinners)
A control without the UI Automation pattern an action needs is refused with pattern_unsupported,
naming the action to use instead.
  (UI Automation on Win32 controls was seen to bring the app forward for a moment, as on Character Map,
  so a native control gets its own window message first.)
  hotkey            keys posted with modifiers in the target thread's key state -> (real, only if asked)
  drag              button down, moves and up posted to the window under the start, with the left button
                    set in the target thread's key state -> real drag (needed for OLE drag and drop,
                    list and tree views, and drags from one window to another)

The method argument picks how far down the ladder an action may go:
  "background"  never the person's mouse or keyboard; fail instead
  "auto"        background first, real input only when a rung reports it cannot reach the window or
                read-back proves the background attempt did nothing
  "input"       real input straight away
Real input is never used to retry something that may already have happened (a hotkey, or a click
whose effect is just not visible), so nothing is done twice.

An app's profile may change the order of the background rungs (order=, from hints.order): a method that
keeps failing in that app is tried after the others. It never adds, removes or skips a rung, and real
input stays last.

Every rung asks the gateway first: p["background_target"](hwnd) before messaging a window, and
p["borrow"]() around any use of the person's mouse or keyboard. A borrow follows their input mode: it
waits until they are idle, brings the window forward, and afterwards puts their cursor and foreground
back. A ForegroundGuard wraps each background attempt and puts the person's foreground window back if
the app grabs it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from overshoulder.driver import background as bg
from overshoulder.driver import capture, uia
from overshoulder.driver import input as inp
from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect
from overshoulder.gateway.errors import NeedsRealInput, PatternUnsupported, TypingMismatch

METHODS = ("auto", "background", "input")
PRESSABLE = {"button", "menuitem", "hyperlink", "splitbutton", "checkbox", "radiobutton", "tabitem", "listitem",
             "treeitem"}
REGION = 90  # half-size of the area checked for a visible change around a posted click
# Posted input is processed asynchronously (Chromium hands it to a renderer process), so "nothing
# arrived" is only concluded after read-back stays unchanged this long. Concluding it early would
# make "auto" type the text a second time with real keys.
LANDED_WAIT_S = 2.0
MISMATCH = "mismatch"  # read-back: the field changed, but does not hold the typed text
PATTERNS = {"uia_invoke": "invoke", "uia_toggle": "toggle", "uia_select": "select"}  # rung -> uia function
# What to use instead when a control lacks the patterns an action tries.
INSTEAD = {"invoke": "use click, which can also click its centre", "select": "use click",
           "toggle": "use click or invoke", "focus": "use click"}
PASS_THROUGH = ("lease_revoked", "paused", "needs_real_input")  # refusals a failed rung must not swallow


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
    def __init__(self, scope, p: dict, method: str = "auto", order=None):
        if method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        self.scope = scope
        self.p = p
        self.method = method
        self.order = order  # [rung names] -> the same names, maybe reordered; None keeps the ladder's order
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

    def _borrowed(self, b, name: str) -> None:
        self._attempt(name, True)
        self.out.extra["borrow"] = b.report

    def _ordered(self, rungs: list[str]) -> list[str]:
        """The rungs in the order the profile suggests, or as given if the suggestion is not a reordering."""
        if self.order is None or len(rungs) < 2:
            return rungs
        try:
            got = list(self.order(list(rungs)))
        except Exception:  # a hint never fails an action
            return rungs
        return got if sorted(got) == sorted(rungs) else rungs

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

    @staticmethod
    def _native(el) -> int:
        return (getattr(getattr(el, "info", None), "handle", None) or 0) if el is not None else 0

    def _button_message(self, el) -> Outcome | None:
        """BM_CLICK to a Win32 button: a background press that leaves the foreground alone."""
        native = self._native(el)
        if self.method == "input" or not native or w.class_name(native) != "Button":
            return None
        try:
            self.p["background_target"](native)
            self.p["abort"]()
            self._guarded(lambda: bg.click_button(native))
        except bg.BackgroundUnsupported as e:
            self._attempt("button_message", False, str(e))
            return None
        self._attempt("button_message", True)
        return self._done("button_message")

    def _rungs(self, el, rungs: list[str]) -> Outcome | None:
        """Try a Win32 button message and UIA patterns in turn; None if the control took none of them."""
        for name in self._ordered(rungs):
            if name == "button_message":
                if (done := self._button_message(el)) is not None:
                    return done
                continue
            fn = getattr(uia, PATTERNS[name])  # looked up per call, so a patched uia function is used
            try:
                self.p["abort"]()
                self._guarded(lambda fn=fn: fn(el))
                self._attempt(name, True)
                return self._done(name)
            except Exception as e:  # pattern not supported: next rung
                if getattr(e, "code", None) in PASS_THROUGH:
                    raise
                self._attempt(name, False, type(e).__name__)
        return None

    def invoke(self, el) -> Outcome:
        done = self._rungs(el, ["button_message", "uia_invoke", "uia_toggle", "uia_select"])
        if done is None:
            raise PatternUnsupported(f"this control takes none of Invoke, Toggle or Select through UI "
                                     f"Automation; {INSTEAD['invoke']}")
        return done

    def select(self, el) -> Outcome:
        done = self._rungs(el, ["uia_select", "uia_invoke"])
        if done is None:
            raise PatternUnsupported(f"this control takes neither SelectionItem nor Invoke through UI "
                                     f"Automation; {INSTEAD['select']}")
        return done

    def press(self, el, target, button: str = "left", count: int = 1) -> Outcome:
        """click / double_click / right_click."""
        kind = (target.control_type or "").lower()
        rungs = []
        if button == "left" and count == 1 and self.method != "input":
            native = self._native(el)
            if native and w.class_name(native) == "Button":
                rungs.append("button_message")
            if el is not None and kind in PRESSABLE:
                rungs += ["uia_invoke", "uia_toggle", "uia_select"]
        if rungs and (done := self._rungs(el, rungs)) is not None:
            return done
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
        with self.p["borrow"]() as b:
            b.check()
            inp.click(x, y, button=button, count=count)
        self._borrowed(b, "real_click")
        return self._done("real_click", background=False)

    def drag(self, src, dst) -> Outcome:
        """Press at src, move to dst, let go there. Both are bound targets the gateway checked."""
        x0, y0 = src.point()
        x1, y1 = dst.point()
        if self.method != "input":
            top = self.top_at(x0, y0)
            if self.top_at(x1, y1) != top:
                why = "the drop point is in another window"
                self._attempt("posted_drag", False, why)
                self._no_background_left(why)
            else:
                try:
                    self.p["background_target"](bg.deepest_child_at(top, x0, y0))
                    self.p["background_target"](bg.deepest_child_at(top, x1, y1))
                    region = Rect(min(x0, x1) - REGION, min(y0, y1) - REGION, max(x0, x1) + REGION,
                                  max(y0, y1) + REGION)
                    self.p["abort"]()
                    before = capture.window_image(top, region)
                    self._guarded(lambda: bg.post_drag(top, x0, y0, x1, y1, abort=self.p["abort"]))
                    time.sleep(0.25)
                    after = capture.window_image(top, region)
                    seen = before is not None and after is not None and \
                        capture.changed_fraction(before, after) > 0.002
                    self._attempt("posted_drag", True, "visible change" if seen else "no visible change")
                    if not seen:
                        self.out.note = ("no visible change; if nothing moved, the app may only take drags from "
                                         "the real mouse: retry with method='input'")
                    return self._done("posted_drag")
                except bg.BackgroundUnsupported as e:
                    self._attempt("posted_drag", False, str(e))
                    self._no_background_left(str(e))
        with self.p["borrow"]() as b:
            b.check()
            inp.drag(x0, y0, x1, y1, abort=b.abort)
        self._borrowed(b, "real_drag")
        return self._done("real_drag", background=False)

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
        # One message (replace_selection) inserts the whole text at the caret, so no character can arrive
        # out of order (posted characters were seen to swap in Windows 11 Notepad: "{x}" read back as
        # "x{}"). Line breaks go as posted characters instead, where Enter keeps its meaning.
        one_message = bg.is_edit_class(hwnd) and "\n" not in text and "\r" not in text
        sends = {"replace_selection": lambda: bg.replace_selection(hwnd, text),
                 "posted_chars": lambda: bg.post_text(hwnd, text, abort=self.p["abort"])}
        why = "the app did not accept posted characters"
        for name in self._ordered(["replace_selection", "posted_chars"] if one_message else ["posted_chars"]):
            try:
                self._guarded(sends[name])
            except bg.BackgroundUnsupported as e:
                self._attempt(name, False, str(e))
                why = str(e)
                continue
            landed = self._wait_landed(el, hwnd, before, text)
            self._attempt(name, landed is not False, _landed_detail(landed))
            if landed is not False:
                return self._typed(name, landed)
            # Read-back has proved nothing arrived, so the next method cannot type the text twice.
        self._no_background_left(why)
        return self._real_type(el, text)

    def _typed(self, name: str, landed) -> Outcome:
        if landed == MISMATCH:
            raise TypingMismatch("the field changed but does not read back as the typed text (characters may "
                                 "have been lost or reordered); read it and correct it before going on")
        if landed is None:
            self.out.note = ("typed, but the text could not be read back to confirm it; check the result "
                             "(observe/read), and pass ref of the field next time")
        return self._done(name)

    def _real_type(self, el, text: str) -> Outcome:
        # The borrow opens before focusing: focusing can bring the window forward, so the mode must allow
        # it and the person's own foreground must be saved first.
        with self.p["borrow"]() as b:
            if el is not None:
                uia.focus(el)
                time.sleep(0.15)
            fg = b.check()
            inp.type_text(fg, text, abort=b.abort)
        self._borrowed(b, "real_typing")
        return self._done("real_typing", background=False)

    def set_value(self, el, value: str) -> Outcome:
        native = self._native(el)
        edit = bool(native) and bg.is_edit_class(native)
        rungs = ["set_text_message", "uia_value"] if edit else ["uia_value", "uia_range_value"]
        for name in self._ordered(rungs):
            if name == "set_text_message":
                self.p["background_target"](native, typing=True)
                try:
                    self._guarded(lambda: bg.set_text(native, value))
                    self._attempt(name, True)
                    return self._done(name)
                except bg.BackgroundUnsupported as e:
                    self._attempt(name, False, str(e))
                continue
            setter, arg = uia.set_value, value
            if name == "uia_range_value":  # a slider or spinner takes a number
                try:
                    setter, arg = uia.set_range_value, float(value)
                except ValueError:
                    self._attempt(name, False, "not a number")
                    continue
            try:
                self._guarded(lambda f=setter, a=arg: f(el, a))
                self._attempt(name, True)
                return self._done(name)
            except ValueError:
                raise  # outside the control's range: the message names the range
            except Exception as e:
                if getattr(e, "code", None) in PASS_THROUGH:
                    raise
                self._attempt(name, False, type(e).__name__)
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
        with self.p["borrow"]() as b:
            fg = b.check()
            inp.hotkey(fg, combo)
        self._borrowed(b, "real_keys")
        return self._done("real_keys", background=False)

    def pattern(self, name: str, fn) -> Outcome:
        """toggle / focus through UI Automation, with the foreground guard. A control without the
        pattern is refused with pattern_unsupported."""
        try:
            result = self._guarded(fn)
        except Exception as e:
            if getattr(e, "code", None) is not None or type(e).__name__ != "NoPatternInterfaceError":
                raise
            self._attempt(name, False, type(e).__name__)
            action = name.removeprefix("uia_")
            raise PatternUnsupported(f"this control has no {action.capitalize()} pattern in UI Automation; "
                                     f"{INSTEAD.get(action, 'use click')}") from e
        self._attempt(name, True)
        out = self._done(name)
        if result is not None:
            out.extra["result"] = result
        return out

    # -- read-back -------------------------------------------------------------------------------

    def _wait_landed(self, el, hwnd: int, before: str | None, text: str):
        """Poll read-back until the text is there or cannot be read. Nothing yet, or only part of it
        (MISMATCH), is concluded only at the deadline: slow apps take characters in over time."""
        deadline = time.monotonic() + LANDED_WAIT_S
        while True:
            time.sleep(0.15)
            landed = _landed(before, self._read(el, hwnd), text)
            if landed is True or landed is None or time.monotonic() >= deadline:
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


def _landed(before: str | None, after: str | None, text: str):
    """True if the text arrived; False if read-back proves nothing changed; MISMATCH if the field changed
    but does not hold the text; None if it cannot be read."""
    if before is None or after is None:
        return None
    needle = text.replace("\r\n", "\n").replace("\n", "\r")[-40:] or text[-40:]
    if needle and after.count(needle) > before.count(needle):
        return True
    plain = text[-40:]
    if plain and after.count(plain) > before.count(plain):
        return True
    return False if after == before else MISMATCH


def _landed_detail(landed) -> str:
    return {True: "read back", False: "read-back shows nothing arrived", None: "could not read back",
            MISMATCH: "read-back differs from the typed text"}[landed]
