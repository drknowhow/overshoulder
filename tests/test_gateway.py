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


DOC_FOCUS = {"name": "", "type": "Document", "pid": 1, "password": False, "hwnd": 0x10}
PERSON_WINDOW = 0x99  # the person's own app, in front unless a test says otherwise


class FakeWatch:
    seen = False
    ok = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def ok_checks(**over):
    base = dict(desktop=lambda: None, not_elevated=lambda pid: None, foreground_in_scope=lambda s: 0x10,
                window_at_point=lambda s, x, y: 0x10, element_at_point=lambda x, y: None,
                focused_element=lambda: dict(DOC_FOCUS), pid_of=lambda h: 1,
                scope_focus=lambda s: dict(DOC_FOCUS), element_in_scope_at=lambda s, x, y: None,
                root_of=lambda h: h, is_password_window=lambda h: False, person_idle_s=lambda: 999.0,
                foreground=lambda: PERSON_WINDOW, bring_forward=lambda h: True,
                desk_state=lambda: ((7, 8), PERSON_WINDOW),
                restore_desk=lambda s: {"cursor": True, "foreground": True}, person_watch=FakeWatch,
                left_open=lambda s, front: False)
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
    lm.pause("operator pause")
    with pytest.raises(E.Paused):
        lm.check(lease.id, "a", human_ok=True)
    with pytest.raises(E.Paused):
        lm.acquire("b", FakeScope())
    lm.resume()
    lm.check(lease.id, "a")


def test_a_human_pause_holds_back_only_the_persons_input():
    lm = LeaseManager(clock=Clock())
    lease = lm.acquire("a", FakeScope())
    lm.pause("human input")
    with pytest.raises(E.Paused):
        lm.check(lease.id, "a")
    assert lm.check(lease.id, "a", human_ok=True) is lease
    lm.release(lease.id, "a")
    assert lm.acquire("b", FakeScope())  # taking a scope does not touch the person's input


def test_background_and_read_actions_go_on_while_the_person_uses_the_pc(gw):
    g, token, lease_id, _ = gw
    g.pause("human input")
    assert g.run(token, lease_id, "observe", lambda t, p: {}, read_only=True)["ok"]
    out = g.run(token, lease_id, "type", lambda t, p: (p["abort"](), p["background_target"](0x10, typing=True)),
                params={"text": "hi"})
    assert out["ok"]


def test_background_work_stops_when_the_person_works_in_the_agents_window(gw):
    g, token, lease_id, _ = gw
    g.pause("human input")
    g.checks.foreground = lambda: 0x10  # the person clicked into the scope's window
    with pytest.raises(E.Paused):
        g.run(token, lease_id, "type", lambda t, p: p["abort"](), params={"text": "hi"})


def test_real_input_waits_out_a_brief_human_pause(gw):
    import threading
    g, token, lease_id, _ = gw
    g.pause("human input")
    threading.Timer(0.5, lambda: g.resume(only_if="human input")).start()
    out = g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))
    assert out["ok"]


def test_action_does_not_wait_out_a_panic(gw):
    g, token, lease_id, _ = gw
    g.human_wait_s = 5
    g.panic()
    import time as _t
    t0 = _t.monotonic()
    with pytest.raises(E.LeaseRevoked):
        g.run(token, lease_id, "observe", lambda t, p: {}, read_only=True)
    assert _t.monotonic() - t0 < 1


def test_human_pause_that_outlasts_the_wait_is_reported(gw):
    g, token, lease_id, _ = gw
    g.human_wait_s = 0.3
    g.pause("human input")
    with pytest.raises(E.Paused):
        g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))


def test_automatic_resume_cannot_undo_a_panic(tmp_path):
    g = gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda s: FakeScope())
    g.pause("human input")
    assert g.resume(only_if="human input")
    g.panic()
    assert not g.resume(only_if="human input")
    assert g.status()["paused"]


def test_status_reports_scope_bounds_for_the_outline():
    lm = LeaseManager(clock=Clock())
    lm.acquire("a", FakeScope())
    assert lm.status()["active"]["bounds"] == [0, 0, 100, 100]


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


def test_hidden_input_element_can_be_typed_into_but_not_clicked(gw):
    g, token, lease_id, _ = gw
    hidden = Target("element", rect=Rect(0, 0, 0, 0), hwnd=0x10, control_type="Edit")
    assert g.run(token, lease_id, "type", lambda t, p: None, target=hidden, params={"text": "x"})["ok"]
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "click", lambda t, p: None, target=hidden)
    outside = Target("element", rect=Rect(0, 0, 0, 0), hwnd=0x99, control_type="Edit")
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "type", lambda t, p: None, target=outside, params={"text": "x"})


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


def test_gateway_refuses_to_run_elevated(tmp_path, monkeypatch):
    monkeypatch.setattr(policy, "self_elevated", lambda: True)
    with pytest.raises(E.Denied):
        gateway_for_tests(tmp_path, allow_elevated=False)




# -- real input vs background (phase 2) ---------------------------------------------------------

def real(p):
    with p["borrow"]() as b:
        return b.check()


def test_real_input_typing_checks_foreground_after_focusing_the_target(gw):
    g, token, lease_id, _ = gw
    order = []
    g.checks.foreground_in_scope = lambda s: order.append("fg") or 0x10

    def perform(t, p):
        with p["borrow"]() as b:
            order.append("focus")
            b.check()
            order.append("type")

    g.run(token, lease_id, "type", perform, target=Target("element", rect=Rect(10, 10, 30, 30)),
          params={"text": "x"})
    assert order == ["focus", "fg", "type"]


def test_real_input_keys_refused_when_foreground_left_the_scope(gw):
    g, token, lease_id, _ = gw

    def not_in_scope(scope):
        raise sc.OutOfScope("foreground elsewhere")

    g.checks.foreground_in_scope = not_in_scope
    calls = []
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "type", lambda t, p: (real(p), calls.append(1)), params={"text": "hi"})
    assert calls == []


def test_background_typing_works_while_another_app_has_the_foreground(gw):
    g, token, lease_id, _ = gw
    g.checks.foreground_in_scope = lambda s: (_ for _ in ()).throw(sc.OutOfScope("person is elsewhere"))
    out = g.run(token, lease_id, "type", lambda t, p: p["background_target"](0x10, typing=True),
                params={"text": "hi"})
    assert out["ok"]


def test_real_click_on_a_covering_window_is_refused(gw):
    g, token, lease_id, _ = gw

    def covered(scope, x, y):
        raise sc.OutOfScope("another window is on top")

    g.checks.window_at_point = covered
    calls = []
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "click", lambda t, p: (real(p), calls.append(1)), target=Target("point", x=5, y=5))
    assert calls == []


def test_background_click_on_a_covered_scope_window_is_allowed(gw):
    g, token, lease_id, _ = gw
    g.checks.window_at_point = lambda s, x, y: (_ for _ in ()).throw(sc.OutOfScope("covered"))
    out = g.run(token, lease_id, "click", lambda t, p: p["background_target"](0x10),
                target=Target("point", x=5, y=5))
    assert out["ok"]


def test_real_click_on_elevated_window_under_point_is_denied(gw):
    g, token, lease_id, _ = gw

    def elevated(pid):
        if pid == 1:
            raise E.Denied("elevated")

    g.checks.not_elevated = elevated
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))


def test_background_target_outside_the_scope_is_refused(gw):
    g, token, lease_id, _ = gw
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "click", lambda t, p: p["background_target"](0x999),
              target=Target("point", x=5, y=5))


def test_background_typing_into_a_password_window_is_denied(gw):
    g, token, lease_id, _ = gw
    g.checks.is_password_window = lambda h: True
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "type", lambda t, p: p["background_target"](0x10, typing=True),
              params={"text": "x"})


def test_point_click_on_a_scope_element_named_send_needs_approval(gw):
    g, token, lease_id, _ = gw
    g.checks.element_in_scope_at = lambda s, x, y: {"name": "Send", "type": "Button", "pid": 1,
                                                     "password": False}
    with pytest.raises(E.ApprovalDenied):
        g.run(token, lease_id, "click", lambda t, p: None, target=Target("point", x=5, y=5))


def test_type_without_target_into_the_scopes_password_box_is_denied(gw):
    g, token, lease_id, _ = gw
    g.checks.scope_focus = lambda s: {**DOC_FOCUS, "type": "Edit", "password": True}
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "type", lambda t, p: None, params={"text": "x"})


def test_type_without_target_fails_closed_when_the_scope_focus_is_unknown(gw):
    g, token, lease_id, _ = gw
    g.checks.scope_focus = lambda s: None
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "type", lambda t, p: None, params={"text": "x"})
    g.checks.scope_focus = lambda s: {**DOC_FOCUS, "password": None}  # a password field exists somewhere
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "type", lambda t, p: None, params={"text": "x"})


def test_newline_into_single_line_field_needs_approval(gw):
    g, token, lease_id, _ = gw
    g.checks.scope_focus = lambda s: {**DOC_FOCUS, "type": "Edit"}
    with pytest.raises(E.ApprovalDenied):
        g.run(token, lease_id, "type", lambda t, p: None, params={"text": "hello\n"})
    g.checks.scope_focus = lambda s: dict(DOC_FOCUS)
    assert g.run(token, lease_id, "type", lambda t, p: None, params={"text": "line1\nline2"})["ok"]


def test_enter_hotkey_in_single_line_field_needs_approval(gw):
    g, token, lease_id, _ = gw
    g.checks.scope_focus = lambda s: {**DOC_FOCUS, "type": "Edit"}
    with pytest.raises(E.ApprovalDenied):
        g.run(token, lease_id, "hotkey", lambda t, p: None, params={"combo": "enter"})
    g.checks.scope_focus = lambda s: dict(DOC_FOCUS)
    assert g.run(token, lease_id, "hotkey", lambda t, p: None, params={"combo": "enter"})["ok"]


def test_agent_cannot_drive_overshoulders_own_windows_either_way(gw):
    import os
    g, token, lease_id, _ = gw
    g.checks.pid_of = lambda h: os.getpid()
    g.approver = policy.CallbackApprover(lambda req: True)
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "hotkey", lambda t, p: real(p), params={"combo": "enter"})
    with pytest.raises(E.Denied):
        g.run(token, lease_id, "click", lambda t, p: p["background_target"](0x10),
              target=Target("point", x=5, y=5))


# -- input mode: how much agents may use the person's mouse and keyboard -------------------------------

def _mode_gateway(tmp_path, idle_s=999.0):
    from overshoulder import settings as st
    path = tmp_path / "settings.json"
    g = gateway_for_tests(tmp_path, checks=ok_checks(person_idle_s=lambda: idle_s),
                          scope_factory=lambda s: FakeScope(), settings_source=lambda: st.load_or_default(path))
    token = g.registry.add("agent-a")
    return g, token, g.acquire(token, "window:16")["lease_id"], path


def _save_from_page(path, **form):
    """Save settings the way the settings page does: through its form parser."""
    from overshoulder import settings as st
    from overshoulder.settings_page import settings_from_form
    st.save(settings_from_form({"waits_enabled": "on", **form}), path)


def test_changing_the_input_mode_on_the_settings_page_changes_what_the_gateway_allows(tmp_path):
    g, token, lease_id, path = _mode_gateway(tmp_path)
    clicked = []

    def real_click(t, p):
        real(p)
        clicked.append(1)

    _save_from_page(path, input_mode="full")
    g.run(token, lease_id, "click", real_click, target=Target("point", x=5, y=5))
    _save_from_page(path, input_mode="never")
    with pytest.raises(E.NeedsRealInput):
        g.run(token, lease_id, "click", real_click, target=Target("point", x=5, y=5))
    assert clicked == [1]
    assert rows(g)[-1]["ok"] is False and rows(g)[-1]["error"]["code"] == "needs_real_input"
    assert g.status()["input_mode"] == "never"
    # Background work goes on in every mode.
    out = g.run(token, lease_id, "type", lambda t, p: p["background_target"](0x10, typing=True),
                params={"text": "hi"})
    assert out["ok"]


def test_borrow_mode_uses_real_input_only_after_the_person_has_been_idle(tmp_path):
    g, token, lease_id, path = _mode_gateway(tmp_path, idle_s=2.0)
    g.human_wait_s = 0.3
    _save_from_page(path, input_mode="borrow", borrow_idle_s="5")
    with pytest.raises(E.PersonActive):
        g.run(token, lease_id, "hotkey", lambda t, p: real(p), params={"combo": "ctrl+s"})
    g.checks.person_idle_s = lambda: 6.0
    assert g.run(token, lease_id, "hotkey", lambda t, p: real(p), params={"combo": "ctrl+s"})["ok"]


def test_the_default_input_mode_borrows_and_a_broken_settings_file_falls_back_to_it(tmp_path):
    g, token, lease_id, path = _mode_gateway(tmp_path, idle_s=0.5)
    g.human_wait_s = 0.3
    assert g.status()["input_mode"] == "borrow"  # no settings file yet
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(E.PersonActive):
        g.run(token, lease_id, "hotkey", lambda t, p: real(p), params={"combo": "ctrl+s"})


# -- borrowing politely ----------------------------------------------------------------------------------

class BoundScope(FakeScope):
    class binding:  # noqa: N801
        hwnd, pid = 0x10, 1


def _borrow_gateway(tmp_path, idles):
    """A borrow-mode gateway whose person goes idle along `idles` (one value per check, last one repeats)."""
    from overshoulder import settings as st
    seen = {"forward": [], "restored": [], "saved": 0}
    left = list(idles)

    def desk_state():
        seen["saved"] += 1
        return ((7, 8), PERSON_WINDOW)

    checks = ok_checks(person_idle_s=lambda: left.pop(0) if len(left) > 1 else left[0],
                       bring_forward=lambda h: seen["forward"].append(h) or True, desk_state=desk_state,
                       restore_desk=lambda s: seen["restored"].append(s) or {"cursor": True, "foreground": True})
    scope = BoundScope()
    g = gateway_for_tests(tmp_path, checks=checks, scope_factory=lambda s: scope,
                          settings_source=lambda: st.Settings(input_mode="borrow", borrow_idle_s=5))
    token = g.registry.add("agent-a")
    return g, token, g.acquire(token, "window:16")["lease_id"], seen


def test_borrow_waits_for_idle_then_puts_the_cursor_and_foreground_back(tmp_path):
    g, token, lease_id, seen = _borrow_gateway(tmp_path, [1.0, 2.5, 4.0, 6.0])
    out = g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))
    assert out["ok"]
    assert seen["forward"] == [0x10]  # the scope's window came forward for the click
    assert seen["restored"] == [((7, 8), PERSON_WINDOW)]  # and the person's cursor and window went back


def test_borrow_reports_what_it_did(tmp_path):
    g, token, lease_id, _ = _borrow_gateway(tmp_path, [6.0])

    def perform(t, p):
        with p["borrow"]() as b:
            b.check()
        return b.report

    report = g.run(token, lease_id, "click", perform, target=Target("point", x=5, y=5))["result"]
    assert report["mode"] == "borrow" and report["brought_forward"] and not report["person_active"]
    assert report["restored"] == {"cursor": True, "foreground": True}


def test_borrow_stops_and_leaves_things_alone_when_the_person_takes_over(tmp_path):
    g, token, lease_id, seen = _borrow_gateway(tmp_path, [6.0])
    typed = []

    def perform(t, p):
        with p["borrow"]() as b:
            b.check()
            for i, ch in enumerate("abcdef"):
                b.abort()
                typed.append(ch)
                if i == 1:
                    b.watch.seen = True  # the person touches the keyboard

    with pytest.raises(E.PersonActive):
        g.run(token, lease_id, "type", perform, params={"text": "abcdef"})
    assert typed == ["a", "b"] and seen["restored"] == []


def test_borrow_gives_up_while_the_person_keeps_working(tmp_path):
    g, token, lease_id, seen = _borrow_gateway(tmp_path, [0.5])
    g.human_wait_s = 0.3
    with pytest.raises(E.PersonActive):
        g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))
    assert seen["forward"] == [] and seen["restored"] == []


def test_activate_needs_full_control(tmp_path):
    g, token, lease_id, _ = _borrow_gateway(tmp_path, [6.0])
    with pytest.raises(E.NeedsRealInput):
        g.run(token, lease_id, "activate", lambda t, p: real(p))
    from overshoulder import settings as st
    g.settings_source = lambda: st.Settings(input_mode="full")
    assert g.run(token, lease_id, "activate", lambda t, p: real(p))["ok"]


def test_full_mode_brings_the_window_forward_and_leaves_it_there(tmp_path):
    from overshoulder import settings as st
    g, token, lease_id, seen = _borrow_gateway(tmp_path, [0.1])  # idle time does not matter in full mode
    g.settings_source = lambda: st.Settings(input_mode="full")
    assert g.run(token, lease_id, "hotkey", lambda t, p: real(p), params={"combo": "ctrl+s"})["ok"]
    assert seen["forward"] == [0x10] and seen["restored"] == []


def test_a_menu_opened_by_a_borrowed_click_stays_until_the_next_step(tmp_path):
    g, token, lease_id, seen = _borrow_gateway(tmp_path, [999.0])
    g.checks.left_open = lambda s, front: True  # the click opened a menu
    first = g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))
    assert seen["restored"] == []  # giving the foreground back would close the menu
    g.checks.left_open = lambda s, front: False  # the next click picks an item and the menu closes

    def perform(t, p):
        with p["borrow"]() as b:
            b.check()
        return b.report

    report = g.run(token, lease_id, "click", perform, target=Target("point", x=6, y=6))["result"]
    assert first["ok"] and seen["saved"] == 1  # the second step reuses what the first saved
    assert seen["restored"] == [((7, 8), PERSON_WINDOW)] and report["waited_s"] == 0.0


def test_release_gives_back_a_kept_window(tmp_path):
    g, token, lease_id, seen = _borrow_gateway(tmp_path, [999.0])
    g.checks.left_open = lambda s, front: True
    g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))
    g.release(token, lease_id)
    assert seen["restored"] == [((7, 8), PERSON_WINDOW)]


def test_a_kept_window_is_left_alone_once_the_person_takes_over(tmp_path):
    g, token, lease_id, seen = _borrow_gateway(tmp_path, [999.0])
    g.checks.left_open = lambda s, front: True
    g.run(token, lease_id, "click", lambda t, p: real(p), target=Target("point", x=5, y=5))
    g.checks.person_idle_s = lambda: 0.0  # they clicked something since
    g.release(token, lease_id)
    assert seen["restored"] == []


def test_status_shows_where_the_agent_last_acted_while_it_holds_the_scope(gw):
    g, token, lease_id, _ = gw
    assert g.status()["pointer"] is None
    g.run(token, lease_id, "click", lambda t, p: None, target=Target("point", x=5, y=6))
    p = g.status()["pointer"]
    assert (p["x"], p["y"], p["action"], p["client"]) == (5, 6, "click", "agent-a")
    g.run(token, lease_id, "hotkey", lambda t, p: None, params={"combo": "ctrl+s"})
    assert (g.status()["pointer"]["x"], g.status()["pointer"]["y"]) == (5, 6)  # keys do not move it
    g.release(token, lease_id)
    assert g.status()["pointer"] is None
