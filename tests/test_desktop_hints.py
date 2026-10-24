"""Hints on a real app: a painted button a vision model found and the screen confirmed is found again in the
next session where it was, without asking the model."""

import json
import sys
import time
import urllib.request
from pathlib import Path

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]

DRAWN = Path(__file__).with_name("drawnapp.ps1")


def _local_model_ready() -> bool:
    from overshoulder import vision
    try:
        with urllib.request.urlopen(f"{vision.DEFAULT_LOCAL_URL.rstrip('/')}/api/tags", timeout=3) as r:
            return vision.DEFAULT_LOCAL_MODEL in {m["name"] for m in json.load(r)["models"]}
    except OSError:
        return False


def test_a_second_describe_of_a_painted_button_skips_the_vision_model(tmp_path):
    from test_desktop_background import _wait_window
    from test_desktop_drag import _launch

    from overshoulder import settings as st
    from overshoulder import vision
    from overshoulder.driver import ocr
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    if not ocr.available() or not _local_model_ready():
        pytest.skip("needs Windows OCR and the local vision model in Ollama")
    settings = st.Settings(input_mode="never", vision_mode="local")
    s = Service(gateway_for_tests(tmp_path, settings_source=lambda: settings), shots_dir=tmp_path / "shots")
    asked = []

    class Counting:
        def __init__(self, inner):
            self.inner, self.name = inner, inner.name

        def locate(self, img, description):
            asked.append(description)
            return self.inner.locate(img, description)

    s.locator_for = lambda cfg: Counting(vision.locator_for(cfg))
    token = s.gw.registry.add("hint-test")
    out = tmp_path / "drawn.json"
    proc = _launch(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(DRAWN), str(out)])
    hits, took = [], []
    try:
        hwnd = _wait_window(r"^Overshoulder drawn test$")
        end = time.time() + 20
        while time.time() < end and not out.exists():
            time.sleep(0.1)
        time.sleep(0.5)
        for _ in (1, 2):
            lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
            t0 = time.perf_counter()
            hits += s.find(token, lease, describe='the "Open" button')
            took.append(time.perf_counter() - t0)
            s.release(token, lease)
            s.gw.wait_learning()
    finally:
        proc.kill()
    print(f"describe with the model {took[0]:.2f} s, from the profile {took[1]:.3f} s")
    first, again = hits
    assert first["confirmed"] and first["model"].startswith("local ")
    assert again["via"] == "profile" and again["confirmed"] and again["rect"] == first["rect"]
    assert asked == ['the "Open" button']
    assert took[1] < took[0] / 5
