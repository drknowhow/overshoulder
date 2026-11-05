import json
import sys
import threading
from dataclasses import asdict
from urllib.parse import urlencode

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_gateway import FakeScope, ok_checks  # noqa: E402
from test_mcp_server import daemon  # noqa: E402,F401  (fixture reuse)
from test_settings_http import _http  # noqa: E402

from overshoulder import phone  # noqa: E402
from overshoulder.gateway import errors as E  # noqa: E402
from overshoulder.gateway.core import gateway_for_tests  # noqa: E402
from overshoulder.gateway.guard import (  # noqa: E402
    CONFIG_PAUSE,
    ConfigGuard,
    widened_clients,
    widened_phone,
    widened_settings,
)


def _client(name, **kw):
    return {"name": name, "token_sha256": kw.pop("sha", "h"), "window": "approve", "screen": "approve",
            "limits": {}, **kw}


# -- what counts as widening --------------------------------------------------------------------

def test_settings_widen_on_more_input_more_detection_and_vision():
    assert widened_settings({"input_mode": "borrow"}, {"input_mode": "full"})
    assert not widened_settings({"input_mode": "full"}, {"input_mode": "never"})
    assert widened_settings({"waits_enabled": False}, {"waits_enabled": True})
    assert widened_settings({"wait_kinds": ["file"]}, {"wait_kinds": ["file", "window_appears"]})
    assert widened_settings({"wait_processes": ["notepad.exe"]}, {"wait_processes": []})
    assert not widened_settings({"wait_processes": []}, {"wait_processes": ["notepad.exe"]})
    assert widened_settings({"vision_mode": "off"}, {"vision_mode": "cloud"})
    assert widened_settings({"vision_mode": "cloud", "vision_cloud_url": "https://a"},
                            {"vision_mode": "cloud", "vision_cloud_url": "https://b"})
    assert not widened_settings({}, {"poll_interval_ms": 900, "beep_on_detect": True})


def test_phone_widens_on_a_new_channel_or_a_new_destination():
    base = asdict(phone.PhoneSettings().ensure_secrets())
    assert widened_phone(base, {**base, "webhook_enabled": True})
    on = {**base, "ntfy_enabled": True}
    assert widened_phone(on, {**on, "ntfy_server": "https://evil.example"})
    assert widened_phone(on, {**on, "share_screenshot": True})
    assert not widened_phone(base, {**base, "ntfy_server": "https://elsewhere"})  # off, so nothing goes there
    assert not widened_phone(on, {**on, "ntfy_enabled": False})
    assert not widened_phone({}, base)  # making the secrets for the first time


def test_clients_widen_on_a_new_client_token_grant_or_lifted_limit():
    old = {"clients": [_client("a", limits={"read_only": True, "deny_processes": ["chrome.exe"]})]}
    assert widened_clients(old, {"clients": old["clients"] + [_client("b")]})
    assert widened_clients(old, {"clients": [_client("a", sha="other", limits=old["clients"][0]["limits"])]})
    assert widened_clients(old, {"clients": [{**old["clients"][0], "window": "allow"}]})
    assert widened_clients(old, {"clients": [{**old["clients"][0], "limits": {"deny_processes": ["chrome.exe"]}}]})
    assert widened_clients(old, {"clients": [{**old["clients"][0], "limits": {"read_only": True}}]})
    more = {**old["clients"][0]["limits"], "allow_builtin": ["cmd.exe"]}
    assert widened_clients(old, {"clients": [{**old["clients"][0], "limits": more}]})
    assert not widened_clients(old, {"clients": []})
    assert not widened_clients(old, {"clients": [{**old["clients"][0], "window": "deny"}]})


# -- the guard ----------------------------------------------------------------------------------

class Events:
    def __init__(self, answer=True):
        self.answer, self.asked, self.paused, self.resumed, self.logged = answer, [], [], [], []

    def guard(self, tmp_path, **files):
        paths = {}
        for name in ("settings", "phone", "clients"):
            p = tmp_path / f"{name}.json"
            if name in files:
                p.write_text(json.dumps(files[name]), encoding="utf-8")
            paths[name] = p
        return ConfigGuard(paths, self.ask, self.paused.append, lambda r: self.resumed.append(r) or True,
                           lambda ev, **kw: self.logged.append((ev, kw)))

    def ask(self, req):
        self.asked.append(req)
        return self.answer(req) if callable(self.answer) else self.answer


def test_a_narrowing_change_is_taken_without_asking(tmp_path):
    ev = Events()
    g = ev.guard(tmp_path, settings={"input_mode": "full"})
    (tmp_path / "settings.json").write_text(json.dumps({"input_mode": "never"}), encoding="utf-8")
    assert g.check() == [] and ev.asked == [] and ev.paused == []
    assert ev.logged[-1][0] == "config_accepted"


def test_a_widening_change_pauses_asks_and_a_no_puts_it_back(tmp_path):
    ev = Events(answer=False)
    old = {"clients": [_client("a")]}
    g = ev.guard(tmp_path, clients=old)
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"clients": old["clients"] + [_client("intruder")]}), encoding="utf-8")
    assert g.check() and ev.paused == [CONFIG_PAUSE]
    g.wait(5)
    assert "intruder" in ev.asked[0]["reason"] and ev.resumed == [CONFIG_PAUSE]
    assert json.loads(path.read_text(encoding="utf-8")) == old
    assert g.check() == []  # the restored file is the accepted one


def test_a_yes_keeps_the_change(tmp_path):
    ev = Events(answer=True)
    g = ev.guard(tmp_path, settings={"input_mode": "borrow"})
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"input_mode": "full"}), encoding="utf-8")
    assert g.check()
    g.wait(5)
    assert json.loads(path.read_text(encoding="utf-8")) == {"input_mode": "full"}
    assert g.check() == [] and len(ev.asked) == 1


def test_a_new_file_that_widens_is_asked_about_and_removed_on_no(tmp_path):
    ev = Events(answer=False)
    g = ev.guard(tmp_path)  # no files yet
    path = tmp_path / "phone.json"
    path.write_text(json.dumps({"webhook_enabled": True, "webhook_url": "https://evil.example"}), encoding="utf-8")
    assert g.check()
    g.wait(5)
    assert not path.exists()


def test_while_the_question_is_open_every_check_stays_paused(tmp_path):
    gate = threading.Event()
    ev = Events(answer=lambda req: gate.wait(5))
    g = ev.guard(tmp_path, settings={"input_mode": "never"})
    (tmp_path / "settings.json").write_text(json.dumps({"input_mode": "full"}), encoding="utf-8")
    assert g.check() and g.check()
    gate.set()
    g.wait(5)
    assert g.check() == [] and len(ev.asked) == 1


def test_propose_asks_before_writing_and_writes_nothing_on_no(tmp_path):
    ev = Events(answer=False)
    g = ev.guard(tmp_path, settings={"input_mode": "never"})
    wrote = []
    assert g.propose("settings", {"input_mode": "full"}, lambda: wrote.append(1))
    assert wrote == [] and len(ev.asked) == 1
    assert g.propose("settings", {"input_mode": "never", "beep_on_detect": True}, lambda: wrote.append(1)) is None
    assert wrote == [1] and len(ev.asked) == 1  # narrowing or neutral: no question


# -- in the gateway and the daemon --------------------------------------------------------------

def test_a_client_added_behind_the_gateways_back_cannot_act_until_the_person_says_yes(tmp_path):
    from overshoulder.gateway.clients import ClientRegistry
    g = gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda s: FakeScope())
    g.registry.add("known", window="allow")
    gate = threading.Event()
    answers = []

    def ask(req):
        gate.wait(5)
        answers.append(req["reason"])
        return False

    g.config_guard = ConfigGuard({"clients": g.registry.path}, ask, g.pause, lambda r: g.resume(only_if=r), g.log.write)
    token = ClientRegistry(g.registry.path).add("sneaky", window="allow")  # another process, e.g. the CLI
    with pytest.raises(E.Paused):
        g.acquire(token, "window:16")
    gate.set()
    g.config_guard.wait(5)
    assert "sneaky" in answers[0] and not g.status()["paused"]
    with pytest.raises(E.Unauthorized):
        g.acquire(token, "window:16")


def test_the_settings_page_asks_on_screen_before_widening(daemon):  # noqa: F811
    from overshoulder import settings as st

    d, port, _ = daemon
    d.port_ref["port"] = port
    asked = []
    g = d.start_config_guard(ask=lambda req: asked.append(req["reason"]) or False, poll_s=60)
    try:
        base = f"http://127.0.0.1:{port}"
        _, _, body = _http("POST", f"{base}/operator/settings_link", b"{}",
                           {"x-overshoulder-operator": d.operator_key, "content-type": "application/json"})
        _, headers, _ = _http("GET", json.loads(body)["url"])
        cookie = (headers.get("set-cookie") or headers.get("Set-Cookie")).split(";")[0]
        page = _http("GET", f"{base}/settings", cookie=cookie)[2]
        token = page.split('name="form_token" value="')[1].split('"')[0]
        before = st.load_or_default().input_mode
        form = urlencode({"form_token": token, "waits_enabled": "on", "kind_file": "on",
                          "input_mode": "full"}).encode()
        status, hdrs, _ = _http("POST", f"{base}/settings", form,
                                {"content-type": "application/x-www-form-urlencoded"}, cookie)
        assert status == 303 and "msg=refused" in (hdrs.get("location") or hdrs.get("Location"))
        assert asked and "input mode" in asked[0] and st.load_or_default().input_mode == before
        assert g.check() == []
    finally:
        d.stop()
