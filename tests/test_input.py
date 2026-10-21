import ctypes
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder.driver import input as inp  # noqa: E402


def test_input_struct_matches_win32_size():
    assert ctypes.sizeof(inp.INPUT) == (40 if ctypes.sizeof(ctypes.c_void_p) == 8 else 28)


def test_utf16_units_splits_astral_characters_into_surrogates():
    assert inp.utf16_units("a") == [0x61]
    assert inp.utf16_units("\U0001F600") == [0xD83D, 0xDE00]


def test_text_inputs_types_key_syntax_characters_literally():
    events = inp.text_inputs("(a+b) {x}")
    typed = [e.ki.wScan for e in events if not e.ki.dwFlags & inp.KEYEVENTF_KEYUP]
    assert typed == [ord(c) for c in "(a+b) {x}"]
    assert all(e.ki.dwFlags & inp.KEYEVENTF_UNICODE for e in events)


def test_text_inputs_turns_newlines_into_enter():
    events = inp.text_inputs("a\r\nb\n")
    vks = [e.ki.wVk for e in events if not e.ki.dwFlags & inp.KEYEVENTF_KEYUP]
    assert vks == [0, inp.VK_RETURN, 0, inp.VK_RETURN]


def test_require_foreground_raises_when_another_window_has_focus():
    with pytest.raises(inp.FocusLost):
        inp.require_foreground(0x100, get_foreground=lambda: 0x200)
    inp.require_foreground(0x100, get_foreground=lambda: 0x100)


def test_type_text_sends_nothing_once_focus_moves(monkeypatch):
    sent = []
    monkeypatch.setattr(inp, "send", lambda events: sent.append(list(events)))
    focus = iter([0x100, 0x100, 0x999])
    with pytest.raises(inp.FocusLost):
        inp.type_text(0x100, "abc", per_char_delay=0, get_foreground=lambda: next(focus))
    assert len(sent) == 2


def test_typing_refuses_while_a_person_holds_ctrl(monkeypatch):
    monkeypatch.setattr(inp, "send", lambda events: None)
    ctrl_down = lambda vk: 0x8000 if vk == 0x11 else 0  # noqa: E731
    with pytest.raises(inp.KeysHeld):
        inp.type_text(0x100, "h", per_char_delay=0, get_foreground=lambda: 0x100, key_state=ctrl_down)
    inp.type_text(0x100, "h", per_char_delay=0, get_foreground=lambda: 0x100, key_state=lambda vk: 0)


def test_hotkey_releases_in_reverse_order():
    events = inp.hotkey_inputs("ctrl+shift+s")
    order = [(e.ki.wVk, bool(e.ki.dwFlags & inp.KEYEVENTF_KEYUP)) for e in events]
    assert order == [(0x11, False), (0x10, False), (ord("S"), False),
                     (ord("S"), True), (0x10, True), (0x11, True)]


def test_hotkey_marks_arrow_keys_extended():
    down = inp.hotkey_inputs("left")[0]
    assert down.ki.dwFlags & inp.KEYEVENTF_EXTENDEDKEY


def test_vk_for_rejects_unknown_names():
    with pytest.raises(ValueError):
        inp.vk_for("hyper")


def test_normalize_point_maps_virtual_desktop_corners():
    virtual = (-1920, 0, 3840, 1080)
    assert inp.normalize_point(-1920, 0, virtual) == (0, 0)
    assert inp.normalize_point(1919, 1079, virtual) == (65535, 65535)


def test_normalize_point_rejects_points_off_the_desktop():
    with pytest.raises(ValueError):
        inp.normalize_point(1920, 10, (0, 0, 1920, 1080))


def test_click_inputs_double_click_has_two_press_pairs():
    events = inp.click_inputs(10, 10, count=2, virtual=(0, 0, 100, 100))
    flags = [e.mi.dwFlags & 0x7E for e in events]
    assert flags == [0, inp.MOUSEEVENTF_LEFTDOWN, inp.MOUSEEVENTF_LEFTUP,
                     inp.MOUSEEVENTF_LEFTDOWN, inp.MOUSEEVENTF_LEFTUP]


def test_person_idle_time_does_not_count_our_own_injected_input(monkeypatch):
    clock = {"now": 10_000, "last": 9_000}
    monkeypatch.setattr(inp, "_tick_now", lambda: clock["now"])
    monkeypatch.setattr(inp, "last_input_tick", lambda: clock["last"])
    monkeypatch.setattr(inp, "_ticks", {"injected": None, "person": None})
    assert inp.person_idle_s() == 1.0  # the person's last input was 1 s ago
    inp._note_person_input()  # what send() does before injecting...
    clock["now"] = clock["last"] = 12_000
    inp._mark_injected()  # ...and after: Windows now reports our input as the last one
    clock["now"] = 13_000
    assert inp.person_idle_s() == 4.0  # still measured from the person's input at 9.0 s
    clock["last"], clock["now"] = 13_500, 14_000  # the person moves the mouse
    assert inp.person_idle_s() == 0.5


def test_tick_differences_survive_the_32_bit_wrap():
    assert inp._ms_after(5, 2**32 - 5) == 10
    assert inp._ms_after(2**32 - 5, 5) == -10
