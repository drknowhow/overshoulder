"""The daemon over real MCP streamable HTTP on loopback."""

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path / "home"))
    import uvicorn

    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.server import Daemon
    from overshoulder.service import Service

    d = Daemon(Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots"))
    port = _free_port()
    app = d.build().streamable_http_app(host="127.0.0.1")
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    token = d.gw.registry.add("mcp-test")
    yield d, port, token
    server.should_exit = True
    t.join(timeout=10)


async def _call(port, token, tool, args=None):
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with create_mcp_http_client(headers=headers) as http:
        async with streamable_http_client(f"http://127.0.0.1:{port}/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as session:
                await session.initialize()
                if tool is None:
                    return await session.list_tools()
                return await session.call_tool(tool, args or {})


def call(port, token, tool, args=None):
    res = asyncio.run(_call(port, token, tool, args))
    if tool is None:
        return res
    texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
    try:
        return json.loads(texts[0]) if texts else res
    except json.JSONDecodeError:
        return {"raw": texts, "is_error": res.is_error}


def test_lists_the_agent_tools(daemon):
    _, port, token = daemon
    names = {t.name for t in call(port, token, None).tools}
    assert {"windows", "take_scope", "observe", "find", "act", "release_scope", "status", "record_start",
            "record_stop", "redact", "annotate", "render", "wait_for", "profile"} <= names


@pytest.mark.skipif(not __import__("shutil").which("ffmpeg"), reason="needs ffmpeg on PATH")
def test_annotate_and_render_run_while_another_caller_holds_the_desktop(daemon):
    from tests.rechelpers import make_recording

    d, port, token = daemon
    rec = make_recording(d.svc.recordings_dir)
    meta = json.loads((rec / "meta.json").read_text(encoding="utf-8"))
    (rec / "meta.json").write_text(json.dumps({**meta, "client": "mcp-test"}), encoding="utf-8")
    results = {}

    def agent():
        results["add"] = call(port, token, "annotate", {"recording": "rec", "op": "add",
                                                        "item": {"kind": "redact", "rect": [20, 20, 120, 60]}})
        results["render"] = call(port, token, "render", {"recording": "rec", "gif": False, "zoom": "off"})

    with d._desk:  # someone else is driving the desktop for the whole time
        t = threading.Thread(target=agent)
        t.start()
        t.join(timeout=60)
        assert not t.is_alive(), "annotate/render waited for the desktop lock"
    assert results["add"]["ok"] and results["add"]["item"]["id"] == "a1"
    assert results["render"]["ok"] and results["render"]["steps"] == 2

    other = d.gw.registry.add("someone-else")
    out = call(port, other, "render", {"recording": "rec"})
    assert out["ok"] is False and "another client" in out["error"]["message"]
    out = call(port, token, "annotate", {"recording": "rec", "op": "add", "item": {"kind": "laser"}})
    assert out["ok"] is False and out["error"]["code"] == "bad_request"


def test_tool_call_without_token_is_refused(daemon):
    _, port, _ = daemon
    out = call(port, None, "status")
    assert out["ok"] is False and out["error"]["code"] == "unauthorized"
    out = call(port, "os_not_a_real_token", "windows")
    assert out["ok"] is False and out["error"]["code"] == "unauthorized"


def test_screen_scope_is_refused_without_a_person_approving(daemon):
    _, port, token = daemon
    out = call(port, token, "take_scope", {"scope": "screen:all"})
    assert out["ok"] is False and out["error"]["code"] == "approval_denied"


def test_operator_endpoints_need_the_operator_key(daemon):
    d, port, _ = daemon
    req = urllib.request.Request(f"http://127.0.0.1:{port}/operator/resume", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as ex:
        urllib.request.urlopen(req, timeout=5)
    assert ex.value.code == 403
    ok = urllib.request.Request(f"http://127.0.0.1:{port}/operator/panic", data=b'{"reason": "test"}',
                                method="POST", headers={"x-overshoulder-operator": d.operator_key,
                                                        "content-type": "application/json"})
    body = json.loads(urllib.request.urlopen(ok, timeout=5).read())
    assert body["status"]["paused"]


def test_answered_status_polls_stay_out_of_the_access_log(tmp_path, monkeypatch):
    """The watchdog polls /operator/status every 250 ms; logging each one grew serve.log by ~22 MB a day."""
    import logging

    import uvicorn

    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.server import Daemon, _QuietStatusPolls, quiet_status_polls
    from overshoulder.service import Service

    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path / "home"))
    d = Daemon(Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots"))
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(d.build().streamable_http_app(host="127.0.0.1"), host="127.0.0.1",
                                           port=port, log_level="info"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    access = logging.getLogger("uvicorn.access")
    lines = []
    handler = logging.Handler()
    handler.emit = lambda r: lines.append(r.getMessage())
    access.addHandler(handler)
    key = {"x-overshoulder-operator": d.operator_key}

    def get(path, headers=None, data=None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers or {}, data=data,
                                     method="POST" if data is not None else "GET")
        try:
            urllib.request.urlopen(req, timeout=5).read()
        except urllib.error.HTTPError:
            pass

    try:
        get("/operator/status", key)
        assert any("/operator/status" in x and x.endswith("200") for x in lines), lines  # the capture works
        quiet_status_polls()
        lines.clear()
        get("/operator/status", key)
        get("/operator/status")  # refused: still logged
        get("/operator/resume", {**key, "content-type": "application/json"}, b"{}")
        assert not any("/operator/status" in x and x.endswith("200") for x in lines), lines
        assert any("/operator/status" in x and x.endswith("403") for x in lines), lines
        assert any("/operator/resume" in x for x in lines), lines
    finally:
        access.removeHandler(handler)
        for f in [f for f in access.filters if isinstance(f, _QuietStatusPolls)]:
            access.removeFilter(f)
        server.should_exit = True
        t.join(timeout=10)


def test_foreign_host_header_is_rejected(daemon):
    _, port, token = daemon
    req = urllib.request.Request(f"http://127.0.0.1:{port}/mcp", data=b"{}", method="POST",
                                 headers={"Host": "evil.example", "content-type": "application/json",
                                          "accept": "application/json, text/event-stream",
                                          "Authorization": f"Bearer {token}"})
    with pytest.raises(urllib.error.HTTPError) as ex:
        urllib.request.urlopen(req, timeout=5)
    assert ex.value.code in (403, 421)


@pytest.mark.desktop
def test_agent_drives_calculator_over_mcp(daemon):
    _, port, token = daemon
    subprocess.Popen(["calc.exe"])
    hwnd = None
    for _ in range(80):
        wins = call(port, token, "windows", {"title_re": "^Calculator$"})["windows"]
        if wins:
            hwnd = wins[0]["hwnd"]
            break
        time.sleep(0.25)
    assert hwnd
    try:
        assert call(port, token, "take_scope", {"scope": f"window:{hwnd}"})["ok"]
        for aid in ("clearButton", "num6Button", "multiplyButton", "num7Button", "equalButton"):
            ref = call(port, token, "find", {"auto_id": aid})["elements"][0]["ref"]
            assert call(port, token, "act", {"action": "invoke", "ref": ref})["ok"]
        res = call(port, token, "find", {"auto_id": "CalculatorResults"})["elements"][0]
        assert res["name"].strip().endswith("42")
        assert call(port, token, "release_scope")["released"]
    finally:
        from pywinauto import Desktop
        try:
            Desktop(backend="uia").window(handle=hwnd).close()
        except Exception:
            pass


def test_input_mode_refusals_reach_the_agent_as_structured_errors(daemon):
    from overshoulder.gateway.errors import NeedsRealInput, PersonActive

    d, _, _ = daemon
    for err in (NeedsRealInput("input mode is never"), PersonActive("the person is typing")):
        def boom(err=err):
            raise err
        assert d.call(boom) == {"ok": False, "error": {"code": err.code, "message": str(err)}}
