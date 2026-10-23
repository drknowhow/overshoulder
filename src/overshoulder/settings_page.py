"""The local settings page, served by the gateway at /settings.

Opened only through `overshoulder settings`, which asks the gateway (with the operator key) for a
one-time link. Visiting the link swaps it for a session cookie (HttpOnly, SameSite=Strict); every
save also carries a per-session form token. Requests whose Host header is not loopback are refused.
"""

from __future__ import annotations

import html
import secrets
import time
from dataclasses import dataclass, field

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from overshoulder import brand
from overshoulder import settings as st

LINK_TTL_S = 15 * 60
SESSION_TTL_S = 8 * 3600
COOKIE = "overshoulder_settings"


@dataclass
class Sessions:
    links: dict[str, float] = field(default_factory=dict)
    sessions: dict[str, tuple[float, str]] = field(default_factory=dict)  # id -> (expiry, form token)

    def new_link(self) -> str:
        token = secrets.token_urlsafe(24)
        self.links[token] = time.time() + LINK_TTL_S
        return token

    def redeem(self, token: str) -> str | None:
        exp = self.links.pop(token, None)
        if not exp or exp < time.time():
            return None
        sid = secrets.token_urlsafe(24)
        self.sessions[sid] = (time.time() + SESSION_TTL_S, secrets.token_urlsafe(16))
        return sid

    def form_token(self, sid: str | None) -> str | None:
        got = self.sessions.get(sid or "")
        if not got or got[0] < time.time():
            return None
        return got[1]


def host_ok(request: Request, port: int) -> bool:
    return request.headers.get("host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Overshoulder settings</title>
<link rel="icon" href="{favicon}">
<style>
  :root {{ --ink:{ink}; --muted:#6b7280; --line:#e5e7eb; --accent:{accent}; --bg:{paper}; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; font:15px/1.5 "Segoe UI",system-ui,sans-serif; color:var(--ink); background:var(--bg); }}
  main {{ max-width:720px; margin:40px auto; padding:0 20px; }}
  h1 {{ font-size:22px; margin:0 0 4px; display:flex; align-items:center; gap:12px; }}
  h1 .mark svg {{ display:block; width:34px; height:34px; }}
  .sub {{ color:var(--muted); margin:0 0 28px; }}
  section {{ background:#fff; border:1px solid var(--line); border-radius:12px; padding:20px 22px; margin-bottom:18px; }}
  h2 {{ font-size:15px; margin:0 0 4px; }}
  .hint {{ color:var(--muted); font-size:13px; margin:0 0 14px; }}
  label.row {{ display:flex; gap:10px; align-items:flex-start; padding:6px 0; }}
  label.row input {{ margin-top:4px; }}
  .field {{ display:grid; grid-template-columns:220px 1fr; gap:12px; align-items:center; padding:6px 0; }}
  input[type=number], input[type=text] {{ width:100%; padding:7px 10px; border:1px solid var(--line); border-radius:8px; font:inherit; }}
  button {{ background:var(--accent); color:#fff; border:0; border-radius:8px; padding:9px 18px; font:inherit; font-weight:600; cursor:pointer; }}
  .saved {{ background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46; padding:10px 14px; border-radius:8px; margin-bottom:18px; }}
  code {{ background:#f1f5f9; padding:1px 5px; border-radius:4px; }}
  ul.plain {{ margin:0; padding-left:18px; }}
</style></head>
<body><main>
<h1><span class="mark" aria-hidden="true">{mark}</span>Overshoulder</h1>
<p class="sub">Settings for this PC. Changes apply to the next action or wait an agent starts.</p>
{saved}
<form method="post" action="/settings">
<input type="hidden" name="form_token" value="{form_token}">
<section>
  <h2>Your mouse and keyboard</h2>
  <p class="hint">Agents work on their window in the background first, so your cursor stays put and the app
  you are using keeps the keyboard. Some apps only respond to a real mouse or keyboard. Choose when an
  agent may use yours for those.</p>
  {input_modes}
  <div class="field"><label for="idle">Idle time before borrowing (seconds)</label>
    <input id="idle" type="number" name="borrow_idle_s" min="1" max="120" value="{borrow_idle_s}"></div>
</section>
<section>
  <h2>Automatic detection</h2>
  <p class="hint">Lets an agent that is waiting on you notice when you have finished, without you typing anything.
  Detection only reads window titles, on-screen controls and files. It never clicks or types.</p>
  <label class="row"><input type="checkbox" name="waits_enabled" {waits_enabled}>
    <span><b>Allow agents to wait for your actions</b></span></label>
</section>
<section>
  <h2>What agents may watch for</h2>
  <p class="hint">Untick anything you don't want agents to watch.</p>
  {kinds}
</section>
<section>
  <h2>Limits</h2>
  <div class="field"><label for="poll">Check every (milliseconds)</label>
    <input id="poll" type="number" name="poll_interval_ms" min="100" max="10000" step="100" value="{poll_interval_ms}"></div>
  <div class="field"><label for="max">Longest wait (minutes)</label>
    <input id="max" type="number" name="max_wait_minutes" min="1" max="1440" value="{max_wait_minutes}"></div>
  <div class="field"><label for="procs">Only watch these apps</label>
    <input id="procs" type="text" name="wait_processes" placeholder="any app, or e.g. chrome.exe, notepad.exe" value="{wait_processes}"></div>
  <label class="row"><input type="checkbox" name="beep_on_detect" {beep_on_detect}>
    <span>Play a sound when an agent detects that you finished</span></label>
</section>
<p><button type="submit">Save</button></p>
</form>
<section>
  <h2>Agents</h2>
  <p class="hint">Manage with <code>overshoulder client add|remove NAME</code>; limit one with
    <code>overshoulder client limit NAME --read-only --no-typing --deny-process chrome.exe</code>.</p>
  <ul class="plain">{clients}</ul>
</section>
<section>
  <h2>Safety</h2>
  <ul class="plain">
    <li>Panic key: <b>Ctrl+Alt+P</b> stops the driving agent; resume with <code>overshoulder resume</code>.</li>
    <li>While you use your mouse or keyboard, agents keep to background work, and anything that needs your
      input waits until you have been idle. Touching them during such an action stops it.</li>
    <li>Action log: <code>{logs}</code></li>
  </ul>
</section>
</main></body></html>"""


def _client_lines(registry) -> list[str]:
    from overshoulder.gateway.limits import Limits
    out = []
    for name in registry.names():
        lim = Limits.from_dict(registry.get(name).limits)
        out.append(f"{name}: {lim.describe()}" if lim else name)
    return out


def render_page(s: st.Settings, form_token: str, clients: list[str], logs: str, saved: bool) -> str:
    kinds = "\n".join(
        f'<label class="row"><input type="checkbox" name="kind_{k}" {"checked" if k in s.wait_kinds else ""}>'
        f"<span>{html.escape(desc)}</span></label>" for k, desc in st.WAIT_KINDS.items())
    modes = "\n".join(
        f'<label class="row"><input type="radio" name="input_mode" value="{m}" '
        f'{"checked" if m == s.input_mode else ""}><span><b>{html.escape(label)}</b>'
        f'{" (default)" if m == st.DEFAULT_INPUT_MODE else ""}<br>'
        f'<span class="hint">{html.escape(desc)}</span></span></label>'
        for m, (label, desc) in st.INPUT_MODES.items())
    return PAGE.format(
        favicon=html.escape(brand.mark_data_uri()), mark=brand.MARK_SVG,
        ink=brand.INK, accent=brand.AGENT_BLUE, paper=brand.PAPER,
        saved='<div class="saved">Saved.</div>' if saved else "",
        form_token=html.escape(form_token),
        input_modes=modes,
        borrow_idle_s=s.borrow_idle_s,
        waits_enabled="checked" if s.waits_enabled else "",
        kinds=kinds,
        poll_interval_ms=s.poll_interval_ms,
        max_wait_minutes=s.max_wait_minutes,
        wait_processes=html.escape(", ".join(s.wait_processes)),
        beep_on_detect="checked" if s.beep_on_detect else "",
        clients="".join(f"<li>{html.escape(c)}</li>" for c in clients) or "<li>none yet</li>",
        logs=html.escape(logs))


def settings_from_form(form) -> st.Settings:
    def num(name: str, default: int) -> int:
        try:
            return int(form.get(name, default))
        except (TypeError, ValueError):
            return default

    return st.Settings(
        waits_enabled=form.get("waits_enabled") == "on",
        wait_kinds=[k for k in st.WAIT_KINDS if form.get(f"kind_{k}") == "on"],
        poll_interval_ms=num("poll_interval_ms", 500),
        max_wait_minutes=num("max_wait_minutes", 60),
        beep_on_detect=form.get("beep_on_detect") == "on",
        wait_processes=[p for p in str(form.get("wait_processes", "")).replace(";", ",").split(",")],
        input_mode=str(form.get("input_mode", st.DEFAULT_INPUT_MODE)),
        borrow_idle_s=num("borrow_idle_s", 5),
    ).clamp()


def routes(mcp, port_ref: dict, sessions: Sessions, registry, logs_dir: str) -> None:
    """Attach /settings handlers to an MCPServer. port_ref["port"] is filled in at serve time."""

    def guard(request: Request) -> Response | None:
        if not host_ok(request, port_ref["port"]):
            return Response("forbidden", status_code=403)
        return None

    @mcp.custom_route("/settings", methods=["GET"])
    async def settings_get(request: Request):
        if (bad := guard(request)) is not None:
            return bad
        token = request.query_params.get("t")
        if token:
            sid = sessions.redeem(token)
            if not sid:
                return HTMLResponse("This link has expired. Run <code>overshoulder settings</code> again.", 403)
            resp = RedirectResponse("/settings", status_code=303)
            resp.set_cookie(COOKIE, sid, httponly=True, samesite="strict", max_age=SESSION_TTL_S)
            return resp
        form_token = sessions.form_token(request.cookies.get(COOKIE))
        if not form_token:
            return HTMLResponse("Open this page with <code>overshoulder settings</code>.", 403)
        return HTMLResponse(render_page(st.load(), form_token, _client_lines(registry), logs_dir,
                                        saved=request.query_params.get("saved") == "1"))

    @mcp.custom_route("/settings", methods=["POST"])
    async def settings_post(request: Request):
        if (bad := guard(request)) is not None:
            return bad
        form = await request.form()
        expected = sessions.form_token(request.cookies.get(COOKIE))
        if not expected or not secrets.compare_digest(str(form.get("form_token", "")), expected):
            return HTMLResponse("Session expired. Run <code>overshoulder settings</code> again.", 403)
        st.save(settings_from_form(form))
        return RedirectResponse("/settings?saved=1", status_code=303)
