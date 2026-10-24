"""What Windows says about a running program: its package, version resource and command line."""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import struct

from overshoulder.appid import SCRIPT_HOSTS, AppIdentity, identify

kernel32 = ctypes.windll.kernel32
version = ctypes.windll.version
ntdll = ctypes.windll.ntdll
shell32 = ctypes.windll.shell32

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
PROCESS_COMMAND_LINE_INFORMATION = 60
STATUS_INFO_LENGTH_MISMATCH = 0xC0000004

kernel32.OpenProcess.restype = wt.HANDLE
kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
kernel32.CloseHandle.argtypes = [wt.HANDLE]
kernel32.GetPackageFamilyName.argtypes = [wt.HANDLE, ctypes.POINTER(wt.UINT), wt.LPWSTR]
kernel32.GetPackageFullName.argtypes = [wt.HANDLE, ctypes.POINTER(wt.UINT), wt.LPWSTR]
kernel32.LocalFree.argtypes = [ctypes.c_void_p]
version.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
version.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
version.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR, ctypes.POINTER(ctypes.c_void_p),
                                   ctypes.POINTER(wt.UINT)]
ntdll.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.ULONG,
                                            ctypes.POINTER(wt.ULONG)]
ntdll.NtQueryInformationProcess.restype = wt.LONG
shell32.CommandLineToArgvW.argtypes = [wt.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
shell32.CommandLineToArgvW.restype = ctypes.POINTER(wt.LPWSTR)


class _UnicodeString(ctypes.Structure):
    _fields_ = [("Length", wt.USHORT), ("MaximumLength", wt.USHORT), ("Buffer", ctypes.c_void_p)]


def _open(pid: int):
    return kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)


def _package_name(h, fn) -> str:
    n = wt.UINT(0)
    fn(h, ctypes.byref(n), None)
    if not n.value:
        return ""  # APPMODEL_ERROR_NO_PACKAGE: not a packaged app
    buf = ctypes.create_unicode_buffer(n.value)
    return buf.value if fn(h, ctypes.byref(n), buf) == 0 else ""


def package(pid: int) -> tuple[str, str]:
    """(family name, version) of a packaged app's process; ("", "") for any other process."""
    h = _open(pid)
    if not h:
        return "", ""
    try:
        family = _package_name(h, kernel32.GetPackageFamilyName)
        full = _package_name(h, kernel32.GetPackageFullName) if family else ""
    finally:
        kernel32.CloseHandle(h)
    parts = full.split("_")  # Name_Version_Architecture_ResourceId_PublisherId
    return family, parts[1] if len(parts) >= 2 else ""


def _query(buf, path: str) -> tuple[int, int] | None:
    p, n = ctypes.c_void_p(), wt.UINT(0)
    if not version.VerQueryValueW(buf, path, ctypes.byref(p), ctypes.byref(n)) or not p.value:
        return None
    return p.value, n.value


def version_info(path: str) -> dict:
    """CompanyName, FileDescription, ProductName and the numeric file version from an exe's version
    resource. Missing fields come back as ""."""
    out = {"company": "", "description": "", "product": "", "version": ""}
    size = version.GetFileVersionInfoSizeW(path, None) if path else 0
    if not size:
        return out
    buf = ctypes.create_string_buffer(size)
    if not version.GetFileVersionInfoW(path, 0, size, buf):
        return out
    fixed = _query(buf, "\\")
    if fixed and fixed[1] >= 16:
        ms, ls = struct.unpack_from("<II", ctypes.string_at(fixed[0], 16), 8)
        out["version"] = f"{ms >> 16}.{ms & 0xFFFF}.{ls >> 16}.{ls & 0xFFFF}"
    langs = ["040904b0", "040904e4", "04090000"]
    tr = _query(buf, "\\VarFileInfo\\Translation")
    if tr and tr[1] >= 4:
        lang, cp = struct.unpack_from("<HH", ctypes.string_at(tr[0], 4))
        langs.insert(0, f"{lang:04x}{cp:04x}")
    for key, name in (("company", "CompanyName"), ("description", "FileDescription"), ("product", "ProductName")):
        for lang in langs:
            hit = _query(buf, f"\\StringFileInfo\\{lang}\\{name}")
            if hit and hit[1] > 1:
                # the reported length can run past the string's own terminator into the next key
                out[key] = ctypes.wstring_at(hit[0], hit[1]).split("\0", 1)[0].strip()
                break
    return out


def command_line(pid: int) -> list[str]:
    """The process's arguments, or [] if Windows won't say."""
    h = _open(pid)
    if not h:
        return []
    try:
        need = wt.ULONG(0)
        ntdll.NtQueryInformationProcess(h, PROCESS_COMMAND_LINE_INFORMATION, None, 0, ctypes.byref(need))
        if not need.value:
            return []
        buf = ctypes.create_string_buffer(need.value)
        if ntdll.NtQueryInformationProcess(h, PROCESS_COMMAND_LINE_INFORMATION, buf, need, ctypes.byref(need)):
            return []
        us = _UnicodeString.from_buffer(buf)
        text = ctypes.wstring_at(us.Buffer, us.Length // 2) if us.Buffer else ""
    finally:
        kernel32.CloseHandle(h)
    if not text:
        return []
    argc = ctypes.c_int(0)
    argv = shell32.CommandLineToArgvW(text, ctypes.byref(argc))
    if not argv:
        return []
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        kernel32.LocalFree(argv)


def read(pid: int, image_path: str) -> AppIdentity:
    exe = image_path.replace("/", "\\").rsplit("\\", 1)[-1]
    info = version_info(image_path)
    family, pkg_version = package(pid)
    argv = command_line(pid) if exe.lower() in SCRIPT_HOSTS else []
    return identify(exe, publisher=info["company"], description=info["description"], product=info["product"],
                    version=info["version"], package_family=family, package_version=pkg_version, argv=argv)
