"""The gateway: every agent request passes through here, and nowhere else.

Each action runs observe -> bind -> act once -> verify:
  1. authenticate the client and check its lease (active, unexpired, not revoked, not paused);
  2. check the scope is still alive and the input desktop is the normal one;
  3. re-resolve the target and refuse if it moved or vanished (bind);
  4. apply hard denies (elevated process, password field) and the approval rule;
  5. for keyboard actions, require the foreground window to be in scope;
  6. perform the action exactly once, then run its verifier;
  7. append the outcome to the action log, success or refusal.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from overshoulder import scope as sc
from overshoulder.driver import input as inp
from overshoulder.driver import uia
from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect
from overshoulder.gateway import policy
from overshoulder.gateway.clients import ClientRegistry, default_home
from overshoulder.gateway.errors import (
    Denied,
    GatewayError,
    NeedsRealInput,
    Paused,
    PersonActive,
    TargetStale,
    VerifyFailed,
)
from overshoulder.gateway.lease import DEFAULT_TTL_S, HUMAN_PAUSE, LeaseManager
from overshoulder.gateway.limits import PROCESS_NAMES, LeaseLimits, Limits, NarrowedScope
from overshoulder.gateway.log import ActionLog

KEYBOARD_ACTIONS = frozenset({"type", "hotkey"})
MOUSE_ACTIONS = frozenset({"click", "double_click", "right_click"})


@dataclass(frozen=True)
class Target:
    """What an action is aimed at: a UI element (rect + identity) or a bare point."""
    kind: str  # "element" | "point"
    rect: Rect | None = None
    x: int | None = None
    y: int | None = None
    name: str = ""
    control_type: str = ""
    automation_id: str = ""
    is_password: bool = False
    pid: int | None = None
    hwnd: int | None = None  # top-level window holding the element
    ref: str = ""  # opaque id the driver can resolve again
    revalidate: Callable[[], Target] | None = field(default=None, compare=False, repr=False)

    def point(self) -> tuple[int, int]:
        if self.kind == "point":
            return int(self.x), int(self.y)
        return self.rect.center()

    def describe(self) -> dict:
        d = {"kind": self.kind, "name": self.name, "control_type": self.control_type,
             "automation_id": self.automation_id, "ref": self.ref}
        if self.rect:
            d["rect"] = [self.rect.left, self.rect.top, self.rect.right, self.rect.bottom]
        if self.kind == "point":
            d["x"], d["y"] = self.x, self.y
        return d


def _hit_test(scope, x: int, y: int) -> int:
    """The top-level window under (x, y) must be in scope; returns it."""
    h = w.window_at(x, y)
    if not h or not scope.owns_window(h):
        raise sc.OutOfScope(f"the window under ({x}, {y}) is {h:#x}, which is not in scope")
    return h


def _scope_focus(scope) -> dict | None:
    """What has keyboard focus inside the scope's own window, background or not.

    "password" is True/False when known. When the focused window is not a Win32 edit (Chromium, XAML),
    the focused field cannot be read from outside; then it is False only if the scope has no password
    field at all, else None (unknown, which the password rule treats as a password field).
    """
    from overshoulder.driver import background as bg
    binding = getattr(scope, "binding", None)
    if binding is None:  # screen scope: the system focus is the scope's focus
        return uia.focused_info()
    hwnd = bg.input_window(binding.hwnd)
    info = dict(uia.info_for_handle(hwnd) or {"name": "", "type": "", "pid": w.pid_of(hwnd)})
    info["hwnd"] = hwnd
    if bg.is_edit_class(hwnd):
        info["password"] = bool(info.get("password")) or bg.is_password_edit(hwnd)
    else:
        info["password"] = uia.any_password(binding.hwnd)
    return info


def _element_in_scope_at(scope, x: int, y: int) -> dict | None:
    binding = getattr(scope, "binding", None)
    if binding is None:
        return uia.info_at_point(x, y)
    return uia.element_in_window_at(binding.hwnd, x, y)


def _foreground_checked(scope, not_elevated: Callable[[int], None]) -> int:
    fg = sc.require_foreground_in_scope(scope)
    not_elevated(w.pid_of(fg))
    return fg


def _desk_state():
    from overshoulder.driver import background as bg
    return bg.cursor_pos(), w.foreground()


def _restore_desk(state) -> dict:
    """Put the person's cursor and foreground window back; says which of the two are back."""
    from overshoulder.driver import background as bg
    (x, y), fg = state
    inp.move_cursor(x, y)
    back = {"cursor": bg.cursor_pos() == (x, y), "foreground": False}
    if fg and w.is_window(fg):
        back["foreground"] = bg.set_foreground(fg)
    return back


def _bring_forward(hwnd: int) -> bool:
    from overshoulder.driver import background as bg
    return bg.set_foreground(hwnd)


def _left_open(scope, front: int) -> bool:
    """After a borrowed action: did it open a menu, or another window of the scope's app, that the
    agent's next step needs in front? Giving the foreground back would close a menu."""
    from overshoulder.driver import background as bg
    fg = w.foreground()
    top = (bg.root_of(fg) or fg) if fg else 0
    try:
        if top and top != front and scope.owns_window(top):
            return True
    except Exception:
        pass
    return bool(front) and bg.in_menu_mode(front)


def _person_watch():
    from overshoulder.driver.person import PersonWatch
    return PersonWatch()


@dataclass
class Checks:
    """Environment checks, injectable for tests."""
    desktop: Callable[[], None] = policy.require_default_desktop
    not_elevated: Callable[[int], None] = policy.require_not_elevated
    # Returns the in-scope foreground hwnd; raises OutOfScope / Denied otherwise.
    foreground_in_scope: Callable[[Any], int] | None = None
    # Returns the top-level hwnd under a point if it is in scope; raises OutOfScope otherwise.
    window_at_point: Callable[[Any, int, int], int] = _hit_test
    # UIA probes; None means "could not tell".
    focused_element: Callable[[], dict | None] = uia.focused_info
    element_at_point: Callable[[int, int], dict | None] = uia.info_at_point
    pid_of: Callable[[int], int] = w.pid_of
    # Background-aware probes: the scope's own focus and the scope's own element under a point.
    scope_focus: Callable[[Any], dict | None] = _scope_focus
    element_in_scope_at: Callable[[Any, int, int], dict | None] = _element_in_scope_at
    root_of: Callable[[int], int] = lambda h: __import__(
        "overshoulder.driver.background", fromlist=["root_of"]).root_of(h)
    is_password_window: Callable[[int], bool] = lambda h: __import__(
        "overshoulder.driver.background", fromlist=["is_password_edit"]).is_password_edit(h)
    # Seconds since the person last touched the mouse or keyboard (our own SendInput not counted).
    person_idle_s: Callable[[], float] = inp.person_idle_s
    # Borrowing the person's input: the foreground window, bringing a window forward, the person's
    # cursor and foreground (saved, then put back), and a watch for their own input meanwhile.
    foreground: Callable[[], int] = w.foreground
    bring_forward: Callable[[int], bool] = _bring_forward
    desk_state: Callable[[], Any] = _desk_state
    restore_desk: Callable[[Any], dict] = _restore_desk
    person_watch: Callable[[], Any] = _person_watch
    left_open: Callable[[Any, int], bool] = _left_open
    # Narrowing: the program names a window belongs to, a pid's program, and the window under a point.
    window_processes: Callable[[int], list[str]] = PROCESS_NAMES.of_window
    pid_process: Callable[[int], str] = PROCESS_NAMES.of_pid
    window_under: Callable[[int, int], int] = w.window_at

    def __post_init__(self):
        if self.foreground_in_scope is None:
            self.foreground_in_scope = lambda scope: _foreground_checked(scope, self.not_elevated)


TEXT_PARAMS = ("text", "value")
# How long a borrowed window may stay in front for the agent's next step (a menu or dialog it opened)
# before the person's window is put back anyway.
KEEP_MAX_S = 30.0


class Borrow:
    """One use of the person's real mouse and keyboard, opened by params["borrow"]() (Gateway._borrow).

    By the time it exists the input mode has been checked, the gateway has waited for the person as
    that mode asks, and the scope's window is in front. Inside it, call check() right before SendInput
    and abort() between keys: abort() stops on any pause, or on the person touching the mouse or
    keyboard. Leaving it in "borrow" mode puts the person's cursor and foreground window back, unless
    they used the PC meanwhile; then the screen is theirs and is left alone. `report` says what happened.
    """

    def __init__(self, gw: Gateway, scope, action: str, target: Target | None, lease_id: str, client: str,
                 mode: str, saved, waited_s: float):
        self.gw, self.scope, self.action, self.target = gw, scope, action, target
        self.lease_id, self.client, self.saved = lease_id, client, saved
        self.front = 0  # the scope window in front while borrowing
        self.report: dict = {"mode": mode, "waited_s": round(waited_s, 2)}
        self.watch = gw.checks.person_watch()
        self.watch.__enter__()

    def __enter__(self) -> Borrow:
        return self

    def __exit__(self, *exc) -> bool:
        try:
            acted = self.person_acted()
        finally:
            self.watch.__exit__(None, None, None)
        self.report["person_active"] = acted
        if self.saved is None:
            return False
        if acted:
            self.report["restored"] = False
        elif self.gw.checks.left_open(self.scope, self.front):
            # A menu or dialog the agent opened: it goes when the agent's next step is done.
            self.gw._kept[self.lease_id] = (self.saved, time.monotonic())
            self.report["kept_in_front"] = True
        else:
            self.report["restored"] = self.gw._restore(self.saved)
        return False

    def person_acted(self) -> bool:
        return bool(self.watch.seen) or self.gw.leases.paused_reason() == HUMAN_PAUSE

    def abort(self) -> None:
        self.gw.leases.check(self.lease_id, self.client, human_ok=True)  # panic, revoke, operator pause
        if self.person_acted():
            raise PersonActive("the person started using the mouse or keyboard, so the agent stopped and left "
                               "the rest to them")

    def check(self) -> int | None:
        """The real-input checks, right before SendInput. Returns the in-scope foreground for keys."""
        self.abort()
        return self.gw._allow_real_input(self.scope, self.action, self.target)


class Gateway:
    def __init__(self, registry: ClientRegistry | None = None, leases: LeaseManager | None = None,
                 log: ActionLog | None = None, approver: policy.Approver | None = None,
                 checks: Checks | None = None, scope_factory: Callable[[str], Any] = sc.parse_scope,
                 allow_elevated: bool = False, protected_pids: set[int] | None = None,
                 human_wait_s: float = 20.0, settings_source: Callable[[], Any] | None = None):
        # An elevated gateway could drive elevated windows past UIPI, defeating the elevated-window deny.
        if not allow_elevated and policy.self_elevated():
            raise Denied("refusing to run elevated; start Overshoulder from a normal (non-admin) process")
        self.registry = registry or ClientRegistry()
        self.leases = leases or LeaseManager()
        self.log = log or ActionLog(default_home() / "logs" / f"actions-{time.strftime('%Y%m%d')}.jsonl")
        self.approver = approver or policy.DenyAll()
        self.checks = checks or Checks()
        self.scope_factory = scope_factory
        # Overshoulder's own windows (approval prompts, watchdog) are never drivable, in any scope,
        # so an agent cannot approve its own request.
        self.protected_pids = {os.getpid()} | set(protected_pids or ())
        # A person touching the mouse/keyboard pauses driving briefly; actions wait that out
        # instead of failing, up to this long. A panic is never waited out.
        self.human_wait_s = human_wait_s
        # The person's settings (input mode), read at each use so a save on the settings page applies at once.
        self.settings_source = settings_source or _current_settings
        # Where the agent last acted (screen pixels), shown on screen by the watchdog as its pointer.
        self.pointer: dict | None = None
        # Borrow mode: the person's cursor and window, kept while a menu or dialog an agent opened stays in
        # front for its next step. lease id -> (saved desk state, monotonic time it was kept).
        self._kept: dict[str, tuple[Any, float]] = {}

    # -- lease lifecycle -----------------------------------------------------------------------

    def acquire(self, token: str, scope_spec: str, ttl_s: float = DEFAULT_TTL_S,
                log_text: bool = False, limits: dict | Limits | None = None) -> dict:
        """log_text=True records typed text in the action log (for tutorial captions); off by default.
        limits: the caller's own narrowing (gateway.limits). Re-scoping on the lease it already holds keeps
        the limits it had and adds these; the person's standing limits for the client always apply."""
        client = None
        try:
            client = self.registry.authenticate(token)
            session = limits if isinstance(limits, Limits) else Limits.from_dict(limits)
            held = self.leases.active()
            if held is not None and held.client == client.name:
                session = held.meta.get("session_limits", Limits()).merge(session)
            inner = self.scope_factory(scope_spec)
            lim = self.lease_limits(client.name, session)
            binding = getattr(inner, "binding", None)
            if binding is not None:
                for proc in [*self.checks.window_processes(binding.hwnd), self.checks.pid_process(binding.pid)]:
                    lim.check_process(proc, "that window")
            scope = NarrowedScope(inner, lambda n=client.name, s=session: self.lease_limits(n, s),
                                  window_processes=self.checks.window_processes)
            grant = self.registry.grant_for(client, scope.kind)
            if grant == "approve":
                eff = lim.effective
                policy.require_approval(self.approver, {
                    "kind": "lease", "client": client.name, "scope": scope.describe(),
                    "reason": f"{client.name} wants {scope.kind} scope {scope.describe()}"
                              + (f" (limits: {eff.describe()})" if eff else "")})
            lease = self.leases.acquire(client.name, scope, ttl_s)
            lease.meta["log_text"] = bool(log_text)
            lease.meta["session_limits"] = session
        except GatewayError as e:
            self.log.write("lease_refused", client=getattr(client, "name", None), scope=scope_spec,
                           error=e.to_dict())
            raise
        self.log.write("lease_acquired", client=client.name, lease=lease.id, scope=scope.describe(),
                       ttl_s=lease.ttl_s, log_text=bool(log_text), limits=lim.to_dict())
        return {"lease_id": lease.id, "scope": scope.describe(), "ttl_s": lease.ttl_s, "limits": lim.to_dict()}

    def hidden_for(self, client_name: str) -> Limits:
        """Every limit on what this client may see now: the person's, plus its lease's own if it holds one.
        For the calls that need no lease (windows, wait_for)."""
        lease = self.leases.active()
        session = lease.meta.get("session_limits", Limits()) if lease and lease.client == client_name else Limits()
        return self.lease_limits(client_name, session).effective

    def lease_limits(self, client_name: str, session: Limits) -> LeaseLimits:
        """The person's standing limits for the client, read now, with the session's own."""
        c = self.registry.get(client_name)
        return LeaseLimits(self.registry.limits_for(c) if c else Limits(), session)

    def heartbeat(self, token: str, lease_id: str) -> dict:
        client = self.registry.authenticate(token)
        lease = self.leases.heartbeat(lease_id, client.name)
        return {"lease_id": lease.id, "ttl_s": lease.ttl_s}

    def release(self, token: str, lease_id: str) -> None:
        client = self.registry.authenticate(token)
        self.leases.release(lease_id, client.name)
        self._give_back(lease_id)
        self.log.write("lease_released", client=client.name, lease=lease_id)

    def status(self) -> dict:
        out = {**self.leases.status(), "input_mode": self.settings_source().input_mode}
        lease = self.leases.active()
        if out.get("active") and lease is not None:
            session = lease.meta.get("session_limits", Limits())
            out["active"]["limits"] = self.lease_limits(lease.client, session).to_dict()
        pointer = self.pointer
        active = out.get("active")
        out["pointer"] = pointer if active and pointer and pointer["client"] == active["client"] else None
        return out

    # -- watchdog hooks ------------------------------------------------------------------------

    def panic(self, reason: str = "panic key") -> None:
        """Revoke the active lease AND pause, so no client can simply re-acquire until resume()."""
        lease = self.leases.revoke(reason)
        self.leases.pause(f"{reason}; an operator must resume")
        self.log.write("lease_revoked", reason=reason, lease=getattr(lease, "id", None),
                       client=getattr(lease, "client", None))

    def pause(self, reason: str) -> None:
        self.leases.pause(reason)
        self.log.write("paused", reason=reason)

    def resume(self, only_if: str | None = None) -> bool:
        lifted = self.leases.resume(only_if)
        if lifted:
            self.log.write("resumed", only_if=only_if)
        return lifted

    # -- actions -------------------------------------------------------------------------------

    def run(self, token: str, lease_id: str, action: str, perform: Callable[[Target | None, dict], Any],
            target: Target | None = None, params: dict | None = None,
            verify: Callable[[Target | None, dict, Any], dict] | None = None,
            read_only: bool = False) -> dict:
        """Run one action through every check. `perform` runs at most once.

        perform receives params plus params["abort"], which raises if the lease was revoked or paused
        mid-action (long background actions call it between steps), and params["borrow"], which every
        use of the person's mouse or keyboard must go through (see Borrow).
        """
        params = dict(params or {})
        t0 = time.time()
        client_name = None
        lease = None
        scope_desc = None
        result = None  # what perform returned; logged with a failed verify to show which methods ran
        logged_params = {k: v for k, v in params.items() if k != "secret"}
        try:
            client = self.registry.authenticate(token)
            client_name = client.name
            lease = self.leases.check(lease_id, client.name, human_ok=True)
            self._expire_kept()
            if not lease.meta.get("log_text"):
                logged_params = _redact_text(logged_params)
            scope = lease.scope
            scope_desc = scope.describe()
            scope.check_alive()
            self.checks.desktop()
            lim = self.lease_limits(client.name, lease.meta.get("session_limits", Limits()))
            if not read_only:
                lim.check_action(action)

            if target is not None:
                self._check_target_processes(lim, target)
                target = self._bind(scope, target, action)
                if target.pid:
                    self.checks.not_elevated(target.pid)
                policy.require_not_password(action, target.is_password)
            pid = getattr(getattr(scope, "binding", None), "pid", None)
            if pid:
                self.checks.not_elevated(pid)

            focus_info = None
            if not read_only:
                if action == "hotkey":
                    policy.require_hotkey_allowed(str(params.get("combo", "")))
                if action in KEYBOARD_ACTIONS and target is None:
                    # The scope's own focus, which is where background keys go.
                    focus_info = self.checks.scope_focus(scope)
                    if focus_info and focus_info.get("pid"):
                        lim.check_process(self.checks.pid_process(focus_info["pid"]), "the focused control")
                    if action == "type":
                        policy.require_not_password("type", None if focus_info is None else focus_info["password"])
                focused_type = target.control_type if target is not None else (focus_info or {}).get("type", "")
                name = target.name if target else ""
                if target is not None and target.kind == "point":
                    under = self.checks.element_in_scope_at(scope, *target.point())
                    name = (under or {}).get("name", "")
                reason = policy.approval_reason(action, name, params, focused_type=focused_type)
                if reason:
                    policy.require_approval(self.approver, {
                        "kind": "action", "client": client.name, "action": action, "reason": reason,
                        "target": target.describe() if target else None, "scope": scope_desc})

            if target is not None and (target.kind == "point" or (target.rect and not target.rect.empty)):
                x, y = target.point()
                self.pointer = {"x": x, "y": y, "action": action, "client": client.name, "at": time.time()}
            params["abort"] = lambda: self._check_background(lease_id, client.name, scope)
            # perform MUST wrap any use of the person's mouse/keyboard (SendInput) in borrow():
            #   with params["borrow"]() as b: fg = b.check(); <SendInput, calling b.abort() between keys>
            # and call background_target(hwnd) before posting to or messaging any window.
            params["borrow"] = lambda: self._borrow(scope, action, target, lease_id, client.name)
            params["background_target"] = lambda hwnd, typing=False: self._allow_background_target(
                scope, hwnd, typing)
            params["scope_focus"] = focus_info
            t_act = time.time()
            result = perform(target, params)
            self.leases.count_action(lease)
            verdict = verify(target, params, result) if verify else {"ok": True, "method": "none"}
            if not verdict.get("ok"):
                raise VerifyFailed(f"{action} did not have the expected effect: "
                                   f"{verdict.get('expect_failed') or 'verify returned not ok'}")
        except Exception as e:
            # Every refusal and driver failure is logged before it propagates.
            error = e.to_dict() if isinstance(e, GatewayError) else \
                {"code": getattr(e, "code", type(e).__name__), "message": str(e)}
            self.log.write("action", client=client_name, lease=lease_id, action=action, params=logged_params,
                           target=target.describe() if target else None, ok=False, error=error,
                           scope=scope_desc, ms=round((time.time() - t0) * 1000),
                           **({"result": result} if isinstance(result, dict) else {}))
            raise
        out = {"ok": True, "result": result, "verify": verdict}
        try:
            row = self.log.write("action", client=client_name, lease=lease_id, action=action,
                                 params=logged_params, target=target.describe() if target else None, ok=True,
                                 verify=_loggable(verdict, lease.meta.get("log_text")), scope=scope_desc,
                                 t_act=round(t_act, 3),
                                 ms=round((time.time() - t0) * 1000))
            out["ts"] = row["ts"]
        except OSError as e:
            # The action already happened; report the log failure instead of inviting a retry.
            out["log_error"] = str(e)
        return out

    def _check_target_processes(self, lim: LeaseLimits, target: Target) -> None:
        """deny_processes, before binding, so the refusal names the limit rather than the scope."""
        if not lim.effective.deny_processes:
            return
        procs = []
        if target.pid:
            procs.append(self.checks.pid_process(target.pid))
        hwnd = target.hwnd
        if hwnd is None and target.kind == "point":
            hwnd = self.checks.window_under(*target.point())
        if hwnd:
            procs += self.checks.window_processes(self.checks.root_of(hwnd) or hwnd)
        for proc in procs:
            lim.check_process(proc, "the target")

    def _check_background(self, lease_id: str, client: str, scope) -> None:
        """Stop check between steps of background work. A panic, revoke or operator pause stops it. The
        person using the PC does not, unless they are working in the agent's own window."""
        self.leases.check(lease_id, client, human_ok=True)
        if self.leases.paused_reason() == HUMAN_PAUSE and self._scope_in_front(scope):
            raise Paused("the person is working in this window, so the agent stopped")

    def _scope_in_front(self, scope) -> bool:
        fg = self.checks.foreground()
        try:
            return bool(fg) and scope.owns_window(self.checks.root_of(fg) or fg)
        except Exception:
            return False

    def _borrow(self, scope, action: str, target: Target | None, lease_id: str, client: str) -> Borrow:
        """Open a Borrow of the person's mouse and keyboard for one action."""
        s = self.settings_source()
        if s.input_mode == "never":
            raise NeedsRealInput(
                "this needs the person's mouse or keyboard, and their input mode is 'never' (agents work in the "
                "background only). Use a ref with invoke, select or set_value instead, or ask the person to do "
                "this step and wait_for() it")
        if action == "activate" and s.input_mode != "full":
            raise NeedsRealInput(
                "activate keeps a window in front, which needs input mode 'full'. In 'borrow' mode an action "
                "that needs real input brings the window forward by itself and gives the foreground back")
        kept = self._kept.pop(lease_id, None)
        if kept is not None and s.input_mode == "borrow" and self._untouched_since(kept[1]):
            # The agent's window is still in front from its last step and the person has not touched
            # anything since: carry on, and later give back the window they had before.
            waited, saved = 0.0, kept[0]
        else:
            waited = self._wait_for_person(s, lease_id, client)
            saved = self.checks.desk_state() if s.input_mode == "borrow" else None
        b = Borrow(self, scope, action, target, lease_id, client, s.input_mode, saved, waited)
        try:
            b.report["brought_forward"] = self._bring_scope_forward(scope, target)
            fg = self.checks.foreground()
            b.front = (self.checks.root_of(fg) or fg) if fg else 0
        except BaseException:
            b.__exit__(None, None, None)
            raise
        return b

    def _untouched_since(self, t: float) -> bool:
        # Strict: with a coarse clock no time may have passed, and idle 0 then means "just touched".
        return self.checks.person_idle_s() > time.monotonic() - t

    def _restore(self, saved) -> dict:
        try:
            return self.checks.restore_desk(saved)
        except Exception as e:  # report it; the action itself already happened
            return {"error": str(e)}

    def _give_back(self, lease_id: str) -> None:
        """Put back the person's window kept for this lease, unless they have taken over since."""
        kept = self._kept.pop(lease_id, None)
        if kept is not None and self._untouched_since(kept[1]):
            self._restore(kept[0])

    def _expire_kept(self) -> None:
        """Give back kept windows whose lease is gone or that have been kept too long."""
        active = self.leases.active()
        for lease_id, (_, t) in list(self._kept.items()):
            if active is None or active.id != lease_id or time.monotonic() - t > KEEP_MAX_S:
                self._give_back(lease_id)

    def _wait_for_person(self, s, lease_id: str, client: str) -> float:
        """Wait until the person's input may be used: no human-input pause and, in "borrow" mode, idle
        for s.borrow_idle_s. Gives up after human_wait_s. Returns the seconds waited."""
        t0 = time.monotonic()
        while True:
            self.leases.check(lease_id, client, human_ok=True)  # a panic or operator pause stops it at once
            human = self.leases.paused_reason() == HUMAN_PAUSE
            idle = self.checks.person_idle_s()
            if not human and (s.input_mode != "borrow" or idle >= s.borrow_idle_s):
                return time.monotonic() - t0
            if time.monotonic() - t0 >= self.human_wait_s:
                if s.input_mode == "borrow":
                    raise PersonActive(
                        f"the person is using the PC (last input {idle:.1f}s ago). Input mode 'borrow' waits for "
                        f"{s.borrow_idle_s}s without input and gave up after {self.human_wait_s:g}s; retry later")
                raise Paused(f"driving is paused: {HUMAN_PAUSE}")
            time.sleep(0.25)
            self.leases.heartbeat_unchecked(lease_id, client)

    def _bring_scope_forward(self, scope, target: Target | None) -> bool:
        """Put the scope's window (or the target's dialog) in front for real input. A screen scope acts
        where the person approved, in place."""
        binding = getattr(scope, "binding", None)
        if binding is None or self._scope_in_front(scope):
            return False
        self.checks.bring_forward(target.hwnd if target is not None and target.hwnd else binding.hwnd)
        return True

    def _allow_real_input(self, scope, action: str, target: Target | None) -> int | None:
        """Checks before the person's real mouse/keyboard is used. Returns the in-scope foreground for keys."""
        fg = None
        if action in KEYBOARD_ACTIONS:
            fg = self.checks.foreground_in_scope(scope)
            self._require_unprotected(fg)
            if action == "type" and target is None:
                focused = self.checks.focused_element()
                policy.require_not_password("type", None if focused is None else focused["password"])
        if action in MOUSE_ACTIONS and target is not None:
            hit = self.checks.window_at_point(scope, *target.point())
            self._require_unprotected(hit)
            self.checks.not_elevated(self.checks.pid_of(hit))
        return fg

    def _allow_background_target(self, scope, hwnd: int, typing: bool = False) -> None:
        """Checks before posting to / messaging a window: it must be the scope's, and not a password box."""
        top = self.checks.root_of(hwnd) or hwnd
        if not scope.owns_window(top):
            raise sc.OutOfScope(f"window {hwnd:#x} is not in scope")
        self._require_unprotected(top)
        self.checks.not_elevated(self.checks.pid_of(hwnd))
        if typing and self.checks.is_password_window(hwnd):
            raise Denied("that window is a password field; agents never type into it")

    def _require_unprotected(self, hwnd: int) -> None:
        if hwnd and self.checks.pid_of(hwnd) in self.protected_pids:
            raise Denied("that window belongs to Overshoulder itself; agents never drive it")

    def protect_pid(self, pid: int) -> None:
        self.protected_pids.add(pid)

    def _bind(self, scope, target: Target, action: str = "") -> Target:
        if target.revalidate is not None:
            fresh = target.revalidate()
            if fresh is None:
                raise TargetStale(f"target {target.describe()} is gone")
            if target.rect and fresh.rect and fresh.rect != target.rect:
                raise TargetStale(f"target moved from {target.rect} to {fresh.rect}; observe again")
            target = replace(fresh, revalidate=target.revalidate)
        if target.hwnd and not scope.owns_window(target.hwnd):
            raise sc.OutOfScope(f"target window {target.hwnd:#x} is not in scope")
        if target.kind == "element" and (target.rect is None or target.rect.empty) and action in MOUSE_ACTIONS:
            raise sc.OutOfScope("the element has no visible area to click; use invoke, select or type instead")
        if target.kind == "element" and (target.rect is None or target.rect.empty):
            # Hidden but real inputs (e.g. Chromium's editor textarea) have no area; nothing is clicked,
            # and the element's window was just checked to be in scope.
            if not target.hwnd:
                raise sc.OutOfScope("element has no area and no window to check against the scope")
            return target
        x, y = target.point()
        binding = getattr(scope, "binding", None)
        if target.hwnd and binding is not None and target.hwnd != binding.hwnd:
            # An owned dialog may sit outside its parent's frame; check against the dialog itself.
            if not w.frame_bounds(target.hwnd).contains(x, y):
                raise sc.OutOfScope(f"point ({x}, {y}) is outside dialog {target.hwnd:#x}")
        else:
            sc.require_point(scope, x, y)
        return target


def _redact_text(params: dict) -> dict:
    """Replace typed text, and expectations that would echo it, with their length."""
    out = dict(params)
    for k in TEXT_PARAMS:
        if isinstance(out.get(k), str):
            out[k] = {"redacted": True, "len": len(out[k])}
    if isinstance(out.get("expect"), dict):
        out["expect"] = {k: ({"redacted": True, "len": len(v)} if isinstance(v, str) else v)
                         for k, v in out["expect"].items()}
    return out


def _loggable(verdict: dict, log_text: bool) -> dict:
    if log_text or "readback" not in verdict:
        return verdict
    return {**verdict, "readback": "redacted"}


def _current_settings():
    from overshoulder import settings as st  # imported late: settings imports this package's clients module
    return st.load_or_default()


def gateway_for_tests(tmp: Path, **kw) -> Gateway:
    """A gateway with an isolated registry and log. CI runners are elevated, so tests opt out of that
    refusal unless they pass allow_elevated=False. The input mode is "full" so tests depend neither on
    this PC's settings nor on someone touching the mouse; input-mode tests pass settings_source."""
    from overshoulder import settings as st
    reg = ClientRegistry(tmp / "clients.json")
    kw.setdefault("allow_elevated", True)
    kw.setdefault("settings_source", lambda: st.Settings(input_mode="full"))
    return Gateway(registry=reg, log=ActionLog(tmp / "actions.jsonl"), **kw)
