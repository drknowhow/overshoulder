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

import time
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from overshoulder import scope as sc
from overshoulder.driver.windows import Rect
from overshoulder.gateway import policy
from overshoulder.gateway.clients import ClientRegistry, default_home
from overshoulder.gateway.errors import GatewayError, TargetStale, VerifyFailed
from overshoulder.gateway.lease import DEFAULT_TTL_S, LeaseManager
from overshoulder.gateway.log import ActionLog

KEYBOARD_ACTIONS = frozenset({"type", "hotkey"})


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


@dataclass
class Checks:
    """Environment checks, injectable for tests."""
    desktop: Callable[[], None] = policy.require_default_desktop
    not_elevated: Callable[[int], None] = policy.require_not_elevated
    foreground_in_scope: Callable[[Any], int] = sc.require_foreground_in_scope


class Gateway:
    def __init__(self, registry: ClientRegistry | None = None, leases: LeaseManager | None = None,
                 log: ActionLog | None = None, approver: policy.Approver | None = None,
                 checks: Checks | None = None, scope_factory: Callable[[str], Any] = sc.parse_scope):
        self.registry = registry or ClientRegistry()
        self.leases = leases or LeaseManager()
        self.log = log or ActionLog(default_home() / "logs" / f"actions-{time.strftime('%Y%m%d')}.jsonl")
        self.approver = approver or policy.DenyAll()
        self.checks = checks or Checks()
        self.scope_factory = scope_factory

    # -- lease lifecycle -----------------------------------------------------------------------

    def acquire(self, token: str, scope_spec: str, ttl_s: float = DEFAULT_TTL_S) -> dict:
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
        except GatewayError as e:
            self.log.write("lease_refused", client=getattr(client, "name", None), scope=scope_spec,
                           error=e.to_dict())
            raise
        self.log.write("lease_acquired", client=client.name, lease=lease.id, scope=scope.describe(),
                       ttl_s=lease.ttl_s)
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
        lease = self.leases.revoke(reason)
        self.log.write("lease_revoked", reason=reason, lease=getattr(lease, "id", None),
                       client=getattr(lease, "client", None))

    def pause(self, reason: str) -> None:
        self.leases.pause(reason)
        self.log.write("paused", reason=reason)

    def resume(self) -> None:
        self.leases.resume()
        self.log.write("resumed")

    # -- actions -------------------------------------------------------------------------------

    def run(self, token: str, lease_id: str, action: str, perform: Callable[[Target | None, dict], Any],
            target: Target | None = None, params: dict | None = None,
            verify: Callable[[Target | None, dict, Any], dict] | None = None,
            read_only: bool = False) -> dict:
        """Run one action through every check. `perform` runs at most once."""
        params = dict(params or {})
        t0 = time.time()
        client_name = None
        lease = None
        logged_params = {k: v for k, v in params.items() if k != "secret"}
        try:
            client = self.registry.authenticate(token)
            client_name = client.name
            lease = self.leases.check(lease_id, client.name)
            scope = lease.scope
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

            if not read_only:
                reason = policy.approval_reason(action, target.name if target else "", params)
                if reason:
                    policy.require_approval(self.approver, {
                        "kind": "action", "client": client.name, "action": action, "reason": reason,
                        "target": target.describe() if target else None, "scope": scope.describe()})
                if action in KEYBOARD_ACTIONS:
                    self.checks.foreground_in_scope(scope)

            result = perform(target, params)
            lease.actions += 1
            verdict = verify(target, params, result) if verify else {"ok": True, "method": "none"}
            if not verdict.get("ok"):
                raise VerifyFailed(f"{action} did not have the expected effect: {verdict}")
        except Exception as e:
            # Every refusal and driver failure is logged before it propagates.
            error = e.to_dict() if isinstance(e, GatewayError) else \
                {"code": getattr(e, "code", type(e).__name__), "message": str(e)}
            self.log.write("action", client=client_name, lease=lease_id, action=action, params=logged_params,
                           target=target.describe() if target else None, ok=False, error=error,
                           ms=round((time.time() - t0) * 1000))
            raise
        row = self.log.write("action", client=client_name, lease=lease_id, action=action,
                             params=logged_params, target=target.describe() if target else None, ok=True,
                             verify=verdict, scope=lease.scope.describe(),
                             ms=round((time.time() - t0) * 1000))
        return {"ok": True, "result": result, "verify": verdict, "ts": row["ts"]}

    @staticmethod
    def _bind(scope, target: Target) -> Target:
        if target.revalidate is not None:
            fresh = target.revalidate()
            if fresh is None:
                raise TargetStale(f"target {target.describe()} is gone")
            if target.rect and fresh.rect and fresh.rect != target.rect:
                raise TargetStale(f"target moved from {target.rect} to {fresh.rect}; observe again")
            target = replace(fresh, revalidate=target.revalidate)
        if target.kind == "point":
            sc.require_point(scope, *target.point())
        elif target.rect is not None:
            sc.require_point(scope, *target.rect.center())
        if target.hwnd and not scope.owns_window(target.hwnd):
            raise sc.OutOfScope(f"target window {target.hwnd:#x} is not in scope")
        return target


def gateway_for_tests(tmp: Path, **kw) -> Gateway:
    reg = ClientRegistry(tmp / "clients.json")
    return Gateway(registry=reg, log=ActionLog(tmp / "actions.jsonl"), **kw)
