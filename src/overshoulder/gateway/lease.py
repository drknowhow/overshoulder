"""The driver lease: one agent drives the desktop at a time.

A lease has a TTL renewed by heartbeats. Others wait in a FIFO queue. The watchdog can revoke the
active lease (panic key, which also pauses everything) or pause with HUMAN_PAUSE while the person uses
the PC, which holds back only the person's real mouse and keyboard. A revoked lease never comes back;
the holder must acquire a new one.
"""

from __future__ import annotations

import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field

from overshoulder.gateway.errors import LeaseBusy, LeaseInvalid, LeaseRevoked, Paused

DEFAULT_TTL_S = 60.0
MAX_TTL_S = 600.0
# A queued client that stops retrying loses its place, so an abandoned request cannot block everyone.
QUEUE_STALE_S = 30.0
# The watchdog's pause while the person uses the PC. It holds back only actions that need their real
# input, which may wait it out; background work goes on. Every other pause (panic, operator) stops all.
HUMAN_PAUSE = "human input"
REVOKED_MEMORY = 1000


@dataclass
class Lease:
    id: str
    client: str
    scope: object  # overshoulder.scope.Scope
    ttl_s: float
    expires_at: float
    acquired_at: float
    revoked: str | None = None
    actions: int = 0
    meta: dict = field(default_factory=dict)


class LeaseManager:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.RLock()
        self._active: Lease | None = None
        self._revoked: OrderedDict[str, str] = OrderedDict()
        self._queue: deque[str] = deque()
        self._queue_seen: dict[str, float] = {}
        self._paused: str | None = None
        # Called with (lease, reason) whenever a lease ends: released, expired or revoked. It runs under
        # the lease lock, so it must only hand the lease off.
        self.on_end: Callable[[Lease, str], None] | None = None

    # -- state ---------------------------------------------------------------------------------

    def _end(self, lease: Lease, reason: str) -> None:
        self._revoked[lease.id] = reason
        while len(self._revoked) > REVOKED_MEMORY:
            self._revoked.popitem(last=False)
        if self.on_end is not None:
            try:
                self.on_end(lease, reason)
            except Exception:  # learning is never allowed to keep a lease alive
                pass

    def _expire(self) -> None:
        now = self._clock()
        if self._active and now >= self._active.expires_at:
            self._end(self._active, "expired")
            self._active = None
        for c in [c for c in self._queue if now - self._queue_seen.get(c, now) > QUEUE_STALE_S]:
            self._queue.remove(c)
            self._queue_seen.pop(c, None)

    def active(self) -> Lease | None:
        with self._lock:
            self._expire()
            return self._active

    def status(self) -> dict:
        with self._lock:
            self._expire()
            a = self._active
            active = None
            if a is not None:
                active = {"client": a.client, "scope": a.scope.describe(),
                          "expires_in_s": round(a.expires_at - self._clock(), 1), "actions": a.actions}
                try:
                    b = a.scope.bounds()
                    active["bounds"] = [b.left, b.top, b.right, b.bottom]
                except Exception:
                    active["bounds"] = None  # scope went stale; the next action will say so
            return {"paused": self._paused, "active": active, "queue": list(self._queue)}

    # -- holder operations ---------------------------------------------------------------------

    def acquire(self, client: str, scope, ttl_s: float = DEFAULT_TTL_S) -> Lease:
        """Take the lease, or raise LeaseBusy with the caller's queue position.

        A client already holding the lease gets it back with the new scope (re-scoping is allowed
        without releasing, so an agent can move from one window to its dialog's parent, etc.).
        """
        ttl_s = max(1.0, min(float(ttl_s), MAX_TTL_S))
        with self._lock:
            self._expire()
            if self._paused and self._paused != HUMAN_PAUSE:
                raise Paused(f"driving is paused: {self._paused}")
            a = self._active
            if a and a.client != client:
                raise LeaseBusy(f"lease held by {a.client}", holder=a.client,
                                position=self._position(client))
            if a is None and self._queue and self._queue[0] != client:
                raise LeaseBusy("another client is first in the queue", holder=self._queue[0],
                                position=self._position(client))
            if client in self._queue:
                self._queue.remove(client)
                self._queue_seen.pop(client, None)
            now = self._clock()
            if a and a.client == client:
                a.scope, a.ttl_s, a.expires_at = scope, ttl_s, now + ttl_s
                return a
            self._active = Lease(secrets.token_urlsafe(12), client, scope, ttl_s, now + ttl_s, now)
            return self._active

    def _position(self, client: str) -> int:
        if client not in self._queue:
            self._queue.append(client)
        self._queue_seen[client] = self._clock()
        return list(self._queue).index(client) + 1

    def check(self, lease_id: str, client: str, human_ok: bool = False) -> Lease:
        """The lease must be the active one, held by this client, unexpired and not paused. human_ok lets
        HUMAN_PAUSE through, for work that does not touch the person's input."""
        with self._lock:
            self._expire()
            if lease_id in self._revoked:
                reason = self._revoked[lease_id]
                if reason == "expired":
                    raise LeaseInvalid("lease expired; acquire a new one")
                if reason == "released":
                    raise LeaseInvalid("lease was released")
                raise LeaseRevoked(f"lease revoked: {reason}")
            a = self._active
            if a is None or a.id != lease_id or a.client != client:
                raise LeaseInvalid("not the active lease for this client")
            if self._paused and not (human_ok and self._paused == HUMAN_PAUSE):
                raise Paused(f"driving is paused: {self._paused}")
            return a

    def heartbeat_unchecked(self, lease_id: str, client: str) -> None:
        """Keep a lease alive while it waits out a pause (check() would refuse during the pause)."""
        with self._lock:
            a = self._active
            if a and a.id == lease_id and a.client == client:
                a.expires_at = max(a.expires_at, self._clock() + 5.0)

    def paused_reason(self) -> str | None:
        with self._lock:
            return self._paused

    def heartbeat(self, lease_id: str, client: str) -> Lease:
        with self._lock:
            a = self.check(lease_id, client, human_ok=True)
            a.expires_at = self._clock() + a.ttl_s
            return a

    def release(self, lease_id: str, client: str) -> None:
        with self._lock:
            a = self._active
            if a and a.id == lease_id and a.client == client:
                self._end(a, "released")
                self._active = None

    def count_action(self, lease: Lease) -> None:
        with self._lock:
            lease.actions += 1

    def leave_queue(self, client: str) -> None:
        with self._lock:
            if client in self._queue:
                self._queue.remove(client)
                self._queue_seen.pop(client, None)

    # -- watchdog / operator -------------------------------------------------------------------

    def revoke(self, reason: str) -> Lease | None:
        """Panic: end the active lease now. Returns the revoked lease, if any."""
        with self._lock:
            a = self._active
            if a:
                self._end(a, reason)
                self._active = None
            return a

    def pause(self, reason: str) -> None:
        with self._lock:
            self._paused = reason

    def resume(self, only_if: str | None = None) -> bool:
        """Lift the pause. With only_if, only a pause with exactly that reason is lifted, so an
        automatic resume (human stopped typing) can never undo a panic."""
        with self._lock:
            if only_if is not None and self._paused != only_if:
                return False
            self._paused = None
            return True
