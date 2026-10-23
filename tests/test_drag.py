"""Drag: the input driver's path and release, the gateway's checks on the drop point, the ladder's
rungs, and how a recorded drag is drawn."""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image  # noqa: E402
from test_actions import POINT, FakeScope, env  # noqa: E402, F401
from test_gateway import FakeScope as GwScope  # noqa: E402
from test_gateway import gw, ok_checks, rows  # noqa: E402, F401
from test_limits import make  # noqa: E402

from overshoulder import actions as A  # noqa: E402
from overshoulder import render as R  # noqa: E402
from overshoulder import scope as sc  # noqa: E402
from overshoulder.driver import background as bg  # noqa: E402
from overshoulder.driver import input as inp  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway import errors as E  # noqa: E402
from overshoulder.gateway import policy  # noqa: E402
from overshoulder.gateway.core import Target, gateway_for_tests  # noqa: E402

VIRTUAL = (0, 0, 1000, 1000)


def noop(t, p):
    return {}


# -- the real-input driver -------------------------------------------------------------------------

def _events(sent):
    """(x, y, down, up) per event, in screen pixels."""
    out = []
    for batch in sent:
        for e in batch:
            mi = e.mi
            x = round(mi.dx * (VIRTUAL[2] - 1) / 65535)
            y = round(mi.dy * (VIRTUAL[3] - 1) / 65535)
            out.append((x, y, bool(mi.dwFlags & inp.MOUSEEVENTF_LEFTDOWN), bool(mi.dwFlags & inp.MOUSEEVENTF_LEFTUP)))
    return out


def test_drag_path_ends_exactly_on_the_drop_point():
    path = inp.drag_path(0, 0, 100, 50, steps=4)
    assert path == [(25, 12), (50, 25), (75, 38), (100, 50)]
    assert inp.drag_path(5, 5, 5, 5, steps=0) == [(5, 5)]


def test_drag_presses_at_the_start_and_lets_go_at_the_drop_point(monkeypatch):
    monkeypatch.setattr(inp.time, "sleep", lambda s: None)
    sent = []
    inp.drag(100, 100, 300, 200, steps=4, virtual=VIRTUAL, sender=lambda b: sent.append(b) or len(b))
    ev = _events(sent)
    downs = [e for e in ev if e[2]]
    ups = [e for e in ev if e[3]]
    assert len(downs) == 1 and len(ups) == 1
    assert abs(downs[0][0] - 100) <= 1 and abs(downs[0][1] - 100) <= 1
    assert abs(ups[0][0] - 300) <= 1 and abs(ups[0][1] - 200) <= 1
    assert ev.index(downs[0]) < ev.index(ups[0]) == len(ev) - 1
    assert len(ev) == 2 + 4 + 1  # move, press, four steps, release


def test_an_aborted_drag_lets_go_back_at_the_start(monkeypatch):
    monkeypatch.setattr(inp.time, "sleep", lambda s: None)
    sent, calls = [], []

    def abort():
        calls.append(1)
        if len(calls) == 3:
            raise E.LeaseRevoked("revoked")

    with pytest.raises(E.LeaseRevoked):
        inp.drag(100, 100, 300, 200, abort=abort, steps=8, virtual=VIRTUAL,
                 sender=lambda b: sent.append(b) or len(b))
    ev = _events(sent)
    ups = [e for e in ev if e[3]]
    assert len(ups) == 1 and ev[-1] == ups[0]
    assert abs(ups[0][0] - 100) <= 1 and abs(ups[0][1] - 100) <= 1  # put back, not dropped midway


# -- policy ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["Recycle Bin", "Trash", "Delete", "Deleted Items"])
def test_dropping_onto_something_that_discards_needs_approval(name):
    assert policy.approval_reason("drag", name) == f"drop onto {name!r}"


@pytest.mark.parametrize("name", ["Documents", "bin", "Inbox", ""])
def test_ordinary_drop_targets_need_no_approval(name):
    assert policy.approval_reason("drag", name) is None


# -- the gateway -----------------------------------------------------------------------------------

def test_a_drop_point_outside_the_scope_is_refused_and_not_performed(gw):  # noqa: F811
    g, token, lease_id, _ = gw
    calls = []
    with pytest.raises(sc.OutOfScope):
        g.run(token, lease_id, "drag", lambda t, p: calls.append(1), target=Target("point", x=10, y=10),
              to_target=Target("point", x=150, y=10))
    assert calls == []
    assert rows(g)[-1]["to"]["x"] == 150


def test_the_drop_point_reaches_perform_and_the_log(gw):  # noqa: F811
    g, token, lease_id, _ = gw
    seen = {}
    out = g.run(token, lease_id, "drag", lambda t, p: seen.update(t=t, to=p["to_target"]) or "dragged",
                target=Target("point", x=10, y=10), to_target=Target("point", x=80, y=90))
    assert out["ok"] and seen["to"].point() == (80, 90)
    last = rows(g)[-1]
    assert last["ok"] and last["to"] == {**last["to"], "kind": "point", "x": 80, "y": 90}
    assert g.pointer["x"] == 80 and g.pointer["y"] == 90  # the agent's pointer ends at the drop


def test_a_drop_onto_the_recycle_bin_asks_with_the_drop_targets_name(gw):  # noqa: F811
    g, token, lease_id, _ = gw
    file = Target("element", rect=Rect(10, 10, 30, 30), name="report.txt")
    binned = Target("element", rect=Rect(60, 60, 90, 90), name="Recycle Bin")
    with pytest.raises(E.ApprovalDenied):
        g.run(token, lease_id, "drag", noop, target=file, to_target=binned)
    seen = []
    g.approver = policy.CallbackApprover(lambda req: seen.append(req) or True)
    assert g.run(token, lease_id, "drag", noop, target=file, to_target=binned)["ok"]
    assert seen[0]["reason"] == "drop onto 'Recycle Bin'" and seen[0]["target"]["name"] == "Recycle Bin"


def test_a_drop_point_in_a_denied_program_is_refused(tmp_path):
    g, token, lease = make(tmp_path, limits={"deny_processes": ["chrome"]})
    with pytest.raises(E.Narrowed) as ex:
        g.run(token, lease["lease_id"], "drag", noop, target=Target("point", x=10, y=10),
              to_target=Target("point", x=60, y=10))  # x >= 50 is chrome's window
    assert ex.value.limit == "deny_processes"


def test_read_only_refuses_a_drag(tmp_path):
    g, token, lease = make(tmp_path, limits={"read_only": True})
    with pytest.raises(E.Narrowed):
        g.run(token, lease["lease_id"], "drag", noop, target=Target("point", x=10, y=10),
              to_target=Target("point", x=20, y=20))


def test_real_input_checks_the_window_under_both_ends(tmp_path):
    hits = []
    checks = ok_checks(window_at_point=lambda s, x, y: hits.append((x, y)) or 0x10)
    scope = GwScope()
    g = gateway_for_tests(tmp_path, checks=checks, scope_factory=lambda spec: scope)
    token = g.registry.add("agent-a")
    lease_id = g.acquire(token, "window:16")["lease_id"]

    def perform(t, p):
        with p["borrow"]() as b:
            b.check()

    g.run(token, lease_id, "drag", perform, target=Target("point", x=10, y=10),
          to_target=Target("point", x=70, y=80))
    assert (10, 10) in hits and (70, 80) in hits


# -- the ladder ------------------------------------------------------------------------------------

DROP = Target("point", x=300, y=400)


def test_a_background_drag_is_posted_and_never_uses_real_input(env, monkeypatch):  # noqa: F811
    calls, p = env
    posted = []
    monkeypatch.setattr(A.bg, "post_drag", lambda top, *a, **k: posted.append(a) or 0x30)
    monkeypatch.setattr(A.inp, "drag", calls.hook("real_drag"))
    out = A.Ladder(FakeScope(), p, "background").drag(POINT, DROP).to_dict()
    assert out["method"] == "posted_drag" and out["background"]
    assert posted == [(50, 60, 300, 400)]
    assert calls.count("background_target") == 2  # both ends' windows were checked
    assert "real_input" not in calls and "real_drag" not in calls


def test_a_list_view_drag_needs_real_input_and_gets_it_only_when_allowed(env, monkeypatch):  # noqa: F811
    calls, p = env

    def unsupported(*a, **k):
        raise bg.BackgroundUnsupported("SysListView32 starts drags from the real mouse")

    monkeypatch.setattr(A.bg, "post_drag", unsupported)
    monkeypatch.setattr(A.inp, "drag", calls.hook("real_drag"))
    with pytest.raises(A.NeedsRealInput):
        A.Ladder(FakeScope(), p, "background").drag(POINT, DROP)
    assert "real_drag" not in calls
    out = A.Ladder(FakeScope(), p, "auto").drag(POINT, DROP).to_dict()
    assert out["method"] == "real_drag" and not out["background"]
    assert calls[-4:] == ["borrow", "real_input", "real_drag", "borrow_end"]


def test_a_drop_into_another_window_skips_the_background_rung(env, monkeypatch):  # noqa: F811
    calls, p = env
    monkeypatch.setattr(A.Ladder, "top_at", lambda self, x, y: 0x10 if x < 100 else 0x40)
    monkeypatch.setattr(A.bg, "post_drag", calls.hook("posted_drag"))
    monkeypatch.setattr(A.inp, "drag", calls.hook("real_drag"))
    with pytest.raises(A.NeedsRealInput):
        A.Ladder(FakeScope(), p, "background").drag(POINT, DROP)
    out = A.Ladder(FakeScope(), p, "auto").drag(POINT, DROP).to_dict()
    assert out["method"] == "real_drag" and "posted_drag" not in calls


# -- the service -----------------------------------------------------------------------------------

def test_a_drag_without_a_drop_point_is_a_value_error(tmp_path):
    from overshoulder.service import Service
    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("t")
    with pytest.raises(ValueError, match="drop point"):
        s.act(token, "no-lease", "drag", x=10, y=10)
    with pytest.raises(ValueError, match="drop point"):
        s.act(token, "no-lease", "drag", to_x=10, to_y=10)


# -- rendering -------------------------------------------------------------------------------------

def _row(action, t, **kw):
    return {"event": "action", "action": action, "ok": True, "t_act": t, "ts": t + 0.4, "ms": 400, **kw}


def test_drag_captions_name_what_moved_and_where():
    file = {"kind": "element", "name": "report.txt", "rect": [0, 0, 20, 20]}
    folder = {"kind": "element", "name": "Archive", "rect": [100, 100, 140, 120]}
    point = {"kind": "point", "x": 5, "y": 5}
    cases = [(file, folder, 'Drag "report.txt" to "Archive"'), (point, folder, 'Drag onto "Archive"'),
             (file, {"kind": "point", "x": 9, "y": 9}, 'Drag "report.txt" along the arrow'),
             (point, {"kind": "point", "x": 9, "y": 9}, "Drag along the arrow")]
    for target, to, caption in cases:
        assert R.build_steps([_row("drag", 1.0, target=target, to=to)], 0.0)[0].caption == caption


def test_a_drag_step_starts_at_its_target_and_drops_at_the_centre_of_its_drop_target():
    s = R.build_steps([_row("drag", 1.0, target={"kind": "point", "x": 10, "y": 20},
                            to={"kind": "element", "name": "Archive", "rect": [100, 100, 140, 120]})], 0.0)[0]
    assert s.point == (10, 20) and s.to == (120, 110)


def test_the_pointer_is_carried_from_the_start_to_the_drop_point():
    steps = R.build_steps([_row("drag", 2.0, target={"kind": "point", "x": 0, "y": 0},
                                to={"kind": "point", "x": 200, "y": 100})], 0.0)
    assert R.pointer_at(2.0, steps, (0, 0)) == (0, 0)
    assert R.pointer_at(2.0 + R.DRAG_S / 2, steps, (0, 0)) == (100, 50)
    assert R.pointer_at(2.0 + R.DRAG_S, steps, (0, 0)) == (200, 100)
    assert R.pointer_at(9.0, steps, (0, 0)) == (200, 100)  # and rests at the drop point
    assert R.still_time(steps[0]) >= 2.0 + R.DRAG_S or R.still_time(steps[0]) == steps[0].end - 0.1


def test_annotate_draws_the_drag_arrow_between_start_and_drop():
    img = Image.new("RGB", (400, 300), (255, 255, 255))
    steps = R.build_steps([_row("drag", 2.0, target={"kind": "point", "x": 50, "y": 150},
                                to={"kind": "point", "x": 350, "y": 150})], 0.0)
    fonts = {"badge": R._font(18), "caption": R._font(26)}
    out = R.annotate(img, 2.1, (0, 0), None, steps, fonts)
    line = [out.getpixel((x, 150)) for x in range(80, 300, 4)]
    blue = [p for p in line if p[2] > 150 and p[0] < 120]
    assert len(blue) > len(line) // 3  # a dashed accent line along the drag
    assert out.getpixel((340, 150)) != (255, 255, 255)  # the arrowhead at the drop


# -- the posted-message driver ----------------------------------------------------------------------

class FakeUser32:
    """Records each posted mouse message with whether the left button read as down when it was posted."""

    def __init__(self):
        self.down, self.posted, self.attached = False, [], []

    def AttachThreadInput(self, me, them, on):  # noqa: N802
        self.attached.append(on)
        return True

    def GetKeyboardState(self, state):  # noqa: N802
        return True

    def SetKeyboardState(self, state):  # noqa: N802
        self.down = bool(state[bg.VK_LBUTTON] & 0x80)
        return True

    def PostMessageW(self, hwnd, msg, wp, lp):  # noqa: N802
        self.posted.append((msg, lp & 0xFFFF, lp >> 16, self.down))
        return True


@pytest.fixture
def posted(monkeypatch):
    fake = FakeUser32()
    monkeypatch.setattr(bg, "user32", fake)
    monkeypatch.setattr(bg, "deepest_child_at", lambda top, x, y: 0x30)
    monkeypatch.setattr(bg.w, "class_name", lambda h: "WindowsForms10.Window.8.app.0")
    monkeypatch.setattr(bg, "thread_of", lambda h: 7)
    monkeypatch.setattr(bg, "_ensure_queue", lambda: None)
    monkeypatch.setattr(bg, "_client_lparam", lambda h, x, y: (y << 16) | x)
    monkeypatch.setattr(bg.time, "sleep", lambda s: None)
    return fake


def test_posted_drag_releases_only_after_the_button_state_is_up(posted):
    bg.post_drag(0x10, 10, 20, 110, 70, steps=4)
    msgs = posted.posted
    assert msgs[0][0] == bg.WM_MOUSEMOVE and msgs[1][0] == bg.WM_LBUTTONDOWN
    assert all(m[3] for m in msgs[:-1])  # the press and every move see the button down ...
    assert msgs[-1] == (bg.WM_LBUTTONUP, 110, 70, False)  # ... and the release sees it up, at the drop
    assert posted.attached == [True, False] and not posted.down


def test_an_aborted_posted_drag_is_let_go_where_it_started(posted):
    calls = []

    def abort():
        calls.append(1)
        if len(calls) == 2:
            raise E.LeaseRevoked("revoked")

    with pytest.raises(E.LeaseRevoked):
        bg.post_drag(0x10, 10, 20, 110, 70, abort=abort, steps=4)
    assert posted.posted[-1] == (bg.WM_LBUTTONUP, 10, 20, False)
    assert posted.attached == [True, False]


def test_list_views_refuse_a_posted_drag(posted, monkeypatch):
    monkeypatch.setattr(bg.w, "class_name", lambda h: "SysListView32")
    with pytest.raises(bg.BackgroundUnsupported):
        bg.post_drag(0x10, 10, 20, 110, 70)
    assert posted.posted == [] and posted.attached == []


def test_tk_refuses_a_posted_drag_because_it_follows_the_real_cursor(posted, monkeypatch):
    monkeypatch.setattr(bg.w, "class_name", lambda h: "TkChild")
    with pytest.raises(bg.BackgroundUnsupported, match="real mouse"):
        bg.post_drag(0x10, 10, 20, 110, 70)
    assert posted.posted == []
