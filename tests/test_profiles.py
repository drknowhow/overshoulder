import pytest

from overshoulder import profiles as pf
from overshoulder.appid import AppIdentity
from overshoulder.osp import Item, OspError
from overshoulder.profiles import ProfileConflict, ProfileStore, new_profile

NOTEPAD = AppIdentity("Microsoft.WindowsNotepad_8wekyb3d8bbwe", "Notepad", "Notepad.exe", "Microsoft",
                      "11.2507.26.0", True)
CODE = AppIdentity("microsoft-corporation.code", "Visual Studio Code", "Code - Insiders.exe")


def test_save_bumps_the_revision_and_keeps_history(tmp_path):
    store = ProfileStore(tmp_path)
    prof = store.load_or_new(NOTEPAD)
    assert prof.revision == 0
    prof.put(Item("view", "main", ["window"]))
    store.save(prof, "first look")
    assert store.history(NOTEPAD.id) == []
    prof = store.load(NOTEPAD.id)
    assert prof.revision == 1
    assert prof.get("rev", "1").text == "first look"
    prof.put(Item("ctl", "add-tab", ["main", "Button"]))
    store.save(prof)
    assert store.load(NOTEPAD.id).revision == 2
    assert store.history(NOTEPAD.id) == [1]
    raw = store.path(NOTEPAD.id).read_bytes()
    assert b"\r" not in raw and raw.startswith(b"#osp 1 made_with=Overshoulder/")


def test_a_stale_save_is_refused(tmp_path):
    store = ProfileStore(tmp_path)
    store.save(new_profile(NOTEPAD))
    a, b = store.load(NOTEPAD.id), store.load(NOTEPAD.id)
    store.save(a)
    with pytest.raises(ProfileConflict):
        store.save(b)
    with pytest.raises(ProfileConflict):
        store.save(new_profile(NOTEPAD))


def test_only_the_last_revisions_are_kept(tmp_path, monkeypatch):
    monkeypatch.setattr(pf, "KEEP_REVS", 3)
    store = ProfileStore(tmp_path)
    for _ in range(6):
        store.save(store.load_or_new(NOTEPAD))
    prof = store.load(NOTEPAD.id)
    assert [x.id for x in prof.of_kind("rev")] == ["4", "5", "6"]
    assert store.history(NOTEPAD.id) == [3, 4, 5]


def test_list_and_find(tmp_path):
    store = ProfileStore(tmp_path)
    assert store.list() == []
    store.save(new_profile(NOTEPAD))
    store.save(new_profile(CODE))
    (store.dir / "broken.osp").write_bytes(b"not a profile\n")
    rows = store.list()
    assert [r["id"] for r in rows] == ["broken", "Microsoft.WindowsNotepad_8wekyb3d8bbwe",
                                       "microsoft-corporation.code"]
    assert "error" in rows[0]
    assert rows[1]["version"] == "11.2507.26.0" and rows[1]["rev"] == 1
    assert store.find("notepad").app_id == NOTEPAD.id
    assert store.find("Visual Studio").app_id == CODE.id
    assert store.find(CODE.id).app.fields["exe"] == "Code%20-%20Insiders.exe"
    with pytest.raises(LookupError, match="2 profiles"):
        store.find("microsoft")
    with pytest.raises(LookupError, match="0 profiles"):
        store.find("gimp")


def test_bad_ids_never_reach_the_file_system(tmp_path):
    store = ProfileStore(tmp_path)
    for bad in ("..\\x", "a/b", "", ".hidden"):
        with pytest.raises(OspError):
            store.path(bad)


def test_cli_profile_list_and_show(tmp_path, monkeypatch, capsys):
    from overshoulder.cli import main
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path))
    assert main(["profile", "list"]) == 0
    assert "no profiles yet" in capsys.readouterr().out
    ProfileStore(tmp_path).save(new_profile(NOTEPAD))
    assert main(["profile", "list"]) == 0
    assert "Microsoft.WindowsNotepad_8wekyb3d8bbwe  Notepad 11.2507.26.0  rev 1, 1 items" in capsys.readouterr().out
    assert main(["profile", "show", "notepad"]) == 0
    assert capsys.readouterr().out.startswith("#osp 1 made_with=Overshoulder/")
    assert main(["profile", "show", "gimp"]) == 2
