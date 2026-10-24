"""The Overshoulder daemon: one gateway, served to every agent as MCP over loopback HTTP.

One process holds the lease, so agents connecting from different hosts (Claude Code, Codex,
scripts) contend for the same desktop through the same rules. Agents authenticate with
`Authorization: Bearer <client token>`; operator endpoints (resume after panic, status) use a
separate key stored in the user's profile.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import secrets
import threading
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from overshoulder import __version__
from overshoulder.approvals import SettingsApprover
from overshoulder.driver import input as inp
from overshoulder.gateway.clients import default_home
from overshoulder.gateway.core import Gateway
from overshoulder.gateway.errors import GatewayError, Paused, Unauthorized
from overshoulder.narrate import NarrationError
from overshoulder.scope import ScopeError
from overshoulder.service import ACTIONS, Service
from overshoulder.settings_page import Sessions
from overshoulder.settings_page import routes as settings_routes

DEFAULT_PORT = 47621

INSTRUCTIONS = """Overshoulder lets you see and operate desktop apps on this Windows PC, one scope at a time.
1. windows() lists candidate windows; take_scope("window:<hwnd>") leases one (or "screen:1"/"screen:all",
   which a person must approve). Only one agent drives at a time; if busy you get your queue position.
2. observe() returns the scope's UI elements with refs (e1, e2...). find() filters them. Apps that draw
   their own buttons (games, canvases, some older apps) show no elements for them: find(text="Save")
   reads the screen with OCR and returns t-refs (t1, t2...) with source "ocr", and observe(text=true)
   lists all the text it reads. For an icon with no text, find(describe="the gear icon") asks a vision
   model, if the person has turned one on, and returns a v-ref (v1...) with source "vision". t- and
   v-refs take click, double_click, right_click and drag, and go stale when the pixels under them change.
3. act(action, ref=... | x=..,y=..) runs one action and verifies it. Prefer refs and invoke/set_value
   over coordinates. Actions: """ + ", ".join(ACTIONS) + """. A drag also takes the drop point:
   to_ref or to_x/to_y.
4. Typing goes to the focused control in your scope; pass ref to focus a control first.
5. Refusals are deliberate (out_of_scope, denied, approval_denied, narrowed, paused, lease_revoked,
   needs_real_input, person_active): observe again or stop; never try to work around them. While the
   person uses the mouse or keyboard, background actions go on, but an action that needs their real
   input waits (up to ~20 s) for them to stop; person_active or paused after that means they are still
   busy, so retry later. If they start using the mouse or keyboard during such an action, it stops. A
   person can press the panic key at any time; after a panic only they can resume.
6. You work beside the person: act() reaches the window through UI Automation and messages to the
   window itself, so their cursor and the app they are using stay theirs. They choose how much you may
   use their real mouse and keyboard (status() shows input_mode): "never", "borrow" (only after they
   have been idle a few seconds; their cursor and window are put back afterwards, except that a menu or
   dialog you opened stays in front for your next step) or "full" (also allows activate).
   needs_real_input means an action has no background method
   and real input is not allowed: use a ref with invoke, select or set_value, or ask the person to do
   that step and wait_for() it. person_active means they are using the PC right now: retry shortly.
   Pass method="background" to make sure an action never touches their input.
7. release_scope() when done.
   To do less than your scope allows, take it with read_only, no_typing or deny_processes; the person
   may also have set such limits for you (take_scope and status() show them). A narrowed refusal names
   the limit and who set it.
8. If you need the person to do something (sign in, approve, finish a form), don't ask them to tell you
   when they're done: wait_for() the window/control/file change that shows it is done.
9. Tutorials: record_start, do the task, record_stop (renders it). While recording, redact(ref) hides a
   secret in every frame. Afterwards annotate(recording) to add arrows, boxes, labels, redactions,
   captions or narration text, then render(recording) again; both work after release_scope too."""


def operator_key_path() -> Path:
    return default_home() / "operator.key"


def ensure_operator_key() -> str:
    p = operator_key_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


class Daemon:
    def __init__(self, service: Service | None = None, port: int = DEFAULT_PORT):
        self.svc = service or Service(Gateway(approver=SettingsApprover()))
        self.port_ref = {"port": port}
        self.settings_sessions = Sessions()
        self.gw = self.svc.gw
        self._leases: dict[str, str] = {}  # client name -> lease id
        # pywinauto/COM and SendInput are not safe to drive from many threads at once; serialize.
        self._desk = threading.Lock()
        self.operator_key = ensure_operator_key()

    # -- auth and lease plumbing ---------------------------------------------------------------

    @staticmethod
    def token_from_headers(headers) -> str:
        headers = headers or {}
        auth = headers.get("authorization") or headers.get("Authorization") or ""
        if not auth.lower().startswith("bearer "):
            raise Unauthorized("send Authorization: Bearer <client token>")
        return auth[7:].strip()

    def lease_for(self, token: str) -> str:
        client = self.gw.registry.authenticate(token)
        lease_id = self._leases.get(client.name)
        if not lease_id:
            raise GatewayError("no scope held; call take_scope first")
        try:
            self.gw.heartbeat(token, lease_id)
        except Paused:
            pass  # the action itself waits out a human-input pause, or reports the pause
        return lease_id

    def call(self, fn, *args, **kwargs):
        """Run fn holding the desktop: everything that reads or drives it goes one at a time."""
        with self._desk:
            return self.guard(fn, *args, **kwargs)

    @staticmethod
    def guard(fn, *args, **kwargs):
        """Run fn, turning expected refusals into {"ok": false, "error": {...}}. Without the desktop
        lock, this is for work that never touches the desktop (rendering, annotation files)."""
        try:
            return fn(*args, **kwargs)
        except (GatewayError, ScopeError, inp.FocusLost, inp.KeysHeld, inp.InputError) as e:
            code = getattr(e, "code", "error")
            extra = e.to_dict() if isinstance(e, GatewayError) else {}
            return {"ok": False, "error": {**extra, "code": code, "message": str(e)}}
        except NarrationError as e:
            return {"ok": False, "error": {"code": "narration_failed", "message": str(e)}}
        except ValueError as e:
            return {"ok": False, "error": {"code": "bad_request", "message": str(e)}}

    # -- MCP app -------------------------------------------------------------------------------

    def build(self) -> MCPServer:
        mcp = MCPServer("overshoulder", version=__version__, instructions=INSTRUCTIONS)
        d = self

        async def authed(ctx: Context, fn):
            """Run fn(token) with the caller's token; auth failures come back as {"ok": false}."""
            headers = ctx.headers
            return await asyncio.to_thread(d.call, lambda: fn(d.token_from_headers(headers)))

        async def offline(ctx: Context, fn):
            """authed without the desktop lock, for work that never touches the desktop."""
            headers = ctx.headers
            return await asyncio.to_thread(d.guard, lambda: fn(d.token_from_headers(headers)))

        def failed(out) -> bool:
            return isinstance(out, dict) and out.get("ok") is False

        @mcp.tool()
        async def windows(ctx: Context, title_re: str | None = None, process: str | None = None) -> dict:
            """List visible top-level windows you could take as a scope."""
            return await authed(ctx, lambda token: {"windows": d.svc.windows(token, title_re, process)})

        @mcp.tool()
        async def take_scope(ctx: Context, scope: str, ttl_s: float = 120, log_text: bool = False,
                             read_only: bool = False, no_typing: bool = False,
                             deny_processes: list[str] | None = None) -> dict:
            """Lease a scope: "window:<hwnd>", "screen:<n>" or "screen:all". log_text records typed
            text in the action log (needed for tutorial captions; off by default). To narrow the
            session: read_only refuses every act (observe, find, screenshot and recording still work),
            no_typing refuses type/set_value/hotkey, deny_processes (e.g. ["chrome.exe"]) takes those
            programs' windows out of the scope and blacks them out of screen screenshots and recordings.
            Taking a scope again while you hold one keeps the limits you had. The result's limits are
            all that apply, including any the person set for you."""
            limits = {"read_only": read_only, "no_typing": no_typing, "deny_processes": deny_processes or []}

            def go(token):
                out = d.svc.acquire(token, scope, ttl_s, log_text=log_text, limits=limits)
                d._leases[d.gw.registry.authenticate(token).name] = out["lease_id"]
                return {"ok": True, **{k: v for k, v in out.items() if k != "lease_id"}}

            return await authed(ctx, go)

        @mcp.tool()
        async def release_scope(ctx: Context) -> dict:
            """Give the desktop back."""
            def go(token):
                name = d.gw.registry.authenticate(token).name
                lease_id = d._leases.pop(name, None)
                out = d.svc.release(token, lease_id) if lease_id else {}
                return {"ok": True, "released": bool(lease_id), **{k: v for k, v in out.items() if k != "released"}}

            out = await authed(ctx, go)
            rec = out.get("recording") if not failed(out) else None
            if rec and rec.get("frames"):  # a recording left running: render it now the desktop is free
                rec["tutorial"] = await offline(ctx, lambda token: d.svc.render_recording(token, rec["recording"]))
            return out

        @mcp.tool()
        async def observe(ctx: Context, max_nodes: int = 300, only_named: bool = True,
                          screenshot: bool = False, text: bool = False) -> list:
            """Describe the scope: bounds and UI elements with refs. screenshot=true also returns an image.
            text=true also reads the text on screen with OCR and lists it under "texts" as t-refs."""
            out = await authed(ctx, lambda token: d.svc.observe(token, d.lease_for(token), max_nodes,
                                                                only_named, screenshot, text))
            if isinstance(out, dict) and out.get("screenshot"):
                return [json.dumps(out), Image(path=out["screenshot"])]
            return [json.dumps(out)]

        @mcp.tool()
        async def find(ctx: Context, name: str | None = None, name_re: str | None = None,
                       control_type: str | None = None, auto_id: str | None = None,
                       text: str | None = None, describe: str | None = None) -> dict:
            """Find elements in scope by exact name, name regex, control type and/or automation id.
            text="Save" instead reads the screen with OCR and returns where those words are as t-refs
            (source "ocr"), for buttons an app draws itself. A find by name alone that matches no element
            looks for the name on screen the same way. describe="the gear icon" asks a vision model where
            that is and returns a v-ref (source "vision"), or nothing if it sees none; it works only when
            the person has turned a vision model on, and can take a minute while a local model loads."""
            return await authed(ctx, lambda token: {"elements": d.svc.find(
                token, d.lease_for(token), name, name_re, control_type, auto_id, text=text, describe=describe)})

        @mcp.tool()
        async def read(ctx: Context, ref: str) -> dict:
            """Read an element's name, value or text."""
            return await authed(ctx, lambda token: d.svc.read(token, d.lease_for(token), ref))

        @mcp.tool()
        async def act(ctx: Context, action: str, ref: str | None = None, x: int | None = None,
                      y: int | None = None, text: str | None = None, combo: str | None = None,
                      value: str | None = None, expect: dict | None = None, method: str = "auto",
                      to_ref: str | None = None, to_x: int | None = None, to_y: int | None = None) -> dict:
            """Run one action and verify it. action: click, double_click, right_click, drag, invoke,
            toggle, select, set_value (value=), type (text=), hotkey (combo="ctrl+s"), focus. Target by
            ref from observe/find, or x/y screen pixels inside the scope. drag goes from the target to
            to_ref or to_x/to_y, both inside the scope; dropping onto something named like Delete or
            Recycle Bin needs the person's approval, and verify.moved says whether a ref'd item moved. A
            click at x/y is checked against the text on screen there too, so x/y on a drawn Delete asks.
            expect: {"changed": true} | {"text_contains": "..."} | {"element": {"name": "...", "control_type": "..."}}.
            method: "auto" (default; works on the window in the background and uses the person's mouse or
            keyboard only as a last resort), "background" (never), "input" (real input). The result's
            verify.method says what ran."""
            return await authed(ctx, lambda token: d.svc.act(
                token, d.lease_for(token), action, ref=ref, x=x, y=y, text=text, combo=combo, value=value,
                expect=expect, method=method, to_ref=to_ref, to_x=to_x, to_y=to_y))

        @mcp.tool()
        async def record_start(ctx: Context, fps: int = 10, name: str = "") -> dict:
            """Start a clean recording of your scope (no overlays; the cursor and step callouts are drawn
            later). For readable captions, take the scope with log_text=true. Stops on record_stop or
            when the scope is released or revoked."""
            return await authed(ctx, lambda token: d.svc.record_start(token, d.lease_for(token), fps, name))

        @mcp.tool()
        async def record_stop(ctx: Context, render: bool = True, zoom: str = "auto", keys: bool = True,
                              gif: bool = True, narrate: bool = False, voice: str | None = None,
                              mlt: bool = False) -> dict:
            """Stop recording. With render=true, also returns a tutorial: an MP4 with a pointer, click
            ripples, numbered step callouts, captions, a zoom onto each step's target (zoom="off" to
            stop it) and keystroke captions (keys), one PNG per step, steps.md, steps.json and
            tutorial.gif (gif). narrate=true speaks each step with the Windows voice (voice: part of a
            voice's name, e.g. "Zira"); the video waits for each line. mlt=true also writes
            tutorial.mlt, a Shotcut project of the same edit a person can open and finish. Rendering runs
            after the desktop is free, so other agents are not held up."""
            out = await authed(ctx, lambda token: d.svc.record_stop(token, d.lease_for(token)))
            if render and not failed(out) and out.get("frames"):
                out["tutorial"] = await offline(ctx, lambda token: d.svc.render_recording(
                    token, out["recording"], zoom=zoom, keys=keys, gif=gif, narrate=narrate, voice=voice,
                    mlt=mlt))
            return out

        @mcp.tool()
        async def redact(ctx: Context, ref: str, style: str = "blur") -> dict:
            """While recording, hide an element (ref from observe/find) in every frame of the tutorial and
            its stills and GIF: a password, an email address, an account number, anything viewers should
            not see. style: blur, pixelate or solid. It becomes a redact item in the recording's layer."""
            return await authed(ctx, lambda token: d.svc.redact(token, d.lease_for(token), ref, style))

        @mcp.tool()
        async def annotate(ctx: Context, recording: str, op: str = "list", item: dict | None = None,
                           id: str | None = None, changes: dict | None = None) -> dict:
            """Read or edit the annotation layer of one of your recordings (the name or path record_stop
            returned); render() then applies it. op: "list" (the items, the steps of the last render with
            their ids, captions and targets, and the frame size), "add" (item=), "edit" (id=, changes=;
            a null value removes a field) or "remove" (id=).
            Item kinds: box {rect, text?, color?}, arrow {tail, head}, text {at, text, size?},
            badge {at, text?}, spotlight {rect}, redact {rect, style: blur|pixelate|solid},
            step {step, caption?, hidden?, narration?, audio?}, cut {t0, t1}, crop {rect}.
            Positions are pixels of the recorded frame: rect [left, top, right, bottom], points [x, y].
            Show an item during one step (step: an id from list's steps), between t0 and t1 seconds of
            the recording, or, with neither, the whole time. A step item changes that step: a new
            caption, hidden=true to drop it, narration text to speak instead of the caption ("" for
            silence), or audio: a sound file inside the recording directory."""
            return await offline(ctx, lambda token: d.svc.annotate(token, recording, op, item, id, changes))

        @mcp.tool()
        async def render(ctx: Context, recording: str, zoom: str = "auto", keys: bool = True, gif: bool = True,
                         narrate: bool = False, voice: str | None = None, pointer: str = "agent",
                         mlt: bool = False) -> dict:
            """Render one of your stopped recordings again, with its annotation layer: after annotate(), or
            to change zoom, keys, gif, narration or the Shotcut project (mlt; see record_stop). pointer:
            "agent" (your pointer, gliding to each step), "real" (the recorded cursor) or "none". Needs no
            scope."""
            return await offline(ctx, lambda token: d.svc.render_recording(
                token, recording, zoom=zoom, keys=keys, gif=gif, narrate=narrate, voice=voice, pointer=pointer,
                mlt=mlt))

        @mcp.tool()
        async def screenshot(ctx: Context) -> list:
            """Screenshot of the scope."""
            out = await authed(ctx, lambda token: d.svc.screenshot(token, d.lease_for(token)))
            if isinstance(out, dict) and out.get("path"):
                return [json.dumps(out), Image(path=out["path"])]
            return [json.dumps(out)]

        @mcp.tool()
        async def wait_for(ctx: Context, conditions: list[dict], timeout_s: float = 300) -> dict:
            """Wait until the person finishes something, then return. No scope needed; this only reads.
            Returns as soon as ANY condition holds, or {"fired": false} at the timeout (capped by the
            person's settings). Condition kinds: window_appears {title_re|process}, window_closes
            {title_re|hwnd}, title_changes {title_re, to_re?}, element_appears / element_gone
            {title_re, name|name_re, control_type?, auto_id?}, text_contains {title_re, text, name|auto_id?},
            file {path, event: exists|modified|gone}. For long waits, prefer running
            `overshoulder wait` as a background command if your host supports that."""
            from overshoulder import settings as st
            from overshoulder.waits import WaitError, wait

            def go(token):
                client = d.gw.registry.authenticate(token)
                hidden = d.gw.hidden_for(client.name).deny_processes
                try:
                    # locked per look, not per wait
                    out = wait(conditions, st.load(), timeout_s, lock=d._desk, hidden=hidden)
                except WaitError as e:
                    return {"ok": False, "error": {"code": e.code, "message": str(e)}}
                d.gw.log.write("wait", client=client.name, conditions=len(conditions), **out)
                return {"ok": True, **out}

            return await offline(ctx, go)

        @mcp.tool()
        async def status(ctx: Context) -> dict:
            """Who holds the desktop, the queue, whether driving is paused, and the person's input mode
            (never, borrow or full: how much you may use their real mouse and keyboard)."""
            def go(token):
                d.gw.registry.authenticate(token)
                return d.gw.status()

            return await authed(ctx, go)

        def operator_ok(request: Request) -> bool:
            return hmac.compare_digest(request.headers.get("x-overshoulder-operator", ""), d.operator_key)

        @mcp.custom_route("/operator/status", methods=["GET"])
        async def op_status(request: Request):
            if not operator_ok(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            return JSONResponse(d.gw.status())

        @mcp.custom_route("/operator/{verb}", methods=["POST"])
        async def op_verb(request: Request):
            # Read the body even to refuse: answering with it unread sometimes resets the connection on
            # Windows, and the caller gets ConnectionAbortedError instead of the 403.
            raw = await request.body()
            if not operator_ok(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            verb = request.path_params["verb"]
            body = json.loads(raw) if raw else {}
            if verb == "panic":
                d.gw.panic(body.get("reason", "operator panic"))
            elif verb == "pause":
                d.gw.pause(body.get("reason", "operator pause"))
            elif verb == "resume":
                d.gw.resume(body.get("only_if"))
            elif verb == "protect":
                d.gw.protect_pid(int(body["pid"]))
            elif verb == "settings_link":
                token = d.settings_sessions.new_link()
                return JSONResponse({"url": f"http://127.0.0.1:{d.port_ref['port']}/settings?t={token}"})
            else:
                return JSONResponse({"error": f"unknown verb {verb}"}, status_code=404)
            return JSONResponse({"ok": True, "status": d.gw.status()})

        settings_routes(mcp, d.port_ref, d.settings_sessions, d.gw.registry, str(default_home() / "logs"))
        return mcp


class _QuietStatusPolls(logging.Filter):
    """Drops uvicorn's access line for each answered /operator/status poll. The watchdog polls every
    250 ms, which put ~22 MB a day into serve.log; refusals and every other request still log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args  # uvicorn: (client, method, path with query, http version, status code)
        return not (isinstance(args, tuple) and len(args) == 5 and args[2] == "/operator/status"
                    and args[4] == 200)


def quiet_status_polls() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _QuietStatusPolls) for f in access.filters):
        access.addFilter(_QuietStatusPolls())


def serve(port: int = DEFAULT_PORT) -> None:
    quiet_status_polls()  # on the logger, which uvicorn's own logging setup leaves in place
    daemon = Daemon(port=port)
    mcp = daemon.build()
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
                                         allowed_origins=[])
    asyncio.run(mcp.run_streamable_http_async(host="127.0.0.1", port=port, transport_security=security))
