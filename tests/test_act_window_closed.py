"""An action that closes the scope's own window (Ctrl+W on the last tab, a dialog's OK) still happened.
act reports window_closed instead of failing while it screenshots a window that is gone (found
2026-10-23: Ctrl+W on Notepad's last tab came back as a bare "Error executing tool act", from a
screen grab of the closed window's zero-size rect)."""

import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from PIL import Image  # noqa: E402
from test_gateway import FakeScope, ok_checks  # noqa: E402

from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway.errors import GatewayError  # noqa: E402


class ClosingScope(FakeScope):
    class binding:  # noqa: N801
        hwnd, pid = 0x10, 1

    def bounds(self):
        return self._bounds if self.alive else Rect(0, 0, 0, 0)  # what a closed window's frame reads as


@pytest.fixture
def svc(tmp_path, monkeypatch):
    from overshoulder import service
    from overshoulder.gateway.core import gateway_for_tests

    scope = ClosingScope()

    def grab(r):
        if r.width <= 0 or r.height <= 0:  # mss raises ScreenShotError for this
            raise RuntimeError(f"Region has zero or negative size: {r}")
        return Image.new("RGB", (r.width, r.height), "white")

    class Done:
        def to_dict(self):
            return {"method": "posted_keys", "background": True}

    class Ladder:
        def __init__(self, scope_, p, method, order=None):
            pass

        def hotkey(self, combo):
            scope.alive = False  # ctrl+w closed the last tab, and with it the window
            return Done()

    monkeypatch.setattr(service.capture, "grab", grab)
    monkeypatch.setattr(service.capture, "window_image",
                        lambda h, r: Image.new("RGB", (r.width, r.height), "white") if scope.alive else None)
    monkeypatch.setattr(service, "Ladder", Ladder)
    s = service.Service(gateway_for_tests(tmp_path, checks=ok_checks(), scope_factory=lambda spec: scope),
                        shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("agent")
    return s, token, s.acquire(token, "window:16")["lease_id"]


def test_an_action_that_closes_the_window_reports_it(svc):
    s, token, lease = svc
    out = s.act(token, lease, "hotkey", combo="ctrl+w", method="background")
    assert out["ok"]
    assert out["verify"]["window_closed"] is True
    assert out["verify"]["method"] == "posted_keys"


def test_an_expectation_about_the_closed_window_fails_with_a_reason(svc):
    s, token, lease = svc
    with pytest.raises(GatewayError, match="window closed"):
        s.act(token, lease, "hotkey", combo="ctrl+w", method="background",
              expect={"element": {"name": "Saved"}})


def test_the_next_action_on_the_closed_window_is_refused_as_stale(svc):
    from overshoulder.scope import ScopeStale

    s, token, lease = svc
    s.act(token, lease, "hotkey", combo="ctrl+w", method="background")
    with pytest.raises(ScopeStale):
        s.act(token, lease, "hotkey", combo="ctrl+s", method="background")
