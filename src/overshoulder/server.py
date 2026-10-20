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
import secrets
import threading
from pathlib import Path

from mcp.server.mcpserver import Context, MCPServer
from mcp.server.mcpserver.utilities.types import Image
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from overshoulder import __version__
from overshoulder.approvals import MessageBoxApprover
from overshoulder.driver import input as inp
from overshoulder.gateway.clients import default_home
from overshoulder.gateway.core import Gateway
from overshoulder.gateway.errors import GatewayError, Unauthorized
from overshoulder.scope import ScopeError
from overshoulder.service import ACTIONS, Service

DEFAULT_PORT = 47621

INSTRUCTIONS = """Overshoulder lets you see and operate desktop apps on this Windows PC, one scope at a time.
1. windows() lists candidate windows; take_scope("window:<hwnd>") leases one (or "screen:1"/"screen:all",
   which a person must approve). Only one agent drives at a time; if busy you get your queue position.
2. observe() returns the scope's UI elements with refs (e1, e2...). find() filters them.
3. act(action, ref=... | x=..,y=..) runs one action and verifies it. Prefer refs and invoke/set_value
   over coordinates. Actions: """ + ", ".join(ACTIONS) + """.
4. Typing goes to the focused control in your scope; pass ref to focus a control first.
5. Refusals are deliberate (out_of_scope, denied, approval_denied, paused, lease_revoked): observe again
   or stop; never try to work around them. A person can press the panic key at any time.
6. release_scope() when done."""


def operator_key_path() -> Path:
    return default_home() / "operator.key"


def ensure_operator_key() -> str:
    p = operator_key_path()
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(secrets.token_urlsafe(32), encoding="utf-8")
    return p.read_text(encoding="utf-8").strip()


class Daemon:
    def __init__(self, service: Service | None = None):
        self.svc = service or Service(Gateway(approver=MessageBoxApprover()))
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
        self.gw.heartbeat(token, lease_id)
        return lease_id

    def call(self, fn, *args, **kwargs):
        with self._desk:
            try:
                return fn(*args, **kwargs)
            except (GatewayError, ScopeError, inp.FocusLost, inp.KeysHeld, inp.InputError) as e:
                code = getattr(e, "code", "error")
                extra = e.to_dict() if isinstance(e, GatewayError) else {}
                return {"ok": False, "error": {**extra, "code": code, "message": str(e)}}
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

        @mcp.tool()
        async def windows(ctx: Context, title_re: str | None = None, process: str | None = None) -> dict:
            """List visible top-level windows you could take as a scope."""
            return await authed(ctx, lambda token: {"windows": d.svc.windows(token, title_re, process)})

        @mcp.tool()
        async def take_scope(ctx: Context, scope: str, ttl_s: float = 120, log_text: bool = False) -> dict:
            """Lease a scope: "window:<hwnd>", "screen:<n>" or "screen:all". log_text records typed
            text in the action log (needed for tutorial captions; off by default)."""
            def go(token):
                out = d.svc.acquire(token, scope, ttl_s)
                d._leases[d.gw.registry.authenticate(token).name] = out["lease_id"]
                if log_text:
                    d.gw.leases.active().meta["log_text"] = True
                return {"ok": True, **{k: v for k, v in out.items() if k != "lease_id"}}

            return await authed(ctx, go)

        @mcp.tool()
        async def release_scope(ctx: Context) -> dict:
            """Give the desktop back."""
            def go(token):
                name = d.gw.registry.authenticate(token).name
                lease_id = d._leases.pop(name, None)
                if lease_id:
                    d.svc.release(token, lease_id)
                return {"ok": True, "released": bool(lease_id)}

            return await authed(ctx, go)

        @mcp.tool()
        async def observe(ctx: Context, max_nodes: int = 300, only_named: bool = True,
                          screenshot: bool = False) -> list:
            """Describe the scope: bounds and UI elements with refs. screenshot=true also returns an image."""
            out = await authed(ctx, lambda token: d.svc.observe(token, d.lease_for(token), max_nodes,
                                                                only_named, screenshot))
            if isinstance(out, dict) and out.get("screenshot"):
                return [json.dumps(out), Image(path=out["screenshot"])]
            return [json.dumps(out)]

        @mcp.tool()
        async def find(ctx: Context, name: str | None = None, name_re: str | None = None,
                       control_type: str | None = None, auto_id: str | None = None) -> dict:
            """Find elements in scope by exact name, name regex, control type and/or automation id."""
            return await authed(ctx, lambda token: {"elements": d.svc.find(
                token, d.lease_for(token), name, name_re, control_type, auto_id)})

        @mcp.tool()
        async def read(ctx: Context, ref: str) -> dict:
            """Read an element's name, value or text."""
            return await authed(ctx, lambda token: d.svc.read(token, d.lease_for(token), ref))

        @mcp.tool()
        async def act(ctx: Context, action: str, ref: str | None = None, x: int | None = None,
                      y: int | None = None, text: str | None = None, combo: str | None = None,
                      value: str | None = None, expect: dict | None = None) -> dict:
            """Run one action and verify it. action: click, double_click, right_click, invoke, toggle,
            select, set_value (value=), type (text=), hotkey (combo="ctrl+s"), focus. Target by ref from
            observe/find, or x/y screen pixels inside the scope. expect: {"changed": true} |
            {"text_contains": "..."} | {"element": {"name": "...", "control_type": "..."}}."""
            return await authed(ctx, lambda token: d.svc.act(
                token, d.lease_for(token), action, ref=ref, x=x, y=y, text=text, combo=combo, value=value,
                expect=expect))

        @mcp.tool()
        async def screenshot(ctx: Context) -> list:
            """Screenshot of the scope."""
            out = await authed(ctx, lambda token: d.svc.screenshot(token, d.lease_for(token)))
            if isinstance(out, dict) and out.get("path"):
                return [json.dumps(out), Image(path=out["path"])]
            return [json.dumps(out)]

        @mcp.tool()
        async def status(ctx: Context) -> dict:
            """Who holds the desktop, the queue, and whether driving is paused."""
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
            if not operator_ok(request):
                return JSONResponse({"error": "forbidden"}, status_code=403)
            verb = request.path_params["verb"]
            body = await request.json() if request.headers.get("content-length") not in (None, "0") else {}
            if verb == "panic":
                d.gw.panic(body.get("reason", "operator panic"))
            elif verb == "pause":
                d.gw.pause(body.get("reason", "operator pause"))
            elif verb == "resume":
                d.gw.resume()
            elif verb == "protect":
                d.gw.protect_pid(int(body["pid"]))
            else:
                return JSONResponse({"error": f"unknown verb {verb}"}, status_code=404)
            return JSONResponse({"ok": True, "status": d.gw.status()})

        return mcp


def serve(port: int = DEFAULT_PORT) -> None:
    daemon = Daemon()
    mcp = daemon.build()
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=[f"127.0.0.1:{port}", f"localhost:{port}"],
                                         allowed_origins=[])
    asyncio.run(mcp.run_streamable_http_async(host="127.0.0.1", port=port, transport_security=security))
