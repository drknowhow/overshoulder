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
    assert status == 200 and "Automatic detection" in page
    token = page.split('name="form_token" value="')[1].split('"')[0]
    hdr = {"content-type": "application/x-www-form-urlencoded"}
    form = urlencode({"form_token": token, "waits_enabled": "on", "kind_file": "on",
                      "poll_interval_ms": "300", "max_wait_minutes": "15", "wait_processes": ""}).encode()
    assert _http("POST", f"{base}/settings", form, hdr, cookie)[0] == 303
    s = st.load()
    assert s.wait_kinds == ["file"] and s.poll_interval_ms == 300 and s.max_wait_minutes == 15
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
