"""Synthetic keyboard and mouse input via Win32 SendInput.

Text is typed as Unicode code units (KEYEVENTF_UNICODE), so no character is ever parsed as key
syntax. Every keyboard call first checks that the expected window is in the foreground, because
keystrokes go to whatever has focus.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import time
from collections.abc import Callable, Iterable

INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_VIRTUALDESK = 0x4000
MOUSEEVENTF_ABSOLUTE = 0x8000

SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79

VK_RETURN = 0x0D


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]


class InputError(RuntimeError):
    """SendInput injected fewer events than asked (blocked by UIPI or the secure desktop)."""


class FocusLost(RuntimeError):
    """The expected window is not in the foreground, so input was not sent."""


def foreground_window() -> int:
    return ctypes.windll.user32.GetForegroundWindow() or 0


def require_foreground(hwnd: int, get_foreground: Callable[[], int] = foreground_window) -> None:
    fg = get_foreground()
    if fg != hwnd:
        raise FocusLost(f"foreground window is {fg:#x}, expected {hwnd:#x}")


def utf16_units(text: str) -> list[int]:
    """UTF-16 code units for text; characters outside the BMP become surrogate pairs."""
    raw = text.encode("utf-16-le")
    return [int.from_bytes(raw[i:i + 2], "little") for i in range(0, len(raw), 2)]


def _key(vk: int = 0, scan: int = 0, flags: int = 0) -> INPUT:
    return INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, scan, flags, 0, 0))


def text_inputs(text: str) -> list[INPUT]:
    """Down/up event pairs that type text literally. Newlines (\\n, \\r\\n) become Enter."""
    events: list[INPUT] = []
    for ch in text.replace("\r\n", "\n"):
        if ch == "\n":
            events += [_key(VK_RETURN), _key(VK_RETURN, flags=KEYEVENTF_KEYUP)]
            continue
        for unit in utf16_units(ch):
            events += [_key(scan=unit, flags=KEYEVENTF_UNICODE),
                       _key(scan=unit, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP)]
    return events


def send(events: Iterable[INPUT]) -> int:
    events = list(events)
    if not events:
        return 0
    arr = (INPUT * len(events))(*events)
    sent = ctypes.windll.user32.SendInput(len(events), arr, ctypes.sizeof(INPUT))
    if sent != len(events):
        raise InputError(f"SendInput injected {sent} of {len(events)} events")
    return sent


def type_text(hwnd: int, text: str, per_char_delay: float = 0.015,
              get_foreground: Callable[[], int] = foreground_window) -> None:
    """Type text into hwnd, re-checking focus before every character."""
    for ch in text.replace("\r\n", "\n"):
        require_foreground(hwnd, get_foreground)
        send(text_inputs(ch))
        if per_char_delay:
            time.sleep(per_char_delay)


# Virtual-key codes for hotkeys. Letters and digits map to their ASCII uppercase code.
_NAMED_VK = {
    "ctrl": 0x11, "control": 0x11, "shift": 0x10, "alt": 0x12, "win": 0x5B,
    "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B, "space": 0x20,
    "backspace": 0x08, "delete": 0x2E, "del": 0x2E, "insert": 0x2D,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
}
# Keys that need KEYEVENTF_EXTENDEDKEY to be told apart from their numpad twins.
_EXTENDED = {0x2D, 0x2E, 0x24, 0x23, 0x21, 0x22, 0x25, 0x26, 0x27, 0x28, 0x5B}


def vk_for(name: str) -> int:
    key = name.strip().lower()
    if key in _NAMED_VK:
        return _NAMED_VK[key]
    if len(key) == 1 and (key.isascii() and key.isalnum()):
        return ord(key.upper())
    raise ValueError(f"unknown key {name!r}")


def hotkey_inputs(combo: str) -> list[INPUT]:
    """Events for a combo like "ctrl+s" or "ctrl+shift+esc": press in order, release in reverse."""
    vks = [vk_for(part) for part in combo.split("+") if part.strip()]
    if not vks:
        raise ValueError("empty hotkey")
    down = [_key(vk, flags=KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0) for vk in vks]
    up = [_key(vk, flags=KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0))
          for vk in reversed(vks)]
    return down + up


def hotkey(hwnd: int, combo: str, get_foreground: Callable[[], int] = foreground_window) -> None:
    require_foreground(hwnd, get_foreground)
    send(hotkey_inputs(combo))


def _virtual_screen() -> tuple[int, int, int, int]:
    m = ctypes.windll.user32.GetSystemMetrics
    return m(SM_XVIRTUALSCREEN), m(SM_YVIRTUALSCREEN), m(SM_CXVIRTUALSCREEN), m(SM_CYVIRTUALSCREEN)


def normalize_point(x: int, y: int, virtual: tuple[int, int, int, int]) -> tuple[int, int]:
    """Physical pixel -> SendInput absolute coordinates (0..65535 across the virtual desktop)."""
    vx, vy, vw, vh = virtual
    if not (vx <= x < vx + vw and vy <= y < vy + vh):
        raise ValueError(f"point ({x}, {y}) is outside the virtual desktop {virtual}")
    nx = ((x - vx) * 65535 + (vw - 1) // 2) // (vw - 1) if vw > 1 else 0
    ny = ((y - vy) * 65535 + (vh - 1) // 2) // (vh - 1) if vh > 1 else 0
    return nx, ny


_BUTTONS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click_inputs(x: int, y: int, button: str = "left", count: int = 1,
                 virtual: tuple[int, int, int, int] | None = None) -> list[INPUT]:
    if button not in _BUTTONS:
        raise ValueError(f"unknown button {button!r}")
    nx, ny = normalize_point(x, y, virtual or _virtual_screen())
    base = MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    down, up = _BUTTONS[button]
    move = INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(nx, ny, 0, base | MOUSEEVENTF_MOVE, 0, 0))
    presses = []
    for _ in range(count):
        presses += [INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(nx, ny, 0, base | down, 0, 0)),
                    INPUT(type=INPUT_MOUSE, mi=MOUSEINPUT(nx, ny, 0, base | up, 0, 0))]
    return [move, *presses]


def click(x: int, y: int, button: str = "left", count: int = 1) -> None:
    """Click at a physical-pixel point. The caller is responsible for scope and target checks."""
    send(click_inputs(x, y, button, count))
