"""Gateway refusals. Each carries a stable `code` that MCP/CLI callers can branch on."""

from __future__ import annotations


class GatewayError(RuntimeError):
    code = "gateway_error"

    def to_dict(self) -> dict:
        return {"code": self.code, "message": str(self)}


class Unauthorized(GatewayError):
    code = "unauthorized"


class NotGranted(GatewayError):
    """The client is known but may not take this kind of scope."""
    code = "not_granted"


class LeaseBusy(GatewayError):
    code = "lease_busy"

    def __init__(self, message: str, holder: str, position: int | None = None):
        super().__init__(message)
        self.holder = holder
        self.position = position

    def to_dict(self) -> dict:
        return {**super().to_dict(), "holder": self.holder, "position": self.position}


class LeaseInvalid(GatewayError):
    """No lease, an expired lease, or a lease held by someone else."""
    code = "lease_invalid"


class LeaseRevoked(GatewayError):
    """The watchdog (panic key, human input) or an operator revoked the lease."""
    code = "lease_revoked"


class Paused(GatewayError):
    code = "paused"


class Denied(GatewayError):
    """A hard deny: secure desktop, elevated window, password field. No grant unlocks these."""
    code = "denied"


class Narrowed(GatewayError):
    """A limit on this session refused the action: read_only, no_typing or deny_processes. set_by says
    whether the caller asked for it in take_scope ("session") or the person set it ("person")."""
    code = "narrowed"

    def __init__(self, message: str, limit: str, set_by: str):
        super().__init__(message)
        self.limit = limit
        self.set_by = set_by

    def to_dict(self) -> dict:
        return {**super().to_dict(), "limit": self.limit, "set_by": self.set_by}


class ApprovalRequired(GatewayError):
    code = "approval_required"


class ApprovalDenied(GatewayError):
    code = "approval_denied"


class TargetStale(GatewayError):
    """The bound target moved, changed or vanished between observe and act."""
    code = "target_stale"


class VerifyFailed(GatewayError):
    code = "verify_failed"


class NeedsRealInput(GatewayError):
    """No background method reaches this, and the input mode or the method forbids the person's mouse
    and keyboard."""
    code = "needs_real_input"


class TypingMismatch(GatewayError):
    """Typing reached the field, but it does not read back as the typed text (characters lost or out of
    order). Nothing is retyped, because part of it arrived."""
    code = "typing_mismatch"


class PersonActive(GatewayError):
    """Input mode "borrow": the person used the mouse or keyboard too recently to borrow them."""
    code = "person_active"
