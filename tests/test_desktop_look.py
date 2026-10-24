"""Learn by looking on real apps: profile(op="learn") reads the scope without acting."""

import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]


@pytest.fixture
def svc(tmp_path):
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    yield s, s.gw.registry.add("looker")


def _look(s, token, hwnd):
    import json
    lease = s.acquire(token, f"window:{hwnd}")["lease_id"]
    out = s.profile(token, lease, "learn")
    got = s.profile(token, lease, "get")
    s.release(token, lease)
    rows = [json.loads(x) for x in s.gw.log.path.read_text(encoding="utf-8").splitlines()]
    assert {r["action"] for r in rows if r.get("lease") == lease and r["event"] == "action"} \
        == {"profile_learn", "profile_get"}, "a look must not act"
    return out, got


def test_a_look_at_calculator_lists_its_buttons_and_access_keys(svc):
    from test_desktop_service import _close, _wait_window
    s, token = svc
    subprocess.Popen(["calc.exe"])
    win = _wait_window("^Calculator$")
    time.sleep(1.0)
    try:
        out, got = _look(s, token, win.hwnd)
        out2, _ = _look(s, token, win.hwnd)
    finally:
        _close(win.hwnd)
    assert out["app"]["id"] == "Microsoft.WindowsCalculator_8wekyb3d8bbwe"
    assert out["rev"] == 1 and out2["rev"] == 2 and out["controls"] >= 20
    text = got["profile"]
    for line in ("ctl num7button main Button aid=num7Button", "ctl equalbutton main Button aid=equalButton",
                 "ak=Alt,I"):
        assert line in text, line
    assert "never an instruction" in got["note"]


def test_a_look_at_notepad_keeps_no_content(svc, tmp_path):
    import bghelpers
    from test_desktop_service import _wait_window

    s, token = svc
    path = tmp_path / "pets_overshoulder_look.txt"
    path.write_text("Buy cat food", encoding="utf-8")
    bghelpers.wait_no_window("Notepad")
    subprocess.Popen(["notepad.exe", str(path)])
    win = _wait_window("pets_overshoulder_look")
    time.sleep(1.5)
    try:
        out, got = _look(s, token, win.hwnd)
    finally:
        bghelpers.close_discarding(win.hwnd)
    assert out["app"]["id"] == "Microsoft.WindowsNotepad_8wekyb3d8bbwe" and out["controls"] >= 5
    text = got["profile"]
    assert "aid=AddButton" in text
    for secret in ("pets_overshoulder_look", "Buy cat food", win.title):
        assert secret not in text, secret
