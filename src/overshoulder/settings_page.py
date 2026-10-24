"""The local settings page, served by the gateway at /settings.

Opened only through `overshoulder settings`, which asks the gateway (with the operator key) for a
one-time link. Visiting the link swaps it for a session cookie (HttpOnly, SameSite=Strict); every
save also carries a per-session form token. Requests whose Host header is not loopback are refused.
"""

from __future__ import annotations

import html
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from overshoulder import brand, phone, share
from overshoulder import settings as st
from overshoulder.osp import MAX_BYTES, OspError
from overshoulder.vision import VISION_MODES

LINK_TTL_S = 15 * 60
SESSION_TTL_S = 8 * 3600
COOKIE = "overshoulder_settings"
# Where a one-time link may lead after it is redeemed: the import page for a file waiting in the inbox
# (`overshoulder profile open FILE`, which an .osp file opens with).
NEXT_RE = re.compile(r"/settings/profiles/import\?p=[A-Za-z0-9_-]{16,64}")
MESSAGES = {"imported": "Imported {n} items into the profile of {app}.", "removed": "Removed the profile of {app}.",
            "cancelled": "Nothing was imported."}


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
  input[type=number], input[type=text], input[type=password] {{ width:100%; padding:7px 10px; border:1px solid var(--line); border-radius:8px; font:inherit; }}
  button {{ background:var(--accent); color:#fff; border:0; border-radius:8px; padding:9px 18px; font:inherit; font-weight:600; cursor:pointer; }}
  .saved {{ background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46; padding:10px 14px; border-radius:8px; margin-bottom:18px; }}
  code {{ background:#f1f5f9; padding:1px 5px; border-radius:4px; }}
  ul.plain {{ margin:0; padding-left:18px; }}
  .secret {{ font-family:Consolas,monospace; font-size:13px; word-break:break-all; }}
  .result {{ background:#eff6ff; border:1px solid #bfdbfe; padding:10px 14px; border-radius:8px; margin-bottom:12px; }}
  table.profiles {{ width:100%; border-collapse:collapse; font-size:14px; margin-bottom:14px; }}
  table.profiles td {{ border-top:1px solid var(--line); padding:7px 6px; vertical-align:top; }}
  table.profiles form {{ display:inline; }}
  button.small {{ padding:4px 10px; font-weight:500; font-size:13px; }}
  button.quiet {{ background:#fff; color:var(--ink); border:1px solid var(--line); }}
  a.button {{ display:inline-block; padding:4px 10px; border:1px solid var(--line); border-radius:8px; color:var(--ink);
    text-decoration:none; font-size:13px; }}
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
  <h2>Finding controls</h2>
  <p class="hint">Some apps draw their own buttons, so Windows can't tell an agent what they are. Reading
  the text on screen lets an agent find a button by its label. The text is read on this PC with the OCR
  built into Windows, only in the window the agent has been given, and nothing is sent anywhere.</p>
  <label class="row"><input type="checkbox" name="ocr_enabled" {ocr_enabled}>
    <span>Let agents read text on screen to find controls</span></label>
  <p class="hint">An icon without text can be found by asking a vision model where it is, for example
  "the gear icon". The model sees the agent's window.</p>
  {vision_modes}
  <div class="field"><label for="vision_local_model">Model on this PC (Ollama)</label>
    <input id="vision_local_model" type="text" name="vision_local_model" value="{vision_local_model}"></div>
  <div class="field"><label for="vision_local_url">Ollama address</label>
    <input id="vision_local_url" type="text" name="vision_local_url" value="{vision_local_url}"></div>
  <div class="field"><label for="vision_cloud_url">Cloud address (OpenAI-compatible)</label>
    <input id="vision_cloud_url" type="text" name="vision_cloud_url" value="{vision_cloud_url}"
      placeholder="https://.../v1"></div>
  <div class="field"><label for="vision_cloud_model">Cloud model</label>
    <input id="vision_cloud_model" type="text" name="vision_cloud_model" value="{vision_cloud_model}"></div>
  <div class="field"><label for="vision_cloud_key">Cloud API key</label>
    <input id="vision_cloud_key" type="password" name="vision_cloud_key" value="" autocomplete="off"
      placeholder="{vision_cloud_key_hint}"></div>
</section>
<section>
  <h2>App profiles</h2>
  <p class="hint">Overshoulder keeps a profile of each app agents use: its windows and controls, which ways of
  pressing and typing work in it, its shortcuts, and notes agents leave. The next agent starts from it and
  works faster. A profile never holds window titles, what was typed or what a field showed, and it never
  lets an agent do more than you allow.</p>
  <label class="row"><input type="checkbox" name="learn_profiles" {learn_profiles}>
    <span>Learn app profiles and use them</span></label>
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
<section>
  <h2>Approvals on your phone</h2>
  <p class="hint">When an agent asks to do something consequential, a box on this PC asks you. You can be asked
  on your phone at the same time: the first answer counts, and no answer in time is No. Your phone is told
  what the agent wants to do, the program's name and when the question expires, and nothing else unless you
  tick the boxes at the bottom. This PC only sends requests out; nothing on it listens for the phone.</p>
  <label class="row"><input type="checkbox" name="ntfy_enabled" {ntfy_enabled}>
    <span><b>Ask on my phone with ntfy</b><br><span class="hint">Install the ntfy app and subscribe to the topic
    below on the same server. Anyone who knows the topic can read the questions, so keep it to yourself.</span></span></label>
  <div class="field"><label for="ntfy_server">ntfy server</label>
    <input id="ntfy_server" type="text" name="ntfy_server" value="{ntfy_server}"></div>
  <div class="field"><label>Topic to subscribe to</label><span class="secret">{ntfy_topic}</span></div>
  <div class="field"><label for="ntfy_token">Access token (only if your server needs one)</label>
    <input id="ntfy_token" type="password" name="ntfy_token" value="{ntfy_token}" autocomplete="off"></div>
  <label class="row"><input type="checkbox" name="renew_topics">
    <span>Make new topics (your phone has to subscribe again)</span></label>
  <label class="row"><input type="checkbox" name="webhook_enabled" {webhook_enabled}>
    <span><b>Ask through a webhook</b><br><span class="hint">For your own server or another app. Questions and
    answers are signed with the secret below; the README describes the format.</span></span></label>
  <div class="field"><label for="webhook_url">Webhook URL</label>
    <input id="webhook_url" type="text" name="webhook_url" value="{webhook_url}" placeholder="https://..."></div>
  <div class="field"><label for="webhook_poll_url">Answer URL (if not the same)</label>
    <input id="webhook_poll_url" type="text" name="webhook_poll_url" value="{webhook_poll_url}"></div>
  <div class="field"><label>Webhook secret</label><span class="secret">{webhook_secret}</span></div>
  <div class="field"><label for="phone_timeout">Time to answer (seconds)</label>
    <input id="phone_timeout" type="number" name="phone_timeout_s" min="10" max="600" value="{phone_timeout_s}"></div>
  <label class="row"><input type="checkbox" name="share_title" {share_title}>
    <span>Also send the window's title</span></label>
  <label class="row"><input type="checkbox" name="share_screenshot" {share_screenshot}>
    <span>Also send a screenshot of the window</span></label>
</section>
<p><button type="submit">Save</button></p>
</form>
<form method="post" action="/settings/phone-test">
<input type="hidden" name="form_token" value="{form_token}">
<section>
  <h2>Test the phone</h2>
  <p class="hint">Sends a test question to the phone channels saved above and waits for your answer. The box on
  this PC is not shown.</p>
  {phone_test}
  <button type="submit">Send a test question</button>
</section>
</form>
<section>
  <h2>Agents</h2>
  <p class="hint">Manage with <code>overshoulder client add|remove NAME</code>; limit one with
    <code>overshoulder client limit NAME --read-only --no-typing --deny-process chrome.exe</code>.</p>
  <ul class="plain">{clients}</ul>
</section>
{profiles}
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


def _test_result(r: dict | None) -> str:
    if not r:
        return ""
    if r.get("answer") is True:
        text = f"Allowed on {r['by']} after {r.get('seconds', '?')} s."
    elif r.get("answer") is False:
        text = f"Denied on {r['by']} after {r.get('seconds', '?')} s."
    else:
        text = "No answer arrived in time."
    errs = "".join(f"<br>{html.escape(k)}: {html.escape(str(v))}" for k, v in (r.get("errors") or {}).items())
    return f'<div class="result">{html.escape(text)}{errs}</div>'


def _profiles_section(rows: list[dict], form_token: str) -> str:
    ft = html.escape(form_token)
    lines = []
    for r in rows:
        pid = html.escape(r["id"])
        if "error" in r:
            lines.append(f"<tr><td><code>{pid}</code><br><span class=\"hint\">can't be read: "
                         f"{html.escape(r['error'])}</span></td><td></td></tr>")
            continue
        ver = f", version {html.escape(r['version'])}" if r.get("version") else ""
        lines.append(
            f"<tr><td><b>{html.escape(str(r['name']))}</b><br><span class=\"hint\"><code>{pid}</code>{ver}, "
            f"revision {r['rev']}, {r['items']} items</span></td><td style=\"white-space:nowrap\">"
            f'<a class="button" href="/settings/profiles/export?id={quote(r["id"])}&amp;ft={quote(form_token)}">'
            "Export</a> "
            f'<form method="post" action="/settings/profiles/remove" '
            f"onsubmit=\"return confirm('Remove this profile? Its last revision is kept in its history folder.')\">"
            f'<input type="hidden" name="form_token" value="{ft}"><input type="hidden" name="id" value="{pid}">'
            '<button class="small quiet" type="submit">Remove</button></form></td></tr>')
    table = f'<table class="profiles">{"".join(lines)}</table>' if lines else \
        '<p class="hint">No profiles yet. One is made the first time an agent works in an app.</p>'
    return f"""<section>
  <h2>Profiles on this PC</h2>
  <p class="hint">Export saves a copy to share. It leaves out what only this PC needs and anything seen just
  once. Importing someone else's profile adds what this PC's profile lacks; what this PC learned stays.</p>
  {table}
  <form method="post" action="/settings/profiles/import" enctype="multipart/form-data">
    <input type="hidden" name="form_token" value="{ft}">
    <input type="file" name="file" accept=".osp" required> <button class="small" type="submit">Import...</button>
  </form>
</section>"""


def render_page(s: st.Settings, form_token: str, clients: list[str], logs: str, saved: bool,
                ph: phone.PhoneSettings | None = None, phone_test: dict | None = None,
                profiles: list[dict] | None = None, message: str = "") -> str:
    ph = ph or phone.PhoneSettings().ensure_secrets()
    kinds = "\n".join(
        f'<label class="row"><input type="checkbox" name="kind_{k}" {"checked" if k in s.wait_kinds else ""}>'
        f"<span>{html.escape(desc)}</span></label>" for k, desc in st.WAIT_KINDS.items())
    modes = "\n".join(
        f'<label class="row"><input type="radio" name="input_mode" value="{m}" '
        f'{"checked" if m == s.input_mode else ""}><span><b>{html.escape(label)}</b>'
        f'{" (default)" if m == st.DEFAULT_INPUT_MODE else ""}<br>'
        f'<span class="hint">{html.escape(desc)}</span></span></label>'
        for m, (label, desc) in st.INPUT_MODES.items())
    vision_modes = "\n".join(
        f'<label class="row"><input type="radio" name="vision_mode" value="{m}" '
        f'{"checked" if m == s.vision_mode else ""}><span><b>{html.escape(label)}</b>'
        f'{" (default)" if m == "off" else ""}<br><span class="hint">{html.escape(desc)}</span></span></label>'
        for m, (label, desc) in VISION_MODES.items())
    return PAGE.format(
        favicon=html.escape(brand.mark_data_uri()), mark=brand.MARK_SVG,
        ink=brand.INK, accent=brand.AGENT_BLUE, paper=brand.PAPER,
        saved=('<div class="saved">Saved.</div>' if saved else "")
        + (f'<div class="saved">{html.escape(message)}</div>' if message else ""),
        learn_profiles="checked" if s.learn_profiles else "",
        profiles=_profiles_section(profiles or [], form_token),
        form_token=html.escape(form_token),
        input_modes=modes,
        borrow_idle_s=s.borrow_idle_s,
        ocr_enabled="checked" if s.ocr_enabled else "",
        vision_modes=vision_modes,
        vision_local_model=html.escape(s.vision_local_model),
        vision_local_url=html.escape(s.vision_local_url),
        vision_cloud_url=html.escape(s.vision_cloud_url),
        vision_cloud_model=html.escape(s.vision_cloud_model),
        vision_cloud_key_hint="saved; leave empty to keep it" if s.vision_cloud_key else "if the service needs one",
        waits_enabled="checked" if s.waits_enabled else "",
        kinds=kinds,
        poll_interval_ms=s.poll_interval_ms,
        max_wait_minutes=s.max_wait_minutes,
        wait_processes=html.escape(", ".join(s.wait_processes)),
        beep_on_detect="checked" if s.beep_on_detect else "",
        clients="".join(f"<li>{html.escape(c)}</li>" for c in clients) or "<li>none yet</li>",
        logs=html.escape(logs),
        ntfy_enabled="checked" if ph.ntfy_enabled else "",
        ntfy_server=html.escape(ph.ntfy_server),
        ntfy_topic=html.escape(ph.ntfy_topic),
        ntfy_token=html.escape(ph.ntfy_token),
        webhook_enabled="checked" if ph.webhook_enabled else "",
        webhook_url=html.escape(ph.webhook_url),
        webhook_poll_url=html.escape(ph.webhook_poll_url),
        webhook_secret=html.escape(ph.webhook_secret),
        phone_timeout_s=ph.timeout_s,
        share_title="checked" if ph.share_title else "",
        share_screenshot="checked" if ph.share_screenshot else "",
        phone_test=_test_result(phone_test))


def settings_from_form(form, current: st.Settings | None = None) -> st.Settings:
    """The settings the form asks for. A saved API key is kept when its field is left empty, since the page
    never shows it."""
    current = current or st.Settings()

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
        ocr_enabled=form.get("ocr_enabled") == "on",
        vision_mode=str(form.get("vision_mode", current.vision_mode)),
        vision_local_url=str(form.get("vision_local_url", current.vision_local_url)),
        vision_local_model=str(form.get("vision_local_model", current.vision_local_model)),
        vision_cloud_url=str(form.get("vision_cloud_url", current.vision_cloud_url)),
        vision_cloud_model=str(form.get("vision_cloud_model", current.vision_cloud_model)),
        vision_cloud_key=str(form.get("vision_cloud_key") or current.vision_cloud_key),
        learn_profiles=form.get("learn_profiles") == "on",
    ).clamp()


def phone_from_form(form, current: phone.PhoneSettings) -> phone.PhoneSettings:
    """The phone settings the form asks for. Topics and secrets are kept unless renewal is ticked."""
    try:
        timeout = int(form.get("phone_timeout_s", current.timeout_s))
    except (TypeError, ValueError):
        timeout = current.timeout_s
    s = phone.PhoneSettings(
        ntfy_enabled=form.get("ntfy_enabled") == "on",
        ntfy_server=str(form.get("ntfy_server", current.ntfy_server)),
        ntfy_topic=current.ntfy_topic, ntfy_reply_topic=current.ntfy_reply_topic,
        ntfy_token=str(form.get("ntfy_token", current.ntfy_token)),
        webhook_enabled=form.get("webhook_enabled") == "on",
        webhook_url=str(form.get("webhook_url", current.webhook_url)),
        webhook_poll_url=str(form.get("webhook_poll_url", current.webhook_poll_url)),
        webhook_secret=current.webhook_secret, answer_key=current.answer_key,
        share_title=form.get("share_title") == "on",
        share_screenshot=form.get("share_screenshot") == "on",
        timeout_s=timeout)
    return s.ensure_secrets(renew_topics=form.get("renew_topics") == "on").clamp()


IMPORT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Import a profile - Overshoulder</title><link rel="icon" href="{favicon}">
<style>
  body {{ margin:0; font:15px/1.5 "Segoe UI",system-ui,sans-serif; color:{ink}; background:{paper}; }}
  main {{ max-width:720px; margin:40px auto; padding:0 20px; }}
  section {{ background:#fff; border:1px solid #e5e7eb; border-radius:12px; padding:20px 22px; margin-bottom:18px; }}
  .hint {{ color:#6b7280; font-size:13px; }}
  .bad {{ background:#fef2f2; border:1px solid #fecaca; color:#991b1b; padding:10px 14px; border-radius:8px; }}
  button {{ background:{accent}; color:#fff; border:0; border-radius:8px; padding:9px 18px; font:inherit; font-weight:600; cursor:pointer; }}
  button.quiet {{ background:#fff; color:{ink}; border:1px solid #e5e7eb; }}
  form {{ display:inline; }} code {{ background:#f1f5f9; padding:1px 5px; border-radius:4px; }}
  li {{ margin-bottom:6px; }}
</style></head><body><main>
<h1 style="font-size:22px">Import a profile</h1>
{body}
</main></body></html>"""


# The .osp item kinds in the words the import page uses.
KIND_WORDS = {"view": "windows", "ctl": "controls", "way": "methods that work", "key": "shortcuts",
              "paint": "painted controls", "quirk": "quirk", "how": "how-to", "note": "guide note"}


def render_import(preview: dict | None, error: str, key: str, form_token: str, local_rev: int) -> str:
    ft, k = html.escape(form_token), html.escape(key)
    cancel = (f'<form method="post" action="/settings/profiles/import/cancel"><input type="hidden" name="form_token" '
              f'value="{ft}"><input type="hidden" name="p" value="{k}"><button class="quiet" type="submit">'
              f'{"Close" if error else "Cancel"}</button></form>')
    if error:
        body = (f'<div class="bad">This file can\'t be imported: {html.escape(error)}</div>'
                f'<p class="hint">Nothing was changed.</p>{cancel}')
    else:
        kinds = ", ".join(f"{n} {html.escape(KIND_WORDS.get(k_, k_))}" for k_, n in sorted(preview["kinds"].items()))
        notes = "".join(
            f"<li><b>{html.escape(KIND_WORDS.get(x['kind'], x['kind']))}</b>: {html.escape(x['text'])}"
            + (f"<br><span class=\"hint\">{html.escape(' ; '.join(x['steps']))}</span>" if x["steps"] else "")
            + (f"<br><span class=\"hint\">{html.escape(x['url'])}</span>" if x["url"] else "") + "</li>"
            for x in preview["notes"])
        here = (f"This PC has a profile of this app (revision {local_rev}); what it has stays as it is."
                if local_rev else "This PC has no profile of this app yet.")
        made = html.escape(preview["made_with"] or "an unknown program")
        body = f"""<section>
  <p><b>{html.escape(preview['name'])}</b> <code>{html.escape(preview['app'])}</code>
  {html.escape(preview['version'])}</p>
  <p>{preview['items']} items: {kinds or 'none'}. Made with {made}.</p>
  <p class="hint">{here} New items are marked as imported. They help agents find their way in the app and
  never let an agent do more than you allow.</p>
</section>
{f'<section><p><b>Notes written by other agents</b></p><p class="hint">Agents read these as information, never as instructions.</p><ul>{notes}</ul></section>' if notes else ''}
<form method="post" action="/settings/profiles/import/confirm"><input type="hidden" name="form_token" value="{ft}">
<input type="hidden" name="p" value="{k}"><button type="submit">Import</button></form> {cancel}"""
    return IMPORT_PAGE.format(favicon=html.escape(brand.mark_data_uri()), ink=brand.INK, paper=brand.PAPER,
                              accent=brand.AGENT_BLUE, body=body)


def routes(mcp, port_ref: dict, sessions: Sessions, registry, logs_dir: str,
           phone_tester=None, profiles=None) -> None:
    """Attach /settings handlers to an MCPServer. port_ref["port"] is filled in at serve time."""

    last_test: dict = {}

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
            nxt = request.query_params.get("next", "")
            resp = RedirectResponse(nxt if NEXT_RE.fullmatch(nxt) else "/settings", status_code=303)
            resp.set_cookie(COOKIE, sid, httponly=True, samesite="strict", max_age=SESSION_TTL_S)
            return resp
        form_token = sessions.form_token(request.cookies.get(COOKIE))
        if not form_token:
            return HTMLResponse("Open this page with <code>overshoulder settings</code>.", 403)
        q = request.query_params
        msg = MESSAGES.get(q.get("msg", ""), "").format(n=q.get("n", "0")[:6], app=q.get("app", "")[:120])
        return HTMLResponse(render_page(st.load(), form_token, _client_lines(registry), logs_dir,
                                        saved=q.get("saved") == "1", ph=phone.load(),
                                        phone_test=last_test.get("result"),
                                        profiles=profiles.list() if profiles is not None else [], message=msg))

    @mcp.custom_route("/settings", methods=["POST"])
    async def settings_post(request: Request):
        if (bad := guard(request)) is not None:
            return bad
        form = await request.form()
        expected = sessions.form_token(request.cookies.get(COOKIE))
        if not expected or not secrets.compare_digest(str(form.get("form_token", "")), expected):
            return HTMLResponse("Session expired. Run <code>overshoulder settings</code> again.", 403)
        st.save(settings_from_form(form, st.load_or_default()))
        phone.save(phone_from_form(form, phone.load()))
        return RedirectResponse("/settings?saved=1", status_code=303)

    @mcp.custom_route("/settings/phone-test", methods=["POST"])
    async def phone_test(request: Request):
        import anyio

        from overshoulder import approvals
        if (bad := guard(request)) is not None:
            return bad
        form = await request.form()
        expected = sessions.form_token(request.cookies.get(COOKIE))
        if not expected or not secrets.compare_digest(str(form.get("form_token", "")), expected):
            return HTMLResponse("Session expired. Run <code>overshoulder settings</code> again.", 403)
        s = phone.load()
        tester = phone_tester or approvals.try_phone
        last_test["result"] = await anyio.to_thread.run_sync(lambda: tester(s, s.timeout_s))
        return RedirectResponse("/settings", status_code=303)

    async def checked_form(request: Request):
        """The posted form, or a refusal when the host or the session's form token is wrong."""
        if (bad := guard(request)) is not None:
            return None, bad
        form = await request.form(max_part_size=MAX_BYTES + 1024)
        expected = sessions.form_token(request.cookies.get(COOKIE))
        if not expected or not secrets.compare_digest(str(form.get("form_token", "")), expected):
            return None, HTMLResponse("Session expired. Run <code>overshoulder settings</code> again.", 403)
        return form, None

    @mcp.custom_route("/settings/profiles/export", methods=["GET"])
    async def profile_export(request: Request):
        if (bad := guard(request)) is not None:
            return bad
        expected = sessions.form_token(request.cookies.get(COOKIE))
        if not expected or not secrets.compare_digest(request.query_params.get("ft", ""), expected):
            return HTMLResponse("Session expired. Run <code>overshoulder settings</code> again.", 403)
        try:
            prof = profiles.load(request.query_params.get("id", ""))
        except OspError as e:
            return HTMLResponse(f"That profile can't be read: {html.escape(str(e))}", 400)
        if prof is None:
            return HTMLResponse("No such profile.", 404)
        shared, _ = share.export(prof)
        return Response(shared.dumps().encode("utf-8"), media_type="application/octet-stream",
                        headers={"content-disposition": f'attachment; filename="{prof.app_id}.osp"'})

    @mcp.custom_route("/settings/profiles/remove", methods=["POST"])
    async def profile_remove(request: Request):
        form, bad = await checked_form(request)
        if bad is not None:
            return bad
        app_id = str(form.get("id", ""))
        try:
            removed = profiles.remove(app_id)
        except OspError:
            removed = False
        return RedirectResponse(f"/settings?msg=removed&app={quote(app_id)}" if removed else "/settings",
                                status_code=303)

    @mcp.custom_route("/settings/profiles/import", methods=["POST"])
    async def profile_upload(request: Request):
        form, bad = await checked_form(request)
        if bad is not None:
            return bad
        f = form.get("file")
        data = await f.read(MAX_BYTES + 1) if hasattr(f, "read") else b""
        key = profiles.inbox_put(data[:MAX_BYTES + 1])
        return RedirectResponse(f"/settings/profiles/import?p={key}", status_code=303)

    @mcp.custom_route("/settings/profiles/import", methods=["GET"])
    async def profile_preview(request: Request):
        if (bad := guard(request)) is not None:
            return bad
        form_token = sessions.form_token(request.cookies.get(COOKIE))
        if not form_token:
            return HTMLResponse("Open this page with <code>overshoulder settings</code>.", 403)
        key = request.query_params.get("p", "")
        data = profiles.inbox_get(key)
        if data is None:
            return HTMLResponse("That file is no longer waiting to be imported.", 404)
        preview, error, local_rev = None, "", 0
        try:
            other = share.read(data)
            preview = share.preview(other)
            local = profiles.load(other.app_id)
            local_rev = local.revision if local else 0
        except OspError as e:
            error = str(e)
        return HTMLResponse(render_import(preview, error, key, form_token, local_rev))

    @mcp.custom_route("/settings/profiles/import/confirm", methods=["POST"])
    async def profile_import(request: Request):
        form, bad = await checked_form(request)
        if bad is not None:
            return bad
        key = str(form.get("p", ""))
        data = profiles.inbox_get(key)
        if data is None:
            return HTMLResponse("That file is no longer waiting to be imported.", 404)
        try:
            other = share.read(data)
            res = share.import_profile(profiles, other)
        except OspError as e:
            return HTMLResponse(render_import(None, str(e), key, sessions.form_token(request.cookies.get(COOKIE))
                                              or "", 0))
        if "error" in res:
            return HTMLResponse(render_import(None, res["error"], key, str(form.get("form_token", "")), 0))
        profiles.inbox_drop(key)
        return RedirectResponse(f"/settings?msg=imported&n={res.get('imported', 0)}&app={quote(other.app_id)}",
                                status_code=303)

    @mcp.custom_route("/settings/profiles/import/cancel", methods=["POST"])
    async def profile_import_cancel(request: Request):
        form, bad = await checked_form(request)
        if bad is not None:
            return bad
        profiles.inbox_drop(str(form.get("p", "")))
        return RedirectResponse("/settings?msg=cancelled", status_code=303)
