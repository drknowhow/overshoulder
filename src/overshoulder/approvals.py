"""Approval prompts: the box on the desk, and the person's phone when they set one up.

The box is a topmost Yes/No message box owned by the Overshoulder process, which the gateway never lets
an agent click or type into. With phone channels on (phone.py), every channel is asked at once: the
first answer wins, the other prompts are closed, and no answer before the deadline is "No".
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import logging
import queue
import threading
import time
from collections.abc import Callable

from overshoulder import phone

log = logging.getLogger(__name__)

MB_YESNO = 0x04
MB_ICONWARNING = 0x30
MB_DEFBUTTON2 = 0x100
MB_SYSTEMMODAL = 0x1000
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDYES = 6
IDNO = 7
WM_COMMAND = 0x0111

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_MessageBoxTimeoutW = _user32.MessageBoxTimeoutW
_MessageBoxTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                                ctypes.c_ushort, ctypes.c_uint]
_MessageBoxTimeoutW.restype = ctypes.c_int
_EnumThreadWndProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
_user32.EnumThreadWindows.argtypes = [wt.DWORD, _EnumThreadWndProc, wt.LPARAM]
_user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]


def _summary(request: dict, also: str = "") -> str:
    lines = [f"{request.get('client', 'An agent')} asks to:", "", f"    {request.get('reason', '?')}", ""]
    if request.get("target"):
        t = request["target"]
        lines.append(f"Target: {t.get('name') or t.get('kind')} {t.get('control_type', '')}".rstrip())
    if request.get("process"):
        lines.append(f"Program: {request['process']}")
    if request.get("scope"):
        lines.append(f"Scope: {json.dumps(request['scope'])[:200]}")
    if also:
        lines += ["", f"Also asked on {also}; the first answer counts."]
    lines += ["", "Allow this once?"]
    return "\n".join(lines)


def _thread_windows(tid: int) -> list[int]:
    found: list[int] = []

    @_EnumThreadWndProc
    def cb(h, _):
        found.append(h)
        return True

    _user32.EnumThreadWindows(tid, cb, 0)
    return found


class DesktopBox:
    """The Yes/No box on the desk, which closes itself when another channel answers first."""

    name = "desktop"

    def __init__(self, also: str = ""):
        self.also = also
        self.last_error = ""

    def ask(self, q: phone.Question, stop: threading.Event) -> bool | None:
        tid = _kernel32.GetCurrentThreadId()
        shown = threading.Event()
        threading.Thread(target=self._close_on_stop, args=(stop, shown, tid), daemon=True).start()
        flags = MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL
        ms = max(1, int((q.expires_at - time.time()) * 1000))
        try:
            answer = _MessageBoxTimeoutW(None, _summary(q.request, self.also),
                                         "Overshoulder: approve agent action?", flags, 0, ms)
        finally:
            shown.set()
        if stop.is_set():
            return None  # closed because another channel answered
        return {IDYES: True, IDNO: False}.get(answer)  # anything else is the timeout

    @staticmethod
    def _close_on_stop(stop: threading.Event, done: threading.Event, tid: int) -> None:
        while not done.is_set():
            if stop.wait(0.1):
                for h in _thread_windows(tid):
                    _user32.PostMessageW(h, WM_COMMAND, IDNO, 0)
                done.wait(0.2)


class ApproverChain:
    """Asks every channel at once. The first answer wins and the others are told to stop; no answer by
    the deadline is No. One question at a time. `last` says who answered the latest question."""

    def __init__(self, channels: Callable[[], list], timeout_s: Callable[[], float] | float = 60,
                 question: Callable[[dict, float], phone.Question] | None = None):
        self._channels = channels
        self._timeout = timeout_s if callable(timeout_s) else (lambda: timeout_s)
        self._question = question or (lambda req, t: phone.make_question(req, phone.PhoneSettings(), t))
        self._lock = threading.Lock()
        self.last: dict = {}

    def approve(self, request: dict) -> bool:
        with self._lock:
            return self._ask(request)

    def _ask(self, request: dict) -> bool:
        channels = list(self._channels())
        q = self._question(request, float(self._timeout()))
        stop, got = threading.Event(), queue.Queue()

        def run(ch):
            try:
                a = ch.ask(q, stop)
            except Exception as e:  # a broken channel must not decide anything
                log.warning("approval channel %s failed: %s", getattr(ch, "name", ch), e)
                ch_error = f"{type(e).__name__}: {e}"
                a = None
            else:
                ch_error = getattr(ch, "last_error", "")
            got.put((ch.name, a, ch_error))

        threads = [threading.Thread(target=run, args=(ch,), daemon=True) for ch in channels]
        t0 = time.time()
        for t in threads:
            t.start()
        answer, by, errors, pending = None, None, {}, len(threads)
        while pending and answer is None:
            try:
                name, a, err = got.get(timeout=max(0.0, q.expires_at - time.time()))
            except queue.Empty:
                break
            pending -= 1
            if err:
                errors[name] = err
            if a is not None:
                answer, by = a, name
        stop.set()
        for t in threads:
            t.join(timeout=5)
        self.last = {"id": q.id, "answer": answer, "by": by, "seconds": round(time.time() - t0, 1),
                     "errors": errors}
        request["answered_by"] = by
        return bool(answer)


class MessageBoxApprover(ApproverChain):
    """The box on the desk alone."""

    def __init__(self, timeout_s: int = 60):
        super().__init__(lambda: [DesktopBox()], timeout_s)


class SettingsApprover(ApproverChain):
    """The box on the desk plus whatever phone channels the person has set up, read at every question
    so a change on the settings page applies to the next one."""

    def __init__(self, load: Callable[[], phone.PhoneSettings] = phone.load):
        self._load = load
        self._settings = phone.PhoneSettings()
        super().__init__(self._make_channels, lambda: self._settings.timeout_s,
                         lambda req, t: phone.make_question(req, self._settings, t))

    def approve(self, request: dict) -> bool:
        with self._lock:
            self._settings = self._load()
            return self._ask(request)

    def _make_channels(self) -> list:
        extra = phone.channels_for(self._settings)
        also = " and ".join({"ntfy": "your phone (ntfy)", "webhook": "your webhook"}[c.name] for c in extra)
        return [DesktopBox(also=also), *extra]


def try_phone(s: phone.PhoneSettings, timeout_s: float = 60) -> dict:
    """Ask the phone channels alone a test question; returns who answered what, or the errors."""
    chain = ApproverChain(lambda: phone.channels_for(s), timeout_s,
                          lambda req, t: phone.make_question(req, phone.PhoneSettings(), t))
    if not phone.channels_for(s):
        return {"answer": None, "by": None, "errors": {"phone": "no phone channel is turned on"}}
    chain.approve({"kind": "test", "reason": "Test from the Overshoulder settings page: tap Allow or Deny"})
    return chain.last
