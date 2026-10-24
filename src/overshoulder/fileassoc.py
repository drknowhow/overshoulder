"""Open .osp files with Overshoulder: a per-user file association, so a profile someone shared opens the
import page instead of a text editor.

It lives under HKCU\\Software\\Classes and needs no admin rights. Opening a file runs
`overshoulder profile open FILE`, which puts the file in the inbox and opens the settings page on its
import preview; nothing is imported until the person says yes there.
"""

from __future__ import annotations

import sys

from overshoulder.autostart import _pythonw

EXT = ".osp"
PROG_ID = "Overshoulder.Profile"
CLASSES = r"Software\Classes"  # tests pass another key under HKCU


def command() -> str:
    return f'"{_pythonw()}" -m overshoulder.cli profile open "%1"'


def _notify() -> None:
    import ctypes
    ctypes.windll.shell32.SHChangeNotify(0x08000000, 0, None, None)  # SHCNE_ASSOCCHANGED, SHCNF_IDLIST


def install(root: str = CLASSES) -> dict:
    import winreg
    hk = winreg.HKEY_CURRENT_USER
    with winreg.CreateKey(hk, rf"{root}\{PROG_ID}") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "Overshoulder app profile")
    with winreg.CreateKey(hk, rf"{root}\{PROG_ID}\DefaultIcon") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, f"{_pythonw()},0")
    with winreg.CreateKey(hk, rf"{root}\{PROG_ID}\shell\open\command") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, command())
    with winreg.CreateKey(hk, rf"{root}\{EXT}") as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, PROG_ID)
        winreg.SetValueEx(k, "Content Type", 0, winreg.REG_SZ, "text/plain")
    with winreg.CreateKey(hk, rf"{root}\{EXT}\OpenWithProgids") as k:
        winreg.SetValueEx(k, PROG_ID, 0, winreg.REG_NONE, b"")
    if root == CLASSES:
        _notify()
    return status(root)


def _delete_tree(hk, path: str) -> None:
    import winreg
    try:
        with winreg.OpenKey(hk, path, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            while True:
                try:
                    child = winreg.EnumKey(k, 0)
                except OSError:
                    break
                _delete_tree(hk, rf"{path}\{child}")
        winreg.DeleteKey(hk, path)
    except FileNotFoundError:
        pass


def uninstall(root: str = CLASSES) -> dict:
    """Remove the association. .osp is left alone if another program has taken it since."""
    import winreg
    hk = winreg.HKEY_CURRENT_USER
    _delete_tree(hk, rf"{root}\{PROG_ID}")
    try:
        with winreg.OpenKey(hk, rf"{root}\{EXT}", 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
            if winreg.QueryValueEx(k, "")[0] == PROG_ID:
                winreg.DeleteValue(k, "")
    except FileNotFoundError:
        pass
    try:
        with winreg.OpenKey(hk, rf"{root}\{EXT}\OpenWithProgids", 0, winreg.KEY_WRITE) as k:
            winreg.DeleteValue(k, PROG_ID)
    except FileNotFoundError:
        pass
    if root == CLASSES:
        _notify()
    return status(root)


def status(root: str = CLASSES) -> dict:
    if sys.platform != "win32":
        return {"associated": False, "reason": "Windows only"}
    import winreg
    hk = winreg.HKEY_CURRENT_USER
    try:
        with winreg.OpenKey(hk, rf"{root}\{EXT}") as k:
            prog = winreg.QueryValueEx(k, "")[0]
    except FileNotFoundError:
        prog = ""
    try:
        with winreg.OpenKey(hk, rf"{root}\{PROG_ID}\shell\open\command") as k:
            cmd = winreg.QueryValueEx(k, "")[0]
    except FileNotFoundError:
        cmd = ""
    return {"associated": prog == PROG_ID and cmd == command(), "extension": EXT, "prog_id": prog or None,
            "command": cmd or None}
