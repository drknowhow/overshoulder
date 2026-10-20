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


class ApprovalRequired(GatewayError):
    code = "approval_required"


class ApprovalDenied(GatewayError):
    code = "approval_denied"


class TargetStale(GatewayError):
    """The bound target moved, changed or vanished between observe and act."""
    code = "target_stale"


class VerifyFailed(GatewayError):
    code = "verify_failed"
