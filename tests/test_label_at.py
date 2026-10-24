"""A click by x/y lands on whatever is drawn there. In an app that paints its own buttons, UI Automation
names nothing at the point, so the text on screen there is read with OCR and a consequential label
(Delete, Send...) needs the person's approval, as it does for a named control."""

import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_gateway import gw  # noqa: E402, F401

from overshoulder import locate  # noqa: E402
from overshoulder import settings as st  # noqa: E402
from overshoulder.driver.ocr import Phrase, Word  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway import errors as E  # noqa: E402
from overshoulder.gateway.core import Target  # noqa: E402

DRAWN = Path(__file__).with_name("drawnapp.ps1")


def phrase(text, left, top, right, bottom):
    return Phrase((Word(text, Rect(left, top, right, bottom)),))


# Three painted buttons 110 x 40 with 16 px labels, as in drawnapp.ps1.
BUTTONS = [phrase("Save", 54, 42, 96, 58), phrase("Open", 181, 42, 229, 58), phrase("Delete", 308, 42, 364, 58)]


def test_the_label_in_the_middle_of_a_button_is_found_from_its_corner():
    assert locate.nearest_label(BUTTONS, 285, 33) == "Delete"  # top-left inside the Delete button
    assert locate.nearest_label(BUTTONS, 75, 50) == "Save"


def test_the_nearest_label_wins_over_a_consequential_neighbour():
    assert locate.nearest_label(BUTTONS, 135, 50) == "Save"  # in the gap, nearer Save than Delete


def test_a_point_far_from_any_text_has_no_label():
    assert locate.nearest_label(BUTTONS, 200, 150) == ""


def test_a_point_click_on_a_drawn_delete_needs_approval(gw):  # noqa: F811
    g, token, lease_id, _ = gw
    g.checks.label_at = lambda s, x, y: "Delete"
    calls = []
    with pytest.raises(E.ApprovalDenied, match="Delete"):
        g.run(token, lease_id, "click", lambda t, p: calls.append(1), target=Target("point", x=5, y=5))
    assert calls == []


def test_a_point_click_on_a_harmless_label_goes_ahead(gw):  # noqa: F811
    g, token, lease_id, _ = gw
    g.checks.label_at = lambda s, x, y: "Save"
    assert g.run(token, lease_id, "click", lambda t, p: "done", target=Target("point", x=5, y=5))["ok"]


def test_a_drop_onto_drawn_trash_needs_approval(gw):  # noqa: F811
    g, token, lease_id, _ = gw
    g.checks.label_at = lambda s, x, y: "Trash" if (x, y) == (50, 50) else ""
    with pytest.raises(E.ApprovalDenied, match="Trash"):
        g.run(token, lease_id, "drag", lambda t, p: None, target=Target("point", x=5, y=5),
              to_target=Target("point", x=50, y=50))


def test_a_point_whose_text_cannot_be_read_is_asked_about(gw):  # noqa: F811
    g, token, lease_id, _ = gw

    def broken(s, x, y):
        raise RuntimeError("capture failed")

    g.checks.label_at = broken
    with pytest.raises(E.ApprovalDenied, match="could not be read"):
        g.run(token, lease_id, "click", lambda t, p: None, target=Target("point", x=5, y=5))


def test_keys_and_element_targets_are_not_read(gw):  # noqa: F811
    g, token, lease_id, _ = gw
    asked = []
    g.checks.label_at = lambda s, x, y: asked.append((x, y)) or "Delete"
    g.run(token, lease_id, "type", lambda t, p: None, params={"text": "x"})
    g.run(token, lease_id, "click", lambda t, p: None,
          target=Target("element", rect=Rect(0, 0, 10, 10), name="Save", hwnd=0x10))
    assert asked == []


def test_with_ocr_turned_off_point_clicks_are_not_read(gw):  # noqa: F811
    g, token, lease_id, _ = gw
    g.settings_source = lambda: st.Settings(input_mode="full", ocr_enabled=False)
    g.checks.label_at = lambda s, x, y: "Delete"
    assert g.run(token, lease_id, "click", lambda t, p: "done", target=Target("point", x=5, y=5))["ok"]


@pytest.mark.desktop
def test_an_xy_click_on_a_painted_delete_asks_and_one_on_save_goes(tmp_path):
    import ctypes
    import ctypes.wintypes as wt

    from test_desktop_background import _wait_window
    from test_desktop_drag import _launch

    from overshoulder.driver import display
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("label-test")
    out = tmp_path / "drawn.json"
    proc = _launch(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DRAWN), str(out)])
    try:
        hwnd = _wait_window(r"^Overshoulder drawn test$")
        end = time.time() + 20
        while time.time() < end and not out.exists():
            time.sleep(0.1)
        time.sleep(0.5)
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        o = wt.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(o))
        k = display.window_scale(hwnd)

        def at(cx, cy):  # a point in the window's own pixels, on screen
            return o.x + round(cx * k), o.y + round(cy * k)

        x, y = at(290, 36)  # near the Delete button's corner, off its label
        with pytest.raises(E.ApprovalDenied, match="Delete"):
            s.act(token, lease, "click", x=x, y=y, method="background")
        x, y = at(40, 60)
        assert s.act(token, lease, "click", x=x, y=y, method="background")["ok"]
        end = time.time() + 5
        while time.time() < end and "Save" not in json.loads(out.read_text(encoding="utf-8-sig"))["clicked"]:
            time.sleep(0.1)
        assert json.loads(out.read_text(encoding="utf-8-sig"))["clicked"] == ["Save"]
        s.release(token, lease)
    finally:
        proc.kill()
