"""Live check for the watchdog: an agent types a long text over MCP; Ctrl+Alt+P fires mid-way.

Run with the daemon and watchdog already running:
    .venv\\Scripts\\python spike\\panic_e2e.py <path-to-mcp.json>
Prints the agent's result, daemon status, the overlay window's properties and the log tail.
"""

import asyncio
import ctypes
import ctypes.wintypes as wt
import json
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from overshoulder.driver import input as inp

cfg = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))["mcpServers"]["overshoulder"]
URL, HEADERS = cfg["url"], cfg["headers"]


async def session_calls(calls):
    async with create_mcp_http_client(headers=HEADERS, timeout=None) as http:
        async with streamable_http_client(URL, http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                out = []
                for name, args in calls:
                    r = await s.call_tool(name, args)
                    out.append(json.loads(r.content[0].text))
                return out


def call(name, args=None):
    return asyncio.run(session_calls([(name, args or {})]))[0]


def overlay_info(pid):
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def cb(h, _):
        p = wt.DWORD()
        user32.GetWindowThreadProcessId(h, ctypes.byref(p))
        if p.value == pid and user32.IsWindowVisible(h):
            ex = user32.GetWindowLongW(h, -20)
            aff = wt.DWORD()
            user32.GetWindowDisplayAffinity(h, ctypes.byref(aff))
            r = wt.RECT()
            user32.GetWindowRect(h, ctypes.byref(r))
            found.append({"hwnd": h, "transparent": bool(ex & 0x20), "topmost": bool(ex & 0x8),
                          "affinity": hex(aff.value), "rect": [r.left, r.top, r.right, r.bottom]})
        return True

    user32.EnumWindows(cb, 0)
    return found


def main():
    path = Path(tempfile.mkdtemp()) / "panic_e2e.txt"
    path.write_text("", encoding="utf-8")
    subprocess.Popen(["notepad.exe", str(path)])
    hwnd = None
    for _ in range(80):
        wins = call("windows", {"title_re": "panic_e2e"})["windows"]
        if wins:
            hwnd = wins[0]["hwnd"]
            break
        time.sleep(0.25)
    print("scope:", call("take_scope", {"scope": f"window:{hwnd}"}))
    time.sleep(1.0)

    print("status while driving:", json.dumps(call("status")))

    def fire_panic():
        time.sleep(2.5)
        print(">>> injecting ctrl+alt+p", flush=True)
        inp.send(inp.hotkey_inputs("ctrl+alt+p"))

    threading.Thread(target=fire_panic, daemon=True).start()
    doc = call("find", {"control_type": "Document"})["elements"][0]["ref"]
    text = "The quick brown fox jumps over the lazy dog. " * 12
    t0 = time.time()
    res = call("act", {"action": "type", "ref": doc, "text": text})
    print(f"type returned after {time.time() - t0:.1f}s:", json.dumps(res)[:400])
    print("status after:", json.dumps(call("status")))
    print("act after panic:", json.dumps(call("act", {"action": "hotkey", "combo": "ctrl+s"}))[:300])
    return hwnd


if __name__ == "__main__":
    main()
