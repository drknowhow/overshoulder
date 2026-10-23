"""Start the gateway and watchdog when the user logs in, as two per-user scheduled tasks.

Both run in the interactive session (UI Automation, SendInput and the watchdog's hooks need the
user's desktop) with the limited token: the gateway refuses to run elevated. They restart up to three
times if they crash.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

TASKS = {"serve": "Overshoulder Gateway", "watchdog": "Overshoulder Watchdog"}


def _pythonw() -> Path:
    exe = Path(sys.executable)
    w = exe.with_name("pythonw.exe")
    return w if w.exists() else exe


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)


def _quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def install(start_now: bool = True) -> dict:
    out = {}
    for cmd, name in TASKS.items():
        delay = "PT10S" if cmd == "watchdog" else "PT0S"  # let the gateway bind its port first
        script = f"""
$ErrorActionPreference = 'Stop'
$user = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute {_quote(str(_pythonw()))} -Argument '-m overshoulder.launch {cmd}'
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$trigger.Delay = '{delay}'
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
  -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1) `
  -MultipleInstances IgnoreNew -StartWhenAvailable
Register-ScheduledTask -TaskName {_quote(name)} -Action $action -Trigger $trigger -Principal $principal `
  -Settings $settings -Description 'Overshoulder: desktop hand for AI agents' -Force | Out-Null
"""
        r = _ps(script)
        out[name] = "registered" if r.returncode == 0 else f"failed: {r.stderr.strip()[:300]}"
    if start_now:
        for name in TASKS.values():
            _ps(f"Start-ScheduledTask -TaskName {_quote(name)}")
    return out


def uninstall() -> dict:
    out = {}
    for name in TASKS.values():
        _ps(f"Stop-ScheduledTask -TaskName {_quote(name)} -ErrorAction SilentlyContinue")
        r = _ps(f"Unregister-ScheduledTask -TaskName {_quote(name)} -Confirm:$false")
        out[name] = "removed" if r.returncode == 0 else f"not removed: {r.stderr.strip()[:200]}"
    return out


def status() -> dict:
    names = ",".join(_quote(n) for n in TASKS.values())
    r = _ps(f"Get-ScheduledTask -TaskName {names} -ErrorAction SilentlyContinue | "
            "ForEach-Object { [pscustomobject]@{name=$_.TaskName; state=[string]$_.State; "
            "last=[string](($_ | Get-ScheduledTaskInfo).LastTaskResult)} } | ConvertTo-Json")
    try:
        rows = json.loads(r.stdout) if r.stdout.strip() else []
    except json.JSONDecodeError:
        rows = []
    rows = rows if isinstance(rows, list) else [rows]
    return {row["name"]: {"state": row["state"], "last_result": row["last"]} for row in rows}
