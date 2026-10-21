import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder.watchdog import HUMAN_REASON, WatchdogState, parse_hotkey  # noqa: E402

DRIVING = {"paused": None, "active": {"client": "a"}}


def test_physical_input_while_driving_pauses():
    s = WatchdogState(idle_resume_s=5)
    s.on_physical_input(100.0)
    assert s.decide(101.0, DRIVING) == "pause"


def test_no_pause_when_nobody_is_driving():
    s = WatchdogState()
    s.on_physical_input(100.0)
    assert s.decide(101.0, {"paused": None, "active": None}) is None


def test_resumes_only_after_the_person_goes_idle():
    s = WatchdogState(idle_resume_s=5)
    s.on_physical_input(100.0)
    assert s.decide(101.0, DRIVING) == "pause"
    paused = {"paused": HUMAN_REASON, "active": {"client": "a"}}
    assert s.decide(103.0, paused) is None
    assert s.decide(106.0, paused) == "resume"


def test_never_resumes_a_panic():
    s = WatchdogState(idle_resume_s=5)
    s.on_physical_input(100.0)
    s.decide(101.0, DRIVING)
    assert s.decide(200.0, {"paused": "panic hotkey; an operator must resume", "active": None}) is None
    assert s.decide(300.0, {"paused": HUMAN_REASON, "active": None}) is None


def test_daemon_unreachable_changes_nothing():
    s = WatchdogState()
    s.on_physical_input(100.0)
    assert s.decide(101.0, None) is None


def test_parse_hotkey_requires_a_modifier():
    mods, vk = parse_hotkey("ctrl+alt+p")
    assert vk == ord("P") and mods & 0x3 == 0x3
    with pytest.raises(ValueError):
        parse_hotkey("p")


def test_the_agents_pointer_shows_its_last_action_point_for_a_while():
    from overshoulder.watchdog import POINTER_SHOW_S, pointer_target
    status = {"active": {"client": "a"}, "pointer": {"x": 10, "y": 20, "at": 100.0, "client": "a"}}
    assert pointer_target(status, 101.0) == (10, 20)
    assert pointer_target(status, 100.0 + POINTER_SHOW_S + 1) is None  # gone a while after the last action
    assert pointer_target({**status, "active": None}, 101.0) is None  # gone when the scope is released
    assert pointer_target(None, 101.0) is None  # daemon down
