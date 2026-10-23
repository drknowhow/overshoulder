"""Phone approvals: the chain, the ntfy and webhook channels against fake servers, and the settings."""

import base64
import hashlib
import hmac
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_gateway import FakeScope, ok_checks  # noqa: E402

from overshoulder import approvals, phone, settings_page  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway import errors as E  # noqa: E402
from overshoulder.gateway.core import Target, gateway_for_tests  # noqa: E402

REQUEST = {"kind": "action", "client": "agent-a", "action": "click", "reason": "click on 'Send'",
           "process": "mail.exe", "hwnd": 0x10}


# -- a fake ntfy server and phone ------------------------------------------------------------------

class FakeNtfy:
    """Enough of ntfy: publish (POST/PUT), stream and poll a topic as JSON lines, delete by sequence ID."""

    def __init__(self, stream=True):
        self.messages, self.deleted, self.stream = [], [], stream
        self.cond = threading.Condition()
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _publish(self):
                topic = self.path.strip("/").split("/")[0]
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                text = body.decode() if self.command == "POST" else self.headers.get("X-Message", "")
                with fake.cond:
                    fake.messages.append({"id": str(len(fake.messages)), "time": int(time.time()), "event": "message",
                                          "topic": topic, "message": text,
                                          "headers": {k.lower(): v for k, v in self.headers.items()},
                                          "attachment": body if self.command == "PUT" else None})
                    fake.cond.notify_all()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            do_POST = do_PUT = _publish

            def do_DELETE(self):
                fake.deleted.append(self.path)
                self.send_response(200)
                self.end_headers()

            def do_GET(self):
                url = urllib.parse.urlparse(self.path)
                topic = url.path.strip("/").split("/")[0]
                qs = urllib.parse.parse_qs(url.query)
                since = int(qs.get("since", ["0"])[0])
                poll = qs.get("poll") == ["1"] or not fake.stream
                self.send_response(200)
                self.end_headers()
                sent = 0
                end = time.time() + 30
                while time.time() < end:
                    with fake.cond:
                        mine = [m for m in fake.messages if m["topic"] == topic and m["time"] >= since]
                        new = mine[sent:]
                        if not new and not poll:
                            fake.cond.wait(0.2)
                            continue
                    for m in new:
                        line = {k: v for k, v in m.items() if k not in ("headers", "attachment")}
                        try:
                            self.wfile.write((json.dumps(line) + "\n").encode())
                            self.wfile.flush()
                        except OSError:
                            return
                    sent += len(new)
                    if poll:
                        return

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()

    def wait_question(self, topic, timeout=5.0, nth=1):
        """The nth question published on the topic (the confirmation after an Allow is the second)."""
        end = time.time() + timeout
        while time.time() < end:
            got = [m for m in self.messages if m["topic"] == topic]
            if len(got) >= nth:
                return got[nth - 1]
            time.sleep(0.05)
        raise AssertionError(f"question {nth} was not published")

    def allow_and_confirm(self, topic):
        """Allow on the question, then Confirm on the confirmation; returns both posted bodies."""
        allow = self.tap(self.wait_question(topic), "Allow")
        return allow, self.tap(self.wait_question(topic, nth=2), "Confirm")

    def tap(self, question, label):
        """What the ntfy app does for an http action button: POST its body to its URL. "Confirm" also
        matches a Confirm button that names the control ("Confirm Send")."""
        for action in question["headers"]["x-actions"].split("; "):
            parts = [p.strip() for p in action.split(",")]
            if parts[1] == label or parts[1].startswith(label + " "):
                opts = dict(p.split("=", 1) for p in parts[3:])
                req = urllib.request.Request(parts[2], data=opts["body"].encode(), method=opts["method"])
                urllib.request.urlopen(req, timeout=5).read()
                return opts["body"]
        raise AssertionError(f"no {label} button")

    def post(self, topic, text):
        req = urllib.request.Request(f"{self.base}/{topic}", data=text.encode(), method="POST")
        urllib.request.urlopen(req, timeout=5).read()


@pytest.fixture
def ntfy():
    f = FakeNtfy()
    yield f
    f.close()


def ntfy_settings(base, **kw):
    return phone.PhoneSettings(ntfy_enabled=True, ntfy_server=base, **kw).ensure_secrets().clamp()


def ask_async(channel, q):
    out, stop = {}, threading.Event()
    t = threading.Thread(target=lambda: out.update(answer=channel.ask(q, stop)), daemon=True)
    t.start()
    return t, out, stop


# -- ntfy ---------------------------------------------------------------------------------------------

def test_allow_then_confirm_on_the_phone_approves_and_both_notifications_are_removed(ntfy):
    s = ntfy_settings(ntfy.base)
    ch = phone.Ntfy(s)
    q = phone.make_question(REQUEST, s, 10)
    t, out, _ = ask_async(ch, q)
    question = ntfy.wait_question(s.ntfy_topic)
    ntfy.tap(question, "Allow")
    confirm = ntfy.wait_question(s.ntfy_topic, nth=2)
    assert t.is_alive()  # Allow alone has approved nothing
    ntfy.tap(confirm, "Confirm")
    t.join(5)
    assert out["answer"] is True
    h = question["headers"]
    assert h["x-sequence-id"] == q.id and h["x-title"] == "Allow click on 'Send'?"
    assert question["message"].splitlines()[:2] == ["click on 'Send'", "in mail.exe"]
    assert "expires " in question["message"] and "Allow asks you to confirm" in question["message"]
    second = confirm["headers"]["x-sequence-id"]
    assert second != q.id and confirm["headers"]["x-title"] == "Confirm click on 'Send'?"
    assert confirm["message"].splitlines()[:2] == ["Tap Confirm Send to allow:", "click on 'Send'"]
    assert [a.split(", ")[1] for a in confirm["headers"]["x-actions"].split("; ")] == ["Cancel", "Confirm Send"]
    assert {f"/{s.ntfy_topic}/{q.id}", f"/{s.ntfy_topic}/{second}"} <= set(ntfy.deleted)  # both taken off the phone
    assert s.answer_key not in json.dumps([question, confirm])  # the key never leaves the PC


def test_an_allow_left_unconfirmed_approves_nothing(ntfy):
    s = ntfy_settings(ntfy.base)
    q = phone.make_question(REQUEST, s, 1.5)
    t, out, _ = ask_async(phone.Ntfy(s), q)
    ntfy.tap(ntfy.wait_question(s.ntfy_topic), "Allow")
    ntfy.wait_question(s.ntfy_topic, nth=2)
    t.join(5)
    assert out["answer"] is None  # expired unconfirmed, which the chain counts as No


def test_cancel_on_the_confirmation_refuses(ntfy):
    s = ntfy_settings(ntfy.base)
    q = phone.make_question(REQUEST, s, 10)
    t, out, _ = ask_async(phone.Ntfy(s), q)
    ntfy.tap(ntfy.wait_question(s.ntfy_topic), "Allow")
    ntfy.tap(ntfy.wait_question(s.ntfy_topic, nth=2), "Cancel")
    t.join(5)
    assert out["answer"] is False


def test_the_allow_posted_again_does_not_count_as_the_confirmation(ntfy):
    s = ntfy_settings(ntfy.base)
    q = phone.make_question(REQUEST, s, 1.5)
    t, out, _ = ask_async(phone.Ntfy(s), q)
    allow = ntfy.tap(ntfy.wait_question(s.ntfy_topic), "Allow")
    ntfy.wait_question(s.ntfy_topic, nth=2)
    ntfy.post(s.ntfy_reply_topic, allow)  # a second tap that lands on the first question's Allow again
    t.join(5)
    assert out["answer"] is None


def test_a_deny_tapped_on_the_phone_refuses(ntfy):
    s = ntfy_settings(ntfy.base)
    q = phone.make_question(REQUEST, s, 10)
    t, out, _ = ask_async(phone.Ntfy(s), q)
    ntfy.tap(ntfy.wait_question(s.ntfy_topic), "Deny")
    t.join(5)
    assert out["answer"] is False
    assert len([m for m in ntfy.messages if m["topic"] == s.ntfy_topic]) == 1  # no confirmation after a Deny


def test_a_forged_reply_is_ignored_and_the_question_times_out(ntfy):
    s = ntfy_settings(ntfy.base)
    q = phone.make_question(REQUEST, s, 1.5)
    t, out, _ = ask_async(phone.Ntfy(s), q)
    ntfy.wait_question(s.ntfy_topic)
    ntfy.post(s.ntfy_reply_topic, f"{q.id}.yes.{'0' * 64}")  # right nonce, no key
    ntfy.post(s.ntfy_reply_topic, f"{q.id}.yes")
    other = hmac.new(b"guessed-key", f"{q.id}.yes".encode(), hashlib.sha256).hexdigest()
    ntfy.post(s.ntfy_reply_topic, f"{q.id}.yes.{other}")
    t.join(5)
    assert out["answer"] is None


def test_a_replayed_allow_from_an_earlier_question_is_ignored(ntfy):
    s = ntfy_settings(ntfy.base)
    first = phone.make_question(REQUEST, s, 10)
    t, out, _ = ask_async(phone.Ntfy(s), first)
    allow, confirm = ntfy.allow_and_confirm(s.ntfy_topic)
    t.join(5)
    assert out["answer"] is True
    second = phone.make_question(REQUEST, s, 1.5)
    t, out, _ = ask_async(phone.Ntfy(s), second)
    ntfy.post(s.ntfy_reply_topic, allow)  # the same signed Allow and Confirm, sent again
    ntfy.post(s.ntfy_reply_topic, confirm)
    t.join(5)
    assert out["answer"] is None


def test_answers_are_read_by_polling_when_the_stream_is_unavailable():
    f = FakeNtfy(stream=False)
    try:
        s = ntfy_settings(f.base)
        q = phone.make_question(REQUEST, s, 10)
        ch = phone.Ntfy(s, poll_s=0.2)
        t, out, _ = ask_async(ch, q)
        f.allow_and_confirm(s.ntfy_topic)
        t.join(5)
        assert out["answer"] is True
    finally:
        f.close()


def test_the_window_title_and_a_screenshot_go_only_when_the_person_shares_them(ntfy, monkeypatch):
    from PIL import Image

    from overshoulder.driver import capture
    from overshoulder.driver import windows as w
    monkeypatch.setattr(w, "title", lambda h: "Quarterly report - Mail")
    monkeypatch.setattr(capture, "window_image", lambda h, r=None: Image.new("RGB", (40, 30), (255, 0, 0)))
    plain = phone.make_question(REQUEST, ntfy_settings(ntfy.base), 10)
    assert plain.title == "" and plain.screenshot is None
    s = ntfy_settings(ntfy.base, share_title=True, share_screenshot=True)
    q = phone.make_question(REQUEST, s, 10)
    assert q.title == "Quarterly report - Mail" and q.screenshot.startswith(b"\x89PNG")
    t, out, stop = ask_async(phone.Ntfy(s), q)
    question = ntfy.wait_question(s.ntfy_topic)
    stop.set()
    t.join(5)
    assert question["attachment"].startswith(b"\x89PNG")
    assert question["headers"]["x-filename"] == "overshoulder.png"
    assert question["headers"]["x-message"].startswith("=?UTF-8?B?")  # several lines, so RFC 2047


def test_titles_and_the_confirm_button_name_the_action():
    s = phone.PhoneSettings()

    def q(reason):
        return phone.make_question({**REQUEST, "reason": reason}, s, 10)

    send, delete = q("click on 'Send'"), q("click on 'Delete'")
    assert phone.headline(send) != phone.headline(delete)  # back-to-back questions don't look alike
    assert phone.headline(delete, confirm=True) == "Confirm click on 'Delete'?"
    assert phone.confirm_label(delete) == "Confirm Delete"
    long = q("type '" + "x" * 80 + "' into Search")
    assert len(phone.headline(long)) == len("Allow ") + phone.TITLE_MAX + 1 and phone.headline(long).endswith("...?")
    # names ntfy's button format could misread, or a phone could cut off, fall back to plain Confirm
    for reason in ("press Enter", "click on 'Save, then close'", "click on 'a=b'", "click on 'Envoyé'",
                   "click on 'A very long button name indeed'"):
        assert phone.confirm_label(q(reason)) == "Confirm"


def test_a_title_that_is_not_ascii_is_sent_as_an_encoded_word(ntfy):
    s = ntfy_settings(ntfy.base)
    q = phone.make_question({**REQUEST, "reason": "click on 'Envoyé'"}, s, 1)
    phone.Ntfy(s).ask(q, threading.Event())
    title = ntfy.wait_question(s.ntfy_topic)["headers"]["x-title"]
    assert title.startswith("=?UTF-8?B?") and "Envoyé" in base64.b64decode(title[10:-2]).decode()


def test_an_unreachable_ntfy_server_is_an_error_not_an_answer():
    s = ntfy_settings("http://127.0.0.1:9")
    ch = phone.Ntfy(s)
    assert ch.ask(phone.make_question(REQUEST, s, 2), threading.Event()) is None
    assert "could not listen" in ch.last_error


# -- webhook ------------------------------------------------------------------------------------------

class FakeWebhook:
    def __init__(self, immediate=None):
        self.posts, self.answer, self.immediate = [], None, immediate
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                fake.posts.append((json.loads(body), self.headers.get("X-Overshoulder-Signature"), body))
                reply = fake.immediate(json.loads(body)) if fake.immediate else None
                self.send_response(200 if reply else 202)
                self.end_headers()
                self.wfile.write(json.dumps(reply).encode() if reply else b"")

            def do_GET(self):
                self.send_response(200 if fake.answer else 204)
                self.end_headers()
                if fake.answer:
                    self.wfile.write(json.dumps(fake.answer).encode())

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/hook"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


def hook_settings(url):
    return phone.PhoneSettings(webhook_enabled=True, webhook_url=url).ensure_secrets().clamp()


def signed_answer(s, nonce, answer):
    return {"id": nonce, "answer": answer,
            "mac": hmac.new(s.webhook_secret.encode(), f"{nonce}.{answer}".encode(), hashlib.sha256).hexdigest()}


def test_a_webhook_question_is_signed_and_can_be_answered_at_once():
    holder = {}
    f = FakeWebhook(immediate=lambda p: signed_answer(holder["s"], p["id"], "yes") if p["type"].endswith("approval")
                    else None)
    try:
        s = holder["s"] = hook_settings(f.url)
        q = phone.make_question(REQUEST, s, 10)
        assert phone.Webhook(s).ask(q, threading.Event()) is True
        payload, sig, body = f.posts[0]
        assert sig == "sha256=" + hmac.new(s.webhook_secret.encode(), body, hashlib.sha256).hexdigest()
        assert payload["reason"] == "click on 'Send'" and payload["process"] == "mail.exe" and payload["id"] == q.id
        assert "title" not in payload and "screenshot_png_base64" not in payload
        assert f.posts[-1][0] == {"type": "overshoulder.approval.closed", "id": q.id, "outcome": "answered"}
    finally:
        f.close()


def test_a_webhook_answer_is_polled_and_a_forged_one_refused():
    f = FakeWebhook()
    try:
        s = hook_settings(f.url)
        ch = phone.Webhook(s, poll_s=0.1)
        q = phone.make_question(REQUEST, s, 10)
        t, out, _ = ask_async(ch, q)
        time.sleep(0.3)
        f.answer = {**signed_answer(s, q.id, "yes"), "answer": "no"}  # mac for yes, claims no
        time.sleep(0.4)
        assert t.is_alive()
        f.answer = signed_answer(s, q.id, "no")
        t.join(5)
        assert out["answer"] is False
    finally:
        f.close()


# -- the chain ----------------------------------------------------------------------------------------

class Fake:
    def __init__(self, name, answer=None, after=0.0, boom=False):
        self.name, self.answer, self.after, self.boom = name, answer, after, boom
        self.stopped = False
        self.last_error = ""

    def ask(self, q, stop):
        if self.boom:
            raise RuntimeError("channel broke")
        if stop.wait(self.after):
            self.stopped = True
            return None
        if self.answer is None:
            stop.wait(30)
            self.stopped = stop.is_set()
            return None
        return self.answer


def chain(*channels, timeout=5):
    return approvals.ApproverChain(lambda: list(channels), timeout)


def test_the_first_answer_wins_and_the_other_prompts_are_closed():
    desk, ph = Fake("desktop", None), Fake("ntfy", True, after=0.1)
    c = chain(desk, ph)
    req = dict(REQUEST)
    assert c.approve(req) is True
    assert desk.stopped and c.last["by"] == "ntfy" and req["answered_by"] == "ntfy"


def test_a_no_at_the_desk_beats_a_slower_yes_on_the_phone():
    desk, ph = Fake("desktop", False, after=0.05), Fake("ntfy", True, after=1.0)
    c = chain(desk, ph)
    assert c.approve(dict(REQUEST)) is False and c.last["by"] == "desktop" and ph.stopped


def test_no_answer_before_the_deadline_is_no():
    a, b = Fake("desktop"), Fake("ntfy")
    c = chain(a, b, timeout=0.5)
    t0 = time.time()
    assert c.approve(dict(REQUEST)) is False
    assert time.time() - t0 < 3 and a.stopped and b.stopped and c.last["answer"] is None


def test_a_broken_channel_decides_nothing():
    c = chain(Fake("ntfy", boom=True), Fake("desktop", True, after=0.1))
    assert c.approve(dict(REQUEST)) is True and "channel broke" in c.last["errors"]["ntfy"]
    c = chain(Fake("ntfy", boom=True), timeout=0.5)
    assert c.approve(dict(REQUEST)) is False


def test_settings_approver_asks_the_desk_and_the_phone_channels_switched_on(monkeypatch, ntfy):
    seen = []

    class Box(Fake):
        def __init__(self, also=""):
            super().__init__("desktop", None)
            seen.append(also)

    monkeypatch.setattr(approvals, "DesktopBox", Box)
    s = ntfy_settings(ntfy.base, timeout_s=10)
    a = approvals.SettingsApprover(load=lambda: s)
    result = {}
    t = threading.Thread(target=lambda: result.update(ok=a.approve(dict(REQUEST))), daemon=True)
    t.start()
    ntfy.allow_and_confirm(s.ntfy_topic)
    t.join(5)
    assert result["ok"] is True and a.last["by"] == "ntfy" and seen == ["your phone (ntfy)"]


# -- the gateway --------------------------------------------------------------------------------------

def test_the_approval_request_names_the_program_and_the_log_says_who_answered(tmp_path):
    scope = FakeScope()
    checks = ok_checks(window_processes=lambda h: ["mail.exe"], window_under=lambda x, y: 0x10)
    seen = []
    approver = chain(Fake("ntfy", True, after=0.05))
    original = approver._question
    approver._question = lambda req, t: seen.append(dict(req)) or original(req, t)
    g = gateway_for_tests(tmp_path, checks=checks, scope_factory=lambda spec: scope, approver=approver)
    token = g.registry.add("agent-a")
    lease_id = g.acquire(token, "window:16")["lease_id"]
    send = Target("element", rect=Rect(10, 10, 30, 30), name="Send", hwnd=0x10)
    assert g.run(token, lease_id, "click", lambda t, p: "sent", target=send)["ok"]
    assert seen[0]["process"] == "mail.exe" and seen[0]["hwnd"] == 0x10
    row = [json.loads(x) for x in g.log.path.read_text(encoding="utf-8").splitlines()][-1]
    assert row["approved_by"] == "ntfy"
    g.approver = chain(Fake("ntfy", False, after=0.05))
    with pytest.raises(E.ApprovalDenied) as denied:
        g.run(token, lease_id, "click", lambda t, p: "sent", target=send)
    assert denied.value.to_dict()["answered_by"] == "ntfy" and "denied on ntfy" in str(denied.value)
    row = [json.loads(x) for x in g.log.path.read_text(encoding="utf-8").splitlines()][-1]
    assert row["error"]["answered_by"] == "ntfy"
    g.approver = chain(Fake("ntfy", None), timeout=0.2)
    with pytest.raises(E.ApprovalDenied) as silent:
        g.run(token, lease_id, "click", lambda t, p: "sent", target=send)
    assert silent.value.answered_by is None and "no answer in time" in str(silent.value)


# -- settings -----------------------------------------------------------------------------------------

def test_phone_settings_make_their_secrets_once_and_keep_them(tmp_path):
    p = tmp_path / "phone.json"
    first = phone.load(p)
    assert first.ntfy_topic.startswith("overshoulder-") and len(first.answer_key) > 30
    again = phone.load(p)
    assert (again.ntfy_topic, again.answer_key, again.webhook_secret) == \
        (first.ntfy_topic, first.answer_key, first.webhook_secret)
    assert not again.ntfy_enabled and again.channels == []


def test_the_form_keeps_topics_unless_renewal_is_ticked():
    cur = phone.PhoneSettings().ensure_secrets()
    form = {"ntfy_enabled": "on", "ntfy_server": "ntfy.example.org/", "phone_timeout_s": "5",
            "share_title": "on"}
    s = settings_page.phone_from_form(form, cur)
    assert s.ntfy_enabled and s.ntfy_server == "https://ntfy.example.org" and s.share_title
    assert s.ntfy_topic == cur.ntfy_topic and s.answer_key == cur.answer_key and s.timeout_s == 10
    renewed = settings_page.phone_from_form({**form, "renew_topics": "on"}, cur)
    assert renewed.ntfy_topic != cur.ntfy_topic and renewed.answer_key == cur.answer_key


def test_the_settings_page_shows_the_topic_and_a_test_result():
    from overshoulder import settings as st
    ph = phone.PhoneSettings(ntfy_enabled=True).ensure_secrets()
    page = settings_page.render_page(st.Settings(), "tok", [], "logs", False, ph=ph,
                                     phone_test={"answer": True, "by": "ntfy", "seconds": 3.2, "errors": {}})
    assert "Approvals on your phone" in page and ph.ntfy_topic in page
    assert 'name="ntfy_enabled" checked' in page and "Allowed on ntfy after 3.2 s." in page
    assert ph.answer_key not in page  # the answer key is never shown
