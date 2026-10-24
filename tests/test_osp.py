import hashlib

import pytest

from overshoulder import osp
from overshoulder.osp import Item, OspError, Profile, dec, enc, loads, parse_line


def _sample() -> Profile:
    p = Profile(Item("app", "Microsoft.WindowsNotepad_8wekyb3d8bbwe", fields={"exe": "Notepad.exe", "rev": "3"},
                     text="Notepad"), made_with="Overshoulder/0.1.0")
    p.put(Item("how", "close-unsaved", fields={"ok": "2"}, text='Close a tab "without" saving \\ fast',
               steps=[["click", "close-tab"], ["click", "dont-save"]]))
    p.put(Item("ctl", "add-tab", ["main", "Button"], {"aid": "AddButton", "seen": "5"}, "Add New Tab"))
    p.put(Item("view", "main", ["window"]))
    p.put(Item("way", "click", ["background"], {"ok": "9", "fail": "1"}))
    return p


def _with_hash(body: str) -> str:
    return body + f"end sha256={hashlib.sha256(body.encode()).hexdigest()}\n"


def test_round_trip_and_kind_order():
    text = _sample().dumps()
    lines = text.splitlines()
    assert lines[0] == "#osp 1 made_with=Overshoulder/0.1.0"
    assert [x.split()[0] for x in lines[1:-1]] == ["app", "view", "ctl", "way", "how"]
    back = loads(text)
    assert back.dumps() == text
    how = back.get("how", "close-unsaved")
    assert how.text == 'Close a tab "without" saving \\ fast'
    assert how.steps == [["click", "close-tab"], ["click", "dont-save"]]
    assert back.get("ctl", "add-tab").args == ["main", "Button"]
    assert back.revision == 3 and back.name == "Notepad"


def test_crlf_files_read_the_same():
    text = _sample().dumps()
    assert loads(text.replace("\n", "\r\n")).dumps() == text


def test_hash_mismatch_is_refused():
    text = _sample().dumps().replace("seen=5", "seen=6")
    with pytest.raises(OspError, match="hash"):
        loads(text)


@pytest.mark.parametrize("line, why", [
    ("bogus x", "unknown kind"),
    ("ctl only-one main", "positional"),
    ('note n "a" "b"', "text must come last"),
    ("note n a=1 a=2", "twice"),
    ("note n a=1 pos", "after a field"),
    ("note n BAD=1", "bad field"),
    ("way click background \"x\"", "takes no text"),
    ("note n : click x", "takes no steps"),
    ("how h : click a ; ; click b", "empty step"),
    ("note -bad", "bad id"),
    ('note n "unterminated', "can't read"),
])
def test_bad_lines(line, why):
    with pytest.raises(OspError, match=why):
        parse_line(line)


def test_text_with_control_characters_is_refused():
    with pytest.raises(OspError, match="control"):
        Item("note", "n", text="a\tb").line()


def test_unknown_fields_are_kept():
    item = parse_line("ctl x main Button future_field=1")
    assert item.fields == {"future_field": "1"}


def test_whole_file_rules():
    good = _sample().body()
    with pytest.raises(OspError, match="header"):
        loads(_with_hash("#nope\n" + good.split("\n", 1)[1]))
    with pytest.raises(OspError, match="version"):
        loads(_with_hash(good.replace("#osp 1", "#osp 2", 1)))
    with pytest.raises(OspError, match="last line"):
        loads(good)
    with pytest.raises(OspError, match="appears twice"):
        loads(_with_hash(good + "view main window\n"))
    head, app, rest = good.split("\n", 2)
    with pytest.raises(OspError, match="line 2"):
        loads(_with_hash(f"{head}\n{rest}{app}\n"))
    with pytest.raises(OspError, match="line 3: unknown kind"):
        loads(_with_hash(f"{head}\n{app}\nzzz q\n"))


def test_caps(monkeypatch):
    with pytest.raises(OspError, match="longer"):
        parse_line("note n " + "a" * osp.MAX_LINE)
    with pytest.raises(OspError, match="text"):
        Item("note", "n", text="x" * (osp.MAX_TEXT + 1)).check()
    with pytest.raises(OspError, match="steps"):
        Item("how", "h", steps=[["click", "a"]] * (osp.MAX_STEPS + 1)).check()
    p = _sample()
    monkeypatch.setattr(osp, "MAX_ITEMS", 3)
    with pytest.raises(OspError, match="at most"):
        p.put(Item("note", "one-more"))
    monkeypatch.setattr(osp, "MAX_BYTES", 100)
    with pytest.raises(OspError, match="larger"):
        p.dumps()


def test_put_replaces_in_place():
    p = _sample()
    p.put(Item("ctl", "add-tab", ["main", "Button"], {"seen": "6"}))
    assert [x.id for x in p.of_kind("ctl")] == ["add-tab"]
    assert p.get("ctl", "add-tab").fields == {"seen": "6"}
    assert p.remove("ctl", "add-tab") and not p.remove("ctl", "add-tab")


def test_enc_dec():
    for text in ("Code - Insiders.exe", 'a"b', "100%", "Блокнот"):
        v = enc(text)
        assert osp.VALUE_RE.match(v) and dec(v) == text
    assert enc("Notepad.exe") == "Notepad.exe"


def test_files_are_written_with_lf_only():
    assert "\r" not in _sample().dumps()
