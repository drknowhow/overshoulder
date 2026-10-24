"""Approvals on the person's phone: ntfy and a generic webhook, beside the box on the desk.

Both channels only make outbound requests, so nothing listens on this PC. A question carries a one-time
nonce; an answer counts only if it names the pending nonce and carries an HMAC of it under a key that
never leaves this PC (ntfy) or the shared webhook secret. What leaves the PC is the reason (the action
and the control it is on), the program's name and when the question expires; the window title and a
screenshot only when the person turns them on.

ntfy: the question goes to a topic the person subscribes to in the ntfy app, with Allow and Deny
buttons. Each button POSTs `<nonce>.<yes|no>.<mac>` to a second, secret reply topic, which this PC
reads as a stream. Allow does not count yet: a second notification with its own nonce asks the person
to confirm, with Cancel where Allow was, and only Confirm approves. A tap that lands on the wrong
button can then refuse an action but not approve one. Both titles name the action ("Allow click on
'Send'?") and the Confirm button names the control ("Confirm Send"), so two questions that arrive
close together don't look alike. When a question is answered anywhere or
expires, its notification is deleted from the phone (ntfy's sequence IDs; servers too old for them
ignore it).

Webhook: the question is POSTed as JSON, signed with `X-Overshoulder-Signature: sha256=<hex>` over the
body. The receiver answers in that response or later at the poll URL (GET `?id=<nonce>`), as JSON
`{"id", "answer": "yes"|"no", "mac": hex HMAC-SHA256(secret, "<id>.<answer>")}`. Anything else means
"not yet". When the question closes, a signed `overshoulder.approval.closed` is POSTed.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import io
import json
import logging
import queue
import re
import secrets
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from overshoulder.gateway.clients import default_home

log = logging.getLogger(__name__)

NTFY_DEFAULT = "https://ntfy.sh"
TITLE_MAX = 60  # characters of the reason a title shows
ANSWERS = {"yes": True, "no": False}


@dataclass
class PhoneSettings:
    ntfy_enabled: bool = False
    ntfy_server: str = NTFY_DEFAULT  # or a self-hosted ntfy
    ntfy_topic: str = ""  # the person subscribes to this one in the ntfy app
    ntfy_reply_topic: str = ""  # the phone's Allow/Deny taps are posted here
    ntfy_token: str = ""  # access token, for a server that needs one
    webhook_enabled: bool = False
    webhook_url: str = ""
    webhook_poll_url: str = ""  # where answers are read; empty means webhook_url
    webhook_secret: str = ""
    answer_key: str = ""  # HMAC key for ntfy answers; never sent anywhere
    share_title: bool = False
    share_screenshot: bool = False
    timeout_s: int = 60

    def ensure_secrets(self, renew_topics: bool = False) -> PhoneSettings:
        if renew_topics or not self.ntfy_topic:
            self.ntfy_topic = "overshoulder-" + secrets.token_urlsafe(18)
        if renew_topics or not self.ntfy_reply_topic:
            self.ntfy_reply_topic = "overshoulder-r-" + secrets.token_urlsafe(18)
        if not self.answer_key:
            self.answer_key = secrets.token_urlsafe(32)
        if not self.webhook_secret:
            self.webhook_secret = secrets.token_urlsafe(32)
        return self

    def clamp(self) -> PhoneSettings:
        self.ntfy_server = (self.ntfy_server or NTFY_DEFAULT).strip().rstrip("/")
        if not self.ntfy_server.startswith(("https://", "http://")):
            self.ntfy_server = "https://" + self.ntfy_server
        self.webhook_url = self.webhook_url.strip()
        self.webhook_poll_url = self.webhook_poll_url.strip()
        self.ntfy_token = self.ntfy_token.strip()
        self.timeout_s = max(10, min(int(self.timeout_s), 600))
        return self

    @property
    def channels(self) -> list[str]:
        return [n for n, on in (("ntfy", self.ntfy_enabled), ("webhook", self.webhook_enabled and self.webhook_url))
                if on]


def phone_path() -> Path:
    return default_home() / "phone.json"


def load(path: Path | None = None) -> PhoneSettings:
    """The phone settings, with their secrets made on first use. Unreadable means off."""
    p = path or phone_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
        known = {f.name for f in fields(PhoneSettings)}
        s = PhoneSettings(**{k: v for k, v in raw.items() if k in known}).clamp()
    except (OSError, ValueError, TypeError):
        return PhoneSettings()
    if not (s.ntfy_topic and s.ntfy_reply_topic and s.answer_key and s.webhook_secret):
        save(s.ensure_secrets(), p)
    return s


def save(s: PhoneSettings, path: Path | None = None) -> Path:
    p = path or phone_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s.clamp()), indent=2), encoding="utf-8")
    tmp.replace(p)
    return p


# -- the question -----------------------------------------------------------------------------------

@dataclass
class Question:
    id: str  # the one-time nonce
    reason: str  # the action and the control, e.g. "click on 'Send'"
    process: str
    expires_at: float
    request: dict = field(default_factory=dict)  # the gateway's request, which stays on this PC
    title: str = ""  # only when the person shares window titles
    screenshot: bytes | None = None  # PNG, only when the person shares screenshots


def make_question(request: dict, s: PhoneSettings, timeout_s: float, now: float | None = None) -> Question:
    hwnd = int(request.get("hwnd") or 0)
    title, shot = "", None
    if hwnd and s.share_title:
        try:
            from overshoulder.driver import windows as w
            title = w.title(hwnd)
        except Exception:  # a closed window has no title to share
            title = ""
    if s.share_screenshot and request.get("picture"):
        shot = _thumbnail(request["picture"])  # the scope around a vision box, with the box drawn
    elif hwnd and s.share_screenshot:
        shot = _screenshot(hwnd)
    return Question(id=secrets.token_urlsafe(16), reason=str(request.get("reason") or "an action"),
                    process=str(request.get("process") or ""),
                    expires_at=(now or time.time()) + timeout_s, request=request, title=title, screenshot=shot)


def _thumbnail(png: bytes) -> bytes | None:
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png))
        img.thumbnail((1280, 1280))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()
    except Exception:
        return None


def _screenshot(hwnd: int) -> bytes | None:
    try:
        from overshoulder.driver import capture
        img = capture.window_image(hwnd)
    except Exception:
        return None
    if img is None:
        return None
    img.thumbnail((1280, 1280))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def phone_text(q: Question) -> str:
    lines = [q.reason]
    if q.process:
        lines.append(f"in {q.process}")
    if q.title:
        lines.append(f"window: {q.title}")
    lines.append("expires " + time.strftime("%H:%M:%S", time.localtime(q.expires_at)))
    return "\n".join(lines)


def headline(q: Question, confirm: bool = False) -> str:
    """The notification's title: "Allow click on 'Send'?", or "Confirm click on 'Send'?" on the confirmation."""
    reason = " ".join(q.reason.split())
    if len(reason) > TITLE_MAX:
        reason = reason[:TITLE_MAX - 3] + "..."
    return f"{'Confirm' if confirm else 'Allow'} {reason}?"


def confirm_label(q: Question) -> str:
    """"Confirm Send" for a reason that names a control in quotes. Plain "Confirm" when the name is long, not
    ASCII, or has a character ntfy's button format treats as a separator."""
    m = re.search(r"'([^']+)'", q.reason)
    name = m.group(1).strip() if m else ""
    if name and len(name) <= 20 and name.isascii() and name.isprintable() and not set(name) & set(",;=\"'"):
        return f"Confirm {name}"
    return "Confirm"


def _mac(key: str, text: str) -> str:
    return hmac.new(key.encode(), text.encode(), hashlib.sha256).hexdigest()


# -- HTTP ---------------------------------------------------------------------------------------------

class ChannelError(Exception):
    pass


def _http(method: str, url: str, body: bytes | None = None, headers: dict | None = None,
          timeout: float = 10.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _header_text(s: str) -> str:
    """Header values are Latin-1 on one line; ntfy reads RFC 2047 encoded words for anything else."""
    if s.isascii() and "\n" not in s:
        return s
    return "=?UTF-8?B?" + base64.b64encode(s.encode()).decode() + "?="


# -- ntfy ---------------------------------------------------------------------------------------------

class Ntfy:
    name = "ntfy"

    def __init__(self, s: PhoneSettings, poll_s: float = 3.0):
        self.s, self.poll_s = s, poll_s
        self.last_error = ""
        self.retracted: int | None = None

    def _url(self, *parts: str) -> str:
        return "/".join([self.s.ntfy_server, *(urllib.parse.quote(p, safe="") for p in parts)])

    def _auth(self) -> dict:
        return {"Authorization": f"Bearer {self.s.ntfy_token}"} if self.s.ntfy_token else {}

    def answer_body(self, nonce: str, answer: str) -> str:
        return f"{nonce}.{answer}.{_mac(self.s.answer_key, f'{nonce}.{answer}')}"

    def verify(self, nonce: str, text: str) -> bool | None:
        """The answer in a reply message, if it is a genuine answer to this question; else None."""
        parts = str(text).strip().split(".")
        if len(parts) != 3 or parts[1] not in ANSWERS or not secrets.compare_digest(parts[0], nonce):
            return None
        if not secrets.compare_digest(parts[2], _mac(self.s.answer_key, f"{nonce}.{parts[1]}")):
            return None
        return ANSWERS[parts[1]]

    def actions(self, q: Question, confirm: bool = False) -> str:
        reply = self._url(self.s.ntfy_reply_topic)
        auth = f", headers.Authorization=Bearer {self.s.ntfy_token}" if self.s.ntfy_token else ""
        buttons = (("Cancel", "no"), (confirm_label(q), "yes")) if confirm else (("Allow", "yes"), ("Deny", "no"))
        return "; ".join(f"http, {label}, {reply}, method=POST, body={self.answer_body(q.id, a)}, clear=true{auth}"
                         for label, a in buttons)

    def publish(self, q: Question, confirm: bool = False) -> None:
        headers = {"X-Title": _header_text(headline(q, confirm)), "X-Priority": "high", "X-Tags": "warning",
                   "X-Actions": self.actions(q, confirm), "X-Sequence-ID": q.id, **self._auth()}
        text = (f"Tap {confirm_label(q)} to allow:\n{phone_text(q)}" if confirm
                else f"{phone_text(q)}\nAllow asks you to confirm")
        if q.screenshot:
            headers.update({"X-Filename": "overshoulder.png", "X-Message": _header_text(text)})
            status, body = _http("PUT", self._url(self.s.ntfy_topic), q.screenshot, headers)
        else:
            status, body = _http("POST", self._url(self.s.ntfy_topic), text.encode(), headers)
        if status >= 300:
            raise ChannelError(f"ntfy refused the question: HTTP {status} {body[:200].decode(errors='replace')}")

    def retract(self, nonce: str) -> None:
        """Take the question off the phone. `retracted` keeps the HTTP status (0: unreachable)."""
        try:
            self.retracted, _ = _http("DELETE", self._url(self.s.ntfy_topic, nonce), headers=self._auth(), timeout=5)
        except OSError:
            self.retracted = 0

    def _replies(self, since: int, stream: bool, timeout: float):
        q = "json?" + urllib.parse.urlencode({"since": since, **({} if stream else {"poll": "1"})})
        req = urllib.request.Request(self._url(self.s.ntfy_reply_topic) + "/" + q, headers=self._auth())
        return urllib.request.urlopen(req, timeout=timeout)

    def _stream(self, since: int):
        """The reply topic as a stream, with its socket: closing a response another thread is reading
        blocks until that read returns, so ask() shuts the socket down instead."""
        url = urllib.parse.urlsplit(self._url(self.s.ntfy_reply_topic) + "/json?"
                                    + urllib.parse.urlencode({"since": since}))
        cls = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
        conn = cls(url.hostname, url.port, timeout=90)
        try:
            conn.request("GET", f"{url.path}?{url.query}", headers=self._auth())
            sock = conn.sock
            resp = conn.getresponse()
        except Exception:
            conn.close()
            raise
        if resp.status >= 300:
            conn.close()
            raise ChannelError(f"ntfy refused to stream the reply topic: HTTP {resp.status}")
        return resp, sock, conn

    def _read(self, resp, nonce: str, out: queue.Queue) -> None:
        try:
            for raw in resp:
                try:
                    m = json.loads(raw)
                except ValueError:
                    continue
                if m.get("event") == "message":
                    a = self.verify(nonce, m.get("message", ""))
                    if a is not None:
                        out.put(("answer", a))
                        return
        except Exception:  # closed by ask(), or the stream broke
            pass
        out.put(("end", None))

    def _poll(self, since: int, nonce: str) -> bool | None:
        try:
            with self._replies(since, stream=False, timeout=10) as r:
                for raw in r:
                    try:
                        m = json.loads(raw)
                    except ValueError:
                        continue
                    if m.get("event") == "message":
                        a = self.verify(nonce, m.get("message", ""))
                        if a is not None:
                            return a
        except OSError:
            pass
        return None

    def ask(self, q: Question, stop: threading.Event) -> bool | None:
        """Allow, then Confirm on a second question with its own nonce, approves. Deny or Cancel refuses;
        an Allow left unconfirmed until the question expires is no answer."""
        answer = self._ask_one(q, stop)
        if answer is not True:
            return answer
        return self._ask_one(replace(q, id=secrets.token_urlsafe(16), screenshot=None), stop, confirm=True)

    def _ask_one(self, q: Question, stop: threading.Event, confirm: bool = False) -> bool | None:
        since = int(time.time()) - 2
        got: queue.Queue = queue.Queue()
        try:
            resp, sock, conn = self._stream(since)
        except (OSError, http.client.HTTPException, ChannelError) as e:
            self.last_error = f"could not listen for answers at {self.s.ntfy_server}: {e}"
            return None
        threading.Thread(target=self._read, args=(resp, q.id, got), daemon=True).start()
        try:
            try:
                self.publish(q, confirm)
            except (OSError, ChannelError) as e:
                self.last_error = str(e)
                return None
            streaming, next_poll = True, 0.0
            while not stop.is_set():
                left = q.expires_at - time.time()
                if left <= 0:
                    return None
                if streaming:
                    try:
                        kind, a = got.get(timeout=min(left, 0.25))
                    except queue.Empty:
                        continue
                    if kind == "answer":
                        return a
                    streaming = False  # the stream ended; read the reply topic every few seconds instead
                elif time.time() >= next_poll:
                    a = self._poll(since, q.id)
                    if a is not None:
                        return a
                    next_poll = time.time() + self.poll_s
                else:
                    stop.wait(min(left, 0.25))
            return None
        finally:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            conn.close()
            self.retract(q.id)


# -- webhook ------------------------------------------------------------------------------------------

class Webhook:
    name = "webhook"

    def __init__(self, s: PhoneSettings, poll_s: float = 2.0):
        self.s, self.poll_s = s, poll_s
        self.last_error = ""

    def sign(self, body: bytes) -> str:
        return "sha256=" + hmac.new(self.s.webhook_secret.encode(), body, hashlib.sha256).hexdigest()

    def verify(self, nonce: str, data) -> bool | None:
        if not isinstance(data, dict) or data.get("answer") not in ANSWERS:
            return None
        if not secrets.compare_digest(str(data.get("id", "")), nonce):
            return None
        want = _mac(self.s.webhook_secret, f"{nonce}.{data['answer']}")
        if not secrets.compare_digest(str(data.get("mac", "")), want):
            return None
        return ANSWERS[data["answer"]]

    def _post(self, payload: dict) -> tuple[int, bytes]:
        body = json.dumps(payload).encode()
        return _http("POST", self.s.webhook_url, body,
                     {"Content-Type": "application/json", "X-Overshoulder-Signature": self.sign(body)})

    def payload(self, q: Question) -> dict:
        p = {"type": "overshoulder.approval", "id": q.id, "reason": q.reason, "process": q.process,
             "expires_at": round(q.expires_at, 3), "text": phone_text(q)}
        if q.title:
            p["title"] = q.title
        if q.screenshot:
            p["screenshot_png_base64"] = base64.b64encode(q.screenshot).decode()
        return p

    def _answer_in(self, nonce: str, body: bytes) -> bool | None:
        try:
            return self.verify(nonce, json.loads(body or b"null"))
        except ValueError:
            return None

    def ask(self, q: Question, stop: threading.Event) -> bool | None:
        answered = None
        try:
            try:
                status, body = self._post(self.payload(q))
            except OSError as e:
                self.last_error = f"webhook unreachable: {e}"
                return None
            if status >= 300:
                self.last_error = f"webhook refused the question: HTTP {status}"
                return None
            answered = self._answer_in(q.id, body)
            if answered is not None:
                return answered
            poll = self.s.webhook_poll_url or self.s.webhook_url
            sep = "&" if "?" in poll else "?"
            url = f"{poll}{sep}{urllib.parse.urlencode({'id': q.id})}"
            while not stop.is_set() and time.time() < q.expires_at:
                try:
                    status, body = _http("GET", url, headers={"X-Overshoulder-Signature": self.sign(q.id.encode())})
                except OSError:
                    status, body = 0, b""
                if status == 200:
                    answered = self._answer_in(q.id, body)
                    if answered is not None:
                        return answered
                stop.wait(min(self.poll_s, max(0.0, q.expires_at - time.time())))
            return None
        finally:
            try:
                self._post({"type": "overshoulder.approval.closed", "id": q.id,
                            "outcome": "answered" if answered is not None else "closed"})
            except OSError:
                pass


def channels_for(s: PhoneSettings) -> list:
    out: list = []
    if s.ntfy_enabled:
        out.append(Ntfy(s))
    if s.webhook_enabled and s.webhook_url:
        out.append(Webhook(s))
    return out
