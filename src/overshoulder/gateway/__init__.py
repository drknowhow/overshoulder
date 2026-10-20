"""The gateway: the safety boundary every agent request passes through."""

from overshoulder.gateway.core import Gateway, Target
from overshoulder.gateway.errors import GatewayError

__all__ = ["Gateway", "GatewayError", "Target"]
