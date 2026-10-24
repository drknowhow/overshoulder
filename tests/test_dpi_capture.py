"""A window that is not DPI aware is captured as it looks on screen. PrintWindow gets such a window's
drawing before DWM stretches it, so on a 125% monitor screenshots, recordings and verify used to see it
in the top-left 80% of the image with black beside and below it (found 2026-10-23 on the phone-test
window). Posted clicks were already right: ScreenToClient answers in the target window's pixels."""

import ctypes
import ctypes.wintypes as wt
import json
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image  # noqa: E402

from overshoulder.recorder import drawn_size, stretch_bgra  # noqa: E402

DRAWN = Path(__file__).with_name("drawnapp.ps1")
CLIENT = (410, 200)  # drawnapp.ps1's client size, in its own pixels


def test_a_window_drawn_at_96_dpi_on_a_125_percent_monitor_is_captured_at_80_percent():
    assert drawn_size(515, 290, 1.25) == (412, 232)
    assert drawn_size(515, 290, 1.0) == (515, 290)
    assert drawn_size(515, 290, 1.004) == (515, 290)


def test_stretching_keeps_the_corners_where_they_are_on_screen():
    img = Image.new("RGBA", (8, 4), (255, 255, 255, 255))
    img.putpixel((7, 3), (0, 160, 0, 255))
    out = stretch_bgra(img.tobytes("raw", "BGRA"), (8, 4), (10, 5))
    big = Image.frombuffer("RGBA", (10, 5), out, "raw", "BGRA", 0, 1)
    assert big.getpixel((9, 4))[1] > big.getpixel((9, 4))[0]
    assert big.getpixel((0, 0)) == (255, 255, 255, 255)


def test_an_unchanged_size_is_not_resampled():
    raw = bytes(range(16))
    assert stretch_bgra(raw, (2, 2), (2, 2)) is raw


def _read(path: Path, key: str, timeout: float = 20.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if data.get(key):
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError(f"{path.name} never had {key}")


@pytest.mark.desktop
def test_an_unaware_window_fills_its_capture_and_takes_clicks_where_they_land(tmp_path):
    from test_desktop_background import _wait_window
    from test_desktop_drag import _launch

    from overshoulder.driver import background, capture, display
    from overshoulder.driver import windows as w

    out = tmp_path / "drawn.json"
    proc = _launch(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DRAWN), str(out)])
    try:
        hwnd = _wait_window(r"^Overshoulder drawn test$")
        _read(out, "ready")
        time.sleep(0.3)
        scale = display.window_scale(hwnd)
        frame = w.frame_bounds(hwnd)
        img = capture.window_image(hwnd, frame)
        assert img.size == (frame.width, frame.height)

        origin = wt.POINT(0, 0)
        ctypes.windll.user32.ClientToScreen(hwnd, ctypes.byref(origin))
        # The green square is drawn 10-34 px in from the bottom-right corner, in the window's own pixels.
        gx = origin.x - frame.left + round((CLIENT[0] - 22) * scale)
        gy = origin.y - frame.top + round((CLIENT[1] - 22) * scale)
        r, g, b = img.getpixel((gx, gy))
        assert g > 120 and r < 90 and b < 120, (scale, (gx, gy), (r, g, b))

        background.post_click(hwnd, origin.x + round(335 * scale), origin.y + round(50 * scale))
        end = time.time() + 5
        while time.time() < end and not json.loads(out.read_text(encoding="utf-8-sig"))["clicked"]:
            time.sleep(0.1)
        assert json.loads(out.read_text(encoding="utf-8-sig"))["clicked"] == ["Delete"]
    finally:
        proc.kill()
