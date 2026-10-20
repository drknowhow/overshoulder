import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import scope as sc  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway import errors as E  # noqa: E402
from overshoulder.gateway import policy  # noqa: E402
from overshoulder.gateway.core import Checks, Target, gateway_for_tests  # noqa: E402
from overshoulder.gateway.lease import QUEUE_STALE_S, LeaseManager  # noqa: E402


class FakeScope:
    kind = "window"

    def __init__(self, alive=True, owned=(0x10,)):
        self._bounds, self.alive, self.owned = Rect(0, 0, 100, 100), alive, set(owned)

    def describe(self):
        return {"kind": self.kind}

    def bounds(self):
        return self._bounds

    def check_alive(self):
        if not self.alive:
            raise sc.ScopeStale("gone")

    def owns_window(self, hwnd):
        return hwnd in self.owned


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def ok_checks(**over):
    base = dict(desktop=lambda: None, not_elevated=lambda pid: None, foreground_in_scope=lambda s: 0x10,
                window_at_point=lambda s, x, y: 0x10, element_at_point=lambda x, y: None,
                focused_element=lambda: {"name": "", "type": "Document", "pid": 1, "password": False},
                pid_of=lambda h: 1)
    return Checks(**{**base, **over})


@pytest.fixture
def gw(tmp_path):
    scope = FakeScope()
    g = gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda spec: scope)
    token = g.registry.add("agent-a")
    lease = g.acquire(token, "window:16")
    return g, token, lease["lease_id"], scope


def rows(g):
    return [json.loads(x) for x in g.log.path.read_text(encoding="utf-8").splitlines()]


# -- lease ---------------------------------------------------------------------------------------

def test_second_client_queues_behind_the_holder():
    lm = LeaseManager(clock=Clock())
    lm.acquire("a", FakeScope())
    with pytest.raises(E.LeaseBusy) as ex:
        lm.acquire("b", FakeScope())
    assert ex.value.holder == "a" and ex.value.position == 1


def test_queue_head_gets_the_lease_before_later_clients():
    lm = LeaseManager(clock=Clock())
    held = lm.acquire("a", FakeScope())
    for c in ("b", "c"):
        with pytest.raises(E.LeaseBusy):
            lm.acquire(c, FakeScope())
    lm.release(held.id, "a")
    with pytest.raises(E.LeaseBusy):
        lm.acquire("c", FakeScope())
    assert lm.acquire("b", FakeScope()).client == "b"


def test_abandoned_queue_entry_stops_blocking_others():
    clock = Clock()
    lm = LeaseManager(clock=clock)
    held = lm.acquire("a", FakeScope())
    with pytest.raises(E.LeaseBusy):
        lm.acquire("b", FakeScope())
    lm.release(held.id, "a")
    clock.t += QUEUE_STALE_S + 1
    assert lm.acquire("c", FakeScope()).client == "c"


def test_lease_expires_without_heartbeat():
    clock = Clock()
    lm = LeaseManager(clock=clock)
    lease = lm.acquire("a", FakeScope(), ttl_s=10)
    clock.t += 5
    lm.heartbeat(lease.id, "a")
    clock.t += 9
    lm.check(lease.id, "a")
    clock.t += 2
    with pytest.raises(E.LeaseInvalid):
        lm.check(lease.id, "a")


def test_revoked_lease_stays_revoked():
    lm = LeaseManager(clock=Clock())
    lease = lm.acquire("a", FakeScope())
    lm.revoke("panic key")
    with pytest.raises(E.LeaseRevoked):
        lm.check(lease.id, "a")


def test_pause_blocks_actions_and_new_leases():
    lm = LeaseManager(clock=Clock())
    lease = lm.acquire("a", FakeScope())
    lm.pause("human input")
    with pytest.raises(E.Paused):
        lm.check(lease.id, "a")
    with pytest.raises(E.Paused):
        lm.acquire("b", FakeScope())
    lm.resume()
    lm.check(lease.id, "a")


def test_other_client_cannot_use_a_stolen_lease_id():
    lm = LeaseManager(clock=Clock())
    lease = lm.acquire("a", FakeScope())
    with pytest.raises(E.LeaseInvalid):
        lm.check(lease.id, "b")


# -- clients -------------------------------------------------------------------------------------

def test_registry_stores_only_token_hashes(tmp_path):
    from overshoulder.gateway.clients import ClientRegistry
    reg = ClientRegistry(tmp_path / "c.json")
    token = reg.add("agent-a")
    assert token not in (tmp_path / "c.json").read_text()
    assert ClientRegistry(tmp_path / "c.json").authenticate(token).name == "agent-a"
    with pytest.raises(E.Unauthorized):
        reg.authenticate("os_wrong")


def test_screen_scope_needs_approval_by_default(tmp_path):
    screen = FakeScope()
    screen.kind = "screen"
    g = gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda s: screen)
    token = g.registry.add("agent-a")
    with pytest.raises(E.ApprovalDenied):
        g.acquire(token, "screen:all")
    g.approver = policy.CallbackApprover(lambda req: req["kind"] == "lease")
    assert g.acquire(token, "screen:all")["lease_id"]


def test_denied_grant_refuses_the_scope_kind(tmp_path):
    g = gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda s: FakeScope())
    token = g.registry.add("agent-a", window="deny")
    with pytest.raises(E.NotGranted):
        g.acquire(token, "window:16")


# -- action pipeline -----------------------------------------------------------------------------

def test_action_runs_once_and_is_logged(gw):
    g, token, lease_id, _ = gw
    calls = []
    out = g.run(token, lease_id, "click", lambda t, p: calls.append(1) or "clicked",
                target=Target("point", x=10, y=10))
    assert out["ok"] and calls == [1]
    last = rows(g)[-1]
    assert last["event"] == "action" and last["ok"] and last["action"] == "click"


def test_point_outside_scope_is_refused_and_not_performed(gw):
    g, token, lease_id, _ = gw
    calls = []
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "click", lambda t, p: calls.append(1), target=Target("point", x=150, y=10))
    assert calls == []
    assert rows(g)[-1]["error"]["code"] == "out_of_scope"


def test_target_that_moved_since_observe_is_stale(gw):
    g, token, lease_id, _ = gw
    moved = Target("element", rect=Rect(20, 20, 40, 40), name="OK")
    t = Target("element", rect=Rect(10, 10, 30, 30), name="OK", revalidate=lambda: moved)
    with pytest.raises(E.TargetStale):
        g.run(token, lease_id, "click", lambda t, p: None, target=t)


def test_target_that_vanished_is_stale(gw):
    g, token, lease_id, _ = gw
    t = Target("element", rect=Rect(10, 10, 30, 30), revalidate=lambda: None)
    with pytest.raises(E.TargetStale):
        g.run(token, lease_id, "click", lambda t, p: None, target=t)


def test_element_in_a_window_outside_scope_is_refused(gw):
    g, token, lease_id, _ = gw
    t = Target("element", rect=Rect(10, 10, 30, 30), hwnd=0x99)
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "click", lambda t, p: None, target=t)


def test_typing_into_password_field_is_a_hard_deny(gw):
    g, token, lease_id, _ = gw
    t = Target("element", rect=Rect(10, 10, 30, 30), is_password=True)
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "type", lambda t, p: None, target=t, params={"text": "x"})


def test_consequential_click_needs_approval(gw):
    g, token, lease_id, _ = gw
    send = Target("element", rect=Rect(10, 10, 30, 30), name="Send")
    with pytest.raises(E.ApprovalDenied):
        g.run(token, lease_id, "click", lambda t, p: None, target=send)
    seen = []
    g.approver = policy.CallbackApprover(lambda req: seen.append(req) or True)
    assert g.run(token, lease_id, "click", lambda t, p: "sent", target=send)["ok"]
    assert seen[0]["reason"] == "click on 'Send'"


def test_read_only_observe_skips_approval_and_focus(gw):
    g, token, lease_id, _ = gw
    g.checks.foreground_in_scope = lambda s: (_ for _ in ()).throw(AssertionError("focus checked"))
    t = Target("element", rect=Rect(10, 10, 30, 30), name="Delete")
    assert g.run(token, lease_id, "observe", lambda t, p: {}, target=t, read_only=True)["ok"]


def test_keyboard_action_requires_foreground_in_scope(gw):
    g, token, lease_id, _ = gw

    def not_in_scope(scope):
        raise sc.OutOfScope("foreground elsewhere")

    g.checks.foreground_in_scope = not_in_scope
    calls = []
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "type", lambda t, p: calls.append(1), params={"text": "hi"})
    assert calls == []


def test_secure_desktop_blocks_every_action(gw):
    g, token, lease_id, _ = gw

    def secure():
        raise E.Denied("secure desktop")

    g.checks.desktop = secure
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "observe", lambda t, p: {}, read_only=True)


def test_elevated_target_process_is_denied(gw):
    g, token, lease_id, _ = gw

    def elevated(pid):
        raise E.Denied("elevated")

    g.checks.not_elevated = elevated
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "click", lambda t, p: None, target=Target("point", x=5, y=5, pid=42))


def test_failed_verify_is_reported_and_logged(gw):
    g, token, lease_id, _ = gw
    with pytest.raises(E.VerifyFailed):
        g.run(token, lease_id, "click", lambda t, p: None, target=Target("point", x=5, y=5),
              verify=lambda t, p, r: {"ok": False})
    assert rows(g)[-1]["error"]["code"] == "verify_failed"


def test_panic_revokes_and_next_action_is_refused(gw):
    g, token, lease_id, _ = gw
    g.panic()
    with pytest.raises(E.LeaseRevoked):
        g.run(token, lease_id, "observe", lambda t, p: {}, read_only=True)
    assert any(r["event"] == "lease_revoked" for r in rows(g))


def test_dead_scope_is_refused(gw):
    g, token, lease_id, scope = gw
    scope.alive = False
    with pytest.raises(sc.ScopeStale):
        g.run(token, lease_id, "observe", lambda t, p: {}, read_only=True)


def test_secret_params_never_reach_the_log(gw):
    g, token, lease_id, _ = gw
    g.run(token, lease_id, "observe", lambda t, p: {}, params={"secret": "hunter2", "q": 1}, read_only=True)
    assert "hunter2" not in g.log.path.read_text(encoding="utf-8")


# -- policy (real Win32 checks against this process) --------------------------------------------

def test_approval_reason_matches_generic_words_only():
    assert policy.approval_reason("click", "Delete file")
    assert policy.approval_reason("click", "Buy now")
    assert policy.approval_reason("click", "Deleted items") is None
    assert policy.approval_reason("click", "Open") is None
    assert policy.approval_reason("hotkey", params={"combo": "Alt+F4"})
    assert policy.approval_reason("hotkey", params={"combo": "ctrl+s"}) is None


def test_this_process_can_inspect_its_own_elevation():
    import os
    assert policy.process_elevated(os.getpid()) in (True, False)


def test_unknown_process_is_treated_as_elevated():
    with pytest.raises(E.Denied):
        policy.require_not_elevated(4, check=lambda pid: None)


@pytest.mark.desktop
def test_interactive_session_input_desktop_is_default():
    assert policy.input_desktop_name() == "Default"
    policy.require_default_desktop()


# -- review findings (2026-10-20) ---------------------------------------------------------------

def test_panic_mid_type_stops_the_remaining_characters(gw):
    g, token, lease_id, _ = gw
    typed = []

    def perform(t, p):
        for i, ch in enumerate("abcdef"):
            p["abort"]()
            typed.append(ch)
            if i == 1:
                g.panic()

    with pytest.raises(E.LeaseRevoked):
        g.run(token, lease_id, "type", perform, params={"text": "abcdef"})
    assert typed == ["a", "b"]


def test_panic_pauses_so_the_client_cannot_reacquire(gw):
    g, token, _, _ = gw
    g.panic()
    with pytest.raises(E.Paused):
        g.acquire(token, "window:16")
    g.resume()
    assert g.acquire(token, "window:16")["lease_id"]


def test_click_on_a_covering_window_is_refused(gw):
    g, token, lease_id, _ = gw

    def covered(scope, x, y):
        raise sc.OutOfScope("another window is on top")

    g.checks.window_at_point = covered
    calls = []
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "click", lambda t, p: calls.append(1), target=Target("point", x=5, y=5))
    assert calls == []


def test_click_on_elevated_window_under_point_is_denied(gw):
    g, token, lease_id, _ = gw

    def elevated(pid):
        if pid == 1:
            raise E.Denied("elevated")

    g.checks.not_elevated = elevated
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "click", lambda t, p: None, target=Target("point", x=5, y=5))


def test_point_click_on_a_named_send_button_needs_approval(gw):
    g, token, lease_id, _ = gw
    g.checks.element_at_point = lambda x, y: {"name": "Send", "type": "Button", "pid": 1, "password": False}
    with pytest.raises(E.ApprovalDenied):
        g.run(token, lease_id, "click", lambda t, p: None, target=Target("point", x=5, y=5))


@pytest.mark.parametrize("combo", ["del", "Shift+Del", "shift+ctrl+delete", "F4+Alt"])
def test_hotkey_spellings_of_consequential_keys_need_approval(gw, combo):
    g, token, lease_id, _ = gw
    with pytest.raises(E.ApprovalDenied):
        g.run(token, lease_id, "hotkey", lambda t, p: None, params={"combo": combo})


@pytest.mark.parametrize("combo", ["win+r", "win+d", "alt+tab", "ctrl+shift+esc", "alt+esc", "ctrl+esc"])
def test_scope_escaping_hotkeys_are_hard_denied(gw, combo):
    g, token, lease_id, _ = gw
    g.approver = policy.CallbackApprover(lambda req: True)
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "hotkey", lambda t, p: None, params={"combo": combo})


def test_type_without_target_into_focused_password_box_is_denied(gw):
    g, token, lease_id, _ = gw
    g.checks.focused_element = lambda: {"name": "Password", "type": "Edit", "pid": 1, "password": True}
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "type", lambda t, p: None, params={"text": "x"})


def test_type_without_target_fails_closed_when_focus_is_unknown(gw):
    g, token, lease_id, _ = gw
    g.checks.focused_element = lambda: None
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "type", lambda t, p: None, params={"text": "x"})


def test_newline_into_single_line_field_needs_approval(gw):
    g, token, lease_id, _ = gw
    g.checks.focused_element = lambda: {"name": "Search", "type": "Edit", "pid": 1, "password": False}
    with pytest.raises(E.ApprovalDenied):
        g.run(token, lease_id, "type", lambda t, p: None, params={"text": "hello\n"})
    g.checks.focused_element = lambda: {"name": "", "type": "Document", "pid": 1, "password": False}
    assert g.run(token, lease_id, "type", lambda t, p: None, params={"text": "line1\nline2"})["ok"]


def test_typed_text_is_redacted_in_the_log_unless_the_lease_opts_in(tmp_path):
    scope = FakeScope()
    g = gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda s: scope)
    token = g.registry.add("agent-a")
    lease_id = g.acquire(token, "window:16")["lease_id"]
    g.run(token, lease_id, "type", lambda t, p: None, params={"text": "hunter2"})
    assert "hunter2" not in g.log.path.read_text(encoding="utf-8")
    g.release(token, lease_id)
    lease_id = g.acquire(token, "window:16", log_text=True)["lease_id"]
    g.run(token, lease_id, "type", lambda t, p: None, params={"text": "visible caption"})
    assert "visible caption" in g.log.path.read_text(encoding="utf-8")


def test_token_removed_by_another_process_stops_working(tmp_path):
    import time as _t

    from overshoulder.gateway.clients import ClientRegistry
    path = tmp_path / "c.json"
    running = ClientRegistry(path)
    token = running.add("agent-a")
    _t.sleep(0.05)
    other = ClientRegistry(path)
    other.remove("agent-a")
    with pytest.raises(E.Unauthorized):
        running.authenticate(token)


def test_revoked_lease_memory_is_bounded():
    from overshoulder.gateway import lease as L
    lm = LeaseManager(clock=Clock())
    for _ in range(L.REVOKED_MEMORY + 50):
        lm.release(lm.acquire("a", FakeScope()).id, "a")
    assert len(lm._revoked) == L.REVOKED_MEMORY
