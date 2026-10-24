import copy
import sys

import pytest

from overshoulder import learn
from overshoulder.appid import AppIdentity
from overshoulder.osp import loads
from overshoulder.profiles import ProfileStore, new_profile

NOTEPAD = "Microsoft.WindowsNotepad_8wekyb3d8bbwe"
TITLE = "pets.txt - Notepad"
SCOPE = {"kind": "window", "hwnd": 7541254, "pid": 30744, "process": "Notepad.exe", "class": "Notepad",
         "title": TITLE, "app": {"id": NOTEPAD, "name": "Notepad", "version": "11.2507.26.0"}}
# The person's content in this session: none of it may reach the profile.
CONTENT = ["pets.txt", "Unmodified", "Buy cat food", "Notepad.exe -", r"C:\Users", "QRn0u20nxuTH97NO",
           "XXUG2FI3ektZ57jH"]


def _el(name, ctype, aid="", ref="e1"):
    return {"kind": "element", "name": name, "control_type": ctype, "automation_id": aid, "ref": ref,
            "rect": [8, 133, 60, 160]}


def _row(action, target=None, ok=True, method=None, code=None, **extra):
    r = {"ts": 1.0, "event": "action", "client": "a", "lease": "QRn0u20nxuTH97NO", "action": action,
         "params": {"ref": None, "combo": extra.pop("combo", None), "text": {"redacted": True, "len": 12}},
         "target": target, "ok": ok, "scope": SCOPE, "ms": extra.pop("ms", 400)}
    if ok:
        r["verify"] = {"ok": True, "changed": True, "method": method, "background": True,
                       "readback": {"value": "Buy cat food"}}
    else:
        r["error"] = {"code": code, "message": f"refused: {TITLE}"}
    r.update(extra)
    return r


def session():
    return [
        _row("click", _el("Add New Tab", "Button", "AddButton"), method="uia_invoke",
             attempts=[{"method": "button_message", "ok": False}, {"method": "uia_invoke", "ok": True}]),
        _row("select", _el("pets.txt. Unmodified.", "TabItem", ref="e72"), method="uia_select"),
        _row("type", _el("Text editor", "Document"), method="replace_selection", ms=1338),
        _row("type", _el("Text editor", "Document"), ok=False, code="typing_mismatch"),
        _row("type", None, ok=False, code="out_of_scope"),  # the gateway's rule, not the app's
        _row("hotkey", None, method="posted_keys", combo="ctrl+s"),
        _row("hotkey", None, ok=False, code="verify_failed", combo="ctrl+h",
             result={"method": "posted_keys", "attempts": [{"method": "posted_keys", "ok": True}]}),
        _row("click", _el("Don't save", "Button", "SecondaryButton"), method="uia_invoke", view="w-popuphost",
             approved_by="person"),
        _row("click", _el("Delete everything", "Button"), ok=False, code="approval_denied"),
        _row("invoke", _el(r"1 C:\Users\me\pets.txt", "MenuItem", "Recent1"), method="uia_invoke"),
        _row("find", None, method="none"),
        {**_row("click", _el("OK", "Button", "1"), method="button_message"),
         "scope": {**SCOPE, "app": {"id": "microsoft-corporation.charmap", "name": "Character Map"}}},
    ]


def _learned(lease="QRn0u20nxuTH97NO", prof=None, rows=None):
    prof = prof or new_profile(AppIdentity(NOTEPAD, "Notepad", "Notepad.exe", version="11.2507.26.0"))
    stats = learn.apply(prof, lease, rows if rows is not None else session(), "11.2507.26.0")
    return prof, stats


def test_one_session_gives_the_expected_profile():
    prof, stats = _learned()
    assert stats["actions"] == 9  # the out_of_scope refusal, the find and the other app's click don't count
    add = prof.get("ctl", "addbutton")
    assert add.args == ["main", "Button"] and add.fields["aid"] == "AddButton"
    assert (add.num("seen"), add.num("ok"), add.num("ses")) == (1, 1, 1)
    assert add.text is None  # a name is kept only once a second session sees it
    tab = prof.get("ctl", "tabitem")
    assert tab.fields["name"] == "content" and "nh" not in tab.fields and tab.text is None
    editor = next(x for x in prof.of_kind("ctl") if x.args == ["main", "Document"])
    assert editor.fields["name"] == "content" and (editor.num("ok"), editor.num("fail")) == (1, 1)
    dont = prof.get("ctl", "secondarybutton")
    assert dont.args == ["w-popuphost", "Button"] and dont.num("ask") == 1
    delete = next(x for x in prof.of_kind("ctl") if x.id.startswith("n-"))
    assert delete.num("ask") == 1 and delete.num("ok") == 0 and delete.num("fail") == 0 and delete.text is None
    recent = prof.get("ctl", "recent1")
    assert recent.fields["name"] == "content" and "nh" not in recent.fields
    ways = {x.id: x for x in prof.of_kind("way")}
    assert ways["click@button.uia_invoke"].num("ok") == 2
    assert ways["click@button.button_message"].fields["why"] == "next_rung"
    assert ways["type@document.replace_selection"].fields["ms"] == "1338"
    assert ways["type@document.any"].fields["why"] == "typing_mismatch"
    hot = ways["hotkey@focus.posted_keys"]
    assert (hot.num("ok"), hot.num("fail"), hot.fields["why"]) == (1, 1, "verify_failed")
    assert [x.id for x in prof.of_kind("key")] == ["ctrl+s"]
    assert {x.id for x in prof.of_kind("view")} == {"main", "w-popuphost"}
    assert prof.app.fields["ver"] == "11.2507.26.0"


def test_none_of_the_sessions_content_is_kept():
    prof, _ = _learned()
    prof, _ = _learned("XXUG2FI3ektZ57jH", prof)
    text = prof.dumps()
    for s in CONTENT:
        assert s not in text, s
    assert "Add New Tab" in text and "Don't save" in text  # labels seen in two sessions are kept


def test_a_name_is_kept_after_two_sessions_and_dropped_when_it_changes():
    prof, _ = _learned()
    prof, _ = _learned("XXUG2FI3ektZ57jH", prof)
    add = prof.get("ctl", "addbutton")
    assert add.text == "Add New Tab" and add.num("ses") == 2 and add.num("seen") == 2
    deleted = prof.get("ctl", "delete-everything")  # a control with no automation id: renamed from n-<hash>
    assert deleted.text == "Delete everything" and deleted.num("ask") == 2
    assert not [x for x in prof.of_kind("ctl") if x.id.startswith("n-")]
    rows = copy.deepcopy(session())
    rows[0]["target"]["name"] = "Add new tab"
    prof, _ = _learned("third-lease-0000", prof, rows)
    add = prof.get("ctl", "addbutton")
    assert add.text is None and add.num("nses") == 1


def test_the_same_session_counts_once_for_names():
    prof, _ = _learned()
    prof, _ = _learned(prof=prof)  # the same lease again: still one session
    assert prof.get("ctl", "addbutton").text is None and prof.get("ctl", "addbutton").num("ses") == 1


@pytest.mark.parametrize("name, ctype, private", [
    ("Add New Tab", "Button", False), ("pets.txt", "Button", True), ("Zoom 100%", "Button", True),
    ("me@example.com", "Button", True), ("https://example.com", "Button", True), ("Save", "TabItem", True),
    ("x" * 41, "Button", True), ("Don't save", "Button", False), ("File name:", "Edit", False),
])
def test_is_private(name, ctype, private):
    assert learn.is_private(name, ctype) is private


def test_learn_lease_writes_one_revision_per_session(tmp_path):
    store = ProfileStore(tmp_path)
    out = learn.learn_lease(store, "QRn0u20nxuTH97NO", session())
    assert out[NOTEPAD]["rev"] == 1 and out["microsoft-corporation.charmap"]["rev"] == 1
    out = learn.learn_lease(store, "XXUG2FI3ektZ57jH", session())
    assert out[NOTEPAD]["rev"] == 2
    prof = store.load(NOTEPAD)
    assert prof.get("rev", "2").text.startswith("use: 9 actions")
    assert store.history(NOTEPAD) == [1]
    assert loads(store.path(NOTEPAD).read_bytes().decode()).dumps() == prof.dumps()


def test_an_unreadable_profile_is_left_alone(tmp_path):
    store = ProfileStore(tmp_path)
    store.dir.mkdir(parents=True)
    store.path(NOTEPAD).write_bytes(b"hand edited\n")
    out = learn.learn_lease(store, "QRn0u20nxuTH97NO", session())
    assert "error" in out[NOTEPAD]
    assert store.path(NOTEPAD).read_bytes() == b"hand edited\n"


def test_a_session_with_only_refusals_writes_nothing(tmp_path):
    store = ProfileStore(tmp_path)
    out = learn.learn_lease(store, "QRn0u20nxuTH97NO", [_row("type", None, ok=False, code="out_of_scope")])
    assert out == {} and store.list() == []


LOOKED = [
    {"view": "main", "type": "Button", "name": "Add New Tab", "aid": "AddButton", "accel": "", "access_key": ""},
    {"view": "main", "type": "MenuItem", "name": "Save", "aid": "SaveItem", "accel": "Ctrl+S", "access_key": "Alt, S"},
    {"view": "main", "type": "TabItem", "name": "pets.txt. Unmodified.", "aid": "", "accel": "", "access_key": ""},
    {"view": "main", "type": "TabItem", "name": "todo.txt. Modified.", "aid": "", "accel": "", "access_key": ""},
    {"view": "main", "type": "Text", "name": "Ln 1, Col 1", "aid": "", "accel": "", "access_key": ""},
    {"view": "main", "type": "Window", "name": TITLE, "aid": "", "accel": "", "access_key": ""},
    {"view": "w-32770", "type": "Button", "name": "OK", "aid": "1", "accel": "", "access_key": ""},
]


def test_a_look_adds_controls_and_keys_without_counting_uses():
    prof = new_profile(AppIdentity(NOTEPAD, "Notepad", "Notepad.exe"))
    stats = learn.look(prof, "lease-look-1", LOOKED)
    assert (stats["controls"], stats["keys"]) == (4, 1)  # Text and Window are not kept; two tabs make one
    save = prof.get("ctl", "saveitem")
    assert save.fields["src"] == "look" and save.fields["ak"] == "Alt,S" and "seen" not in save.fields
    key = prof.get("key", "ctrl+s")
    assert key.fields["of"] == "saveitem" and key.text is None and "n" not in key.fields
    assert prof.get("ctl", "tabitem").fields["name"] == "content"
    assert prof.get("ctl", "1").args == ["w-32770", "Button"]
    learn.look(prof, "lease-look-2", LOOKED)
    assert prof.get("key", "ctrl+s").text == "Save"  # the name, once a second look saw it
    learn.apply(prof, "QRn0u20nxuTH97NO", session())
    add = prof.get("ctl", "addbutton")
    assert add.fields["src"] == "look+use" and add.num("seen") == 1
    text = prof.dumps()
    for s in ["pets.txt", "todo.txt", "Ln 1", TITLE]:
        assert s not in text, s


def test_view_id():
    assert learn.view_id(True, "Notepad") == "main"
    assert learn.view_id(False, "#32770") == "w-32770"
    assert learn.view_id(False, "") == "w-popup"


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")
def test_the_gateway_learns_when_a_lease_ends(tmp_path):
    from test_gateway import FakeScope, ok_checks

    from overshoulder import settings as st
    from overshoulder.driver.windows import Rect
    from overshoulder.gateway.core import Target, gateway_for_tests

    class AppScope(FakeScope):
        def describe(self):
            return dict(SCOPE)

    for enabled in (True, False):
        home = tmp_path / str(enabled)
        g = gateway_for_tests(home, checks=ok_checks(), scope_factory=lambda spec: AppScope(),
                              settings_source=lambda e=enabled: st.Settings(input_mode="full", learn_profiles=e))
        token = g.registry.add("agent-a")
        lease = g.acquire(token, "window:16")["lease_id"]
        target = Target("element", rect=Rect(10, 10, 30, 30), name="Add New Tab", control_type="Button",
                        automation_id="AddButton", hwnd=0x10, ref="e1")
        g.run(token, lease, "invoke", lambda t, p: {"method": "uia_invoke", "attempts": [
            {"method": "uia_invoke", "ok": True}]}, target=target,
            verify=lambda t, p, r: {"ok": True, "method": r["method"]})
        g.release(token, lease)
        g.wait_learning()
        store = ProfileStore(home)
        if enabled:
            prof = store.load(NOTEPAD)
            assert prof.revision == 1 and prof.get("ctl", "addbutton").num("ok") == 1
            assert TITLE not in prof.dumps()
            assert '"profile_learned"' in (home / "actions.jsonl").read_text(encoding="utf-8")
        else:
            assert store.list() == []
