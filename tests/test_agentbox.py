"""overshoulder agent-box: building, checking and removing the locked-down WSL distro, with wsl.exe and
PowerShell faked."""

import base64
import hashlib
import json
import subprocess

import pytest

from overshoulder import agentbox as A
from overshoulder.gateway.clients import ClientRegistry

IMAGE = b"debian image bytes"
SHA = hashlib.sha256(IMAGE).hexdigest()
INDEX = {"ModernDistributions": {"Debian": [{"Name": "Debian", "Amd64Url": {
    "Url": "https://example.test/debian.wsl", "Sha256": "0x" + SHA.upper()}}]}}


class FakeWindows:
    """wsl.exe and powershell.exe: a distro that may or may not exist, files written into it, and the
    firewall rules."""

    def __init__(self, exists=False, checks_ok=True, uac=True):
        self.exists, self.checks_ok, self.uac = exists, checks_ok, uac
        self.files: dict[str, bytes] = {}
        self.calls: list[list[str]] = []
        self.elevated: list[str] = []
        self.rules = 0

    def __call__(self, args, input=None, timeout=None):
        self.calls.append(list(args))
        ok = lambda out=b"": subprocess.CompletedProcess(args, 0, out, b"")  # noqa: E731
        if args[0] == "powershell.exe":
            cmd = args[-1]
            if "Start-Process" in cmd:
                enc = cmd.split("'-EncodedCommand','")[1].split("'")[0]
                inner = base64.b64decode(enc).decode("utf-16-le")
                self.elevated.append(inner)
                if not self.uac:
                    return subprocess.CompletedProcess(args, 1, b"", b"")
                self.rules = inner.count("New-NetFirewallRule")
                return ok()
            if ").Count" in cmd:
                return ok(str(self.rules).encode())
            return ok()
        rest = args[1:]
        if rest[:2] == ["--list", "--quiet"]:
            return ok(("Ubuntu\n" + (A.DISTRO + "\n" if self.exists else "")).encode())
        if rest[0] == "--import":
            self.exists = True
            return ok()
        if rest[0] == "--unregister":
            self.exists = False
            return ok()
        if rest[0] == "--terminate":
            return ok()
        script = input.decode() if rest[-1] == "-s" else rest[-1]
        if script.startswith("mkdir -p") and "cat >" in script:
            path = script.split("cat > ")[1].split(".new")[0].strip("'")
            self.files[path] = input
            return ok()
        if script.startswith("cat " + A.TOKEN_PATH):
            return ok(self.files.get(A.TOKEN_PATH, b""))
        if "echo ok" in script:
            return ok(b"ok\n" if self.checks_ok else b"")
        return ok()


def box(tmp_path, win, **kw):
    return A.Box(run=win, say=lambda s: None, registry=ClientRegistry(tmp_path / "clients.json"),
                 fetch=lambda url: json.dumps(INDEX).encode(),
                 download=lambda url, to: to.write_bytes(kw.get("image", IMAGE)))


def test_the_image_comes_from_microsofts_list_with_its_sha256(tmp_path):
    assert box(tmp_path, FakeWindows()).image() == ("https://example.test/debian.wsl", SHA)


def test_an_image_whose_sha256_differs_is_not_imported(tmp_path):
    win = FakeWindows()
    b = A.Box(run=win, say=lambda s: None, registry=ClientRegistry(tmp_path / "clients.json"),
              fetch=lambda url: json.dumps(INDEX).encode(), download=lambda url, to: to.write_bytes(b"tampered"))
    with pytest.raises(A.BoxError, match="SHA-256"):
        b.create(tmp_path / "home")
    assert not any(c[1] == "--import" for c in win.calls if c[0] == "wsl.exe")


def test_create_builds_a_locked_down_box_with_a_token_of_its_own(tmp_path):
    win = FakeWindows()
    b = box(tmp_path, win)
    assert b.create(tmp_path / "home") == []
    assert ["wsl.exe", "--import", A.DISTRO, str(tmp_path / "home")] == win.calls[1][:4]
    conf = win.files["/etc/wsl.conf"]
    assert not conf.startswith(b"\xef\xbb\xbf")  # WSL ignores a wsl.conf with a byte-order mark
    for line in (b"[interop]\nenabled=false", b"[automount]\nenabled=false", b"default=agent"):
        assert line in conf
    assert b"--uid-owner $U -j AGENT_OUT" in win.files["/usr/local/sbin/agent-egress.sh"]
    assert win.files[A.PORT_PATH] == b"47621\n"
    c = b.registry.get(A.CLIENT)
    assert (c.window, c.screen) == ("approve", "deny")
    token = win.files[A.TOKEN_PATH].decode().strip()
    assert hashlib.sha256(token.encode()).hexdigest() == c.token_sha256
    assert any(c[1] == "--terminate" for c in win.calls if c[0] == "wsl.exe")
    rule = win.elevated[-1]
    assert "-LocalPort 47621" in rule and "pythonw.exe" in rule and "'Overshoulder MCP from WSL'" in rule


def test_running_create_again_keeps_a_token_that_still_matches(tmp_path):
    win = FakeWindows()
    b = box(tmp_path, win)
    b.create(tmp_path / "home")
    first = b.registry.get(A.CLIENT).token_sha256
    elevated = len(win.elevated)
    b.create(tmp_path / "home")
    assert b.registry.get(A.CLIENT).token_sha256 == first
    assert sum(1 for c in win.calls if c[:2] == ["wsl.exe", "--import"]) == 1
    assert len(win.elevated) == elevated  # the firewall rules are there: no second UAC prompt


def test_a_box_whose_token_was_lost_gets_a_new_one(tmp_path):
    win = FakeWindows()
    b = box(tmp_path, win)
    b.create(tmp_path / "home")
    first = b.registry.get(A.CLIENT).token_sha256
    del win.files[A.TOKEN_PATH]
    b.create(tmp_path / "home")
    assert b.registry.get(A.CLIENT).token_sha256 != first


def test_create_reports_each_part_of_the_lock_down_that_does_not_hold(tmp_path):
    win = FakeWindows(checks_ok=False)
    failed = box(tmp_path, win).create(tmp_path / "home", firewall=False)
    assert len(failed) == len(A.CHECKS) and any("Windows drive" in f for f in failed)
    assert win.elevated == []


def test_a_declined_uac_prompt_is_an_error(tmp_path):
    with pytest.raises(A.BoxError, match="UAC"):
        box(tmp_path, FakeWindows(uac=False)).create(tmp_path / "home")


def test_remove_deletes_the_distro_the_client_and_the_rules(tmp_path):
    win = FakeWindows()
    b = box(tmp_path, win)
    b.create(tmp_path / "home")
    b.remove()
    assert not win.exists and b.registry.get(A.CLIENT) is None
    assert "Remove-NetFirewallRule" in win.elevated[-1]


def test_the_cli_removes_nothing_without_yes(monkeypatch, capsys):
    from overshoulder import cli
    called = []
    monkeypatch.setattr(A.Box, "remove", lambda self: called.append(1))
    monkeypatch.setattr(A.Box, "__post_init__", lambda self: None)
    assert cli.main(["agent-box", "remove"]) == 2 and called == []
    assert "--yes" in capsys.readouterr().out
