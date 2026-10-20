"""overshoulder CLI: run the daemon, manage clients, operate the gateway."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.request

from overshoulder.gateway.clients import ClientRegistry, default_home


def _operator(verb: str, port: int, body: dict | None = None, method: str = "POST") -> dict:
    from overshoulder.server import ensure_operator_key
    data = json.dumps(body or {}).encode() if method == "POST" else None
    req = urllib.request.Request(f"http://127.0.0.1:{port}/operator/{verb}", data=data, method=method,
                                 headers={"x-overshoulder-operator": ensure_operator_key(),
                                          "content-type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def main(argv: list[str] | None = None) -> int:
    from overshoulder.server import DEFAULT_PORT

    ap = argparse.ArgumentParser(prog="overshoulder", description="A desktop hand for AI agents.")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("serve", help="run the gateway daemon (MCP at http://127.0.0.1:<port>/mcp)")
    wd = sub.add_parser("watchdog", help="run the watchdog: panic hotkey, human-input pause, scope outline")
    wd.add_argument("--hotkey", default="ctrl+alt+p")
    wd.add_argument("--idle-resume", type=float, default=5.0, help="seconds of no physical input before resuming")

    c = sub.add_parser("client", help="manage agent clients")
    csub = c.add_subparsers(dest="ccmd", required=True)
    add = csub.add_parser("add", help="create or rotate a client; prints its token once")
    add.add_argument("name")
    add.add_argument("--window", default="allow", choices=["allow", "approve", "deny"])
    add.add_argument("--screen", default="approve", choices=["allow", "approve", "deny"])
    rm = csub.add_parser("remove")
    rm.add_argument("name")
    csub.add_parser("list")

    for verb in ("status", "resume"):
        sub.add_parser(verb, help=f"{verb} the running gateway")
    for verb in ("panic", "pause"):
        p = sub.add_parser(verb, help=f"{verb} the running gateway")
        p.add_argument("--reason", default=f"operator {verb}")
    sub.add_parser("home", help="print the data directory")
    rr = sub.add_parser("render", help="(re)render a recording directory into a tutorial")
    rr.add_argument("dir")

    a = ap.parse_args(argv)
    if a.cmd == "serve":
        from overshoulder.server import serve
        print(f"Overshoulder MCP on http://127.0.0.1:{a.port}/mcp  (data: {default_home()})", flush=True)
        serve(a.port)
        return 0
    if a.cmd == "watchdog":
        from overshoulder.watchdog import run
        run(a.port, a.hotkey, a.idle_resume)
        return 0
    if a.cmd == "client":
        reg = ClientRegistry()
        if a.ccmd == "add":
            token = reg.add(a.name, window=a.window, screen=a.screen)
            print(token)
            print(f"# store this token now; only its hash is kept. Grants: window={a.window} screen={a.screen}",
                  file=sys.stderr)
        elif a.ccmd == "remove":
            print("removed" if reg.remove(a.name) else "no such client")
        else:
            for name in reg.names():
                print(name)
        return 0
    if a.cmd == "render":
        from pathlib import Path

        from overshoulder.render import render
        print(json.dumps(render(Path(a.dir)), indent=2))
        return 0
    if a.cmd == "home":
        print(default_home())
        return 0
    if a.cmd == "status":
        print(json.dumps(_operator("status", a.port, method="GET"), indent=2))
    elif a.cmd in ("panic", "pause"):
        print(json.dumps(_operator(a.cmd, a.port, {"reason": a.reason}), indent=2))
    else:
        print(json.dumps(_operator(a.cmd, a.port), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
