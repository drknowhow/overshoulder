"""Text refs: words read off a scope's pixels with Windows OCR, for apps that paint their own controls.
UI Automation sees one window and nothing in it for those, so find(text=...) is how an agent gets a
target there, and the target goes stale when the pixels under it change."""

import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image, ImageDraw, ImageFont  # noqa: E402
from test_gateway import FakeScope, ok_checks  # noqa: E402

from overshoulder.driver import ocr  # noqa: E402
from overshoulder.driver.ocr import Phrase, Word  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway.errors import GatewayError  # noqa: E402

DRAWN = Path(__file__).with_name("drawnapp.ps1")


def words(*specs):
    return [Word(t, Rect(x, y, x + w, y + 16)) for t, x, y, w in specs]


# -- grouping and matching ------------------------------------------------------------------------

def test_buttons_on_one_ocr_line_become_separate_phrases():
    line = words(("Save", 54, 82, 42), ("Open", 181, 82, 48), ("Delete", 308, 82, 56))
    assert [p.text for p in ocr.phrases([line])] == ["Save", "Open", "Delete"]


def test_words_of_one_label_stay_together():
    line = words(("Nothing", 27, 155, 70), ("clicked", 102, 155, 60), ("yet", 167, 155, 28))
    assert [p.text for p in ocr.phrases([line])] == ["Nothing clicked yet"]


def test_find_gives_the_box_of_just_the_matching_words():
    passes = [[Phrase(tuple(words(("Nothing", 27, 155, 70), ("clicked", 102, 155, 60), ("yet", 167, 155, 28))))]]
    [hit] = ocr.find(passes, "clicked yet")
    assert hit.text == "clicked yet" and hit.box == Rect(102, 155, 195, 171)


def test_find_ignores_case_and_punctuation_around_words():
    passes = [[Phrase(tuple(words(("copy", 93, 423, 27), (":", 124, 423, 4)))),
               Phrase(tuple(words(("Copy:", 482, 427, 29))))]]
    assert [h.box.left for h in ocr.find(passes, "COPY")] == [93, 482]


def test_a_later_pass_adds_what_earlier_ones_missed_but_not_the_same_place_again():
    first = [Phrase(tuple(words(("Save", 54, 82, 42))))]
    second = [Phrase(tuple(words(("Save", 55, 83, 41)))), Phrase(tuple(words(("Delete", 308, 82, 56))))]
    assert [p.text for p in ocr.merged([first, second])] == ["Save", "Delete"]
    assert [h.box.left for h in ocr.find([first, second], "save")] == [54]


def test_unlined_paints_out_long_borders_and_keeps_short_strokes():
    img = Image.new("RGB", (200, 80), "white")
    d = ImageDraw.Draw(img)
    d.rectangle((10, 10, 190, 70), outline=(55, 65, 81), width=2)
    d.rectangle((95, 35, 99, 45), fill=(17, 24, 39))  # a letter-sized stroke
    out = ocr.unlined(img)
    assert out.getpixel((100, 10))[0] > 200 and out.getpixel((10, 40))[0] > 200
    assert out.getpixel((97, 40))[0] < 60


# -- the engine on a rendered window ---------------------------------------------------------------

def _painted_buttons() -> tuple[Image.Image, dict]:
    """Three bordered buttons, like drawnapp.ps1 at 100% scaling."""
    img = Image.new("RGB", (410, 200), "white")
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype("segoeui.ttf", 16)
    boxes = {"Save": (20, 30, 130, 70), "Open": (150, 30, 260, 70), "Delete": (280, 30, 390, 70)}
    for name, (x0, y0, x1, y1) in boxes.items():
        d.rectangle((x0, y0, x1, y1), fill=(229, 231, 235), outline=(55, 65, 81), width=2)
        d.text(((x0 + x1) / 2, (y0 + y1) / 2), name, font=font, fill=(17, 24, 39), anchor="mm")
    d.text((20, 110), "Nothing clicked yet", font=font, fill=(17, 24, 39))
    return img, boxes


@pytest.mark.skipif(not ocr.available(), reason="Windows OCR has no language installed here")
def test_labels_inside_a_row_of_bordered_buttons_are_found():
    img, boxes = _painted_buttons()
    passes = ocr.scan(img)
    for name, (x0, y0, x1, y1) in boxes.items():
        hits = ocr.find(passes, name)
        assert len(hits) == 1, (name, [p.text for p in ocr.merged(passes)])
        cx, cy = hits[0].box.center()
        assert x0 < cx < x1 and y0 < cy < y1, (name, hits[0].box)
    assert ocr.find(passes, "Nothing clicked yet")


def test_a_capture_caught_mid_repaint_is_taken_again(monkeypatch):
    from overshoulder import locate

    whole, half = Image.new("RGB", (40, 20), "white"), Image.new("RGB", (40, 20), "white")
    ImageDraw.Draw(whole).rectangle((5, 5, 35, 15), fill="black")
    ImageDraw.Draw(half).rectangle((5, 5, 20, 15), fill="black")
    frames = iter([half, whole, whole])
    monkeypatch.setattr(locate.capture, "window_image", lambda h, r: next(frames))
    img, _ = locate.scope_pixels(BoundScope())
    assert img.getpixel((30, 10)) == (0, 0, 0)


# -- t-refs in the service -------------------------------------------------------------------------

class BoundScope(FakeScope):
    class binding:  # noqa: N801
        hwnd, pid = 0x10, 1

    def __init__(self):
        super().__init__()
        self._bounds = Rect(100, 50, 300, 150)


PHRASES = [[Phrase(tuple(words(("Save", 10, 10, 40)))), Phrase(tuple(words(("Delete", 120, 10, 50)))),
            Phrase(tuple(words(("Status", 10, 60, 50))))]]


@pytest.fixture
def svc(tmp_path, monkeypatch):
    from overshoulder import service
    from overshoulder.gateway.core import gateway_for_tests

    scope = BoundScope()
    pixels = {"img": Image.new("RGB", (200, 100), "white")}
    pressed = []

    class Done:
        def to_dict(self):
            return {"method": "posted_click", "background": True}

    class Ladder:
        def __init__(self, scope_, p, method, order=None):
            pass

        def press(self, el, t, button="left", count=1):
            pressed.append(t.point())
            return Done()

    monkeypatch.setattr(service.capture, "window_image", lambda h, r: pixels["img"].copy())
    monkeypatch.setattr(service.ocr, "scan", lambda img: PHRASES)
    monkeypatch.setattr(service.ocr, "available", lambda: True)
    monkeypatch.setattr(service.uia, "roots_for_window", lambda h: [])
    monkeypatch.setattr(service, "Ladder", Ladder)

    def make(**kw):
        s = service.Service(gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda spec: scope, **kw),
                            shots_dir=tmp_path / "shots")
        token = s.gw.registry.add("agent")
        return s, token, s.acquire(token, "window:16")["lease_id"]

    return make, pixels, pressed


def test_find_text_returns_t_refs_in_screen_pixels(svc):
    make, _, _ = svc
    s, token, lease = make()
    [hit] = s.find(token, lease, text="save")
    assert hit == {"ref": "t1", "name": "Save", "text": "Save", "source": "ocr", "rect": [110, 60, 150, 76]}


def test_a_click_on_a_t_ref_goes_to_the_middle_of_its_box(svc):
    make, _, pressed = svc
    s, token, lease = make()
    ref = s.find(token, lease, text="Save")[0]["ref"]
    out = s.act(token, lease, "click", ref=ref)
    assert out["ok"] and pressed == [(130, 68)]


def test_a_t_ref_goes_stale_when_its_pixels_change(svc):
    make, pixels, pressed = svc
    s, token, lease = make()
    ref = s.find(token, lease, text="Status")[0]["ref"]
    ImageDraw.Draw(pixels["img"]).rectangle((10, 60, 60, 76), fill="black")
    with pytest.raises(GatewayError, match="changed or moved") as e:
        s.act(token, lease, "click", ref=ref)
    assert e.value.code == "target_stale" and pressed == []


def test_a_consequential_word_under_a_t_ref_needs_approval(svc):
    make, _, pressed = svc
    s, token, lease = make()
    ref = s.find(token, lease, text="Delete")[0]["ref"]
    with pytest.raises(GatewayError) as e:
        s.act(token, lease, "click", ref=ref)
    assert e.value.code == "approval_denied" and "Delete" in str(e.value) and pressed == []


def test_a_t_ref_takes_mouse_actions_only(svc):
    make, _, _ = svc
    s, token, lease = make()
    ref = s.find(token, lease, text="Save")[0]["ref"]
    with pytest.raises(ValueError, match="click it first"):
        s.act(token, lease, "type", ref=ref, text="hello")
    with pytest.raises(ValueError, match="not a control"):
        s.read(token, lease, ref)


def test_a_find_by_name_with_no_element_looks_on_screen(svc):
    make, _, _ = svc
    s, token, lease = make()
    assert [h["ref"] for h in s.find(token, lease, name="Delete")] == ["t1"]
    assert s.find(token, lease, name="Delete", control_type="Button") == []


def test_observe_text_lists_every_phrase(svc):
    make, _, _ = svc
    s, token, lease = make()
    out = s.observe(token, lease, text=True)
    assert [t["text"] for t in out["texts"]] == ["Save", "Delete", "Status"]


def test_refs_from_an_ended_lease_are_gone(svc):
    make, _, _ = svc
    s, token, lease = make()
    s.find(token, lease, text="Save")
    s.release(token, lease)
    lease = s.acquire(token, "window:16")["lease_id"]
    with pytest.raises(GatewayError, match="unknown ref"):
        s.act(token, lease, "click", ref="t1")


def test_with_ocr_turned_off_text_is_not_read(svc):
    from overshoulder import settings as st

    make, _, _ = svc
    s, token, lease = make(settings_source=lambda: st.Settings(input_mode="full", ocr_enabled=False))
    with pytest.raises(GatewayError, match="turned off"):
        s.find(token, lease, text="Save")
    assert s.find(token, lease, name="Save") == []


def test_the_settings_page_switch_turns_ocr_off():
    from overshoulder import settings_page as sp

    assert sp.settings_from_form({"ocr_enabled": "on"}).ocr_enabled is True
    assert sp.settings_from_form({}).ocr_enabled is False


# -- live, on a window of painted buttons ----------------------------------------------------------

def _clicked(out: Path) -> list:
    try:
        return json.loads(out.read_text(encoding="utf-8-sig")).get("clicked", [])
    except (OSError, ValueError):
        return []


@pytest.mark.desktop
def test_an_agent_clicks_a_painted_button_by_its_text(tmp_path):
    from test_desktop_background import _wait_window
    from test_desktop_drag import _launch

    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("ocr-test")
    out = tmp_path / "drawn.json"
    proc = _launch(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DRAWN), str(out)])
    try:
        hwnd = _wait_window(r"^Overshoulder drawn test$")
        end = time.time() + 20
        while time.time() < end and not out.exists():
            time.sleep(0.1)
        time.sleep(0.5)
        lease = s.acquire(token, f"window:{hwnd}")["lease_id"]

        # OCR's box for the real button lies inside UI Automation's box for it.
        [el] = s.find(token, lease, name="Apply", control_type="Button")
        [tx] = s.find(token, lease, text="Apply")
        assert tx["source"] == "ocr"
        assert el["rect"][0] <= tx["rect"][0] and tx["rect"][2] <= el["rect"][2], (el, tx)
        assert el["rect"][1] <= tx["rect"][1] and tx["rect"][3] <= el["rect"][3], (el, tx)

        status = s.find(token, lease, text="Nothing clicked yet")[0]["ref"]
        [save] = s.find(token, lease, name="Save")  # no such element: found on screen instead
        assert save["source"] == "ocr"
        res = s.act(token, lease, "click", ref=save["ref"], method="background")
        assert res["ok"] and res["result"]["method"] == "posted_click"
        end = time.time() + 5
        while time.time() < end and not _clicked(out):
            time.sleep(0.1)
        assert _clicked(out) == ["Save"]

        with pytest.raises(GatewayError) as e:  # the status line now reads "Clicked Save"
            s.act(token, lease, "click", ref=status, method="background")
        assert e.value.code == "target_stale"

        delete = s.find(token, lease, text="Delete")[0]["ref"]
        with pytest.raises(GatewayError) as e:  # nobody approves in tests
            s.act(token, lease, "click", ref=delete, method="background")
        assert e.value.code == "approval_denied"
        time.sleep(0.5)
        assert _clicked(out) == ["Save"]
        s.release(token, lease)
    finally:
        proc.kill()
