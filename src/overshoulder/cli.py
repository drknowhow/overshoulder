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


def _wait(a) -> int:
    from overshoulder import settings as st
    from overshoulder.gateway.log import ActionLog
    from overshoulder.waits import WaitError, parse_conditions, wait

    conds = parse_conditions(a.json) if a.json else []
    if a.window_appears:
        conds.append({"kind": "window_appears", "title_re": a.window_appears})
    if a.window_closes:
        conds.append({"kind": "window_closes", "title_re": a.window_closes})
    if a.title_changes:
        conds.append({"kind": "title_changes", "title_re": a.title_changes})
    for flag, kind in ((a.element_appears, "element_appears"), (a.element_gone, "element_gone")):
        if flag:
            title_re, _, name_re = flag.partition("::")
            conds.append({"kind": kind, "title_re": title_re, "name_re": name_re or ".+"})
    if a.file:
        path, _, event = a.file.partition("::")
        conds.append({"kind": "file", "path": path, "event": event or "exists"})
    try:
        out = wait(conds, st.load(), a.timeout)
    except WaitError as e:
        print(json.dumps({"fired": False, "error": str(e)}))
        return 2
    ActionLog(default_home() / "logs" / "waits.jsonl").write("wait", source="cli", conditions=conds, **out)
    print(json.dumps(out))
    return 0 if out["fired"] else 3


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
    sub.add_parser("settings", help="open the settings page in your browser")
    wt = sub.add_parser("wait", help="block until the person finishes something (exit 0), or time out (exit 3)")
    wt.add_argument("--json", help="condition object or list, see overshoulder.waits")
    wt.add_argument("--window-appears", metavar="TITLE_RE")
    wt.add_argument("--window-closes", metavar="TITLE_RE")
    wt.add_argument("--title-changes", metavar="TITLE_RE")
    wt.add_argument("--element-appears", metavar="TITLE_RE::NAME_RE")
    wt.add_argument("--element-gone", metavar="TITLE_RE::NAME_RE")
    wt.add_argument("--file", metavar="PATH[::exists|modified|gone]")
    wt.add_argument("--timeout", type=float, default=None, help="seconds (capped by settings)")
    au = sub.add_parser("autostart", help="start the gateway and watchdog at login (scheduled tasks)")
    au.add_argument("action", choices=["install", "uninstall", "status"])
    rr = sub.add_parser("render", help="(re)render a recording directory into a tutorial")
    rr.add_argument("dir")
    rr.add_argument("--pointer", default="agent", choices=["agent", "real", "none"],
                    help="draw the agent's pointer (default), the recorded real cursor, or none")
    rr.add_argument("--zoom", default="auto", choices=["auto", "off"],
                    help="zoom in on each step's target (default) or never")
    rr.add_argument("--no-keys", action="store_true", help="do not show keystrokes and typed text")

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
    if a.cmd == "settings":
        import os
        url = _operator("settings_link", a.port)["url"]
        os.startfile(url)
        print("opened the settings page in your browser")
        return 0
    if a.cmd == "wait":
        return _wait(a)
    if a.cmd == "autostart":
        from overshoulder import autostart
        fn = {"install": autostart.install, "uninstall": autostart.uninstall, "status": autostart.status}
        print(json.dumps(fn[a.action](), indent=2))
        return 0
    if a.cmd == "render":
        from pathlib import Path

        from overshoulder.render import render
        print(json.dumps(render(Path(a.dir), pointer=a.pointer, zoom=a.zoom, keys=not a.no_keys), indent=2))
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
