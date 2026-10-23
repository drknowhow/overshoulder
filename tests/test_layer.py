import json
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image, ImageDraw  # noqa: E402

from overshoulder import layer as L  # noqa: E402
from overshoulder import render as R  # noqa: E402
from overshoulder import zoom as Z  # noqa: E402
from tests.rechelpers import HAVE_FFMPEG, make_recording, red_pixels, video_frames  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg on PATH")


def _row(action, t, **kw):
    return {"event": "action", "action": action, "ok": True, "t_act": t, "ts": t + 0.4, "ms": 400, **kw}


def _fonts():
    return {"badge": R._font(18), "caption": R._font(26), "make": R._font}


@pytest.mark.parametrize("item, message", [
    ({"kind": "laser"}, "kind must be"),
    ({"kind": "box"}, "box needs rect"),
    ({"kind": "box", "rect": [10, 10, 5, 20]}, "right > left"),
    ({"kind": "box", "rect": [0, 0, 5, 5], "colour": "red"}, "unknown field"),
    ({"kind": "box", "rect": [0, 0, 5, 5], "color": "not-a-colour"}, "is not a colour"),
    ({"kind": "redact", "rect": [0, 0, 5, 5], "style": "smudge"}, "style must be"),
    ({"kind": "cut", "t0": 5, "t1": 2}, "t1 must be after t0"),
    ({"kind": "arrow", "tail": [1, 2], "head": [3]}, "head must be 2 numbers"),
])
def test_bad_items_are_refused_with_the_reason(item, message):
    with pytest.raises(L.LayerError, match=message):
        L.validate(item)


def test_items_get_ids_and_can_be_edited_and_removed(tmp_path):
    layer = L.Layer(tmp_path / "annotations.json")
    a = layer.add({"kind": "box", "rect": [1, 2, 30, 40], "text": "here"})
    b = layer.add({"kind": "badge", "at": [5, 5]})
    assert (a["id"], b["id"]) == ("a1", "a2")
    edited = layer.edit("a1", {"text": None, "color": "#ff0000"})
    assert "text" not in edited and edited["color"] == "#ff0000"
    assert layer.remove("a2") and not layer.remove("a2")
    assert [x["id"] for x in layer.items()] == ["a1"]
    assert json.loads((tmp_path / "annotations.json").read_text())["version"] == L.VERSION


def test_one_step_item_per_step_and_one_crop_per_layer(tmp_path):
    layer = L.Layer(tmp_path / "annotations.json")
    first = layer.add({"kind": "step", "step": 2, "caption": "Save it"})
    again = layer.add({"kind": "step", "step": 2, "hidden": True})
    assert again["id"] == first["id"] and again["caption"] == "Save it" and again["hidden"] is True
    layer.add({"kind": "crop", "rect": [0, 0, 50, 50]})
    with pytest.raises(L.LayerError, match="already has a crop"):
        layer.add({"kind": "crop", "rect": [0, 0, 60, 60]})


def test_a_hand_edited_file_with_a_bad_item_names_it(tmp_path):
    (tmp_path / "annotations.json").write_text(json.dumps(
        {"version": 1, "items": [{"id": "a7", "kind": "box", "rect": [0, 0, 1]}]}))
    with pytest.raises(L.LayerError, match="item 1 \\(a7\\)"):
        L.Layer(tmp_path / "annotations.json").items()


def test_items_show_in_their_step_or_time_window_and_move_to_screen_pixels():
    items = [L.validate({"id": "a1", "kind": "box", "rect": [0, 0, 10, 10], "step": 2}),
             L.validate({"id": "a2", "kind": "badge", "at": [5, 5], "t0": 3.0, "t1": 4.0}),
             L.validate({"id": "a3", "kind": "text", "at": [1, 1], "text": "always"})]
    windows = {2: (1.0, 2.0)}
    assert [x["id"] for x in L.active(items, 1.5, windows, (0, 0))] == ["a1", "a3"]
    assert [x["id"] for x in L.active(items, 3.5, windows, (0, 0))] == ["a2", "a3"]
    moved = L.active(items, 1.5, windows, (100, 200))[0]
    assert moved["rect"] == [100, 200, 110, 210]
    assert [x["id"] for x in L.active(items, 9.0, windows, (0, 0), still_of=2)] == ["a1", "a3"]


@pytest.mark.parametrize("style", L.STYLES)
def test_a_redaction_destroys_what_it_covers_and_nothing_else(style):
    img = Image.new("RGB", (200, 100), (255, 255, 255))
    d = ImageDraw.Draw(img)
    for x in range(20, 100, 4):
        d.line((x, 20, x, 60), fill=(0, 0, 0), width=1)  # fine black stripes, like text
    d.rectangle((150, 20, 180, 60), fill=(0, 0, 0))
    out = L.redact(img.copy(), [L.validate({"kind": "redact", "rect": [16, 16, 104, 64], "style": style})],
                   lambda x, y: (x, y))
    region = out.crop((20, 20, 100, 60)).convert("L")
    lo, hi = region.getextrema()
    assert hi - lo < 120  # the stripes are gone: no black-next-to-white edges survive
    assert out.crop((150, 20, 180, 60)).tobytes() == img.crop((150, 20, 180, 60)).tobytes()


def test_hidden_steps_and_steps_in_cuts_drop_out_and_the_rest_renumber():
    rows = [_row("click", t, target={"kind": "point", "x": 1, "y": 1}) for t in (1.0, 3.0, 5.0, 7.0)]
    steps = R.build_steps(rows, 0.0)
    kept = R.edit_steps(steps, {2: {"hidden": True}, 4: {"caption": "Finish"}}, cuts=[(4.5, 5.5)])
    assert [(s.id, s.n, s.caption) for s in kept] == [(1, 1, "Click here"), (4, 2, "Finish")]


def test_redaction_is_applied_before_the_zoom_so_no_zoomed_frame_shows_it():
    size = (400, 300)
    img = Image.new("RGB", size, (255, 255, 255))
    ImageDraw.Draw(img).rectangle((190, 140, 210, 160), fill=(220, 30, 30))
    steps = R.build_steps([_row("click", 1.0, target={"kind": "element", "name": "x",
                                                      "rect": [190, 140, 210, 160]})], 0.0)
    view = Z.camera_at(1.0, Z.plan(steps, size), size, (0, 0))
    items = [L.validate({"kind": "redact", "rect": [185, 135, 215, 165], "style": "solid"})]
    out = R.annotate(img, 1.0, (0, 0), None, steps, _fonts(), view=view, items=items)
    assert view is not None and red_pixels(out) == 0


def test_a_screenshot_takes_a_layer_too(tmp_path):
    shot = tmp_path / "shot.png"
    img = Image.new("RGB", (120, 80), (255, 255, 255))
    ImageDraw.Draw(img).rectangle((10, 10, 40, 30), fill=(220, 30, 30))
    img.save(shot)
    layer = L.Layer.for_image(shot)
    layer.add({"kind": "redact", "rect": [5, 5, 45, 35]})
    layer.add({"kind": "arrow", "tail": [100, 70], "head": [60, 40]})
    out = R.render_image(shot)
    with Image.open(out["image"]) as result:
        assert out["annotations"] == 2 and red_pixels(result) == 0
        assert result.getpixel((80, 55)) != (255, 255, 255)  # on the arrow's shaft


@needs_ffmpeg
def test_editing_the_layer_and_rendering_again_changes_every_output(tmp_path):
    rec = make_recording(tmp_path)
    first = R.render(rec, zoom="off")  # a zoom on the button would crop the red square out anyway
    size = tuple(json.loads((rec / "meta.json").read_text())["size"])
    before = video_frames(rec / "tutorial" / "tutorial.mp4", (size[0], size[1] + R.STRIP))
    assert all(red_pixels(f) > 1000 for f in before)

    steps = json.loads((rec / "tutorial" / "steps.json").read_text())
    assert [s["id"] for s in steps] == [1, 2]
    assert steps[0]["rect"] == [200, 150, 260, 180]  # clean-frame pixels, not screen pixels
    layer = L.Layer.for_recording(rec)
    layer.add({"kind": "redact", "rect": [16, 16, 124, 64], "style": "solid"})  # a blur keeps a block's colour
    layer.add({"kind": "step", "step": 2, "caption": "Save the file"})
    second = R.render(rec, zoom="off")

    after = video_frames(rec / "tutorial" / "tutorial.mp4", (size[0], size[1] + R.STRIP))
    assert all(red_pixels(f) < 20 for f in after)
    for p in (rec / "tutorial").glob("step-*.png"):
        with Image.open(p) as still:
            assert red_pixels(still) < 20
    assert "2. Save the file" in (rec / "tutorial" / "steps.md").read_text()
    assert first["annotations"] == 0 and second["annotations"] == 2


@needs_ffmpeg
def test_hiding_a_step_removes_its_still_and_a_crop_shrinks_the_video(tmp_path):
    rec = make_recording(tmp_path)
    R.render(rec)
    assert sorted(p.name for p in (rec / "tutorial").glob("step-*.png")) == ["step-01.png", "step-02.png"]
    layer = L.Layer.for_recording(rec)
    layer.add({"kind": "step", "step": 1, "hidden": True})
    layer.add({"kind": "crop", "rect": [150, 100, 301, 221]})
    out = R.render(rec, zoom="off")
    assert sorted(p.name for p in (rec / "tutorial").glob("step-*.png")) == ["step-01.png"]
    with Image.open(rec / "tutorial" / "step-01.png") as still:
        assert still.size == (150, 120 + R.STRIP)
    steps = json.loads((rec / "tutorial" / "steps.json").read_text())
    assert [(s["id"], s["n"], s["hidden"]) for s in steps] == [(1, None, True), (2, 1, False)]
    assert out["steps"] == 1
