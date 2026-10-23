"""An agent's screenshot of a window scope holds that window's pixels only, never the screen area it
occupies: the person's own windows are often on top of it (found 2026-10-23, when an observe
screenshot of a covered Notepad showed the person's chat window instead)."""

import sys
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image  # noqa: E402

from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway.errors import GatewayError  # noqa: E402


@pytest.fixture
def svc(tmp_path, monkeypatch):
    from overshoulder import service
    from overshoulder.gateway.core import gateway_for_tests

    calls = []
    monkeypatch.setattr(service.capture, "grab", lambda r: calls.append("grab") or Image.new("RGB", (4, 4), "red"))
    monkeypatch.setattr(service.capture, "window_image",
                        lambda h, r: calls.append(("window", h)) or Image.new("RGB", (4, 4), "blue"))
    return service.Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots"), calls


def scope(binding):
    return SimpleNamespace(binding=binding, bounds=lambda: Rect(0, 0, 4, 4))


def test_a_window_scope_screenshot_uses_the_windows_own_pixels(svc):
    s, calls = svc
    path = s._shot(scope(SimpleNamespace(hwnd=42)), "shot")
    assert calls == [("window", 42)]
    with Image.open(path) as img:
        assert img.getpixel((0, 0)) == (0, 0, 255)


def test_no_window_pixels_means_no_screenshot_rather_than_the_screen(svc, monkeypatch):
    from overshoulder import service

    s, calls = svc
    monkeypatch.setattr(service.capture, "window_image", lambda h, r: None)
    with pytest.raises(GatewayError, match="own pixels"):
        s._shot(scope(SimpleNamespace(hwnd=42)), "shot")
    assert "grab" not in calls


def test_a_screen_scope_screenshot_is_the_screen(svc):
    s, calls = svc
    s._shot(scope(None), "shot")
    assert calls == ["grab"]
