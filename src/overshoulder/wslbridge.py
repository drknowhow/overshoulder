"""Serving the agent box: a listener on the WSL adapter that passes each connection to the gateway's
second loopback address, where only /mcp is served.

The WSL adapter's address changes whenever WSL starts again, and the person can turn serving the box on
or off on the settings page while the gateway runs, so the listener follows both: every few seconds the
bridge compares what it should listen on with what it does, and moves. The gateway's own server keeps
its sockets for its whole life; it listens on 127.0.0.1 (everything) and on BOX_LOOPBACK, which
server.OnlyMcpOn limits to /mcp. A connection that came in on the WSL adapter only ever reaches
BOX_LOOPBACK, so it gets /mcp and a client token check, never the settings page or the operator routes.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Callable

BOX_LOOPBACK = "127.0.0.2"
BOX_HOSTNAME = "overshoulder-gateway"  # what the box's /etc/hosts calls the Windows host
CHECK_EVERY_S = 15.0

log = logging.getLogger(__name__)


class WslBridge:
    def __init__(self, port: int, wanted: Callable[[], bool], address: Callable[[], str | None],
                 allowed_hosts: list[str] | None = None, every_s: float = CHECK_EVERY_S,
                 record: Callable[..., None] = lambda *a, **k: None, target: str = BOX_LOOPBACK):
        """wanted(): whether to serve the box now. address(): the WSL adapter's address, or None.
        allowed_hosts: the MCP transport's Host allow-list, kept in step with the address."""
        self.port, self.wanted, self.address, self.every_s = port, wanted, address, every_s
        self.allowed_hosts = allowed_hosts if allowed_hosts is not None else []
        self.record, self.target = record, target
        self.current: str | None = None
        self._listener: socket.socket | None = None
        self._lock = threading.Lock()

    def sync(self) -> str | None:
        """Listen where we should right now; returns the address listened on (None: not listening)."""
        try:
            want = self.address() if self.wanted() else None
        except Exception:
            log.exception("could not tell whether to serve the agent box")
            want = None
        with self._lock:
            if want == self.current:
                return self.current
            self._close()
            if want:
                try:
                    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    s.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", socket.SO_REUSEADDR), 1)
                    s.bind((want, self.port))
                    s.listen(16)
                except OSError as e:
                    log.warning("could not listen for the agent box on %s:%s: %s", want, self.port, e)
                    self.record("wsl_listen_failed", address=want, error=str(e))
                    return None
                self._listener, self.current = s, want
                self.allowed_hosts.append(f"{want}:{self.port}")
                threading.Thread(target=self._accept, args=(s,), daemon=True, name="overshoulder-wsl-accept").start()
            self.record("wsl_listen", address=self.current)
            return self.current

    def _close(self) -> None:
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass
            if f"{self.current}:{self.port}" in self.allowed_hosts:
                self.allowed_hosts.remove(f"{self.current}:{self.port}")
        self._listener, self.current = None, None

    def close(self) -> None:
        with self._lock:
            self._close()

    def start(self, stop: threading.Event) -> threading.Thread:
        def loop():
            self.sync()
            while not stop.wait(self.every_s):
                self.sync()
            self.close()
        t = threading.Thread(target=loop, daemon=True, name="overshoulder-wsl-bridge")
        t.start()
        return t

    def _accept(self, listener: socket.socket) -> None:
        while True:
            try:
                conn, _ = listener.accept()
            except OSError:
                return  # closed: the address moved, or serving the box was turned off
            threading.Thread(target=self._relay, args=(conn,), daemon=True, name="overshoulder-wsl-relay").start()

    def _relay(self, conn: socket.socket) -> None:
        try:
            up = socket.create_connection((self.target, self.port), timeout=10)
            up.settimeout(None)
        except OSError:
            conn.close()
            return
        t = threading.Thread(target=_pump, args=(up, conn), daemon=True)
        t.start()
        _pump(conn, up)
        t.join()
        up.close()
        conn.close()


def _pump(src: socket.socket, dst: socket.socket) -> None:
    """Copy one direction until it ends, then pass the end on; _relay closes both sockets afterwards."""
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    try:
        dst.shutdown(socket.SHUT_WR)
    except OSError:
        pass
