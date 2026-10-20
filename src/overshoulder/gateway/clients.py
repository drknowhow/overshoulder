"""Client registry: who may drive, and which scopes each client may take.

Tokens are stored only as SHA-256 hashes. The registry file lives in the user's profile, so the
Windows user ACL protects it; loopback reachability alone is not authorization.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from dataclasses import asdict, dataclass, field
from pathlib import Path

from overshoulder.gateway.errors import NotGranted, Unauthorized


def default_home() -> Path:
    return Path(os.environ.get("OVERSHOULDER_HOME")
                or Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Overshoulder")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class Client:
    name: str
    token_sha256: str
    # "allow" | "approve" (needs a one-shot approval per lease) | "deny"
    window: str = "allow"
    screen: str = "approve"
    notes: str = ""
    extra: dict = field(default_factory=dict)


class ClientRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path or default_home() / "clients.json"
        self._clients: dict[str, Client] = {}
        self.load()

    def load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._clients = {c["name"]: Client(**c) for c in data.get("clients", [])}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"clients": [asdict(c) for c in self._clients.values()]}, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)

    def add(self, name: str, window: str = "allow", screen: str = "approve") -> str:
        """Create or rotate a client; returns the plaintext token (shown once, never stored)."""
        for grant in (window, screen):
            if grant not in ("allow", "approve", "deny"):
                raise ValueError(f"grant must be allow|approve|deny, got {grant!r}")
        token = "os_" + secrets.token_urlsafe(32)
        self._clients[name] = Client(name, _hash(token), window, screen)
        self.save()
        return token

    def remove(self, name: str) -> bool:
        gone = self._clients.pop(name, None) is not None
        if gone:
            self.save()
        return gone

    def names(self) -> list[str]:
        return sorted(self._clients)

    def authenticate(self, token: str) -> Client:
        digest = _hash(token or "")
        for c in self._clients.values():
            if hmac.compare_digest(c.token_sha256, digest):
                return c
        raise Unauthorized("unknown client token")

    @staticmethod
    def grant_for(client: Client, scope_kind: str) -> str:
        grant = getattr(client, scope_kind, "deny")
        if grant == "deny":
            raise NotGranted(f"client {client.name!r} may not take {scope_kind} scopes")
        return grant
