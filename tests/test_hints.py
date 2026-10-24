"""Using a profile: the brief take_scope returns, profile(op="get"), and the ladder's order. A profile only
informs: it reorders methods an action may already use and never adds or skips one."""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_actions import FakeScope, env  # noqa: E402, F401
from test_learn import CONTENT, NOTEPAD, SCOPE, _learned  # noqa: E402

from overshoulder import actions as A  # noqa: E402
from overshoulder import hints  # noqa: E402
from overshoulder.appid import AppIdentity  # noqa: E402
from overshoulder.osp import Item  # noqa: E402
from overshoulder.profiles import new_profile  # noqa: E402
from overshoulder.service import NO_PROFILE_NOTE, UNTRUSTED_NOTE  # noqa: E402

VERSION = "11.2507.26.0"


def _prof():
    return new_profile(AppIdentity(NOTEPAD, "Notepad", "Notepad.exe", version=VERSION))


def _way(prof, wid, ok, fail):
    prof.put(Item("way", wid, fields={"ok": str(ok), "fail": str(fail)}))


# -- the brief ----------------------------------------------------------------------------------------

def test_the_brief_drops_bookkeeping_and_puts_the_most_used_first():
    prof, _ = _learned()
    prof, _ = _learned("XXUG2FI3ektZ57jH", prof)
    text = hints.brief(prof, VERSION)
    lines = text.splitlines()
    assert lines[0] == f'app {NOTEPAD} rev=0 ver={VERSION} "Notepad"'
    for field in ("last=", "nh=", "nses=", "ses=", "src=", "at=", "exe="):
        assert f" {field}" not in text, field
    assert " ver=" not in "\n".join(lines[1:])  # every item was seen on the running version
    for s in CONTENT:
        assert s not in text, s
    shown = [ln.split()[1] for ln in lines if ln.startswith("way ")]
    used = {w.id: w.num("ok") + w.num("fail") for w in prof.of_kind("way")}
    assert sorted(shown) == sorted(used) and [used[x] for x in shown] == sorted(used.values(), reverse=True)
    kinds = [ln.split()[0] for ln in lines[1:]]
    assert kinds == sorted(kinds, key=["view", "way", "ctl", "key"].index)
    assert 'ctl addbutton main Button aid=AddButton' in text and '"Add New Tab"' in text


def test_items_not_yet_seen_on_a_new_version_keep_their_ver(tmp_path):
    from overshoulder.profiles import ProfileStore

    store = ProfileStore(tmp_path)
    prof, _ = _learned()
    store.save(prof, "use: 9 actions")
    prof = store.load(NOTEPAD)
    text = hints.brief(prof, "11.2600.1.0")
    lines = text.splitlines()
    assert lines[0] == f'app {NOTEPAD} rev=1 ver=11.2600.1.0 "Notepad"'
    assert lines[1] == "# learned on version 11.2507.26.0; this is 11.2600.1.0: items with ver= are not yet seen on it"
    assert "ctl addbutton main Button aid=AddButton ver=11.2507.26.0" in text
    assert not any(ln.startswith("rev ") for ln in lines)  # the revision history stays in the file


def test_a_long_profile_is_cut_with_a_pointer_to_the_rest():
    prof = _prof()
    for i in range(300):
        prof.put(Item("ctl", f"c{i}", ["main", "Button"], {"seen": str(i)}))
    lines = hints.brief(prof, VERSION, limit=50).splitlines()
    assert len(lines) == 51 and lines[-1] == '# 251 more items: profile(op="get") has them all'
    assert lines[1] == "ctl c299 main Button seen=299"
    assert len(hints.brief(prof, VERSION, limit=None).splitlines()) == 301


# -- the order of methods ---------------------------------------------------------------------------

def test_a_method_that_keeps_failing_here_is_tried_last():
    prof = _prof()
    rungs = ["replace_selection", "posted_chars"]
    _way(prof, "type@edit.replace_selection", 0, 1)
    assert hints.order(prof, "type", "edit", rungs) == rungs  # once is not enough
    _way(prof, "type@edit.replace_selection", 0, 2)
    assert hints.order(prof, "type", "edit", rungs) == ["posted_chars", "replace_selection"]
    assert hints.order(prof, "type", "document", rungs) == rungs  # another kind of target
    assert hints.order(prof, "set_value", "edit", rungs) == rungs  # another action
    _way(prof, "type@edit.replace_selection", 1, 5)
    assert hints.order(prof, "type", "edit", rungs) == rungs  # it has worked here


def _typing(monkeypatch, calls, reads):
    monkeypatch.setattr(A.bg, "is_edit_class", lambda h: True)
    monkeypatch.setattr(A.bg, "replace_selection", calls.hook("replace_selection"))
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    it = iter(reads)
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: next(it)))


def test_typing_goes_straight_to_the_method_that_worked(env, monkeypatch):  # noqa: F811
    calls, p = env
    _typing(monkeypatch, calls, ["x = 1", "x = 1{y}"])
    prof = _prof()
    _way(prof, "type@focus.replace_selection", 0, 2)
    _way(prof, "type@focus.posted_chars", 2, 0)
    ladder = A.Ladder(FakeScope(), p, "background", order=lambda r: hints.order(prof, "type", "focus", r))
    out = ladder.type_text(None, "{y}").to_dict()
    assert out["method"] == "posted_chars" and [a["method"] for a in out["attempts"]] == ["posted_chars"]
    assert "replace_selection" not in calls


def test_a_method_tried_last_is_still_tried(env, monkeypatch):  # noqa: F811
    calls, p = env
    _typing(monkeypatch, calls, ["x", "x", "xy"])  # posted characters never arrive
    prof = _prof()
    _way(prof, "type@focus.replace_selection", 0, 2)
    ladder = A.Ladder(FakeScope(), p, "background", order=lambda r: hints.order(prof, "type", "focus", r))
    out = ladder.type_text(None, "y").to_dict()
    assert [a["method"] for a in out["attempts"]] == ["posted_chars", "replace_selection"]
    assert out["method"] == "replace_selection" and "real_input" not in calls


def test_an_order_that_is_not_a_reordering_is_ignored(env, monkeypatch):  # noqa: F811
    calls, p = env
    _typing(monkeypatch, calls, ["x", "xy"])
    for bad in (lambda r: ["real_typing"], lambda r: r[:1], lambda r: 1 / 0):
        calls.clear()
        _typing(monkeypatch, calls, ["x", "xy"])
        out = A.Ladder(FakeScope(), p, "auto", order=bad).type_text(None, "y").to_dict()
        assert out["method"] == "replace_selection" and calls == ["background_target", "replace_selection"]


def test_a_press_skips_ahead_of_a_pattern_the_app_lacks(env, monkeypatch):  # noqa: F811
    from overshoulder.driver.windows import Rect
    from overshoulder.gateway.core import Target

    calls, p = env

    def no_invoke(el):
        calls.append("uia_invoke")
        raise RuntimeError("no invoke pattern")

    monkeypatch.setattr(A.uia, "invoke", no_invoke)
    monkeypatch.setattr(A.uia, "toggle", calls.hook("uia_toggle"))
    target = Target("element", rect=Rect(0, 0, 10, 10), control_type="CheckBox", name="Word wrap")
    out = A.Ladder(FakeScope(), p, "background").press(object(), target).to_dict()
    assert calls == ["uia_invoke", "uia_toggle"] and out["method"] == "uia_toggle"
    calls.clear()
    prof = _prof()
    _way(prof, "click@checkbox.uia_invoke", 0, 2)
    order = lambda r: hints.order(prof, "click", "checkbox", r)  # noqa: E731
    out = A.Ladder(FakeScope(), p, "background", order=order).press(object(), target).to_dict()
    assert calls == ["uia_toggle"] and out["method"] == "uia_toggle"


# -- the service ---------------------------------------------------------------------------------------

@pytest.fixture
def svc(tmp_path, monkeypatch):
    from test_gateway import FakeScope as GwScope
    from test_gateway import ok_checks

    from overshoulder import service
    from overshoulder import settings as st
    from overshoulder.gateway.core import gateway_for_tests

    class AppScope(GwScope):
        class binding:  # noqa: N801
            hwnd, pid = 0x10, 1
            app = AppIdentity(NOTEPAD, "Notepad", "Notepad.exe", version=VERSION)

        def describe(self):
            return dict(SCOPE)

    settings = {"s": st.Settings(input_mode="full")}
    orders = []

    class Done:
        def to_dict(self):
            return {"method": "posted_chars", "background": True}

    class Ladder:
        def __init__(self, scope_, p, method, order=None):
            self.order = order

        def type_text(self, el, text):
            orders.append(self.order(["replace_selection", "posted_chars"]) if self.order else None)
            return Done()

    monkeypatch.setattr(service, "Ladder", Ladder)
    s = service.Service(gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda spec: AppScope(),
                                          settings_source=lambda: settings["s"]), shots_dir=tmp_path / "shots")
    return s, s.gw.registry.add("agent"), settings, orders


def test_take_scope_brings_the_brief(svc):
    s, token, settings, _ = svc
    first = s.acquire(token, "window:16")
    assert first["profile"] == {"rev": 0, "note": NO_PROFILE_NOTE}
    s.release(token, first["lease_id"])
    prof, _ = _learned()
    s.gw.profiles.save(prof, "use: 9 actions")
    out = s.acquire(token, "window:16")
    assert out["profile"]["rev"] == 1 and out["profile"]["note"] == UNTRUSTED_NOTE
    assert out["profile"]["brief"] == hints.brief(s.gw.profiles.load(NOTEPAD), VERSION)
    got = s.profile(token, out["lease_id"], "get")
    assert got["profile"] == hints.brief(s.gw.profiles.load(NOTEPAD), VERSION, limit=None)
    assert " nh=" not in got["profile"] and " last=" not in got["profile"]
    s.release(token, out["lease_id"])
    from overshoulder import settings as st
    settings["s"] = st.Settings(input_mode="full", learn_profiles=False)
    assert "profile" not in s.acquire(token, "window:16")


def test_an_unreadable_profile_gives_no_hints_and_still_takes_the_scope(svc):
    s, token, _, orders = svc
    s.gw.profiles.dir.mkdir(parents=True, exist_ok=True)
    s.gw.profiles.path(NOTEPAD).write_bytes(b"#osp 1 made_with=x\nnot a profile\n")
    out = s.acquire(token, "window:16")
    assert out["profile"] == {"rev": 0, "note": NO_PROFILE_NOTE}
    s.act(token, out["lease_id"], "type", text="hi")
    assert orders == [None]


def test_act_orders_the_ladder_from_the_profile(svc):
    s, token, settings, orders = svc
    prof = _prof()
    _way(prof, "type@focus.replace_selection", 0, 3)
    s.gw.profiles.save(prof, "seeded")
    lease = s.acquire(token, "window:16")["lease_id"]
    s.act(token, lease, "type", text="hi")
    assert orders == [["posted_chars", "replace_selection"]]
    from overshoulder import settings as st
    settings["s"] = st.Settings(input_mode="full", learn_profiles=False)
    s.act(token, lease, "type", text="hi")
    assert orders[-1] is None  # profiles turned off: the ladder's own order


# -- anchors: find by the automation id the profile knows ---------------------------------------------

def _anchor_svc(svc, monkeypatch, there=True):  # noqa: F811
    from types import SimpleNamespace

    from overshoulder import service
    from overshoulder.driver.uia import Element

    s, token, _, _ = svc
    equals = Element("e9", SimpleNamespace(name="Equals", control_type="Button", automation_id="equalButton"), 0x10, 1)
    calls = []
    state = {"there": there}
    monkeypatch.setattr(service.uia, "roots_for_window", lambda h: [0x10])
    monkeypatch.setattr(service.uia, "by_automation_id",
                        lambda cache, root, aid: calls.append(("aid", aid)) or ([equals] if state["there"] else []))
    monkeypatch.setattr(service.uia, "walk", lambda cache, root, max_nodes=0: calls.append(("walk",)) or [equals])
    monkeypatch.setattr(service.uia, "describe_all",
                        lambda els: [{"ref": e.ref, "name": e.info.name, "type": e.info.control_type} for e in els])
    prof = _prof()
    prof.put(Item("ctl", "equalbutton", ["main", "Button"], {"aid": "equalButton", "seen": "4"}, "Equals"))
    s.gw.profiles.save(prof, "seeded")
    return s, token, calls, state


def _lease(s, token, fn):
    lease = s.acquire(token, "window:16")["lease_id"]
    try:
        return fn(lease)
    finally:
        s.release(token, lease)
        s.gw.wait_learning()


def test_a_find_by_a_known_name_goes_straight_to_its_automation_id(svc, monkeypatch):
    s, token, calls, _ = _anchor_svc(svc, monkeypatch)
    hits = _lease(s, token, lambda lease: s.find(token, lease, name="Equals"))
    assert hits == [{"ref": "e9", "name": "Equals", "type": "Button", "via": "profile"}]
    assert calls == [("aid", "equalButton")]  # no walk of the window
    assert s.gw.profiles.load(NOTEPAD).get("ctl", "equalbutton").num("hit") == 1
    calls.clear()
    _lease(s, token, lambda lease: s.find(token, lease, name_re="^Equals$"))
    assert calls == [("walk",)]  # a pattern is a search, not a known control


def test_an_anchor_that_misses_three_times_is_no_longer_tried(svc, monkeypatch):
    s, token, calls, state = _anchor_svc(svc, monkeypatch, there=False)
    for n in (1, 2, 3):
        hits = _lease(s, token, lambda lease: s.find(token, lease, name="Equals"))
        assert hits == [{"ref": "e9", "name": "Equals", "type": "Button"}]  # the walk found it
        assert s.gw.profiles.load(NOTEPAD).get("ctl", "equalbutton").num("miss") == n
    calls.clear()
    state["there"] = True
    _lease(s, token, lambda lease: s.find(token, lease, auto_id="equalButton"))
    assert calls == [("walk",)]
    assert "miss=3" in hints.brief(s.gw.profiles.load(NOTEPAD), VERSION)


def test_a_hit_clears_the_misses(svc, monkeypatch):
    s, token, _, state = _anchor_svc(svc, monkeypatch, there=False)
    _lease(s, token, lambda lease: s.find(token, lease, auto_id="equalButton"))
    state["there"] = True
    _lease(s, token, lambda lease: s.find(token, lease, auto_id="equalButton"))
    ctl = s.gw.profiles.load(NOTEPAD).get("ctl", "equalbutton")
    assert "miss" not in ctl.fields and ctl.num("hit") == 1


def test_an_ambiguous_name_is_not_an_anchor(svc, monkeypatch):
    s, token, calls, _ = _anchor_svc(svc, monkeypatch)
    prof = s.gw.profiles.load(NOTEPAD)
    prof.put(Item("ctl", "equals2", ["w-dialog", "Button"], {"aid": "okEquals"}, "Equals"))
    s.gw.profiles.save(prof, "two")
    _lease(s, token, lambda lease: s.find(token, lease, name="Equals"))
    assert calls == [("walk",)]


# -- vision boxes seen before ------------------------------------------------------------------------

def test_learning_keeps_a_confirmed_box_and_its_words_only_after_two_sessions():
    from overshoulder import learn
    row = {"event": "hint", "scope": SCOPE, "hint": "paint", "view": "main", "desc": "the  gear icon",
           "box": [150, 20, 180, 50], "size": [200, 100], "dh": "00ff00ff00ff00ff"}
    prof = _prof()
    learn.apply(prof, "lease-one", [row], VERSION)
    [item] = prof.of_kind("paint")
    assert item.id == learn.paint_id(NOTEPAD, "main", "The gear icon")
    assert (item.fields["box"], item.fields["sz"], item.fields["dh"], item.text) == \
        ("150,20,180,50", "200,100", "00ff00ff00ff00ff", None)
    learn.apply(prof, "lease-two", [row], VERSION)
    assert item.text == "the gear icon"
    secret = {**row, "desc": "the attachment pets.txt"}
    for lease in ("a", "b"):
        learn.apply(prof, lease, [secret], VERSION)
    assert all(x.text != "the attachment pets.txt" for x in prof.of_kind("paint"))
    for bad in ({**row, "box": [1, 2, 3]}, {**row, "dh": "zz"}, {**row, "view": "Main Window"}, {**row, "desc": ""}):
        before = prof.dumps()
        learn.apply(prof, "c", [bad], VERSION)
        assert prof.dumps() == before
