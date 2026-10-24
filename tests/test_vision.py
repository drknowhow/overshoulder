"""Vision refs: a vision model is asked where a described control is, for icons with no text that neither
UI Automation nor OCR can name. The hit is a v-ref, checked against the scope's pixels before every
action like a t-ref. Off unless the person picks a model on the settings page.

A v-ref is confirmed when text read inside its box, or the control at its centre, matches the
description. An action on a confirmed one goes ahead; on an unconfirmed one the person is asked, and the
box on the desk shows them a picture of where the model looked."""

import json
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image, ImageDraw  # noqa: E402
from test_ocr import svc  # noqa: E402, F401

from overshoulder import settings as st  # noqa: E402
from overshoulder import vision  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway.errors import GatewayError  # noqa: E402

LOCAL = st.Settings(input_mode="full", vision_mode="local")
GEAR = Rect(150, 20, 180, 50)  # where the fake model sees things, in scope pixels


class FakeModel:
    name = "fake"

    def __init__(self, box=GEAR, error=None):
        self.box, self.error, self.asked = box, error, []

    def locate(self, img, description):
        self.asked.append((img.size, description))
        if self.error:
            raise vision.VisionError(self.error)
        return self.box


# -- reading a model's answer ------------------------------------------------------------------------

def test_a_box_on_the_0_to_1000_grid_becomes_pixels():
    assert vision.parse_box('{"bbox_2d": [100, 250, 300, 750]}', (400, 200)) == Rect(40, 50, 120, 150)


def test_a_box_in_a_code_fence_or_a_list_is_read():
    reply = '```json\n[{"bbox_2d": [500, 500, 600, 600], "label": "gear"}]\n```'
    assert vision.parse_box(reply, (1000, 1000)) == Rect(500, 500, 600, 600)


def test_corners_given_the_wrong_way_round_are_put_right_and_clipped():
    assert vision.parse_box('{"bbox_2d": [600, 900, 400, 1200]}', (1000, 1000)) == Rect(400, 900, 600, 1000)


def test_not_there_is_none_and_no_box_is_an_error():
    assert vision.parse_box('{"bbox_2d": null}', (100, 100)) is None
    assert vision.parse_box('{"bbox_2d": [10, 10, 10, 90]}', (100, 100)) is None  # no width
    with pytest.raises(vision.VisionError, match="no box"):
        vision.parse_box("I can't see a gear icon.", (100, 100))


# -- asking the models --------------------------------------------------------------------------------

def test_the_local_model_is_asked_to_unload_after_five_idle_minutes(monkeypatch):
    sent = {}

    def post(url, payload, headers=None, timeout=0):
        sent.update(url=url, payload=payload)
        return {"message": {"content": '{"bbox_2d": [0, 0, 500, 500]}'}}

    monkeypatch.setattr(vision, "_post_json", post)
    box = vision.OllamaLocator().locate(Image.new("RGB", (200, 100), "white"), "the gear icon")
    assert box == Rect(0, 0, 100, 50)
    p = sent["payload"]
    assert sent["url"] == "http://127.0.0.1:11434/api/chat"
    assert p["model"] == "qwen3-vl:8b-instruct" and p["keep_alive"] == "5m" and p["options"]["temperature"] == 0
    assert "the gear icon" in p["messages"][0]["content"] and len(p["messages"][0]["images"]) == 1


def test_a_cloud_model_gets_the_image_as_a_data_url_and_the_key(monkeypatch):
    sent = {}

    def post(url, payload, headers=None, timeout=0):
        sent.update(url=url, payload=payload, headers=headers)
        return {"choices": [{"message": {"content": '{"bbox_2d": [500, 0, 1000, 1000]}'}}]}

    monkeypatch.setattr(vision, "_post_json", post)
    loc = vision.CloudLocator("https://models.example/v1/", "some-vl", "k-123")
    assert loc.locate(Image.new("RGB", (100, 100)), "the close button") == Rect(50, 0, 100, 100)
    assert sent["url"] == "https://models.example/v1/chat/completions"
    assert sent["headers"] == {"Authorization": "Bearer k-123"}
    parts = sent["payload"]["messages"][0]["content"]
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_a_huge_screen_is_shrunk_before_it_is_sent():
    import base64
    import io

    png = base64.b64decode(vision._png_b64(Image.new("RGB", (5120, 2880))))
    assert max(Image.open(io.BytesIO(png)).size) == vision.MAX_SIDE


def test_a_real_http_round_trip_and_a_server_error():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(body["model"])
            if body["model"] == "missing":
                self.send_response(404)
                self.end_headers()
                self.wfile.write(b'{"error": "model not found"}')
                return
            out = json.dumps({"message": {"content": '{"bbox_2d": [250, 250, 750, 750]}'}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_port}"
        img = Image.new("RGB", (40, 40))
        assert vision.OllamaLocator(url, "m").locate(img, "x") == Rect(10, 10, 30, 30)
        with pytest.raises(vision.VisionError, match="404"):
            vision.OllamaLocator(url, "missing").locate(img, "x")
    finally:
        server.shutdown()
    assert seen == ["m", "missing"]


def test_nothing_is_asked_unless_a_model_is_chosen():
    assert "turned off" in vision.unavailable(st.Settings())
    assert vision.unavailable(LOCAL) is None
    assert "missing" in vision.unavailable(st.Settings(vision_mode="cloud"))
    assert isinstance(vision.locator_for(st.Settings(vision_mode="cloud", vision_cloud_url="u",
                                                     vision_cloud_model="m")), vision.CloudLocator)


# -- v-refs in the service ------------------------------------------------------------------------------

def _make(svc, model=None, settings=LOCAL):  # noqa: F811
    make, pixels, pressed = svc
    s, token, lease = make(settings_source=lambda: settings)
    s.locator_for = lambda _settings: model or FakeModel()
    return s, token, lease, pixels, pressed


def test_find_describe_returns_a_v_ref_in_screen_pixels(svc):  # noqa: F811
    model = FakeModel()
    s, token, lease, _, _ = _make(svc, model)
    [hit] = s.find(token, lease, describe="the gear icon")
    assert hit == {"ref": "v1", "name": "the gear icon", "description": "the gear icon", "source": "vision",
                   "rect": [250, 70, 280, 100], "model": "fake", "confirmed": False,
                   "confirmed_by": "nothing on screen; an action on it asks the person"}
    assert model.asked == [((200, 100), "the gear icon")]


# The svc fixture's OCR reads "Save", "Delete" and "Status" from any image, the box's included.

def test_text_read_in_the_box_confirms_it_and_a_click_goes_ahead(svc):  # noqa: F811
    s, token, lease, _, pressed = _make(svc)
    [hit] = s.find(token, lease, describe='the "Save" button')
    assert hit["confirmed"] and hit["confirmed_by"] == "it shows the text 'Save'"
    assert s.act(token, lease, "click", ref=hit["ref"])["ok"] and pressed == [(265, 85)]


def test_the_control_at_the_centre_confirms_it(svc):  # noqa: F811
    s, token, lease, _, pressed = _make(svc)
    s.gw.checks.element_in_scope_at = lambda scope, x, y: {"name": "Settings"} if (x, y) == (265, 85) else None
    [hit] = s.find(token, lease, describe="the settings gear")
    assert hit["confirmed_by"] == "the control at its centre is named 'Settings'"
    assert s.act(token, lease, "click", ref=hit["ref"])["ok"] and pressed == [(265, 85)]


def test_an_unconfirmed_v_ref_asks_and_the_question_shows_the_box(svc):  # noqa: F811
    import io

    from overshoulder.gateway.policy import CallbackApprover

    asked = []
    make, pixels, pressed = svc
    s, token, lease = make(settings_source=lambda: LOCAL,
                           approver=CallbackApprover(lambda req: asked.append(req) or True))
    s.locator_for = lambda _settings: FakeModel()
    ref = s.find(token, lease, describe="the gear icon")[0]["ref"]
    out = s.act(token, lease, "click", ref=ref)
    assert out["ok"] and pressed == [(265, 85)]
    [req] = asked
    assert req["reason"] == "click on 'the gear icon', where a vision model saw it and nothing on screen confirms it"
    pic = Image.open(io.BytesIO(req["picture"])).convert("RGB")
    assert pic.size == (200, 100)  # the whole scope, which is smaller than the picture's limit
    assert pic.getpixel((150, 30)) == (37, 99, 235) and pic.getpixel((160, 30)) == (255, 255, 255)
    assert pic.getpixel((165, 35)) == (37, 99, 235)  # the cross where the click lands


def test_an_unconfirmed_v_ref_is_not_clicked_when_the_person_says_no(svc):  # noqa: F811
    s, token, lease, _, pressed = _make(svc)
    ref = s.find(token, lease, describe="the gear icon")[0]["ref"]
    with pytest.raises(GatewayError, match="nothing on screen confirms it") as e:
        s.act(token, lease, "click", ref=ref)
    assert e.value.code == "approval_denied" and pressed == []


def test_agreeing_ignores_words_like_button_and_is_strict_about_the_rest():
    assert vision.agrees("Save", 'the "Save" button')
    assert vision.agrees("Settings", "the settings gear icon")
    assert not vision.agrees("Save As...", "the Save button")
    assert not vision.agrees("Settings", "the gear icon")
    assert not vision.agrees("the", "the button") and not vision.agrees("", "Save")


def test_the_picture_is_cut_around_the_box_on_a_big_screen():
    import io

    from test_ocr import BoundScope

    from overshoulder import locate

    class Big(BoundScope):
        def __init__(self):
            super().__init__()
            self._bounds = Rect(0, 0, 3000, 2000)

    big = Image.new("RGB", (3000, 2000), "white")
    import overshoulder.driver.capture as capture
    old = capture.window_image
    capture.window_image = lambda h, r: big.copy()
    try:
        pic = Image.open(io.BytesIO(locate.picture(Big(), Rect(2900, 1950, 2960, 1990)))).convert("RGB")
    finally:
        capture.window_image = old
    assert pic.size == locate.PICTURE_SIZE
    assert pic.getpixel((900 - 100, 600 - 50 + 20))[2] == 235  # the box's left edge, near the corner


def test_a_shared_screenshot_on_the_phone_is_the_picture_with_the_box():
    import io

    from overshoulder import phone

    png = io.BytesIO()
    Image.new("RGB", (900, 600), "white").save(png, "PNG")
    req = {"reason": "click on 'the gear icon'", "process": "app.exe", "picture": png.getvalue(), "hwnd": 0}
    shared = phone.make_question(req, phone.PhoneSettings(share_screenshot=True), 10)
    assert Image.open(io.BytesIO(shared.screenshot)).size == (900, 600)
    assert phone.make_question(req, phone.PhoneSettings(share_screenshot=False), 10).screenshot is None


def test_a_v_ref_goes_stale_when_its_pixels_change(svc):  # noqa: F811
    s, token, lease, pixels, pressed = _make(svc)
    ref = s.find(token, lease, describe="the gear icon")[0]["ref"]
    ImageDraw.Draw(pixels["img"]).rectangle((150, 20, 180, 50), fill="black")
    with pytest.raises(GatewayError, match="what looked like 'the gear icon'") as e:
        s.act(token, lease, "click", ref=ref)
    assert e.value.code == "target_stale" and pressed == []


def test_a_v_ref_takes_mouse_actions_only(svc):  # noqa: F811
    s, token, lease, _, _ = _make(svc)
    ref = s.find(token, lease, describe="the search box")[0]["ref"]
    with pytest.raises(ValueError, match="click it first"):
        s.act(token, lease, "type", ref=ref, text="hello")
    with pytest.raises(ValueError, match="not a control"):
        s.read(token, lease, ref)


def test_a_v_ref_described_as_consequential_needs_approval(svc):  # noqa: F811
    s, token, lease, _, pressed = _make(svc)
    ref = s.find(token, lease, describe="the delete button")[0]["ref"]
    with pytest.raises(GatewayError) as e:
        s.act(token, lease, "click", ref=ref)
    assert e.value.code == "approval_denied" and pressed == []


def test_a_v_ref_whose_box_lands_on_a_drawn_delete_needs_approval(svc):  # noqa: F811
    s, token, lease, _, pressed = _make(svc)
    s.gw.checks.label_at = lambda scope, x, y: "Delete" if (x, y) == (265, 85) else ""
    ref = s.find(token, lease, describe="the third icon")[0]["ref"]
    with pytest.raises(GatewayError, match="Delete") as e:
        s.act(token, lease, "click", ref=ref)
    assert e.value.code == "approval_denied" and pressed == []


def test_when_the_model_sees_nothing_find_returns_nothing(svc):  # noqa: F811
    s, token, lease, _, _ = _make(svc, FakeModel(box=None))
    assert s.find(token, lease, describe="a unicorn") == []


def test_a_model_that_cannot_be_reached_is_an_error_not_an_empty_answer(svc):  # noqa: F811
    s, token, lease, _, _ = _make(svc, FakeModel(error="could not reach http://127.0.0.1:11434"))
    with pytest.raises(GatewayError, match=r"vision model \(fake\) could not be asked"):
        s.find(token, lease, describe="the gear icon")


def test_with_vision_off_no_model_is_asked(svc):  # noqa: F811
    model = FakeModel()
    s, token, lease, _, _ = _make(svc, model, settings=st.Settings(input_mode="full"))
    with pytest.raises(GatewayError, match="turned off"):
        s.find(token, lease, describe="the gear icon")
    assert model.asked == []


@pytest.mark.desktop
def test_the_desk_box_shows_the_picture_and_closing_it_is_no():
    import ctypes
    import io
    import os

    from overshoulder import approvals, phone
    from overshoulder.driver import windows as w

    png = io.BytesIO()
    Image.new("RGB", (300, 200), (37, 99, 235)).save(png, "PNG")
    req = {"client": "agent", "reason": "click on 'the gear icon'", "picture": png.getvalue()}

    def ask(stop):
        q = phone.Question("id", req["reason"], "", time.time() + 20, req)
        got = {}
        t = threading.Thread(target=lambda: got.setdefault("a", approvals.DesktopBox().ask(q, stop)))
        t.start()
        end, box = time.time() + 10, None
        while time.time() < end and not box:
            box = next((x.hwnd for x in w.top_windows()
                        if x.pid == os.getpid() and x.title == approvals.BOX_TITLE), None)
            time.sleep(0.1)
        return t, got, box

    stop = threading.Event()
    t, got, box = ask(stop)
    assert box, "the box with the picture did not open"
    assert w.frame_bounds(box).height > 200  # tall enough to hold the picture above the buttons
    ctypes.windll.user32.PostMessageW(box, 0x0010, 0, 0)  # WM_CLOSE, as the window's X does
    t.join(10)
    assert got == {"a": False}

    stop = threading.Event()
    t, got, box = ask(stop)
    assert box
    stop.set()  # another channel answered first
    t.join(10)
    assert got == {"a": None} and not w.is_window(box)


# -- the settings page ------------------------------------------------------------------------------------

def test_the_settings_page_offers_the_three_choices_and_never_shows_the_key():
    from overshoulder import settings_page as sp

    page = sp.render_page(st.Settings(vision_mode="cloud", vision_cloud_key="secret-key"), "tok", [], "logs", False)
    for mode in ("off", "local", "cloud"):
        assert f'name="vision_mode" value="{mode}"' in page
    assert 'value="cloud" checked' in page and "secret-key" not in page and "leave empty to keep it" in page


def test_saving_the_page_keeps_a_saved_key_when_its_field_is_empty():
    from overshoulder import settings_page as sp

    current = st.Settings(vision_cloud_key="secret-key")
    s = sp.settings_from_form({"vision_mode": "cloud", "vision_cloud_url": " https://x/v1 ",
                               "vision_cloud_model": "vl", "vision_cloud_key": ""}, current)
    assert (s.vision_mode, s.vision_cloud_url, s.vision_cloud_key) == ("cloud", "https://x/v1", "secret-key")
    assert sp.settings_from_form({"vision_cloud_key": "new"}, current).vision_cloud_key == "new"
    assert sp.settings_from_form({"vision_mode": "everything"}).vision_mode == "off"
    assert "secret-key" not in repr(current)


# -- live, with the local model ---------------------------------------------------------------------------

def _local_model_ready() -> bool:
    try:
        with urllib.request.urlopen(f"{vision.DEFAULT_LOCAL_URL}/api/tags", timeout=3) as r:
            names = {m["name"] for m in json.loads(r.read())["models"]}
    except OSError:
        return False
    return vision.DEFAULT_LOCAL_MODEL in names


@pytest.mark.desktop
@pytest.mark.skipif(not _local_model_ready(), reason=f"Ollama with {vision.DEFAULT_LOCAL_MODEL} is not running")
def test_the_local_model_finds_character_maps_select_button(tmp_path):
    """The vision model is asked for Select by description. Its box has to cover the centre of the button
    UI Automation reports, and that button's name confirms it."""
    import subprocess

    from overshoulder.driver import windows as w
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path, settings_source=lambda: LOCAL), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("vision-test")
    proc = subprocess.Popen(["charmap.exe"])
    try:
        end, hwnd = time.time() + 20, None
        while time.time() < end and hwnd is None:
            hwnd = next((x.hwnd for x in w.top_windows() if x.pid == proc.pid and x.title == "Character Map"), None)
            time.sleep(0.2)
        assert hwnd, "Character Map did not open"
        time.sleep(1)
        lease = s.acquire(token, f"window:{hwnd}", ttl_s=300)["lease_id"]
        [select] = s.find(token, lease, name="Select", control_type="Button")
        [hit] = s.find(token, lease, describe='the "Select" button')
        assert hit["ref"].startswith("v") and hit["source"] == "vision"
        assert hit["confirmed_by"] == "the control at its centre is named 'Select'"
        left, top, right, bottom = hit["rect"]
        cx, cy = (select["rect"][0] + select["rect"][2]) // 2, (select["rect"][1] + select["rect"][3]) // 2
        assert left <= cx <= right and top <= cy <= bottom, (hit["rect"], select["rect"])
        s.release(token, lease)
    finally:
        proc.kill()
        vision.OllamaLocator().unload()


# -- a box the app's profile kept ------------------------------------------------------------------------

APP = "Microsoft.WindowsNotepad_8wekyb3d8bbwe"


def _profiled(svc, monkeypatch, model):  # noqa: F811
    from test_ocr import BoundScope

    from overshoulder.appid import AppIdentity

    monkeypatch.setattr(BoundScope.binding, "app", AppIdentity(APP, "Notepad", "Notepad.exe"), raising=False)
    s, token, lease, pixels, pressed = _make(svc, model)
    s.release(token, lease)
    return s, token, pixels


def _describe(s, token, words='the "Save" button'):
    lease = s.acquire(token, "window:16")["lease_id"]
    try:
        return s.find(token, lease, describe=words)
    finally:
        s.release(token, lease)
        s.gw.wait_learning()


def test_a_second_describe_of_a_confirmed_box_skips_the_model(svc, monkeypatch):  # noqa: F811
    model = FakeModel()
    s, token, _ = _profiled(svc, monkeypatch, model)
    [first] = _describe(s, token)
    assert first["model"] == "fake" and first["confirmed"] and len(model.asked) == 1
    [again] = _describe(s, token)
    assert len(model.asked) == 1, "the model was asked again"
    assert again["via"] == "profile" and again["rect"] == first["rect"]
    assert again["confirmed_by"] == "it shows the text 'Save'"  # confirmed by the screen now, not the file
    item = s.gw.profiles.load(APP).of_kind("paint")[0]
    assert item.num("hit") == 1 and item.text == 'the "Save" button'


def test_an_unconfirmed_box_is_not_kept(svc, monkeypatch):  # noqa: F811
    model = FakeModel()
    s, token, _ = _profiled(svc, monkeypatch, model)
    _describe(s, token, "the gear icon")  # the fake OCR reads nothing like it
    _describe(s, token, "the gear icon")
    assert len(model.asked) == 2 and s.gw.profiles.load(APP) is None


def test_changed_pixels_or_a_resized_window_ask_the_model(svc, monkeypatch):  # noqa: F811
    model = FakeModel()
    s, token, pixels = _profiled(svc, monkeypatch, model)
    _describe(s, token)
    ImageDraw.Draw(pixels["img"]).rectangle((165, 20, 180, 50), fill="black")  # half the box repainted
    [hit] = _describe(s, token)
    assert len(model.asked) == 2 and hit["model"] == "fake"
    pixels["img"] = Image.new("RGB", (240, 100), "white")
    _describe(s, token)
    assert len(model.asked) == 3
    item = s.gw.profiles.load(APP).of_kind("paint")[0]
    assert item.fields["sz"] == "240,100" and "miss" not in item.fields  # each new box replaced the old


def test_a_box_that_misses_three_times_is_no_longer_checked(svc, monkeypatch):  # noqa: F811
    model = FakeModel()
    s, token, pixels = _profiled(svc, monkeypatch, model)
    _describe(s, token)
    model.box = None  # the model no longer sees it either
    ImageDraw.Draw(pixels["img"]).rectangle((165, 20, 180, 50), fill="black")
    for n in (1, 2, 3):
        assert _describe(s, token) == []
        assert s.gw.profiles.load(APP).of_kind("paint")[0].num("miss") == n
    rev = s.gw.profiles.load(APP).revision
    _describe(s, token)
    assert s.gw.profiles.load(APP).revision == rev  # not checked, so nothing to learn
