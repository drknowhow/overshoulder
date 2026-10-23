"""Hard denies and approval rules. Generic: nothing here knows about a particular application.

Hard denies fail closed: when a check cannot be performed (access denied, API error), the answer is
"deny", never "probably fine".
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import re
from collections.abc import Callable
from typing import Protocol

from overshoulder.gateway.errors import ApprovalDenied, Denied

kernel32 = ctypes.windll.kernel32
user32 = ctypes.windll.user32
advapi32 = ctypes.windll.advapi32

DESKTOP_READOBJECTS = 0x0001
UOI_NAME = 2
TOKEN_QUERY = 0x0008
TOKEN_ELEVATION = 20
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

user32.OpenInputDesktop.restype = wt.HANDLE
user32.OpenInputDesktop.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
user32.CloseDesktop.argtypes = [wt.HANDLE]
user32.GetUserObjectInformationW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                             ctypes.POINTER(wt.DWORD)]
kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.GetCurrentProcess.restype = wt.HANDLE
kernel32.CloseHandle.argtypes = [wt.HANDLE]
advapi32.OpenProcessToken.argtypes = [wt.HANDLE, wt.DWORD, ctypes.POINTER(wt.HANDLE)]
advapi32.GetTokenInformation.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD,
                                         ctypes.POINTER(wt.DWORD)]


def input_desktop_name() -> str | None:
    """Name of the desktop receiving input ("Default" normally; "Winlogon" for UAC / lock)."""
    h = user32.OpenInputDesktop(0, False, DESKTOP_READOBJECTS)
    if not h:
        return None  # cannot open it: the secure desktop is up
    try:
        buf = ctypes.create_unicode_buffer(256)
        needed = wt.DWORD()
        if not user32.GetUserObjectInformationW(h, UOI_NAME, buf, ctypes.sizeof(buf), ctypes.byref(needed)):
            return None
        return buf.value
    finally:
        user32.CloseDesktop(h)


def require_default_desktop(name_fn: Callable[[], str | None] = input_desktop_name) -> None:
    name = name_fn()
    if name != "Default":
        raise Denied(f"input desktop is {name or 'secure/unavailable'}; agents never act on it")


def _token_elevated(process_handle) -> bool | None:
    tok = wt.HANDLE()
    if not advapi32.OpenProcessToken(process_handle, TOKEN_QUERY, ctypes.byref(tok)):
        return None
    try:
        val, size = wt.DWORD(), wt.DWORD()
        if not advapi32.GetTokenInformation(tok, TOKEN_ELEVATION, ctypes.byref(val), ctypes.sizeof(val),
                                            ctypes.byref(size)):
            return None
        return bool(val.value)
    finally:
        kernel32.CloseHandle(tok)


def process_elevated(pid: int) -> bool | None:
    """True/False, or None when the process cannot be inspected."""
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        return _token_elevated(h)
    finally:
        kernel32.CloseHandle(h)


def self_elevated() -> bool:
    return bool(_token_elevated(kernel32.GetCurrentProcess()))


def require_not_elevated(pid: int, check: Callable[[int], bool | None] = process_elevated) -> None:
    state = check(pid)
    if state is None:
        raise Denied(f"cannot inspect process {pid}; treating it as elevated")
    if state:
        raise Denied(f"process {pid} runs elevated; agents never drive elevated windows")


# Actions that write text or read a field's value are refused on password fields.
TEXT_ACTIONS = frozenset({"type", "set_value", "read_value"})


def require_not_password(action: str, is_password: bool | None) -> None:
    """is_password None means it could not be determined, which counts as a password field."""
    if action in TEXT_ACTIONS and is_password is not False:
        raise Denied("target is a password field (or could not be checked); agents never type into "
                     "or read it")


_CONSEQUENTIAL = re.compile(
    r"\b(send|delete|remove|erase|discard|purchase|buy|pay|checkout|order|place order|install|uninstall|"
    r"format|submit|publish|post|transfer|wipe|reset|sign out|log out|empty trash|overwrite|replace all)\b",
    re.IGNORECASE)
_VK_CTRL, _VK_SHIFT, _VK_ALT, _VK_WIN, _VK_RWIN = 0x11, 0x10, 0x12, 0x5B, 0x5C
_VK_TAB, _VK_ESC, _VK_DEL, _VK_ENTER, _VK_F4 = 0x09, 0x1B, 0x2E, 0x0D, 0x73

# Combos that act on the shell rather than the focused window, so they would leave the scope.
_SCOPE_ESCAPES = [
    frozenset({_VK_ALT, _VK_TAB}), frozenset({_VK_ALT, _VK_ESC}), frozenset({_VK_CTRL, _VK_ESC}),
    frozenset({_VK_CTRL, _VK_SHIFT, _VK_ESC}), frozenset({_VK_CTRL, _VK_ALT, _VK_DEL}),
]
_CONSEQUENTIAL_KEYS = [
    frozenset({_VK_DEL}), frozenset({_VK_SHIFT, _VK_DEL}), frozenset({_VK_CTRL, _VK_SHIFT, _VK_DEL}),
    frozenset({_VK_ALT, _VK_F4}), frozenset({_VK_CTRL, _VK_ENTER}),
]
_CLICK_ACTIONS = frozenset({"click", "invoke", "double_click", "right_click", "toggle", "select"})
# Drop targets that throw away what is dropped on them, on top of the consequential names above. A bare
# "bin" is left out: build folders are called that.
_DISCARD_DROP = re.compile(r"\b(recycle bin|trash|wastebasket|deleted items)\b", re.IGNORECASE)
_SINGLE_LINE = frozenset({"edit", "combobox", "spinner"})


def combo_keys(combo: str) -> frozenset[int]:
    from overshoulder.driver.input import vk_for
    return frozenset(vk_for(p) for p in combo.split("+") if p.strip())


def require_hotkey_allowed(combo: str) -> None:
    keys = combo_keys(combo)
    if keys & {_VK_WIN, _VK_RWIN}:
        raise Denied(f"hotkey {combo!r} uses the Windows key, which acts outside any scope")
    if any(keys == esc for esc in _SCOPE_ESCAPES):
        raise Denied(f"hotkey {combo!r} switches or leaves the active window")


def approval_reason(action: str, target_name: str = "", params: dict | None = None,
                    focused_type: str = "") -> str | None:
    """Why this action needs a one-shot approval, or None. Generic signals only.

    target_name is the element's name (for point targets, the element under the point when UIA can
    tell; canvas apps expose none, so their point clicks are not name-checked). For a drag it is the
    drop target's.
    """
    params = params or {}
    if action in _CLICK_ACTIONS and target_name and _CONSEQUENTIAL.search(target_name):
        return f"{action} on {target_name!r}"
    if action == "drag" and target_name and (_CONSEQUENTIAL.search(target_name)
                                             or _DISCARD_DROP.search(target_name)):
        return f"drop onto {target_name!r}"
    if action == "hotkey":
        combo = str(params.get("combo", ""))
        keys = combo_keys(combo)
        if keys in _CONSEQUENTIAL_KEYS:
            return f"hotkey {combo}"
        if keys == frozenset({_VK_ENTER}) and focused_type.lower() in _SINGLE_LINE:
            return "Enter in a single-line field may submit a form"
    if action == "type" and "\n" in str(params.get("text", "")) and focused_type.lower() in _SINGLE_LINE:
        return "Enter in a single-line field may submit a form"
    return None


class Approver(Protocol):
    def approve(self, request: dict) -> bool: ...


class DenyAll:
    """Default approver: nothing consequential happens until a real approver is configured."""

    def approve(self, request: dict) -> bool:
        return False


class CallbackApprover:
    def __init__(self, fn: Callable[[dict], bool]):
        self.fn = fn

    def approve(self, request: dict) -> bool:
        return bool(self.fn(request))


def require_approval(approver: Approver, request: dict) -> None:
    if not approver.approve(request):
        by = request.get("answered_by")
        why = "" if "answered_by" not in request else f"denied on {by}: " if by else "no answer in time: "
        raise ApprovalDenied(f"not approved: {why}{request.get('reason')}", by)
