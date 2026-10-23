"""Narrowing: the caller's own limits from take_scope and the person's standing limits per client."""

import sys
import types

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_gateway import FakeScope, ok_checks  # noqa: E402

from overshoulder import cli  # noqa: E402
from overshoulder import scope as sc  # noqa: E402
from overshoulder import settings as st  # noqa: E402
from overshoulder import waits as W  # noqa: E402
from overshoulder.driver.windows import Rect, WindowInfo  # noqa: E402
from overshoulder.gateway import errors as E  # noqa: E402
from overshoulder.gateway.clients import ClientRegistry  # noqa: E402
from overshoulder.gateway.core import Target, gateway_for_tests  # noqa: E402
from overshoulder.gateway.limits import LeaseLimits, Limits, NarrowedScope, black_out, process_key  # noqa: E402
from overshoulder.recorder import Recording  # noqa: E402

# Fake desktop: pid 1 is the scope's own app, pid 2 is chrome.
PROCS = {1: "notepad.exe", 2: "chrome.exe"}
WINDOW_PID = {0x10: 1, 0x20: 2}


def narrowing_checks(**over):
    return ok_checks(pid_process=lambda pid: PROCS.get(pid, ""),
                     window_processes=lambda h: [PROCS.get(WINDOW_PID.get(h), "")],
                     window_under=lambda x, y: 0x20 if x >= 50 else 0x10, **over)


def make(tmp_path, scope=None, person=None, **acquire):
    scope = scope or FakeScope(owned=(0x10, 0x20))
    g = gateway_for_tests(tmp_path, checks=narrowing_checks(), scope_factory=lambda spec: scope)
    token = g.registry.add("agent-a", limits=person)
    lease = g.acquire(token, "screen:1", **acquire)
    return g, token, lease


def noop(t, p):
    return {}


def refused(g, token, lease_id, action, **kw):
    with pytest.raises(E.Narrowed) as ex:
        g.run(token, lease_id, action, noop, **kw)
    return ex.value


# -- the limits themselves -----------------------------------------------------------------------

def test_process_names_are_matched_without_case_or_the_exe():
    lim = Limits.make(deny_processes=["Chrome", "C:\\Apps\\Slack.EXE"])
    assert lim.deny_processes == {"chrome.exe", "slack.exe"}
    assert lim.denies("CHROME.EXE") and not lim.denies("notepad.exe") and not lim.denies("")
    assert process_key("msedge") == "msedge.exe"


def test_merged_limits_keep_every_limit_from_both():
    a = Limits.make(read_only=True, deny_processes=["chrome"])
    b = Limits.make(no_typing=True, deny_processes=["slack"])
    m = a.merge(b)
    assert m.read_only and m.no_typing and m.deny_processes == {"chrome.exe", "slack.exe"}
    assert m.describe() == "read-only; no typing; hides chrome.exe, slack.exe"
    assert not Limits() and Limits().describe() == "none"


# -- read_only and no_typing ----------------------------------------------------------------------

def test_read_only_refuses_acts_but_not_looking(tmp_path):
    g, token, lease = make(tmp_path, limits={"read_only": True})
    assert lease["limits"]["read_only"] is True
    for action in ("click", "invoke", "type", "set_value", "hotkey", "toggle"):
        e = refused(g, token, lease["lease_id"], action)
        assert e.limit == "read_only" and e.set_by == "session"
        assert e.to_dict()["code"] == "narrowed"
    for action in ("observe", "find", "screenshot", "record_start"):
        assert g.run(token, lease["lease_id"], action, noop, read_only=True)["ok"]


def test_no_typing_refuses_type_set_value_and_hotkey_only(tmp_path):
    g, token, lease = make(tmp_path, limits={"no_typing": True})
    for action in ("type", "set_value", "hotkey"):
        e = refused(g, token, lease["lease_id"], action, params={"text": "x", "combo": "ctrl+s"})
        assert e.limit == "no_typing"
    assert g.run(token, lease["lease_id"], "click", noop, target=Target("point", x=5, y=5))["ok"]


def test_a_refusal_is_logged_with_its_limit(tmp_path):
    import json
    g, token, lease = make(tmp_path, limits={"read_only": True})
    refused(g, token, lease["lease_id"], "click")
    row = [json.loads(x) for x in g.log.path.read_text(encoding="utf-8").splitlines()][-1]
    assert row["ok"] is False and row["error"]["code"] == "narrowed" and row["error"]["limit"] == "read_only"


# -- the person's limits ----------------------------------------------------------------------------

def test_the_persons_limit_holds_whatever_the_agent_asks_for(tmp_path):
    g, token, lease = make(tmp_path, person={"read_only": True}, limits={"read_only": False})
    assert lease["limits"]["read_only"] and lease["limits"]["set_by_person"]["read_only"]
    e = refused(g, token, lease["lease_id"], "click")
    assert e.set_by == "person" and "the person set this limit" in str(e)


def test_a_limit_the_person_adds_mid_lease_applies_at_once(tmp_path):
    g, token, lease = make(tmp_path)
    assert g.run(token, lease["lease_id"], "invoke", noop)["ok"]
    ClientRegistry(g.registry.path).set_limits("agent-a", no_typing=True)  # the CLI, another process
    assert refused(g, token, lease["lease_id"], "type", params={"text": "x"}).set_by == "person"
    assert g.status()["active"]["limits"]["no_typing"] is True


def test_rotating_a_token_keeps_the_clients_limits(tmp_path):
    reg = ClientRegistry(tmp_path / "clients.json")
    reg.add("a", limits={"no_typing": True, "deny_processes": ["chrome"]})
    reg.add("a")  # rotate
    assert Limits.from_dict(reg.get("a").limits) == Limits.make(no_typing=True, deny_processes=["chrome"])


def test_set_limits_adds_and_takes_away_programs(tmp_path):
    reg = ClientRegistry(tmp_path / "clients.json")
    reg.add("a")
    reg.set_limits("a", deny=["chrome", "slack"])
    assert reg.set_limits("a", allow=["slack"])["deny_processes"] == ["chrome.exe"]
    assert reg.set_limits("a", read_only=True)["read_only"] is True
    assert reg.set_limits("a", clear=True) == Limits().to_dict()
    with pytest.raises(KeyError):
        reg.set_limits("nobody", read_only=True)


def test_cli_sets_and_lifts_a_clients_limits(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path))
    assert cli.main(["client", "add", "a", "--no-typing", "--deny-process", "chrome"]) == 0
    capsys.readouterr()
    assert cli.main(["client", "limit", "a", "--read-only", "--typing"]) == 0
    assert capsys.readouterr().out.strip() == "a: read-only; hides chrome.exe"
    assert cli.main(["client", "list"]) == 0
    assert "a  (read-only; hides chrome.exe)" in capsys.readouterr().out
    assert cli.main(["client", "limit", "nobody", "--read-only"]) == 2


# -- re-scoping ----------------------------------------------------------------------------------------

def test_taking_a_scope_again_keeps_the_limits_the_lease_had(tmp_path):
    g, token, lease = make(tmp_path, limits={"no_typing": True})
    again = g.acquire(token, "screen:1", limits={"read_only": True})
    assert again["lease_id"] == lease["lease_id"]
    assert again["limits"]["no_typing"] and again["limits"]["read_only"]
    g.release(token, lease["lease_id"])
    fresh = g.acquire(token, "screen:1")
    assert not fresh["limits"]["no_typing"]  # a released lease's own limits end with it


# -- deny_processes ------------------------------------------------------------------------------------

def test_denied_programs_windows_are_out_of_scope(tmp_path):
    g, token, lease = make(tmp_path, limits={"deny_processes": ["chrome"]})
    scope = g.leases.active().scope
    assert scope.owns_window(0x10) and not scope.owns_window(0x20)
    assert scope.describe()["hidden"] == ["chrome.exe"]


def test_acts_on_a_denied_program_are_refused_by_name(tmp_path):
    g, token, lease = make(tmp_path, limits={"deny_processes": ["chrome"]})
    lid = lease["lease_id"]
    e = refused(g, token, lid, "click", target=Target("point", x=60, y=5))  # the point is over chrome
    assert e.limit == "deny_processes" and "chrome.exe" in str(e)
    assert refused(g, token, lid, "invoke", target=Target("element", rect=Rect(0, 0, 10, 10), pid=2,
                                                          hwnd=0x20)).limit == "deny_processes"
    assert g.run(token, lid, "click", noop, target=Target("point", x=5, y=5))["ok"]  # notepad is fine


def test_a_point_in_a_window_scope_goes_to_that_window_even_where_a_denied_one_covers_it(tmp_path):
    """Background actions are sent to the scope's own window, so the denied program on top of the point is
    not the target, and an approval asks about the scope's program."""
    scope = FakeScope(owned=(0x10,))
    scope.binding = types.SimpleNamespace(hwnd=0x10, pid=1)
    g, token, lease = make(tmp_path, scope=scope, limits={"deny_processes": ["chrome"]})
    target = Target("point", x=60, y=5)  # chrome's window is on top here
    assert g.run(token, lease["lease_id"], "click", noop, target=target)["ok"]
    assert g._asked_about(g.leases.active().scope, target)["process"] == "notepad.exe"


def test_keys_are_refused_when_a_denied_program_has_the_focus(tmp_path):
    g, token, lease = make(tmp_path, limits={"deny_processes": ["chrome"]})
    g.checks.scope_focus = lambda s: {"name": "", "type": "Edit", "pid": 2, "password": False}
    assert refused(g, token, lease["lease_id"], "type", params={"text": "x"}).limit == "deny_processes"


def test_a_window_scope_on_a_denied_program_is_refused(tmp_path):
    class Bound(FakeScope):
        binding = type("B", (), {"hwnd": 0x20, "pid": 2})()

    g = gateway_for_tests(tmp_path, checks=narrowing_checks(), scope_factory=lambda spec: Bound(owned=(0x20,)))
    token = g.registry.add("agent-a", limits={"deny_processes": ["chrome"]})
    with pytest.raises(E.Narrowed) as ex:
        g.acquire(token, "window:32")
    assert ex.value.set_by == "person" and g.leases.active() is None


def test_windows_and_waits_leave_out_denied_programs(tmp_path, monkeypatch):
    from overshoulder.service import Service
    infos = [WindowInfo(0x10, "Notes", "Notepad", 1, "notepad.exe", Rect(0, 0, 10, 10)),
             WindowInfo(0x20, "Inbox", "Chrome_WidgetWin_1", 2, "chrome.exe", Rect(0, 0, 10, 10))]
    monkeypatch.setattr(sc, "find_windows", lambda **kw: infos)
    monkeypatch.setattr(W, "_windows", lambda: infos)
    g = gateway_for_tests(tmp_path, checks=narrowing_checks())
    token = g.registry.add("agent-a", limits={"deny_processes": ["chrome"]})
    svc = Service(g, shots_dir=tmp_path / "shots")
    assert [x["process"] for x in svc.windows(token)] == ["notepad.exe"]
    hidden = g.hidden_for("agent-a").deny_processes
    with pytest.raises(W.WaitError):  # the only matching window is hidden, so there is nothing to watch
        W.Waiter([{"kind": "title_changes", "title_re": "Inbox"}], st.Settings(), hidden)
    waiter = W.Waiter([{"kind": "window_appears", "title_re": "Inbox"}], st.Settings(), hidden)
    infos.append(WindowInfo(0x21, "Inbox (2)", "Chrome_WidgetWin_1", 2, "chrome.exe", Rect(0, 0, 10, 10)))
    assert waiter.check() is None  # a hidden program's new window does not fire


def test_hidden_rects_cover_every_window_of_a_denied_program():
    lim = LeaseLimits(session=Limits.make(deny_processes=["chrome"]))
    windows = [(0x20, Rect(10, 10, 50, 40)), (0x21, Rect(60, 0, 80, 20)), (0x10, Rect(0, 0, 100, 100)),
               (0x22, Rect(-32000, -32000, -31999, -31999))]
    procs = {0x20: ["chrome.exe"], 0x21: ["chrome.exe"], 0x10: ["notepad.exe"], 0x22: ["chrome.exe"]}
    scope = NarrowedScope(FakeScope(owned=(0x10, 0x20, 0x21)), lambda: lim, window_processes=procs.get,
                          visible_windows=lambda: windows)
    assert scope.hidden_rects() == [Rect(10, 10, 50, 40), Rect(60, 0, 80, 20), Rect(-32000, -32000, -31999, -31999)]
    none = NarrowedScope(FakeScope(), lambda: LeaseLimits(), visible_windows=lambda: 1 / 0)
    assert none.hidden_rects() == []  # no deny list: the desktop is not even enumerated


def test_black_out_paints_only_the_hidden_rects():
    from PIL import Image
    img = Image.new("RGB", (100, 100), (255, 255, 255))
    black_out(img, Rect(100, 100, 200, 200), [Rect(110, 110, 150, 140), Rect(-32000, -32000, -31999, -31999)])
    assert img.getpixel((10, 10)) == (0, 0, 0) and img.getpixel((49, 39)) == (0, 0, 0)
    assert img.getpixel((50, 40)) == (255, 255, 255) and img.getpixel((5, 5)) == (255, 255, 255)


def test_screen_recording_frames_are_blacked_out_before_the_encoder(tmp_path):
    class Shot:
        size = (4, 2)
        rgb = bytes([255]) * (4 * 2 * 3)

    class Grabber:
        def grab(self, box):
            assert box == {"left": 0, "top": 0, "width": 4, "height": 2}
            return Shot()

    class Pipe:
        def __init__(self):
            self.data = b""

        def write(self, b):
            self.data += b

    class Hiding(FakeScope):
        kind = "screen"

        def hidden_rects(self):
            return [Rect(0, 0, 2, 2)]  # the left half

    rec = Recording(Hiding(), tmp_path)
    rec._origin = Rect(0, 0, 4, 2)
    rec._proc = type("P", (), {"stdin": Pipe()})()
    assert rec._write_screen_frame(Grabber())
    px = [rec._proc.stdin.data[i:i + 3] for i in range(0, 24, 3)]
    assert px == [b"\0\0\0", b"\0\0\0", b"\xff\xff\xff", b"\xff\xff\xff"] * 2
