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
from overshoulder.driver import uia
from overshoulder.driver import windows as w
from overshoulder.driver.windows import Rect
from overshoulder.gateway import policy
from overshoulder.gateway.clients import ClientRegistry, default_home
from overshoulder.gateway.errors import Denied, GatewayError, TargetStale, VerifyFailed
from overshoulder.gateway.lease import DEFAULT_TTL_S, LeaseManager
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


def _foreground_checked(scope, not_elevated: Callable[[int], None]) -> int:
    fg = sc.require_foreground_in_scope(scope)
    not_elevated(w.pid_of(fg))
    return fg


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

    def __post_init__(self):
        if self.foreground_in_scope is None:
            self.foreground_in_scope = lambda scope: _foreground_checked(scope, self.not_elevated)


TEXT_PARAMS = ("text", "value")


class Gateway:
    def __init__(self, registry: ClientRegistry | None = None, leases: LeaseManager | None = None,
                 log: ActionLog | None = None, approver: policy.Approver | None = None,
                 checks: Checks | None = None, scope_factory: Callable[[str], Any] = sc.parse_scope,
                 allow_elevated: bool = False, protected_pids: set[int] | None = None):
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

    # -- lease lifecycle -----------------------------------------------------------------------

    def acquire(self, token: str, scope_spec: str, ttl_s: float = DEFAULT_TTL_S,
                log_text: bool = False) -> dict:
        """log_text=True records typed text in the action log (for tutorial captions); off by default."""
        client = None
        try:
            client = self.registry.authenticate(token)
            scope = self.scope_factory(scope_spec)
            grant = self.registry.grant_for(client, scope.kind)
            if grant == "approve":
                policy.require_approval(self.approver, {
                    "kind": "lease", "client": client.name, "scope": scope.describe(),
                    "reason": f"{client.name} wants {scope.kind} scope {scope.describe()}"})
            lease = self.leases.acquire(client.name, scope, ttl_s)
            lease.meta["log_text"] = bool(log_text)
        except GatewayError as e:
            self.log.write("lease_refused", client=getattr(client, "name", None), scope=scope_spec,
                           error=e.to_dict())
            raise
        self.log.write("lease_acquired", client=client.name, lease=lease.id, scope=scope.describe(),
                       ttl_s=lease.ttl_s, log_text=bool(log_text))
        return {"lease_id": lease.id, "scope": scope.describe(), "ttl_s": lease.ttl_s}

    def heartbeat(self, token: str, lease_id: str) -> dict:
        client = self.registry.authenticate(token)
        lease = self.leases.heartbeat(lease_id, client.name)
        return {"lease_id": lease.id, "ttl_s": lease.ttl_s}

    def release(self, token: str, lease_id: str) -> None:
        client = self.registry.authenticate(token)
        self.leases.release(lease_id, client.name)
        self.log.write("lease_released", client=client.name, lease=lease_id)

    def status(self) -> dict:
        return self.leases.status()

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

        perform receives params plus params["abort"], a callable that raises if the lease was revoked
        or paused mid-action; long actions (typing) must call it between steps.
        """
        params = dict(params or {})
        t0 = time.time()
        client_name = None
        lease = None
        scope_desc = None
        logged_params = {k: v for k, v in params.items() if k != "secret"}
        try:
            client = self.registry.authenticate(token)
            client_name = client.name
            lease = self.leases.check(lease_id, client.name)
            if not lease.meta.get("log_text"):
                logged_params = _redact_text(logged_params)
            scope = lease.scope
            scope_desc = scope.describe()
            scope.check_alive()
            self.checks.desktop()

            if target is not None:
                target = self._bind(scope, target)
                if target.pid:
                    self.checks.not_elevated(target.pid)
                policy.require_not_password(action, target.is_password)
            pid = getattr(getattr(scope, "binding", None), "pid", None)
            if pid:
                self.checks.not_elevated(pid)

            focused_type = ""
            if not read_only:
                if action == "hotkey":
                    policy.require_hotkey_allowed(str(params.get("combo", "")))
                if action in KEYBOARD_ACTIONS and target is None:
                    fg = self.checks.foreground_in_scope(scope)
                    self._require_unprotected(fg)
                if action in KEYBOARD_ACTIONS:
                    if target is None:
                        focused = self.checks.focused_element()
                        if action == "type":
                            policy.require_not_password(
                                "type", None if focused is None else focused["password"])
                        focused_type = (focused or {}).get("type", "")
                    else:
                        focused_type = target.control_type
                name = target.name if target else ""
                if action in MOUSE_ACTIONS and target is not None:
                    hit = self.checks.window_at_point(scope, *target.point())
                    self._require_unprotected(hit)
                    self.checks.not_elevated(self.checks.pid_of(hit))
                    if target.kind == "point":
                        under = self.checks.element_at_point(*target.point())
                        name = (under or {}).get("name", "")
                reason = policy.approval_reason(action, name, params, focused_type=focused_type)
                if reason:
                    policy.require_approval(self.approver, {
                        "kind": "action", "client": client.name, "action": action, "reason": reason,
                        "target": target.describe() if target else None, "scope": scope_desc})

            params["abort"] = lambda: self.leases.check(lease_id, client.name)

            def require_foreground() -> int:
                # Keyboard actions with a target focus it first, then must call this before any key.
                fg = self.checks.foreground_in_scope(scope)
                self._require_unprotected(fg)
                return fg

            params["require_foreground"] = require_foreground
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
                           scope=scope_desc, ms=round((time.time() - t0) * 1000))
            raise
        out = {"ok": True, "result": result, "verify": verdict}
        try:
            row = self.log.write("action", client=client_name, lease=lease_id, action=action,
                                 params=logged_params, target=target.describe() if target else None, ok=True,
                                 verify=_loggable(verdict, lease.meta.get("log_text")), scope=scope_desc,
                                 ms=round((time.time() - t0) * 1000))
            out["ts"] = row["ts"]
        except OSError as e:
            # The action already happened; report the log failure instead of inviting a retry.
            out["log_error"] = str(e)
        return out

    def _require_unprotected(self, hwnd: int) -> None:
        if hwnd and self.checks.pid_of(hwnd) in self.protected_pids:
            raise Denied("that window belongs to Overshoulder itself; agents never drive it")

    def protect_pid(self, pid: int) -> None:
        self.protected_pids.add(pid)

    def _bind(self, scope, target: Target) -> Target:
        if target.revalidate is not None:
            fresh = target.revalidate()
            if fresh is None:
                raise TargetStale(f"target {target.describe()} is gone")
            if target.rect and fresh.rect and fresh.rect != target.rect:
                raise TargetStale(f"target moved from {target.rect} to {fresh.rect}; observe again")
            target = replace(fresh, revalidate=target.revalidate)
        if target.hwnd and not scope.owns_window(target.hwnd):
            raise sc.OutOfScope(f"target window {target.hwnd:#x} is not in scope")
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


def gateway_for_tests(tmp: Path, **kw) -> Gateway:
    """A gateway with an isolated registry and log. CI runners are elevated, so tests opt out of that
    refusal unless they pass allow_elevated=False."""
    reg = ClientRegistry(tmp / "clients.json")
    kw.setdefault("allow_elevated", True)
    return Gateway(registry=reg, log=ActionLog(tmp / "actions.jsonl"), **kw)
