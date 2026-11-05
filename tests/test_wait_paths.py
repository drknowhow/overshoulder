"""File waits watch only local paths the person allows, and clients made under the old grant default
ask again before window scopes."""

import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import cli  # noqa: E402
from overshoulder import settings as st  # noqa: E402
from overshoulder import waits as W  # noqa: E402
from overshoulder.gateway.clients import ClientRegistry  # noqa: E402
from overshoulder.gateway.guard import widened_clients  # noqa: E402


def file_wait(path):
    return [{"kind": "file", "path": str(path)}]


@pytest.mark.parametrize("path", [r"\\attacker.example\share\x", "//attacker.example/share/x",
                                  r"\\?\UNC\attacker.example\share\x", r"\\?\C:\x", "relative.txt", "C:x"])
def test_network_device_and_relative_paths_are_refused(path):
    with pytest.raises(W.WaitError):
        W.Waiter(file_wait(path), st.Settings())


def test_overshoulder_data_folder_is_refused_even_through_dotdot(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("OVERSHOULDER_HOME", str(home))
    for p in (home / "operator.key", tmp_path / "x" / ".." / "home" / "clients.json", home):
        with pytest.raises(W.WaitError, match="data folder"):
            W.Waiter(file_wait(p), st.Settings())
    W.Waiter(file_wait(tmp_path / "homework.txt"), st.Settings())  # a sibling with the same prefix is fine


def test_a_client_limited_to_folders_waits_only_in_them(tmp_path):
    ok, other = tmp_path / "downloads", tmp_path / "secret"
    W.Waiter(file_wait(ok / "report.pdf"), st.Settings(), file_roots=(str(ok),))
    for p in (other / "x", ok / ".." / "secret" / "x"):
        with pytest.raises(W.WaitError, match="limited"):
            W.Waiter(file_wait(p), st.Settings(), file_roots=(str(ok),))


def test_a_folder_limit_added_during_a_wait_ends_it(tmp_path):
    calls = []

    def authorize():
        calls.append(1)
        roots = None if len(calls) == 1 else (str(tmp_path / "elsewhere"),)
        return st.Settings(poll_interval_ms=10), frozenset(), roots

    with pytest.raises(W.WaitError, match="limited"):
        W.wait(file_wait(tmp_path / "never"), st.Settings(), timeout_s=5, authorize=authorize)


def test_client_limit_sets_and_lifts_wait_folders_and_rotation_keeps_them(tmp_path, capsys):
    reg = ClientRegistry(tmp_path / "clients.json")
    reg.add("a")
    out = reg.set_limits("a", wait_roots=[str(tmp_path / "dl")])
    assert out["wait_roots"] == [str(tmp_path / "dl")]
    assert reg.wait_roots_for(reg.get("a")) == (str(tmp_path / "dl"),)
    reg.add("a")  # rotate the token
    assert reg.wait_roots_for(ClientRegistry(reg.path).get("a")) == (str(tmp_path / "dl"),)
    reg.set_limits("a", read_only=True)  # other limits leave the folders alone
    assert reg.get("a").limits["wait_roots"] == [str(tmp_path / "dl")]
    reg.set_limits("a", wait_roots=[])
    assert reg.wait_roots_for(reg.get("a")) is None


def test_widening_wait_folders_counts_as_widening():
    old = {"clients": [{"name": "a", "token_sha256": "h", "limits": {"wait_roots": ["C:\\dl"]}}]}
    lifted = {"clients": [{"name": "a", "token_sha256": "h", "limits": {}}]}
    more = {"clients": [{"name": "a", "token_sha256": "h", "limits": {"wait_roots": ["C:\\dl", "C:\\"]}}]}
    fewer = {"clients": [{"name": "a", "token_sha256": "h", "limits": {"wait_roots": []}}]}
    assert widened_clients(old, lifted) and widened_clients(old, more)
    assert not widened_clients(lifted, old)
    assert widened_clients(old, fewer)  # an empty list means any folder


# -- clients made under the old default --------------------------------------------------------------

def test_an_old_client_with_window_allow_asks_again_once(tmp_path):
    path = tmp_path / "clients.json"
    path.write_text(json.dumps({"clients": [
        {"name": "old", "token_sha256": "h", "window": "allow", "screen": "approve"},
        {"name": "careful", "token_sha256": "h2", "window": "deny", "screen": "deny"}]}), encoding="utf-8")
    reg = ClientRegistry(path)
    assert reg.get("old").window == "approve" and reg.get("careful").window == "deny"
    assert reg.get("old").extra["window_was"] == "allow"
    reg.set_grants("old", window="allow")  # the person gives it back on purpose
    again = ClientRegistry(path)
    assert again.get("old").window == "allow" and "window_was" not in again.get("old").extra


def test_a_new_client_given_window_allow_keeps_it(tmp_path):
    reg = ClientRegistry(tmp_path / "clients.json")
    reg.add("chosen", window="allow")
    assert ClientRegistry(reg.path).get("chosen").window == "allow"


def test_client_grant_command_keeps_the_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path))
    reg = ClientRegistry()
    token = reg.add("a")
    assert cli.main(["client", "grant", "a", "--window", "allow"]) == 0
    reg = ClientRegistry()
    assert reg.get("a").window == "allow" and reg.authenticate(token).name == "a"
    assert cli.main(["client", "grant", "nobody", "--window", "deny"]) == 2
