"""Notes agents add to a profile: quirks, shortcuts, how-tos and guide notes. They are untrusted text with
size limits, they describe the app and never the person, and a how-to counts its uses when a later
session follows its steps."""

import sys

import pytest

from overshoulder import hints, learn, notes
from overshoulder.appid import AppIdentity
from overshoulder.profiles import new_profile

NOTEPAD = "Microsoft.WindowsNotepad_8wekyb3d8bbwe"
SCOPE = {"kind": "window", "process": "Notepad.exe", "app": {"id": NOTEPAD, "name": "Notepad"}}
PDF = {"kind": "how", "text": "save as PDF",
       "steps": [["hotkey", "ctrl+p"], ["select", "Microsoft Print to PDF"], ["click", "Print"]]}


def _prof():
    return new_profile(AppIdentity(NOTEPAD, "Notepad", "Notepad.exe"))


def test_each_kind_of_note_lands_as_one_line():
    prof = _prof()
    notes.add(prof, {"kind": "quirk", "text": "Find opens a panel at the top, not a dialog"})
    notes.add(prof, {"kind": "key", "combo": "Ctrl+Shift+N", "text": "opens a new window"})
    notes.add(prof, PDF)
    notes.add(prof, {"kind": "note", "text": "Tabs can be dragged out into their own window",
                     "url": "https://support.example.com/notepad/tabs?session=abc123#drag"})
    text = prof.dumps()
    for line in ('quirk q-', 'key ctrl+shift+n src=agent "opens a new window"',
                 'how h-save-as-pdf n=1 src=agent "save as PDF" : hotkey ctrl+p ; select '
                 'Microsoft%20Print%20to%20PDF ; click Print',
                 'note g-', 'url=https://support.example.com/notepad/tabs#drag'):
        assert line in text, line
    assert "abc123" not in text  # a query string can carry a token, so links lose it
    brief = hints.brief(prof)
    assert 'src=agent "Find opens a panel at the top, not a dialog"' in brief


def test_the_same_note_again_counts_rather_than_copies():
    prof = _prof()
    for _ in range(2):
        item, _ = notes.add(prof, {"kind": "quirk", "text": "Find  opens a panel"})
    assert len(prof.of_kind("quirk")) == 1 and item.num("n") == 2
    _, new = notes.add(prof, {"kind": "quirk", "text": "find opens a PANEL"})
    assert not new  # the same words, spaced or cased differently


def test_a_key_the_app_named_keeps_its_name():
    prof = _prof()
    s = learn._Session(prof, "lease", "")
    s.key("ctrl+s")
    prof.get("key", "ctrl+s").text = "Save"
    item, new = notes.add(prof, {"kind": "key", "combo": "ctrl+s", "text": "saves without asking"})
    assert not new and item.text == "Save" and item.fields["src"] == "agent+use"


@pytest.mark.parametrize("note, why", [
    ({"kind": "rule", "text": "x"}, "kind must be one of"),
    ({"kind": "quirk"}, "text is required"),
    ({"kind": "quirk", "text": "x" * 301}, "at most 300"),
    ({"kind": "quirk", "text": "it saves to C:\\Users\\dtsel\\notes"}, "address, a path or a link"),
    ({"kind": "quirk", "text": "mail it to pat@example.com"}, "address, a path or a link"),
    ({"kind": "quirk", "text": "see https://example.com"}, "a guide's link goes in url"),
    ({"kind": "key", "combo": "ctrl p!", "text": "print"}, "needs combo"),
    ({"kind": "how", "text": "print", "steps": [["press", "Print"]]}, "step action 'press'"),
    ({"kind": "how", "text": "log in", "steps": [["type", "Password", "hunter2"]]}, "typed values are not kept"),
    ({"kind": "how", "text": "open", "steps": [["click", "/home/pat/file.txt"]]}, "no address, path or link"),
    ({"kind": "how", "text": "open", "steps": []}, "steps must be a list"),
    ({"kind": "note", "text": "guide", "url": "file:///C:/guide.html"}, "public http(s) link"),
    ({"kind": "note", "text": "guide", "url": "http://localhost:8080/guide"}, "public http(s) link"),
    ({"kind": "note", "text": "guide", "url": "https://pat:secret@example.com/"}, "public http(s) link"),
    ({"kind": "note", "text": "guide"}, "needs url"),
])
def test_notes_the_profile_cannot_keep_are_refused(note, why):
    prof = _prof()
    with pytest.raises(ValueError, match=why.replace("(", r"\(").replace(")", r"\)")):
        notes.add(prof, note)
    assert prof.items == []


def test_a_profile_keeps_only_so_many_notes(monkeypatch):
    monkeypatch.setattr(notes, "MAX_NOTES", 3)
    prof = _prof()
    for i in range(3):
        notes.add(prof, {"kind": "quirk", "text": f"quirk number {i}"})
    with pytest.raises(ValueError, match="keeps 3 notes"):
        notes.add(prof, {"kind": "quirk", "text": "one more"})
    notes.add(prof, {"kind": "quirk", "text": "quirk number 0"})  # counting one it has is fine


def _row(action, name=None, combo=None, ok=True, app=NOTEPAD):
    return {"event": "action", "action": action, "ok": ok, "scope": {**SCOPE, "app": {"id": app}},
            "target": {"kind": "element", "name": name, "control_type": "Button"} if name else None,
            "params": {"combo": combo}, "verify": {"method": "uia_invoke"}}


def test_a_how_to_counts_a_session_that_follows_its_steps():
    prof = _prof()
    notes.add(prof, PDF)
    rows = [_row("hotkey", combo="Ctrl+P"), _row("click", "Layout"), _row("select", "Microsoft Print to PDF"),
            _row("click", "Print")]
    learn.apply(prof, "s1", rows)
    assert prof.get("how", "h-save-as-pdf").num("used") == 1
    for wrong in ([rows[0], rows[3], rows[2]],  # out of order
                  [rows[0], rows[2], {**rows[3], "ok": False}],  # the last step failed
                  [_row(r["action"], (r["target"] or {}).get("name"), r["params"]["combo"], app="other.app")
                   for r in rows]):  # another app's session
        learn.apply(prof, "s2", wrong)
    assert prof.get("how", "h-save-as-pdf").num("used") == 1


# -- through the service ------------------------------------------------------------------------------

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")


@windows_only
def test_profile_add_through_the_service(tmp_path, monkeypatch):
    import json

    from test_gateway import FakeScope, ok_checks

    from overshoulder import service
    from overshoulder import settings as st
    from overshoulder.gateway.core import gateway_for_tests

    class AppScope(FakeScope):
        class binding:  # noqa: N801
            hwnd, pid = 0x10, 1
            app = AppIdentity(NOTEPAD, "Notepad", "Notepad.exe")

    cfg = {"s": st.Settings()}
    s = service.Service(gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda spec: AppScope(),
                                          settings_source=lambda: cfg["s"]), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("agent")
    lease = s.acquire(token, "window:16")["lease_id"]
    secret = "Find opens a panel at the top"
    out = s.profile(token, lease, "add", {"kind": "quirk", "text": secret})
    assert out["added"]["kind"] == "quirk" and out["added"]["new"] and out["rev"] == 1
    assert secret in s.profile(token, lease, "get")["profile"]
    log = s.gw.log.path.read_text(encoding="utf-8")
    assert secret not in log and json.loads(log.splitlines()[-2])["params"] == {"op": "add", "kind": "quirk"}
    with pytest.raises(ValueError, match="needs item"):
        s.profile(token, lease, "add")
    monkeypatch.setattr(service, "ADDS_PER_LEASE", 2)
    s.profile(token, lease, "add", {"kind": "quirk", "text": "another"})
    with pytest.raises(ValueError, match="at most 2 notes"):
        s.profile(token, lease, "add", {"kind": "quirk", "text": "a third"})
    s.release(token, lease)
    cfg["s"] = st.Settings(learn_profiles=False)
    lease = s.acquire(token, "window:16")["lease_id"]
    with pytest.raises(Exception, match="turned profile learning off"):
        s.profile(token, lease, "add", {"kind": "quirk", "text": "x"})
