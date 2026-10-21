"""Record an agent session in Notepad and render it into a tutorial."""

import json
import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]


def test_record_and_render_a_notepad_session(tmp_path):
    from overshoulder import scope as sc
    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("rec-test")
    path = tmp_path / "rec_notes.txt"
    path.write_text("", encoding="utf-8")
    __import__("bghelpers").wait_no_window("Notepad")
    subprocess.Popen(["notepad.exe", str(path)])
    win = None
    for _ in range(80):
        hits = sc.find_windows(title_re="rec_notes")
        if hits:
            win = hits[0]
            break
        time.sleep(0.25)
    assert win
    try:
        lease = s.acquire(token, f"window:{win.hwnd}", log_text=True)["lease_id"]
        s.act(token, lease, "activate")
        started = s.record_start(token, lease, fps=10, name="notepad-demo")
        time.sleep(1.0)
        doc = s.find(token, lease, control_type="Document")[0]["ref"]
        s.act(token, lease, "click", ref=doc)
        s.act(token, lease, "type", text="Hello from an Overshoulder tutorial.")
        s.act(token, lease, "hotkey", combo="ctrl+s")
        time.sleep(1.5)
        out = s.record_stop(token, lease)
        assert out["frames"] > 30, out
        tut = out["tutorial"]
        assert tut["steps"] == 4 and tut["images"] == 4, tut  # activate, click, type, ctrl+s
        rec_dir = started["recording"]
        meta = json.loads((tmp_path / "x").parent.joinpath(rec_dir, "meta.json").read_text())
        assert meta["frames"] == out["frames"]
        steps_md = (tmp_path / "x").parent.joinpath(rec_dir, "tutorial", "steps.md").read_text()
        assert 'Type "Hello from an Overshoulder tutorial."' in steps_md
        s.release(token, lease)
    finally:
        import bghelpers
        bghelpers.close_discarding(win.hwnd)
