"""After an action on a target found by its pixels (a t-ref, a v-ref, an x/y point), the result says how
much changed in the target's own area, which a whole-window share hides. expect={"text_appears": ...}
checks with OCR that the words showed up where they were not before."""

import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import ImageDraw  # noqa: E402
from test_ocr import PHRASES, svc, words  # noqa: E402, F401

from overshoulder import service  # noqa: E402
from overshoulder.driver.ocr import Phrase  # noqa: E402
from overshoulder.gateway.errors import GatewayError  # noqa: E402

DRAWN = Path(__file__).with_name("drawnapp.ps1")
MARK = (199, 99)  # a pixel the fake press paints black, so the fake OCR can tell after from before
SAVED = Phrase(tuple(words(("Saved", 120, 60, 50))))


class Done:
    def to_dict(self):
        return {"method": "posted_click", "background": True}


@pytest.fixture
def painting(svc, monkeypatch):  # noqa: F811
    """The svc fixture, with a press that paints what `paint` says on the scope's pixels."""
    make, pixels, pressed = svc
    paint = {"rects": [(10, 10, 50, 26)], "fill": "black"}

    class Ladder:
        def __init__(self, scope_, p, method):
            pass

        def press(self, el, t, button="left", count=1):
            pressed.append(t.point())
            d = ImageDraw.Draw(pixels["img"])
            for r in paint["rects"]:
                d.rectangle(r, fill=paint["fill"])
            d.point(MARK, fill="black")
            return Done()

        def drag(self, src, dst):
            pressed.append((src.point(), dst.point()))
            ImageDraw.Draw(pixels["img"]).rectangle((150, 60, 199, 99), fill="black")
            return Done()

    monkeypatch.setattr(service, "Ladder", Ladder)
    return make, pixels, pressed, paint


def _verdict(out):
    return out["verify"] if "verify" in out else out


def test_a_click_that_repaints_its_button_reports_the_change_there(painting):
    make, _, _, _ = painting
    s, token, lease = make()
    ref = s.find(token, lease, text="Save")[0]["ref"]  # box (10, 10, 50, 26) in the scope, at (100, 50)
    v = _verdict(s.act(token, lease, "click", ref=ref))
    assert v["area"]["rect"] == [102, 52, 158, 84]  # the box grown by half its height on each side
    assert v["area"]["changed"] and v["area"]["pixels_changed"] > 0.3


def test_a_change_elsewhere_is_not_a_change_in_the_area(painting):
    make, _, _, paint = painting
    s, token, lease = make()
    paint["rects"] = [(120, 60, 190, 90)]  # only the status line repaints
    ref = s.find(token, lease, text="Save")[0]["ref"]
    v = _verdict(s.act(token, lease, "click", ref=ref))
    assert v["changed"] and not v["area"]["changed"]
    with pytest.raises(GatewayError, match="nothing changed around the target"):
        s.act(token, lease, "click", ref=s.find(token, lease, text="Save")[0]["ref"], expect={"area_changed": True})


def test_an_xy_click_is_judged_around_its_point(painting):
    make, _, _, _ = painting
    s, token, lease = make()
    v = _verdict(s.act(token, lease, "click", x=130, y=68))
    assert v["area"]["rect"] == [100, 50, 190, 98]  # 120 x 60 around the point, cut to the scope
    assert v["area"]["changed"]


def test_a_drag_reports_the_area_it_dropped_on(painting):
    make, _, _, _ = painting
    s, token, lease = make()
    ref = s.find(token, lease, text="Save")[0]["ref"]
    v = _verdict(s.act(token, lease, "drag", ref=ref, to_x=270, to_y=130))
    assert not v["area"]["changed"] and v["drop_area"]["changed"]


@pytest.fixture
def reading(painting, monkeypatch):
    """OCR that reads "Saved" in the status line once the press has painted MARK."""
    shown_before = {"saved": False}

    def scan(img):
        after = img.size == (200, 100) and img.getpixel(MARK) == (0, 0, 0)
        return [PHRASES[0] + ([SAVED] if after or shown_before["saved"] else [])]

    monkeypatch.setattr(service.ocr, "scan", scan)
    return (*painting, shown_before)


def test_text_that_appears_meets_the_expectation(reading):
    make, _, _, paint, _ = reading
    s, token, lease = make()
    paint["rects"] = []
    v = _verdict(s.act(token, lease, "click", x=130, y=68, expect={"text_appears": "saved"}))
    assert v["expect"] == "met" and v["text_appeared"] == [{"text": "Saved", "rect": [220, 110, 270, 126]}]


def test_text_that_was_there_already_did_not_appear(reading):
    make, _, _, _, shown_before = reading
    s, token, lease = make()
    shown_before["saved"] = True
    with pytest.raises(GatewayError, match="on screen already") as e:
        s.act(token, lease, "click", x=130, y=68, expect={"text_appears": "Saved"})
    assert e.value.code == "verify_failed"


def test_text_that_never_shows_fails(reading):
    make, _, _, _, _ = reading
    s, token, lease = make()
    with pytest.raises(GatewayError, match="'Done' is not on screen after the action"):
        s.act(token, lease, "click", x=130, y=68, expect={"text_appears": "Done"})


def test_with_ocr_off_text_appears_cannot_pass(reading):
    from overshoulder import settings as st

    make, _, _, _, _ = reading
    s, token, lease = make(settings_source=lambda: st.Settings(input_mode="full", ocr_enabled=False))
    with pytest.raises(GatewayError, match="can't be checked: reading text on screen is turned off"):
        s.act(token, lease, "click", x=130, y=68, expect={"text_appears": "Saved"})


# -- live, on the window of painted buttons -------------------------------------------------------------

@pytest.mark.desktop
def test_a_click_on_a_painted_button_reports_its_own_change_and_the_new_text(tmp_path):
    from test_desktop_background import _wait_window
    from test_desktop_drag import _launch

    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("region-test")
    out = tmp_path / "drawn.json"
    proc = _launch(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DRAWN), str(out)])
    try:
        hwnd = _wait_window(r"^Overshoulder drawn test$")
        end = time.time() + 20
        while time.time() < end and not out.exists():
            time.sleep(0.1)
        time.sleep(0.5)
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
        [save] = s.find(token, lease, text="Save")
        v = _verdict(s.act(token, lease, "click", ref=save["ref"], method="background",
                           expect={"text_appears": "Clicked Save", "area_changed": True}))
        assert v["area"]["changed"], v  # the button turned blue
        assert v["expect"] == "met" and len(v["text_appeared"]) == 1
        assert json.loads(out.read_text(encoding="utf-8-sig"))["clicked"] == ["Save"]
        [open_] = s.find(token, lease, text="Open")
        x, y = (open_["rect"][0] + open_["rect"][2]) // 2, (open_["rect"][1] + open_["rect"][3]) // 2
        v = _verdict(s.act(token, lease, "click", x=x, y=y, method="background"))
        assert v["area"]["changed"], v
        s.release(token, lease)
    finally:
        proc.kill()
