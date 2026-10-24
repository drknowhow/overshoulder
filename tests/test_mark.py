"""The "Made with Overshoulder" mark: drawn in the caption strip of everything the renderer makes for
people, kept out of the caption's way, on a track of its own in the Shotcut project, and written into
each file's metadata. Agent screenshots stay clean."""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image, ImageColor
from rechelpers import HAVE_FFMPEG, make_recording, video_frames
from test_narrate import FakeVoice
from test_shotcut import MELT, needs_melt

from overshoulder import brand, mark
from overshoulder import render as R

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg")
INK = ImageColor.getrgb(brand.INK)


def blue(img: Image.Image, box: tuple[int, int, int, int]) -> int:
    """Pixels of the mark's blue pointer inside box (allowing for video compression)."""
    raw = img.convert("RGB").crop(box).tobytes()
    return sum(1 for i in range(0, len(raw), 3) if raw[i + 2] > 180 and raw[i] < 120 and 90 < raw[i + 1] < 175)


def words(img: Image.Image, box: tuple[int, int, int, int]) -> int:
    """Light pixels inside box: the words or the white pointer."""
    raw = img.convert("RGB").crop(box).tobytes()
    return sum(1 for i in range(0, len(raw), 3) if min(raw[i:i + 3]) > 110)


def strip_end(img: Image.Image, strip: int = R.STRIP) -> tuple[int, int, int, int]:
    return img.width - mark.footprint(img.width), img.height - strip, img.width, img.height


def test_the_mark_sits_at_the_strips_right_end_with_its_words_on_wide_frames():
    wide = Image.new("RGB", (1906, R.STRIP), INK)
    mark.draw(wide, (0, 0, 1906, R.STRIP))
    end = 1906 - mark.footprint(1906)
    assert blue(wide, (end, 0, 1906, R.STRIP)) > 40 and words(wide, (end + 60, 0, 1906, R.STRIP)) > 200
    assert wide.crop((0, 0, end, R.STRIP)).getcolors() == [(end * R.STRIP, INK)]  # nothing outside its room
    narrow = Image.new("RGB", (480, R.STRIP), INK)
    mark.draw(narrow, (0, 0, 480, R.STRIP))
    assert mark.footprint(480) == 2 * mark.PAD + mark.TILE  # the mark alone, so the caption keeps its room
    assert blue(narrow, (480 - mark.footprint(480), 0, 480, R.STRIP)) > 40


def test_a_long_caption_stays_left_of_the_mark():
    img = Image.new("RGB", (900, 300), (255, 255, 255))
    steps = R.build_steps([{"event": "action", "action": "type", "ok": True, "t_act": 1.0, "ts": 1.0, "ms": 10,
                            "params": {"text": "a very long sentence " * 8}}], 0.0)
    fonts = {"badge": R._font(18), "caption": R._font(26)}
    quiet = R.annotate(img, 9.0, (0, 0), None, steps, fonts)
    busy = R.annotate(img, 1.1, (0, 0), None, steps, fonts)
    end = strip_end(busy)
    assert busy.crop(end).tobytes() == quiet.crop(end).tobytes()  # the caption never reaches the mark
    assert blue(busy, end) > 40


def test_a_shotcut_overlay_leaves_the_mark_to_its_own_track():
    img = Image.new("RGB", (900, 300), (255, 255, 255))
    parts = {}
    out = R.annotate(img, 0.0, (0, 0), None, [], {"badge": R._font(18), "caption": R._font(26)}, parts=parts)
    assert blue(out, strip_end(out)) > 40 and blue(parts["overlay"], strip_end(out)) == 0
    layer = mark.layer(out.size, R.STRIP)
    assert blue(layer, strip_end(out)) > 40 and layer.getpixel((10, 10))[3] == 0


def test_an_annotated_screenshot_gets_a_thin_strip_and_a_text_chunk(tmp_path):
    shot = tmp_path / "shot.png"
    Image.new("RGB", (800, 500), (255, 255, 255)).save(shot)
    out = R.render_image(shot)
    with Image.open(out["image"]) as img:
        assert img.size == (800, 500 + mark.SHOT_STRIP)
        assert img.crop((0, 0, 800, 500)).getcolors() == [(800 * 500, (255, 255, 255))]
        assert blue(img, strip_end(img, mark.SHOT_STRIP)) > 40
        assert img.text["Comment"].startswith("Made with Overshoulder") and img.text["Software"]
    with Image.open(shot) as original:
        assert "Comment" not in original.info  # the screenshot itself stays as the agent took it


@needs_ffmpeg
def test_every_rendered_file_carries_the_mark_and_says_so(tmp_path):
    rec = make_recording(tmp_path, size=(720, 400))
    out = R.render(rec, mlt=True, zoom="off")
    tut = Path(out["video"]).parent
    frames = video_frames(Path(out["video"]), (720, 400 + R.STRIP))
    assert all(blue(f, strip_end(f)) > 30 and words(f, strip_end(f)) > 150 for f in frames)
    tags = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json",
                                      out["video"]], capture_output=True, text=True, check=True).stdout)
    assert tags["format"]["tags"]["comment"] == mark.comment()
    for still in tut.glob("step-*.png"):
        with Image.open(still) as img:
            assert blue(img, strip_end(img)) > 40 and img.text["Comment"] == mark.comment()
    with Image.open(out["gif"]) as gif:
        assert gif.info["comment"].decode() == mark.comment()
        gif.seek(gif.n_frames - 1)
        last = gif.convert("RGB")
        assert blue(last, (last.width - round(mark.footprint(720) * last.width / 720), last.height - 40,
                           last.width, last.height)) > 10
    md = (tut / "steps.md").read_text(encoding="utf-8").rstrip().splitlines()
    assert md[-1] == mark.markdown() and "Made with Overshoulder" in md[-1]
    project = Path(out["project"]).read_text(encoding="utf-8")
    assert 'id="mark_track"' in project and "edit/mark.png" in project and (tut / "edit" / "mark.png").exists()


@needs_ffmpeg
def test_narration_keeps_the_comment(tmp_path):
    rec = make_recording(tmp_path)
    out = R.render(rec, narrate=True, engine=FakeVoice(1.0), gif=False)
    tags = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json",
                                      out["video"]], capture_output=True, text=True, check=True).stdout)
    assert tags["format"]["tags"]["comment"] == mark.comment()


@needs_melt
def test_melt_draws_the_mark_track(tmp_path):
    rec = make_recording(tmp_path, size=(720, 400))
    out = R.render(rec, gif=False, mlt=True, zoom="off")
    melt = tmp_path / "melt.mp4"
    subprocess.run([MELT, "-silent", out["project"], "-consumer", f"avformat:{melt}", "vcodec=libx264", "crf=12",
                    "pix_fmt=yuv420p"], capture_output=True, check=True, cwd=tmp_path.anchor)
    frames = video_frames(melt, (720, 400 + R.STRIP))
    assert frames and all(blue(f, strip_end(f)) > 30 for f in frames)


def test_a_gif_comment_goes_after_the_colour_table(tmp_path):
    path = tmp_path / "x.gif"
    frames = [Image.new("RGB", (8, 8), c) for c in ((255, 0, 0), (0, 0, 255))]
    frames[0].save(path, save_all=True, append_images=frames[1:], loop=0, duration=100)
    assert path.read_bytes()[:6] == b"GIF89a"
    mark.tag_gif(path)
    with Image.open(path) as gif:
        assert gif.info["comment"].decode() == mark.comment() and gif.n_frames == 2


# -- the switch: on for a new install, off hides what people see, the metadata stays ----------------------

def _switch(on: bool) -> None:
    from overshoulder import settings as st
    st.save(st.Settings(show_mark=on))  # in this test's own data folder (conftest)


def test_the_switch_is_on_for_a_new_install_and_read_when_it_changes():
    from overshoulder import settings as st
    assert st.Settings().show_mark and mark.visible() and mark.URL == "https://overshoulder.yepgent.com"
    _switch(False)
    assert not mark.visible() and mark.footprint(1906) == 0 and mark.markdown() == ""
    img = Image.new("RGB", (900, R.STRIP), INK)
    mark.draw(img, (0, 0, 900, R.STRIP))
    assert img.getcolors() == [(900 * R.STRIP, INK)]
    assert mark.layer((900, 300), R.STRIP).getbbox() is None
    _switch(True)
    assert mark.visible() and mark.markdown() == f"*Made with Overshoulder* ({mark.URL})"


def test_the_settings_page_has_the_switch():
    from overshoulder import settings as st
    from overshoulder import settings_page as sp
    page = sp.render_page(st.Settings(), "tok", [], "logs", False)
    assert 'name="show_mark" checked' in page and "Made with Overshoulder" in page
    assert sp.settings_from_form({}).show_mark is False  # an unticked box is off
    assert sp.settings_from_form({"show_mark": "on"}).show_mark is True
    assert 'name="show_mark" >' in sp.render_page(st.Settings(show_mark=False), "tok", [], "logs", False)


def test_with_the_switch_off_an_annotated_screenshot_has_no_strip_and_keeps_its_text_chunk(tmp_path):
    _switch(False)
    shot = tmp_path / "shot.png"
    Image.new("RGB", (800, 500), (255, 255, 255)).save(shot)
    with Image.open(R.render_image(shot)["image"]) as img:
        assert img.size == (800, 500) and img.text["Comment"] == mark.comment()


@needs_ffmpeg
def test_with_the_switch_off_nothing_shows_the_mark_and_the_files_still_say_it(tmp_path):
    room = mark.footprint(720)  # where the mark goes while it is on
    _switch(False)
    rec = make_recording(tmp_path, size=(720, 400))
    out = R.render(rec, mlt=True, zoom="off")
    tut = Path(out["video"]).parent
    box = (720 - room, 400, 720, 400 + R.STRIP)
    tail = (720 - (2 * mark.PAD + mark.TILE), 400, 720, 400 + R.STRIP)
    frames = video_frames(Path(out["video"]), (720, 400 + R.STRIP))
    assert frames and all(blue(f, box) == 0 and words(f, tail) == 0 for f in frames)
    tags = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "json",
                                      out["video"]], capture_output=True, text=True, check=True).stdout)
    assert tags["format"]["tags"]["comment"] == mark.comment()
    stills = sorted(tut.glob("step-*.png"))
    assert stills
    for still in stills:
        with Image.open(still) as img:
            assert blue(img, box) == 0 and img.text["Comment"] == mark.comment()
    with Image.open(out["gif"]) as gif:
        assert gif.info["comment"].decode() == mark.comment()
    md = (tut / "steps.md").read_text(encoding="utf-8")
    assert "Made with Overshoulder" not in md
    project = Path(out["project"]).read_text(encoding="utf-8")
    assert 'id="mark_track"' not in project and not (tut / "edit" / "mark.png").exists()
