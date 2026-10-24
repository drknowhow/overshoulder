"""Learn from use on a real app: two Notepad sessions revise its profile, and none of the person's content
(the file name, the window title, the typed text, the tab names) reaches the file."""

import subprocess
import sys
import time

import pytest

pytestmark = [pytest.mark.desktop,
              pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")]

NOTEPAD = "Microsoft.WindowsNotepad_8wekyb3d8bbwe"
SECRET_TEXT = "Buy cat food on Tuesday"


def test_two_notepad_sessions_revise_its_profile(tmp_path):
    import bghelpers
    from test_desktop_service import _focus, _one, _wait_window

    from overshoulder.gateway.core import gateway_for_tests
    from overshoulder.profiles import ProfileStore
    from overshoulder.service import Service

    s = Service(gateway_for_tests(tmp_path), shots_dir=tmp_path / "shots")
    token = s.gw.registry.add("learner")
    path = tmp_path / "pets_overshoulder_learn.txt"
    path.write_text("", encoding="utf-8")
    bghelpers.wait_no_window("Notepad")
    subprocess.Popen(["notepad.exe", str(path)])
    win = _wait_window("pets_overshoulder_learn")
    time.sleep(1.5)  # the title shows before the file has loaded
    titles = []
    try:
        _focus(win.hwnd)
        for n in (1, 2):
            lease = s.acquire(token, f"window:{win.hwnd}")
            titles.append(lease["scope"]["title"])
            assert lease["scope"]["app"]["id"] == NOTEPAD
            lease = lease["lease_id"]
            doc = _one(s.find(token, lease, control_type="Document"))
            s.act(token, lease, "type", ref=doc, text=f" {SECRET_TEXT} {n}", expect={"text_contains": SECRET_TEXT})
            s.act(token, lease, "hotkey", combo="ctrl+s")
            tabs = s.find(token, lease, control_type="TabItem")
            if tabs:
                s.act(token, lease, "select", ref=tabs[0]["ref"])
            s.release(token, lease)
            s.gw.wait_learning()
            prof = ProfileStore(tmp_path).load(NOTEPAD)
            assert prof is not None and prof.revision == n
        text = prof.dumps()
        for secret in [SECRET_TEXT, "pets_overshoulder_learn", *titles, *(t["name"] for t in tabs)]:
            assert secret and secret not in text, secret
        ways = {x.id: x for x in prof.of_kind("way")}
        typed = [w for w in ways if w.startswith("type@document.")]
        assert typed and sum(ways[w].num("ok") for w in typed) == 2
        assert ways["hotkey@focus.posted_keys"].num("ok") == 2
        assert prof.get("key", "ctrl+s").num("n") == 2
        doc_ctl = next(x for x in prof.of_kind("ctl") if x.args == ["main", "Document"])
        assert doc_ctl.fields["name"] == "content" and doc_ctl.num("ses") == 2
        assert (tmp_path / "profiles" / "history" / NOTEPAD / "rev-1.osp").exists()
    finally:
        bghelpers.close_discarding(win.hwnd)
