"""Approval prompts shown to the person at the desk.

The default is a topmost Yes/No message box owned by the Overshoulder process, which the gateway
never lets an agent click or type into. Unanswered prompts time out as "No".
"""

from __future__ import annotations

import ctypes
import json
import threading

MB_YESNO = 0x04
MB_ICONWARNING = 0x30
MB_DEFBUTTON2 = 0x100
MB_SYSTEMMODAL = 0x1000
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDYES = 6

_user32 = ctypes.windll.user32
_MessageBoxTimeoutW = _user32.MessageBoxTimeoutW
_MessageBoxTimeoutW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint,
                                ctypes.c_ushort, ctypes.c_uint]
_MessageBoxTimeoutW.restype = ctypes.c_int


def _summary(request: dict) -> str:
    lines = [f"{request.get('client', 'An agent')} asks to:", "", f"    {request.get('reason', '?')}", ""]
    if request.get("target"):
        t = request["target"]
        lines.append(f"Target: {t.get('name') or t.get('kind')} {t.get('control_type', '')}".rstrip())
    if request.get("scope"):
        lines.append(f"Scope: {json.dumps(request['scope'])[:200]}")
    lines += ["", "Allow this once?"]
    return "\n".join(lines)


class MessageBoxApprover:
    def __init__(self, timeout_s: int = 60):
        self.timeout_s = timeout_s
        self._lock = threading.Lock()  # one prompt at a time

    def approve(self, request: dict) -> bool:
        with self._lock:
            flags = MB_YESNO | MB_ICONWARNING | MB_DEFBUTTON2 | MB_TOPMOST | MB_SETFOREGROUND | MB_SYSTEMMODAL
            answer = _MessageBoxTimeoutW(None, _summary(request), "Overshoulder: approve agent action?",
                                         flags, 0, int(self.timeout_s * 1000))
            return answer == IDYES
