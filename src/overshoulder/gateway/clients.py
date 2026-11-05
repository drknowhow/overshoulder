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
    window: str = "approve"
    screen: str = "approve"
    notes: str = ""
    extra: dict = field(default_factory=dict)
    # The person's standing limits for this client (gateway.limits): read_only, no_typing, deny_processes.
    # Every lease the client takes carries them, and nothing the agent sends can lift them.
    # limits["allow_builtin"] lists the programs of limits.BUILTIN_DENY the person allowed this client.
    limits: dict = field(default_factory=dict)


# Version of the grant defaults a client was made or last granted under. 2: window scopes ask by default.
GRANTS_V = 2


class ClientRegistry:
    def __init__(self, path: Path | None = None):
        self.path = path or default_home() / "clients.json"
        self._clients: dict[str, Client] = {}
        self._mtime: float | None = None
        self.load()

    def load(self) -> None:
        if self.path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self._clients = {c["name"]: Client(**c) for c in data.get("clients", [])}
            self._mtime = self.path.stat().st_mtime
            if self._ask_old_grants():
                self.save()
        else:
            self._clients, self._mtime = {}, None

    def _ask_old_grants(self) -> bool:
        """Clients made before new clients defaulted to window="approve" got window="allow" without the
        person choosing it. Each such client goes back to approve once; `client grant NAME --window allow`
        gives it back on purpose. Returns whether anything changed."""
        changed = False
        for c in self._clients.values():
            if c.extra.get("grants_v", 1) >= GRANTS_V:
                continue
            if c.window == "allow":
                c.window = "approve"
                c.extra["window_was"] = "allow"
            c.extra["grants_v"] = GRANTS_V
            changed = True
        return changed

    def _reload_if_changed(self) -> None:
        # Another process (the CLI) may rotate or remove a token while the gateway runs.
        mtime = self.path.stat().st_mtime if self.path.exists() else None
        if mtime != self._mtime:
            self.load()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"clients": [asdict(c) for c in self._clients.values()]}, indent=2),
                       encoding="utf-8")
        tmp.replace(self.path)
        self._mtime = self.path.stat().st_mtime

    def add(self, name: str, window: str = "approve", screen: str = "approve", limits: dict | None = None) -> str:
        """Create or rotate a client; returns the plaintext token (shown once, never stored). Rotating keeps
        the client's limits, adding any given here: a new token never quietly widens what it may do.
        Programs the person allowed off the built-in deny list stay allowed."""
        for grant in (window, screen):
            if grant not in ("allow", "approve", "deny"):
                raise ValueError(f"grant must be allow|approve|deny, got {grant!r}")
        from overshoulder.gateway.limits import Limits
        old = self._clients.get(name)
        kept = Limits.from_dict(old.limits if old else None).merge(Limits.from_dict(limits))
        token = "os_" + secrets.token_urlsafe(32)
        stored = kept.to_dict() if kept else {}
        for side in ("allow_builtin", "wait_roots"):
            if old and old.limits.get(side):
                stored[side] = list(old.limits[side])
        self._clients[name] = Client(name, _hash(token), window, screen, limits=stored,
                                     extra={"grants_v": GRANTS_V})
        self.save()
        return token

    def set_limits(self, name: str, read_only: bool | None = None, no_typing: bool | None = None,
                   deny: list[str] | None = None, allow: list[str] | None = None, clear: bool = False,
                   wait_roots: list[str] | None = None) -> dict:
        """Change a client's standing limits without touching its token. None leaves a flag as it is;
        deny adds programs to the deny list, allow takes them off it (a program on the built-in list is
        allowed for this client), clear starts from no limits and the full built-in list. wait_roots
        replaces the folders this client's file waits may watch ([] lifts the limit)."""
        from overshoulder.gateway.limits import BUILTIN_DENY, Limits, process_key
        self._reload_if_changed()
        c = self._clients.get(name)
        if c is None:
            raise KeyError(f"no client named {name!r}")
        cur = Limits() if clear else Limits.from_dict(c.limits)
        denied = {process_key(p) for p in deny or ()}
        allowed = {process_key(p) for p in allow or ()}
        procs = (set(cur.deny_processes) | denied) - allowed
        lifted = set() if clear else {process_key(p) for p in c.limits.get("allow_builtin", ())}
        lifted = ((lifted | allowed) - denied) & BUILTIN_DENY
        new = Limits.make(cur.read_only if read_only is None else read_only,
                          cur.no_typing if no_typing is None else no_typing, procs)
        roots = [] if clear else list(c.limits.get("wait_roots") or [])
        if wait_roots is not None:
            roots = sorted({os.path.abspath(r) for r in wait_roots if r})
        sides = {**({"allow_builtin": sorted(lifted)} if lifted else {}), **({"wait_roots": roots} if roots else {})}
        c.limits = {**(new.to_dict() if new else {}), **sides}
        self.save()
        return {**new.to_dict(), **sides}

    def set_grants(self, name: str, window: str | None = None, screen: str | None = None) -> Client:
        """Change which scopes a client may take, keeping its token."""
        for grant in (window, screen):
            if grant is not None and grant not in ("allow", "approve", "deny"):
                raise ValueError(f"grant must be allow|approve|deny, got {grant!r}")
        self._reload_if_changed()
        c = self._clients.get(name)
        if c is None:
            raise KeyError(f"no client named {name!r}")
        c.window = window or c.window
        c.screen = screen or c.screen
        c.extra = {**c.extra, "grants_v": GRANTS_V}
        c.extra.pop("window_was", None)
        self.save()
        return c

    @staticmethod
    def wait_roots_for(client: Client | None) -> tuple[str, ...] | None:
        """The folders the person allows this client's file waits in, or None for any local folder."""
        roots = tuple(client.limits.get("wait_roots") or ()) if client else ()
        return roots or None

    def limits_for(self, client: Client):
        from overshoulder.gateway.limits import Limits
        return Limits.from_dict(client.limits)

    @staticmethod
    def builtin_for(client: Client | None):
        """The built-in deny list less what the person allowed this client; all of it for no client."""
        from overshoulder.gateway.limits import BUILTIN_DENY, Limits, process_key
        lifted = {process_key(p) for p in (client.limits.get("allow_builtin", ()) if client else ())}
        return Limits.make(deny_processes=BUILTIN_DENY - lifted)

    def get(self, name: str) -> Client | None:
        self._reload_if_changed()
        return self._clients.get(name)

    def remove(self, name: str) -> bool:
        gone = self._clients.pop(name, None) is not None
        if gone:
            self.save()
        return gone

    def names(self) -> list[str]:
        return sorted(self._clients)

    def authenticate(self, token: str) -> Client:
        self._reload_if_changed()
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
