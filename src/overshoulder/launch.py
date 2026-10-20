"""Windowless entry point for autostart: run a CLI command with output going to a log file.

pythonw has no console, so stdout/stderr are None; uvicorn and print() need somewhere to write.
Usage: pythonw -m overshoulder.launch serve|watchdog
"""

from __future__ import annotations

import sys

from overshoulder.gateway.clients import default_home


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    logs = default_home() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stream = open(logs / f"{cmd}.log", "a", encoding="utf-8", buffering=1)  # noqa: SIM115
    sys.stdout = sys.stderr = stream
    from overshoulder.cli import main as cli_main
    return cli_main([cmd, *sys.argv[2:]])


if __name__ == "__main__":
    raise SystemExit(main())
