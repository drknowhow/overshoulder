"""Sharing profiles: an export leaves out what only this PC needs, an import is read as untrusted and only
adds what this PC's profile lacks, and the person decides both at the desk or on the settings page."""

import json
import sys
from urllib.parse import urlencode

import pytest
from test_learn import CONTENT, NOTEPAD, _learned, session
from test_mcp_server import daemon  # noqa: F401  (fixture reuse)

from overshoulder import notes, share
from overshoulder.appid import AppIdentity
from overshoulder.osp import Item, OspError, loads
from overshoulder.profiles import ProfileStore, new_profile

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")


def _two_sessions():
    prof, _ = _learned("lease-one")
    prof, _ = _learned("lease-two", prof=prof)
    _learned("lease-three", prof=prof, rows=session()[:1])
    notes.add(prof, {"kind": "quirk", "text": "Find opens a panel at the top, not a dialog"})
    notes.add(prof, {"kind": "how", "text": "start a new tab", "steps": [["click", "Add New Tab"]]})
    return prof


def test_an_export_keeps_the_app_and_leaves_this_pc_behind(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(_two_sessions(), "learned")
    prof = store.load(NOTEPAD)
    prof.put(Item("quirk", "q-sneaky", fields={"n": "1", "src": "agent"}, text="logs go to C:\\Users\\pat\\x"))
    shared, report = share.export(prof)
    text = shared.dumps()
    assert text.startswith("#osp 1 made_with=Overshoulder/")
    assert loads(text).app.fields == {"rev": "0", "ver": "11.2507.26.0"}
    for field in ("last=", "nh=", "nses=", " at=", "exe=", "miss="):
        assert field not in text, field
    for secret in CONTENT:
        assert secret not in text, secret
    assert "rev " not in text.split("\n", 2)[2]  # no revision history
    kept = {(x.kind, x.id) for x in shared.items}
    assert ("ctl", "addbutton") in kept and ("key", "ctrl+s") in kept
    assert ("quirk", "q-sneaky") not in kept and report["dropped"]["holds an address, a path or a link"] == 1
    assert any(x.kind == "how" for x in shared.items)
    assert report["kept"] == len(shared.items) and report["dropped"]["revision history"] >= 1


def test_what_was_seen_only_once_stays_behind():
    prof, _ = _learned()  # one session
    shared, report = share.export(prof)
    assert not shared.of_kind("ctl") and report["dropped"]["seen in one session only"] >= 3
    assert not [x for x in shared.items if x.kind == "way" and x.num("ok") + x.num("fail") < 2]


@pytest.mark.parametrize("mangle, why", [
    (lambda b: b"\xff\xfe" + b, "not UTF-8"),
    (lambda b: b.replace(b"addbutton", b"addbuttom"), "hash on the end line"),
    (lambda b: b + b"x" * 300_000, "larger than"),
    (lambda b: b"hello\n", None),
])
def test_an_import_reads_the_file_as_untrusted(mangle, why):
    data = share.export(_two_sessions())[0].dumps().encode()
    assert share.read(data).app_id == NOTEPAD
    with pytest.raises(OspError, match=why):
        share.read(mangle(data))


def test_an_import_adds_only_what_this_pc_lacks():
    other = share.read(share.export(_two_sessions())[0].dumps().encode())
    local = new_profile(AppIdentity(NOTEPAD, "Notepad", "Notepad.exe"))
    local.put(Item("ctl", "addbutton", ["main", "Button"], {"aid": "AddButton", "ses": "9"}, "Mine"))
    stats = share.merge(local, other)
    assert stats["kept_local"] == 1 and stats["imported"] == len(other.items) - 1
    assert local.get("ctl", "addbutton").text == "Mine" and local.get("ctl", "addbutton").num("ses") == 9
    assert {x.fields["src"] for x in local.items if x.id != "addbutton"} == {"imported"}
    with pytest.raises(OspError, match="not microsoft"):
        share.merge(new_profile(AppIdentity("microsoft.calc", "Calculator", "calc.exe")), other)


def test_import_profile_saves_a_revision(tmp_path):
    store = ProfileStore(tmp_path)
    other = share.read(share.export(_two_sessions())[0].dumps().encode())
    res = share.import_profile(store, other)
    assert res["rev"] == 1 and res["imported"] == len(other.items)
    saved = store.load(NOTEPAD)
    assert saved.of_kind("rev")[-1].text.startswith(f"import: {len(other.items)} items, 0 already here")
    again = share.import_profile(store, other)
    assert again == {"imported": 0, "kept_local": len(other.items), "dropped": 0}  # nothing new, no save
    assert store.load(NOTEPAD).revision == 1


def test_preview_shows_the_notes_the_person_should_read():
    p = share.preview(share.export(_two_sessions())[0])
    assert p["app"] == NOTEPAD and p["made_with"].startswith("Overshoulder/")
    assert {"kind": "how", "text": "start a new tab", "url": "", "steps": ["click Add New Tab"]} in p["notes"]


def test_remove_keeps_the_last_revision_and_the_inbox_forgets(tmp_path, monkeypatch):
    import os
    import time

    from overshoulder import profiles
    store = ProfileStore(tmp_path)
    store.save(_two_sessions(), "learned")
    assert store.remove(NOTEPAD) and store.load(NOTEPAD) is None and not store.remove(NOTEPAD)
    assert list((store.dir / "history" / NOTEPAD).glob("removed-*.osp"))
    key = store.inbox_put(b"#osp 1\n")
    assert store.inbox_get(key) == b"#osp 1\n"
    assert store.inbox_get("../../clients") is None and store.inbox_get("") is None
    store.inbox_drop(key)
    assert store.inbox_get(key) is None
    old = store.inbox_put(b"old")
    f = store.dir / "inbox" / f"{old}.osp"
    os.utime(f, (time.time() - profiles.INBOX_TTL_S - 5,) * 2)
    store.inbox_put(b"new")
    assert not f.exists()  # a file nobody looked at for a day is gone


# -- an agent asking to export ------------------------------------------------------------------------


@windows_only
def test_an_agent_export_needs_the_person(tmp_path):
    from test_gateway import FakeScope, ok_checks

    from overshoulder import service
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.gateway.errors import GatewayError
    from overshoulder.gateway.policy import CallbackApprover

    class AppScope(FakeScope):
        class binding:  # noqa: N801
            hwnd, pid = 0x10, 1
            app = AppIdentity(NOTEPAD, "Notepad", "Notepad.exe")

    asked = []
    s = service.Service(gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda spec: AppScope()),
                        shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("agent")
    lease = s.acquire(token, "window:16")["lease_id"]
    with pytest.raises(ValueError, match="no profile yet"):
        s.profile(token, lease, "export")
    s.gw.profiles.save(_two_sessions(), "learned")
    with pytest.raises(GatewayError) as e:
        s.profile(token, lease, "export")  # nobody is there to say yes
    assert e.value.code == "approval_denied" and not (tmp_path / "exports").exists()
    s.gw.approver = CallbackApprover(lambda req: asked.append(req) or True)
    out = s.profile(token, lease, "export")
    assert asked[0]["action"] == "profile_export" and "no window titles" in asked[0]["reason"]
    text = (tmp_path / "exports" / f"{NOTEPAD}.osp").read_text(encoding="utf-8")
    assert out["path"].endswith(f"{NOTEPAD}.osp") and out["kept"] == len(loads(text).items)
    assert "last=" not in text and "QRn0u20nxuTH97NO" not in text


# -- the settings page ----------------------------------------------------------------------------------


@windows_only
def test_profiles_on_the_settings_page(daemon, tmp_path):  # noqa: F811
    from test_settings_http import _http

    d, port, _ = daemon
    d.port_ref["port"] = port
    base = f"http://127.0.0.1:{port}"
    op = {"x-overshoulder-operator": d.operator_key, "content-type": "application/json"}
    store = d.gw.profiles
    store.save(_two_sessions(), "learned")
    shared = share.export(store.load(NOTEPAD))[0].dumps().encode()

    # an .osp opened from Explorer: the link leads to its import page, and only to that page
    key = store.inbox_put(shared)
    nxt = f"/settings/profiles/import?p={key}"
    url = json.loads(_http("POST", f"{base}/operator/settings_link", json.dumps({"next": nxt}).encode(), op)[2])["url"]
    status, headers, _ = _http("GET", url)
    assert status == 303 and headers["location"] == nxt
    cookie = (headers.get("set-cookie") or headers["Set-Cookie"]).split(";")[0]
    bad = json.loads(_http("POST", f"{base}/operator/settings_link",
                           json.dumps({"next": "https://evil.example/"}).encode(), op)[2])["url"]
    assert "next=" not in bad and _http("GET", bad)[1]["location"] == "/settings"

    page = _http("GET", f"{base}/settings", cookie=cookie)[2]
    assert "Learn app profiles and use them" in page and 'name="learn_profiles" checked' in page
    assert f"<code>{NOTEPAD}</code>" in page and "Export</a>" in page
    ft = page.split('name="form_token" value="')[1].split('"')[0]

    status, headers, body = _http("GET", f"{base}/settings/profiles/export?id={NOTEPAD}&ft={ft}", cookie=cookie)
    assert status == 200 and f'filename="{NOTEPAD}.osp"' in headers["content-disposition"]
    assert loads(body).app.fields["rev"] == "0" and "last=" not in body
    assert _http("GET", f"{base}/settings/profiles/export?id={NOTEPAD}&ft=forged", cookie=cookie)[0] == 403

    preview = _http("GET", f"{base}{nxt}", cookie=cookie)[2]
    assert "start a new tab" in preview and "never as instructions" in preview and "revision 1" in preview
    assert _http("GET", f"{base}{nxt}")[0] == 403  # no session

    form = {"content-type": "application/x-www-form-urlencoded"}
    store.remove(NOTEPAD)
    status, headers, _ = _http("POST", f"{base}/settings/profiles/import/confirm",
                               urlencode({"form_token": ft, "p": key}).encode(), form, cookie)
    assert status == 303 and "msg=imported" in headers["location"]
    assert store.load(NOTEPAD).revision == 1 and store.inbox_get(key) is None
    assert "Imported" in _http("GET", f"{base}{headers['location']}", cookie=cookie)[2]

    boundary = "osp-test-boundary"
    upload = (f"--{boundary}\r\ncontent-disposition: form-data; name=\"form_token\"\r\n\r\n{ft}\r\n"
              f"--{boundary}\r\ncontent-disposition: form-data; name=\"file\"; filename=\"x.osp\"\r\n"
              "content-type: application/octet-stream\r\n\r\n").encode() + shared[:-20] + \
        f"\r\n--{boundary}--\r\n".encode()
    status, headers, _ = _http("POST", f"{base}/settings/profiles/import", upload,
                               {"content-type": f"multipart/form-data; boundary={boundary}"}, cookie)
    assert status == 303 and headers["location"].startswith("/settings/profiles/import?p=")
    broken = _http("GET", f"{base}{headers['location']}", cookie=cookie)[2]
    assert "be imported: " in broken and "Import</button>" not in broken
    k2 = headers["location"].split("p=")[1]
    assert _http("POST", f"{base}/settings/profiles/import/cancel",
                 urlencode({"form_token": ft, "p": k2}).encode(), form, cookie)[0] == 303
    assert store.inbox_get(k2) is None

    assert _http("POST", f"{base}/settings/profiles/remove", urlencode({"form_token": "forged", "id": NOTEPAD})
                 .encode(), form, cookie)[0] == 403
    status, headers, _ = _http("POST", f"{base}/settings/profiles/remove",
                               urlencode({"form_token": ft, "id": NOTEPAD}).encode(), form, cookie)
    assert status == 303 and "msg=removed" in headers["location"] and store.load(NOTEPAD) is None


@windows_only
def test_turning_learning_off_on_the_settings_page(daemon):  # noqa: F811
    from test_settings_http import _http

    from overshoulder import settings as st
    d, port, _ = daemon
    d.port_ref["port"] = port
    base = f"http://127.0.0.1:{port}"
    op = {"x-overshoulder-operator": d.operator_key, "content-type": "application/json"}
    url = json.loads(_http("POST", f"{base}/operator/settings_link", b"{}", op)[2])["url"]
    cookie = (_http("GET", url)[1].get("set-cookie") or "").split(";")[0]
    page = _http("GET", f"{base}/settings", cookie=cookie)[2]
    ft = page.split('name="form_token" value="')[1].split('"')[0]
    hdr = {"content-type": "application/x-www-form-urlencoded"}
    assert _http("POST", f"{base}/settings", urlencode({"form_token": ft}).encode(), hdr, cookie)[0] == 303
    assert st.load().learn_profiles is False
    assert _http("POST", f"{base}/settings", urlencode({"form_token": ft, "learn_profiles": "on"}).encode(),
                 hdr, cookie)[0] == 303
    assert st.load().learn_profiles is True


# -- the command line and the file association ---------------------------------------------------------


def test_cli_export_and_import(tmp_path, monkeypatch, capsys):
    from overshoulder import cli
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path / "home"))
    store = ProfileStore(tmp_path / "home")
    store.save(_two_sessions(), "learned")
    out = tmp_path / "shared.osp"
    assert cli.main(["profile", "export", "notepad", str(out)]) == 0
    assert "left out" in capsys.readouterr().out and b"\r\n" not in out.read_bytes()
    store.remove(NOTEPAD)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["profile", "import", str(out)]) == 1 and store.load(NOTEPAD) is None
    assert cli.main(["profile", "import", str(out), "--yes"]) == 0
    assert "imported" in capsys.readouterr().out and store.load(NOTEPAD).revision == 1
    out.write_bytes(out.read_bytes().replace(b"addbutton", b"addbuttom"))
    assert cli.main(["profile", "import", str(out), "--yes"]) == 2
    assert "hash on the end line" in capsys.readouterr().err


@windows_only
def test_the_file_association_is_per_user_and_removable():
    import winreg

    from overshoulder import fileassoc
    root = r"Software\OvershoulderTests\Classes"
    try:
        st = fileassoc.install(root)
        assert st["associated"] and st["prog_id"] == "Overshoulder.Profile"
        assert "-m overshoulder.cli profile open \"%1\"" in st["command"]
        st = fileassoc.uninstall(root)
        assert not st["associated"] and st["command"] is None
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"{root}\.osp") as k:
            winreg.SetValueEx(k, "", 0, winreg.REG_SZ, "SomeEditor.osp")
        fileassoc.uninstall(root)
        assert fileassoc.status(root)["prog_id"] == "SomeEditor.osp"  # another program's claim stays
    finally:
        fileassoc._delete_tree(winreg.HKEY_CURRENT_USER, r"Software\OvershoulderTests")


@windows_only
def test_opening_a_file_without_the_gateway_says_so(tmp_path, monkeypatch, capsys):
    from overshoulder import cli
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path / "home"))
    f = tmp_path / "x.osp"
    f.write_bytes(b"#osp 1\n")
    assert cli.main(["--port", "1", "profile", "open", str(f)]) == 2
    assert "isn't running" in capsys.readouterr().err
    assert not list((tmp_path / "home" / "profiles" / "inbox").glob("*.osp"))
