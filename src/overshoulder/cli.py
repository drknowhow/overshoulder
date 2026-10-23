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


def _pair(v: str) -> list[float]:
    return [float(x) for x in v.split(",")]


def _limit_flags(p: argparse.ArgumentParser) -> None:
    """A client's standing limits (gateway.limits). Unset flags leave a limit as it is."""
    p.add_argument("--read-only", dest="read_only", action="store_const", const=True, default=None,
                   help="every act is refused; observe, find, screenshots and recording still work")
    p.add_argument("--no-typing", dest="no_typing", action="store_const", const=True, default=None,
                   help="type, set_value and hotkey are refused")
    p.add_argument("--deny-process", action="append", default=[], metavar="EXE",
                   help="hide a program's windows from this client, e.g. chrome.exe (repeatable)")


def _item_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--rect", type=_pair, metavar="L,T,R,B")
    p.add_argument("--at", type=_pair, metavar="X,Y")
    p.add_argument("--tail", type=_pair, metavar="X,Y", help="arrow start")
    p.add_argument("--head", type=_pair, metavar="X,Y", help="arrow point")
    p.add_argument("--text")
    p.add_argument("--size", type=int)
    p.add_argument("--color")
    p.add_argument("--width", type=int)
    p.add_argument("--style", choices=["blur", "pixelate", "solid"])
    p.add_argument("--step", type=int, help="step id from steps.json")
    p.add_argument("--t0", type=float, help="seconds from the recording start")
    p.add_argument("--t1", type=float)
    p.add_argument("--caption")
    p.add_argument("--hide", dest="hidden", action="store_const", const=True)
    p.add_argument("--show", dest="hidden", action="store_const", const=False)
    p.add_argument("--narration")
    p.add_argument("--audio")
    p.add_argument("--note")
    p.add_argument("--json", dest="raw", help="fields as a JSON object, merged over the flags")


def _annotate(a) -> int:
    from pathlib import Path

    from overshoulder.layer import Layer, LayerError

    target = Path(a.target)
    layer = Layer.for_recording(target) if target.is_dir() else Layer.for_image(target)
    fields = ("rect", "at", "tail", "head", "text", "size", "color", "width", "style", "step", "t0", "t1",
              "caption", "hidden", "narration", "audio", "note")
    try:
        if a.acmd == "list":
            print(json.dumps(layer.items(), indent=2))
            return 0
        if a.acmd == "remove":
            print(json.dumps({"removed": layer.remove(a.id)}))
            return 0
        item = {k: getattr(a, k) for k in fields if getattr(a, k, None) is not None}
        if a.raw:
            item.update(json.loads(a.raw))
        out = layer.add({"kind": a.kind, **item}) if a.acmd == "add" else layer.edit(a.id, item)
    except (LayerError, json.JSONDecodeError) as e:
        print(json.dumps({"error": str(e)}), file=sys.stderr)
        return 2
    print(json.dumps(out))
    return 0


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
    _limit_flags(add)
    lim = csub.add_parser("limit", help="set a client's standing limits; its agent cannot lift them")
    lim.add_argument("name")
    _limit_flags(lim)
    lim.add_argument("--writable", dest="read_only", action="store_false", default=None, help="lift --read-only")
    lim.add_argument("--typing", dest="no_typing", action="store_false", default=None, help="lift --no-typing")
    lim.add_argument("--allow-process", action="append", default=[], metavar="EXE",
                     help="take a program off the deny list (repeatable)")
    lim.add_argument("--clear", action="store_true", help="start from no limits")
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
    an = sub.add_parser("annotate", help="edit the annotation layer of a recording or a screenshot")
    an.add_argument("target", help="recording directory or image file")
    asub = an.add_subparsers(dest="acmd", required=True)
    asub.add_parser("list")
    from overshoulder.layer import KINDS
    _item_flags(asub.add_parser("add", help="add an item (see overshoulder.layer for kinds)"))
    asub.choices["add"].add_argument("kind", choices=KINDS)
    _item_flags(asub.add_parser("edit", help="change an item's fields"))
    asub.choices["edit"].add_argument("id")
    asub.add_parser("remove").add_argument("id")
    rr = sub.add_parser("render", help="(re)render a recording directory into a tutorial, or an annotated image")
    rr.add_argument("dir", help="recording directory, or an image with a .annotations.json beside it")
    rr.add_argument("--pointer", default="agent", choices=["agent", "real", "none"],
                    help="draw the agent's pointer (default), the recorded real cursor, or none")
    rr.add_argument("--zoom", default="auto", choices=["auto", "off"],
                    help="zoom in on each step's target (default) or never")
    rr.add_argument("--no-keys", action="store_true", help="do not show keystrokes and typed text")
    rr.add_argument("--no-gif", action="store_true", help="do not write tutorial.gif")
    rr.add_argument("--mlt", action="store_true",
                    help="also write tutorial.mlt, a Shotcut project of the same edit (media in edit/)")
    rr.add_argument("--narrate", action="store_true",
                    help="speak each step and add the voice track (Windows voice unless --tts-cmd)")
    rr.add_argument("--voice", help="part of an installed Windows voice's name, e.g. Zira (see: overshoulder voices)")
    rr.add_argument("--tts-cmd", metavar="TEMPLATE",
                    help='any TTS program, run once per line: {text_file} is the line, {out} the audio to write; '
                         'implies --narrate. Example: piper -m en.onnx -f {out} < {text_file}')
    sub.add_parser("voices", help="list the Windows voices --narrate can use")

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
        from overshoulder.gateway.limits import Limits
        if a.ccmd == "add":
            token = reg.add(a.name, window=a.window, screen=a.screen,
                            limits={"read_only": bool(a.read_only), "no_typing": bool(a.no_typing),
                                    "deny_processes": a.deny_process})
            print(token)
            lim = Limits.from_dict(reg.get(a.name).limits)
            print(f"# store this token now; only its hash is kept. Grants: window={a.window} screen={a.screen}"
                  f"; limits: {lim.describe()}", file=sys.stderr)
        elif a.ccmd == "limit":
            try:
                out = reg.set_limits(a.name, read_only=a.read_only, no_typing=a.no_typing, deny=a.deny_process,
                                     allow=a.allow_process, clear=a.clear)
            except KeyError as e:
                print(e.args[0], file=sys.stderr)
                return 2
            print(f"{a.name}: {Limits.from_dict(out).describe()}")
        elif a.ccmd == "remove":
            print("removed" if reg.remove(a.name) else "no such client")
        else:
            for name in reg.names():
                lim = Limits.from_dict(reg.get(name).limits)
                print(f"{name}  ({lim.describe()})" if lim else name)
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
    if a.cmd == "annotate":
        return _annotate(a)
    if a.cmd == "render":
        from pathlib import Path

        from overshoulder import narrate as nr
        from overshoulder.render import render, render_image
        target = Path(a.dir)
        if target.is_file():
            print(json.dumps(render_image(target), indent=2))
            return 0
        try:
            out = render(target, pointer=a.pointer, zoom=a.zoom, keys=not a.no_keys, gif=not a.no_gif,
                         narrate=a.narrate or bool(a.tts_cmd), voice=a.voice,
                         engine=nr.command(a.tts_cmd) if a.tts_cmd else None, outside_audio=True, mlt=a.mlt)
        except nr.NarrationError as e:
            print(json.dumps({"error": str(e)}), file=sys.stderr)
            return 2
        print(json.dumps(out, indent=2))
        return 0
    if a.cmd == "voices":
        from overshoulder.narrate import voices
        print("\n".join(voices()) or "no Windows voices installed")
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
