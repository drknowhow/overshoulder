"""Approval prompts: the box on the desk, and the person's phone when they set one up.

The box is a topmost Yes/No message box owned by the Overshoulder process, which the gateway never lets
an agent click or type into. A question about a box a vision model drew comes with a picture of it
(request["picture"]); then the box is a small window of this process that shows the picture above the
same Yes/No. With phone channels on (phone.py), every channel is asked at once: the first answer wins,
the other prompts are closed, and no answer before the deadline is "No".
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import gc
import io
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

BOX_TITLE = "Overshoulder: approve agent action?"

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
        if q.request.get("picture"):
            try:
                return self._ask_with_picture(q, stop)
            finally:
                gc.collect()  # Tk objects must be freed on the thread that made them, never on another
        tid = _kernel32.GetCurrentThreadId()
        shown = threading.Event()
        threading.Thread(target=self._close_on_stop, args=(stop, shown, tid), daemon=True).start()
        flags = MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL
        ms = max(1, int((q.expires_at - time.time()) * 1000))
        try:
            answer = _MessageBoxTimeoutW(None, _summary(q.request, self.also), BOX_TITLE, flags, 0, ms)
        finally:
            shown.set()
        if stop.is_set():
            return None  # closed because another channel answered
        return {IDYES: True, IDNO: False}.get(answer)  # anything else is the timeout

    def _ask_with_picture(self, q: phone.Question, stop: threading.Event) -> bool | None:
        """The same question in a window that shows request["picture"]. No is the default button, and
        closing the window or Escape is No. Runs a Tk loop on this channel's own thread."""
        import tkinter as tk

        from PIL import Image, ImageTk

        answer: dict = {}
        root = tk.Tk()
        root.title(BOX_TITLE)
        root.attributes("-topmost", True)
        root.resizable(False, False)

        def done(value: bool | None) -> None:
            answer.setdefault("value", value)
            root.quit()

        img = Image.open(io.BytesIO(q.request["picture"]))
        img.thumbnail((900, 600))
        photo = ImageTk.PhotoImage(img, master=root)
        tk.Label(root, text=_summary(q.request, self.also), justify="left", anchor="w",
                 padx=14, pady=10).pack(fill="x")
        tk.Label(root, image=photo, borderwidth=1, relief="solid").pack(padx=14)
        buttons = tk.Frame(root)
        buttons.pack(pady=12)
        tk.Button(buttons, text="Yes", width=12, command=lambda: done(True)).pack(side="left", padx=6)
        no = tk.Button(buttons, text="No", width=12, command=lambda: done(False), default="active")
        no.pack(side="left", padx=6)
        root.protocol("WM_DELETE_WINDOW", lambda: done(False))
        root.bind("<Escape>", lambda _e: done(False))

        def on_return(_e) -> None:  # Enter presses the focused button, which starts as No
            focused = root.focus_get()
            if isinstance(focused, tk.Button):
                focused.invoke()
            else:
                done(False)

        root.bind("<Return>", on_return)

        def watch() -> None:
            if stop.is_set() or time.time() >= q.expires_at:
                done(None)
            else:
                root.after(100, watch)

        root.after(100, watch)
        root.lift()
        root.focus_force()
        no.focus_set()
        try:
            root.mainloop()
        finally:
            root.destroy()
            del photo
        if stop.is_set():
            return None  # closed because another channel answered
        return answer.get("value")

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
    chain.approve({"kind": "test", "reason": "a test question from the settings page"})
    return chain.last
