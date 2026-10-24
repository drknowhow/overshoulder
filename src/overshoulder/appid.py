"""App identity: one id per app that stays the same across restarts and updates.

A Store app is its package family name (Microsoft.WindowsNotepad_8wekyb3d8bbwe). Any other app is its
publisher and program file (microsoft-corporation.charmap). A script host such as python.exe or
powershell.exe runs many unrelated apps, so its id also names the script (microsoft-corporation.powershell
+drawnapp). Version numbers are kept beside the id, never in it, so an update keeps its profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+~-]{0,119}$")

SCRIPT_HOSTS = frozenset({
    "python.exe", "pythonw.exe", "py.exe", "pyw.exe", "powershell.exe", "pwsh.exe", "java.exe", "javaw.exe",
    "node.exe", "wscript.exe", "cscript.exe", "mshta.exe", "autohotkey.exe", "autohotkey64.exe",
})
SCRIPT_EXTS = (".py", ".pyw", ".ps1", ".jar", ".js", ".mjs", ".cjs", ".vbs", ".hta", ".ahk")


@dataclass(frozen=True)
class AppIdentity:
    id: str
    name: str
    exe: str
    publisher: str = ""
    version: str = ""
    packaged: bool = False

    def describe(self) -> dict:
        return {"id": self.id, "name": self.name, "version": self.version}


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].strip("-")


def _stem(path: str) -> str:
    name = re.split(r"[\\/]", path)[-1]
    return name.rsplit(".", 1)[0] if "." in name else name


def script_of(exe: str, argv: list[str]) -> str:
    """The script a script host runs, as a slug ("drawnapp" for powershell -File tests\\drawnapp.ps1);
    "" for any other program or when no script is named."""
    if exe.lower() not in SCRIPT_HOSTS:
        return ""
    python = exe.lower().startswith("py")
    for i, arg in enumerate(argv[1:], 1):
        if arg.lower().endswith(SCRIPT_EXTS):
            return slug(_stem(arg))
        if python and arg == "-m" and i + 1 < len(argv):
            return slug(argv[i + 1])
    return ""


def make_id(exe: str, publisher: str = "", package_family: str = "", script: str = "") -> str:
    if package_family and ID_RE.match(package_family):
        base = package_family
    else:
        base = f"{slug(publisher) or 'unknown'}.{slug(_stem(exe)) or 'app'}"
    return f"{base}+{script}" if script else base


def display_name(exe: str, description: str = "", product: str = "", script: str = "") -> str:
    if script:
        return script
    exe_name = re.split(r"[\\/]", exe)[-1].lower()
    for text in (description, product):
        text = text.strip()
        if text and len(text) <= 60 and text.lower() != exe_name:  # Store Notepad's description is "Notepad.exe"
            return text
    return _stem(exe)


def identify(exe: str, *, publisher: str = "", description: str = "", product: str = "", version: str = "",
             package_family: str = "", package_version: str = "", argv: list[str] | None = None) -> AppIdentity:
    """Compose an identity from what Windows reports about a process (see driver.appinfo.read)."""
    script = script_of(exe, argv or [])
    return AppIdentity(id=make_id(exe, publisher, package_family, script),
                       name=display_name(exe, description, product, script), exe=exe,
                       publisher=publisher.strip(), version=package_version or version,
                       packaged=bool(package_family))
