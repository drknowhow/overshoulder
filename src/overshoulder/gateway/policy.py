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
    r"format|submit|publish|post|transfer|wipe|reset|sign out|log out|empty trash|overwrite|replace all|save|"
    r"replace)\b",
    re.IGNORECASE)
# Buttons that say yes to whatever their dialog asks; what they confirm is in the dialog's text.
_AFFIRMATIVE = re.compile(
    r"^\W*(ok|okay|yes|yes to all|continue|confirm|allow|accept|agree|i agree|proceed|apply|finish|run|"
    r"go|do it)\W*$", re.IGNORECASE)
_VK_CTRL, _VK_SHIFT, _VK_ALT, _VK_WIN, _VK_RWIN = 0x11, 0x10, 0x12, 0x5B, 0x5C
_VK_TAB, _VK_ESC, _VK_DEL, _VK_ENTER, _VK_F4 = 0x09, 0x1B, 0x2E, 0x0D, 0x73
_VK_Q, _VK_S, _VK_W = 0x51, 0x53, 0x57

# Combos that act on the shell rather than the focused window, so they would leave the scope.
_SCOPE_ESCAPES = [
    frozenset({_VK_ALT, _VK_TAB}), frozenset({_VK_ALT, _VK_ESC}), frozenset({_VK_CTRL, _VK_ESC}),
    frozenset({_VK_CTRL, _VK_SHIFT, _VK_ESC}), frozenset({_VK_CTRL, _VK_ALT, _VK_DEL}),
]
_CONSEQUENTIAL_KEYS = [
    frozenset({_VK_DEL}), frozenset({_VK_SHIFT, _VK_DEL}), frozenset({_VK_CTRL, _VK_SHIFT, _VK_DEL}),
    frozenset({_VK_ALT, _VK_F4}), frozenset({_VK_CTRL, _VK_ENTER}),
    # save (over a file), close a tab or window, quit the app
    frozenset({_VK_CTRL, _VK_S}), frozenset({_VK_CTRL, _VK_W}), frozenset({_VK_CTRL, _VK_SHIFT, _VK_W}),
    frozenset({_VK_CTRL, _VK_Q}), frozenset({_VK_CTRL, _VK_F4}),
]
_VK_INSERT, _VK_C, _VK_V, _VK_X = 0x2D, 0x43, 0x56, 0x58
# Clipboard combos: in a password field they paste text in or copy it out.
_CLIPBOARD_KEYS = [frozenset({_VK_CTRL, _VK_V}), frozenset({_VK_SHIFT, _VK_INSERT}), frozenset({_VK_CTRL, _VK_C}),
                   frozenset({_VK_CTRL, _VK_X}), frozenset({_VK_CTRL, _VK_INSERT}), frozenset({_VK_SHIFT, _VK_DEL})]
_PASTE_KEYS = [frozenset({_VK_CTRL, _VK_V}), frozenset({_VK_SHIFT, _VK_INSERT})]
_CLICK_ACTIONS = frozenset({"click", "invoke", "double_click", "right_click", "toggle", "select"})
# Actions aimed at a point whose on-screen text is checked when UI Automation names nothing there.
POINTED_ACTIONS = frozenset({"click", "double_click", "right_click", "drag"})
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


def _printable_vk(vk: int) -> bool:
    return vk == 0x20 or 0x30 <= vk <= 0x39 or 0x41 <= vk <= 0x5A or 0x60 <= vk <= 0x6F \
        or 0xBA <= vk <= 0xC0 or 0xDB <= vk <= 0xDF or vk == 0xE2


def moves_text(keys: frozenset[int]) -> bool:
    """Whether a combo puts text into the focused field or copies it out: a clipboard combo, or a key that
    types a character (alone, with Shift, with AltGr as Ctrl+Alt, or a number-pad digit with Alt)."""
    if keys in _CLIPBOARD_KEYS:
        return True
    mods = keys & {_VK_CTRL, _VK_SHIFT, _VK_ALT}
    rest = keys - mods - {_VK_WIN, _VK_RWIN}
    if len(rest) != 1 or not _printable_vk(next(iter(rest))):
        return False
    vk = next(iter(rest))
    if _VK_CTRL in mods and _VK_ALT not in mods:
        return False  # Ctrl+letter is a shortcut
    if _VK_ALT in mods and _VK_CTRL not in mods:
        return 0x60 <= vk <= 0x69  # Alt+letter opens a menu; Alt+number-pad digit enters a character
    return True


def require_hotkey_not_into_password(combo: str, is_password: bool | None) -> None:
    """is_password None (could not be checked) counts as a password field, as for typing."""
    if is_password is not False and moves_text(combo_keys(combo)):
        raise Denied(f"hotkey {combo!r} would put text into or copy it out of a password field (or one that "
                     "could not be checked); agents never do that")


def require_no_tab_in_text(action: str, params: dict) -> None:
    """A tab inside typed text moves focus mid-string, possibly into a password field the type check never
    saw, so it has to be its own hotkey."""
    if action == "type" and "\t" in str(params.get("text", "")):
        raise Denied("text to type may not hold a tab; type each field on its own, with hotkey('tab') between")


_VK_APPS, _VK_F10 = 0x5D, 0x79


def opens_context_menu(combo: str) -> bool:
    """Shift+F10 or the menu key: the keyboard's right click."""
    keys = combo_keys(combo)
    return keys in (frozenset({_VK_SHIFT, _VK_F10}), frozenset({_VK_APPS}), frozenset({_VK_SHIFT, _VK_APPS}))


def is_affirmative(name: str) -> bool:
    return bool(name) and bool(_AFFIRMATIVE.match(name.replace("&", "")))


def dialog_reason(action: str, button: str, dialog_text: str | None) -> str | None:
    """Why pressing a dialog's OK or Yes needs approval: the dialog's own text names something
    consequential ("Delete 3 files?", "Replace the existing file?"). dialog_text None means the window
    is a dialog whose text could not be read, which is asked about too."""
    if action not in _CLICK_ACTIONS or not is_affirmative(button):
        return None
    if dialog_text is None:
        return f"{action} on {button!r} in a dialog whose text could not be read"
    if _CONSEQUENTIAL.search(dialog_text):
        shown = " ".join(dialog_text.split())
        return f"{action} on {button!r} in a dialog that says {shown[:200]!r}"
    return None


def approval_reason(action: str, target_name: str = "", params: dict | None = None,
                    focused_type: str = "") -> str | None:
    """Why this action needs a one-shot approval, or None. Generic signals only.

    target_name is the element's name. For a point target it is the element under the point when UIA
    can tell; when that names nothing consequential, the gateway asks again with the text on screen at
    the point (Checks.label_at), since canvas apps expose no names. For a drag it is the drop target's.
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
        if keys in _PASTE_KEYS and focused_type.lower() in _SINGLE_LINE:
            return "a paste into a single-line field may hold a line break and submit a form"
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
