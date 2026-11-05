"""The settings page and the wait_for tool over the real daemon."""

import json
import sys
import threading
import urllib.error
import urllib.request
from urllib.parse import urlencode

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_mcp_server import call, daemon  # noqa: E402,F401  (fixture reuse)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def _http(method, url, data=None, headers=None, cookie=None):
    h = dict(headers or {})
    if cookie:
        h["Cookie"] = cookie
    req = urllib.request.Request(url, data=data, method=method, headers=h)
    try:
        r = urllib.request.build_opener(_NoRedirect).open(req, timeout=5)
        return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def test_settings_page_needs_a_one_time_link_and_saves(daemon):  # noqa: F811
    from overshoulder import settings as st

    d, port, _ = daemon
    d.port_ref["port"] = port
    base = f"http://127.0.0.1:{port}"
    assert _http("GET", f"{base}/settings")[0] == 403
    _, _, body = _http("POST", f"{base}/operator/settings_link", b"{}",
                       {"x-overshoulder-operator": d.operator_key, "content-type": "application/json"})
    url = json.loads(body)["url"]
    status, headers, _ = _http("GET", url)
    assert status == 303
    set_cookie = headers.get("set-cookie") or headers.get("Set-Cookie")
    cookie = set_cookie.split(";")[0]
    assert "samesite=strict" in set_cookie.lower() and "httponly" in set_cookie.lower()
    assert _http("GET", url)[0] == 403  # single-use link
    status, _, page = _http("GET", f"{base}/settings", cookie=cookie)
    assert status == 200 and "Automatic detection" in page and "Your mouse and keyboard" in page
    assert 'value="borrow" checked' in page  # the default input mode is shown selected
    token = page.split('name="form_token" value="')[1].split('"')[0]
    hdr = {"content-type": "application/x-www-form-urlencoded"}
    form = urlencode({"form_token": token, "waits_enabled": "on", "kind_file": "on",
                      "poll_interval_ms": "300", "max_wait_minutes": "15", "wait_processes": "",
                      "input_mode": "never", "borrow_idle_s": "7"}).encode()
    assert _http("POST", f"{base}/settings", form, hdr, cookie)[0] == 303
    s = st.load()
    assert s.wait_kinds == ["file"] and s.poll_interval_ms == 300 and s.max_wait_minutes == 15
    assert s.input_mode == "never" and s.borrow_idle_s == 7
    assert 'value="never" checked' in _http("GET", f"{base}/settings", cookie=cookie)[2]
    forged = urlencode({"form_token": "forged", "waits_enabled": "on"}).encode()
    assert _http("POST", f"{base}/settings", forged, hdr, cookie)[0] == 403
    assert _http("GET", f"{base}/settings", headers={"Host": "evil.example"}, cookie=cookie)[0] == 403


def test_wait_for_tool_respects_settings(daemon, tmp_path):  # noqa: F811
    from overshoulder import settings as st

    _, port, token = daemon
    st.save(st.Settings(waits_enabled=False))
    out = call(port, token, "wait_for", {"conditions": [{"kind": "file", "path": str(tmp_path / "x")}],
                                         "timeout_s": 1})
    assert out["ok"] is False and out["error"]["code"] == "wait_refused"
    st.save(st.Settings(poll_interval_ms=100))
    flag = tmp_path / "done.flag"
    threading.Timer(0.5, lambda: flag.write_text("x")).start()
    out = call(port, token, "wait_for", {"conditions": [{"kind": "file", "path": str(flag)}],
                                         "timeout_s": 10})
    assert out["ok"] and out["fired"]


# -- bodies are bounded, and checked for auth before they are parsed ------------------------------

def _receive_of(chunks):
    msgs = [{"type": "http.request", "body": c, "more_body": i < len(chunks) - 1} for i, c in enumerate(chunks)]

    async def receive():
        return msgs.pop(0) if msgs else {"type": "http.disconnect"}

    return receive


def _request(chunks, headers=()):
    from starlette.requests import Request
    scope = {"type": "http", "method": "POST", "path": "/", "headers": [(k.encode(), v.encode()) for k, v in headers]}
    return Request(scope, _receive_of(chunks))


def test_bounded_body_stops_reading_a_chunked_body_past_the_limit():
    import asyncio

    from overshoulder.settings_page import bounded_body
    got = asyncio.run(bounded_body(_request([b"x" * 600, b"y" * 600, b"z" * 600]), 1000))
    assert got is None
    req = _request([b"a=1", b"&b=2"])
    assert asyncio.run(bounded_body(req, 1000)) == b"a=1&b=2"
    assert asyncio.run(req.body()) == b"a=1&b=2"  # kept for form() and json()
    assert asyncio.run(bounded_body(_request([b"x"], [("content-length", "5000")]), 1000)) is None


def test_operator_routes_check_the_key_and_the_body(daemon):  # noqa: F811
    d, port, _ = daemon
    base = f"http://127.0.0.1:{port}/operator"
    key = {"x-overshoulder-operator": d.operator_key, "content-type": "application/json"}
    huge = {"Content-Length": "999999999"}
    assert _http("POST", f"{base}/pause", b"{}", huge)[0] == 403
    assert _http("POST", f"{base}/pause", b"{}", {**key, **huge})[0] == 413
    assert _http("POST", f"{base}/pause", b"{not json", key)[0] == 400
    assert _http("POST", f"{base}/pause", b"[1, 2]", key)[0] == 400
    assert _http("POST", f"{base}/protect", b'{"pid": "x"}', key)[0] == 400
    assert _http("POST", f"{base}/resume", b"{}", key)[0] == 200


def test_settings_forms_need_a_session_before_their_body_is_read(daemon):  # noqa: F811
    d, port, _ = daemon
    d.port_ref["port"] = port
    base = f"http://127.0.0.1:{port}"
    hdr = {"content-type": "application/x-www-form-urlencoded", "Content-Length": "999999999"}
    assert _http("POST", f"{base}/settings", b"form_token=x", hdr)[0] == 403
    _, _, body = _http("POST", f"{base}/operator/settings_link", b"{}",
                       {"x-overshoulder-operator": d.operator_key, "content-type": "application/json"})
    _, headers, _ = _http("GET", json.loads(body)["url"])
    cookie = (headers.get("set-cookie") or headers.get("Set-Cookie")).split(";")[0]
    assert _http("POST", f"{base}/settings", b"form_token=x", hdr, cookie)[0] == 413


def test_a_wait_ends_when_its_client_is_removed(daemon, tmp_path):  # noqa: F811
    from overshoulder import settings as st

    d, port, token = daemon
    st.save(st.Settings(poll_interval_ms=100))
    threading.Timer(0.6, lambda: d.gw.registry.remove("mcp-test")).start()
    out = call(port, token, "wait_for", {"conditions": [{"kind": "file", "path": str(tmp_path / "never")}],
                                         "timeout_s": 20})
    assert out["ok"] is False and out["error"]["code"] in ("unauthorized", "wait_refused")


def test_a_panic_ends_a_wait(daemon, tmp_path):  # noqa: F811
    from overshoulder import settings as st

    d, port, token = daemon
    st.save(st.Settings(poll_interval_ms=100))
    threading.Timer(0.6, lambda: d.gw.panic("test")).start()
    out = call(port, token, "wait_for", {"conditions": [{"kind": "file", "path": str(tmp_path / "never")}],
                                         "timeout_s": 20})
    assert out["ok"] is False and "paused" in out["error"]["message"]


def test_on_the_wsl_address_only_mcp_is_served():
    import asyncio

    from overshoulder.server import OnlyMcpOn
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["path"])

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b""}

    wrapped = OnlyMcpOn(app, {"172.20.176.1"})
    for server, path in [(("172.20.176.1", 47621), "/mcp"), (("172.20.176.1", 47621), "/settings"),
                         (("172.20.176.1", 47621), "/operator/panic"), (("172.20.176.1", 47621), "/mcpx"),
                         (("127.0.0.1", 47621), "/settings")]:
        sent = []
        asyncio.run(wrapped({"type": "http", "path": path, "server": server, "method": "GET",
                             "headers": [(b"host", b"127.0.0.1:47621")]}, receive, send))
        if path in ("/settings", "/operator/panic", "/mcpx") and server[0] != "127.0.0.1":
            assert sent and sent[0]["status"] == 404, path
    assert reached == ["/mcp", "/settings"]  # /settings only from loopback
