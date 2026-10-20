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
    base = dict(desktop=lambda: None, not_elevated=lambda pid: None, foreground_in_scope=lambda s: 0x10)
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
