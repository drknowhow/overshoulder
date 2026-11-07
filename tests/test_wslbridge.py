"""The relay that serves the agent box: it listens on the WSL adapter only while the setting is on,
follows the adapter's address, and passes connections to the gateway's /mcp-only loopback address."""

import socket
import threading

from overshoulder.gateway.guard import widened_settings
from overshoulder.wslbridge import WslBridge

TARGET = "127.0.0.2"  # stands in for the gateway's /mcp-only address


def echo_server():
    """An upper-casing server on TARGET; returns its port."""
    srv = socket.socket()
    srv.bind((TARGET, 0))
    srv.listen(8)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            with c:
                while data := c.recv(1024):
                    c.sendall(data.upper())

    threading.Thread(target=serve, daemon=True).start()
    return srv, srv.getsockname()[1]


def ask(addr, port, text=b"hello"):
    with socket.create_connection((addr, port), timeout=5) as c:
        c.sendall(text)
        c.shutdown(socket.SHUT_WR)
        out = b""
        while data := c.recv(1024):
            out += data
        return out


def refused(addr, port):
    try:
        socket.create_connection((addr, port), timeout=2).close()
    except OSError:
        return True
    return False


def test_it_listens_only_while_wanted_and_follows_the_address():
    srv, port = echo_server()
    state = {"wanted": False, "addr": "127.0.0.3"}
    hosts = ["127.0.0.1:1"]
    events = []
    b = WslBridge(port, lambda: state["wanted"], lambda: state["addr"], allowed_hosts=hosts,
                  record=lambda ev, **kw: events.append((ev, kw)), target=TARGET)
    try:
        assert b.sync() is None and refused("127.0.0.3", port)

        state["wanted"] = True
        assert b.sync() == "127.0.0.3"
        assert ask("127.0.0.3", port) == b"HELLO"
        assert f"127.0.0.3:{port}" in hosts

        state["addr"] = "127.0.0.4"  # WSL started again with a new address
        assert b.sync() == "127.0.0.4"
        assert refused("127.0.0.3", port) and ask("127.0.0.4", port) == b"HELLO"
        assert f"127.0.0.3:{port}" not in hosts and f"127.0.0.4:{port}" in hosts

        state["addr"] = None  # WSL stopped
        assert b.sync() is None and refused("127.0.0.4", port)

        state["addr"], state["wanted"] = "127.0.0.4", False  # turned off on the settings page
        assert b.sync() is None and refused("127.0.0.4", port)
        assert hosts == ["127.0.0.1:1"]
        assert [kw["address"] for ev, kw in events if ev == "wsl_listen"] == ["127.0.0.3", "127.0.0.4", None]
    finally:
        b.close()
        srv.close()


def test_an_address_it_cannot_listen_on_is_tried_again_later():
    srv, port = echo_server()
    blocker = socket.socket()
    blocker.bind(("127.0.0.5", port))
    blocker.listen(1)
    events = []
    b = WslBridge(port, lambda: True, lambda: "127.0.0.5", record=lambda ev, **kw: events.append(ev), target=TARGET)
    try:
        assert b.sync() is None and events == ["wsl_listen_failed"]
        blocker.close()
        assert b.sync() == "127.0.0.5" and ask("127.0.0.5", port) == b"HELLO"
    finally:
        b.close()
        srv.close()


def test_a_failing_lookup_means_not_listening():
    def broken():
        raise OSError("powershell missing")

    b = WslBridge(1, lambda: True, broken)
    assert b.sync() is None


def test_turning_it_on_is_asked_about():
    assert widened_settings({}, {"listen_wsl": True}) == ["the gateway takes MCP calls from WSL (the agent box)"]
    assert widened_settings({"listen_wsl": True}, {"listen_wsl": False}) == []
