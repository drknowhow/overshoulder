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


def require_not_password(action: str, is_password: bool) -> None:
    if is_password and action in TEXT_ACTIONS:
        raise Denied("target is a password field; agents never type into or read it")


_CONSEQUENTIAL = re.compile(
    r"\b(send|delete|remove|erase|discard|purchase|buy|pay|checkout|order|place order|install|uninstall|"
    r"format|submit|publish|post|transfer|wipe|reset|sign out|log out|empty trash|overwrite|replace all)\b",
    re.IGNORECASE)
_CONSEQUENTIAL_KEYS = frozenset({"delete", "shift+delete", "alt+f4", "ctrl+enter", "ctrl+shift+delete"})
_CLICK_ACTIONS = frozenset({"click", "invoke", "double_click", "toggle", "select"})


def approval_reason(action: str, target_name: str = "", params: dict | None = None) -> str | None:
    """Why this action needs a one-shot approval, or None. Generic signals only."""
    params = params or {}
    if action in _CLICK_ACTIONS and target_name and _CONSEQUENTIAL.search(target_name):
        return f"{action} on {target_name!r}"
    if action == "hotkey":
        combo = str(params.get("combo", "")).lower().replace(" ", "")
        if combo in _CONSEQUENTIAL_KEYS:
            return f"hotkey {combo}"
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
        raise ApprovalDenied(f"not approved: {request.get('reason')}")
