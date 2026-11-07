"""The agent box is checked when the gateway starts and daily; the person sees the result on the operator
status and the settings page, and the agent's own status tool doesn't carry it."""

import json
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from test_mcp_server import call, daemon  # noqa: E402,F401  (fixture reuse)
from test_settings_http import _http  # noqa: E402

from overshoulder import agentbox as A  # noqa: E402


class FakeBox:
    def __init__(self, exists=True, failed=(), error=None):
        self._exists, self.failed, self.error = exists, list(failed), error

    def exists(self):
        if self.error:
            raise self.error
        return self._exists

    def check(self):
        return list(self.failed)


def watch(box, logged=None):
    return A.Watch(box=lambda: box, log=lambda ev, **kw: (logged if logged is not None else []).append((ev, kw)))


def test_a_box_that_holds_is_logged_and_reported():
    logged = []
    w = watch(FakeBox(), logged)
    assert w.status() is None and "not been checked" in A.describe(None)
    w.run_once()
    r = w.status()
    assert r["ok"] and r["failed"] == [] and r["age_s"] >= 0
    assert logged == [("agent_box_check", {"ok": True, "failed": []})]
    assert "held" in A.describe(r)


def test_a_box_that_does_not_hold_says_what_failed():
    w = watch(FakeBox(failed=["network filter: gone"]))
    w.run_once()
    assert "NOT LOCKED DOWN" in A.describe(w.status()) and "network filter: gone" in A.describe(w.status())


def test_no_box_is_not_an_alarm_and_is_not_logged():
    logged = []
    w = watch(FakeBox(exists=False), logged)
    w.run_once()
    assert w.status()["exists"] is False and logged == []
    assert "no agent box" in A.describe(w.status())


def test_a_check_that_cannot_run_counts_as_not_holding():
    w = watch(FakeBox(error=OSError("wsl.exe not found")))
    w.run_once()
    r = w.status()
    assert r["ok"] is False and "could not run" in r["failed"][0]


def test_it_checks_at_start_and_again_each_period():
    runs = []
    box = FakeBox()
    w = A.Watch(box=lambda: runs.append(1) or box, every_s=0.01)
    stop = threading.Event()
    t = w.start(stop)
    deadline = time.monotonic() + 5
    while len(runs) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    stop.set()
    t.join(2)
    assert len(runs) >= 3 and not t.is_alive()


def test_the_person_sees_it_and_the_agent_does_not(daemon):  # noqa: F811
    d, port, token = daemon
    d.box_watch = watch(FakeBox(failed=["no Windows drives: a Windows drive is mounted"]))
    d.box_watch.run_once()
    base = f"http://127.0.0.1:{port}"
    _, _, body = _http("GET", f"{base}/operator/status", headers={"x-overshoulder-operator": d.operator_key})
    assert json.loads(body)["agent_box"]["failed"] == ["no Windows drives: a Windows drive is mounted"]
    agent_view = call(port, token, "status")
    assert "agent_box" not in json.dumps(agent_view)


def test_the_settings_page_shows_a_box_that_does_not_hold():
    from overshoulder import settings as st
    from overshoulder.settings_page import render_page
    r = {"exists": True, "ok": False, "failed": ["network filter: gone"], "age_s": 120}
    page = render_page(st.Settings(), "tok", [], "logs", saved=False, agent_box=r)
    assert "THE AGENT BOX IS NOT LOCKED DOWN (checked 2 min ago): network filter: gone" in page
    page = render_page(st.Settings(), "tok", [], "logs", saved=False, agent_box={"exists": False, "age_s": 5})
    assert "no agent box" in page
