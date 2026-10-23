import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image  # noqa: E402

from overshoulder import render as R  # noqa: E402
from tests.rechelpers import HAVE_FFMPEG, make_recording  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg on PATH")


@needs_ffmpeg
def test_render_writes_an_animated_gif_no_wider_than_the_cap(tmp_path):
    rec = make_recording(tmp_path, size=(800, 240))
    out = R.render(rec)
    with Image.open(out["gif"]) as gif:
        assert gif.format == "GIF" and gif.n_frames > 5
        assert gif.width == R.GIF_WIDTH and gif.info.get("loop") == 0
    assert out["gif_bytes"] > 0


@needs_ffmpeg
def test_a_render_without_gif_removes_an_old_one(tmp_path):
    rec = make_recording(tmp_path)
    R.render(rec)
    assert (rec / "tutorial" / "tutorial.gif").exists()
    out = R.render(rec, gif=False)
    assert "gif" not in out and not (rec / "tutorial" / "tutorial.gif").exists()


@needs_ffmpeg
def test_a_small_video_keeps_its_own_size_in_the_gif(tmp_path):
    rec = make_recording(tmp_path, size=(320, 240))
    with Image.open(R.render(rec)["gif"]) as gif:
        assert gif.size == (320, 240 + R.STRIP)


@needs_ffmpeg
def test_a_recording_whose_actions_were_all_refused_still_renders(tmp_path):
    """Nothing to number (a narrowed session refused the only action), but the video and project come out."""
    refused = {"event": "action", "action": "drag", "ok": False, "t_act": 1001.0, "ts": 1001.1, "ms": 5,
               "error": {"code": "narrowed"}}
    rec = make_recording(tmp_path, actions=[refused])
    out = R.render(rec, gif=False, mlt=True)
    assert out["steps"] == 0 and out["frames"] > 0 and Path(out["project"]).is_file()
