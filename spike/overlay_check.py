"""Resume after panic, take a scope, and report the watchdog's outline window (it is excluded from capture,
so it can only be checked by its properties, not by a screenshot)."""

import json
import subprocess
import sys
import time

from panic_e2e import call, overlay_info


def watchdog_pid() -> int:
    out = subprocess.run(["powershell", "-NoProfile", "-Command",
                          "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*overshoulder*watchdog*' "
                          "-and $_.Name -like 'python*' } | Select-Object -ExpandProperty ProcessId"],
                         capture_output=True, text=True).stdout.split()
    return int(out[-1])


if __name__ == "__main__":
    subprocess.run([sys.executable, "-m", "overshoulder.cli", "resume"], check=True, capture_output=True)
    hwnd = call("windows", {"title_re": "panic_e2e"})["windows"][0]["hwnd"]
    print("scope:", call("take_scope", {"scope": f"window:{hwnd}"})["ok"])
    time.sleep(1.5)
    pid = watchdog_pid()
    status = call("status")
    print("bounds:", status["active"]["bounds"])
    print("overlay windows of watchdog pid", pid, ":", json.dumps(overlay_info(pid)))
    print("release:", call("release_scope"))
    time.sleep(1.0)
    print("overlay after release:", json.dumps(overlay_info(pid)))
