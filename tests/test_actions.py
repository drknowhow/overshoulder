import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

from overshoulder import actions as A  # noqa: E402
from overshoulder.driver import background as bg  # noqa: E402
from overshoulder.driver.windows import Rect  # noqa: E402
from overshoulder.gateway.core import Target  # noqa: E402


class FakeScope:
    kind = "window"

    class binding:  # noqa: N801
        hwnd = 0x10
        pid = 1
        process = "app.exe"

    def owns_window(self, h):
        return True


class Calls(list):
    def hook(self, name, ret=None):
        def f(*a, **k):
            self.append(name)
            return ret
        return f


class FakeBorrow:
    """Stands in for the gateway's Borrow: records when it opens, checks and closes."""

    def __init__(self, calls):
        self.calls, self.report = calls, {"mode": "full", "waited_s": 0.0}
        calls.append("borrow")

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.calls.append("borrow_end")
        return False

    def check(self):
        self.calls.append("real_input")
        return 0x10

    def abort(self):
        pass


@pytest.fixture
def env(monkeypatch):
    calls = Calls()
    p = {"abort": lambda: None, "borrow": lambda: FakeBorrow(calls),
         "background_target": calls.hook("background_target"), "scope_focus": {"hwnd": 0x20, "type": "Document"}}
    monkeypatch.setattr(A.uia, "roots_for_window", lambda h: [h])
    monkeypatch.setattr(A.w, "frame_bounds", lambda h: Rect(0, 0, 1000, 1000))
    monkeypatch.setattr(A.bg, "deepest_child_at", lambda top, x, y: 0x30)
    monkeypatch.setattr(A.bg, "thread_focus", lambda top: 0x20)
    monkeypatch.setattr(A.bg, "input_window", lambda top: 0x20)
    monkeypatch.setattr(A.capture, "window_image", lambda h, r=None: None)
    monkeypatch.setattr(A.inp, "click", calls.hook("real_click"))
    monkeypatch.setattr(A.inp, "type_text", calls.hook("real_typing"))
    monkeypatch.setattr(A.inp, "hotkey", calls.hook("real_keys"))
    monkeypatch.setattr(A.time, "sleep", lambda s: None)
    monkeypatch.setattr(A.bg, "is_chromium", lambda h: False)
    monkeypatch.setattr(A, "LANDED_WAIT_S", 0.0)

    class NoGuard:
        def __init__(self, owns):
            self.result = bg.GuardResult()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(A.bg, "ForegroundGuard", NoGuard)
    return calls, p


POINT = Target("point", x=50, y=60)


def test_background_click_posts_and_never_uses_real_input(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_click", calls.hook("posted_click"))
    monkeypatch.setattr(A.bg, "is_chromium", lambda h: False)
    out = A.Ladder(FakeScope(), p, "background").press(None, POINT).to_dict()
    assert out["method"] == "posted_click" and out["background"]
    assert "real_input" not in calls and "real_click" not in calls
    assert calls.count("posted_click") == 1


def test_unreachable_window_falls_back_to_real_click_only_in_auto(env, monkeypatch):
    calls, p = env

    def unsupported(*a, **k):
        raise bg.BackgroundUnsupported("CoreWindow ignores posted mouse input")

    monkeypatch.setattr(A.bg, "post_click", unsupported)
    with pytest.raises(A.NeedsRealInput):
        A.Ladder(FakeScope(), p, "background").press(None, POINT)
    assert "real_click" not in calls
    out = A.Ladder(FakeScope(), p, "auto").press(None, POINT).to_dict()
    assert out["method"] == "real_click" and not out["background"]
    assert calls.index("real_input") < calls.index("real_click")


def test_chromium_gets_exactly_one_retry_when_nothing_visibly_changed(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_click", calls.hook("posted_click"))
    monkeypatch.setattr(A.bg, "is_chromium", lambda h: True)
    out = A.Ladder(FakeScope(), p, "auto").press(None, POINT).to_dict()
    assert calls.count("posted_click") == 2
    assert "real_click" not in calls  # an invisible effect is not retried with the person's mouse
    assert "retry with method='input'" in out["note"]


def test_pressable_element_uses_uia_patterns_first(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.uia, "invoke", calls.hook("uia_invoke"))
    monkeypatch.setattr(A.bg, "post_click", calls.hook("posted_click"))
    target = Target("element", rect=Rect(0, 0, 10, 10), control_type="Button", name="OK")
    out = A.Ladder(FakeScope(), p, "background").press(object(), target).to_dict()
    assert out["method"] == "uia_invoke" and calls == ["uia_invoke"]


def test_typing_posts_chars_to_the_scopes_focus_window(env, monkeypatch):
    calls, p = env
    sent = []
    monkeypatch.setattr(A.bg, "post_text", lambda h, text, abort=None: sent.append((h, text)))
    reads = iter(["hello", "hello world"])
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: next(reads)))
    out = A.Ladder(FakeScope(), p, "background").type_text(None, " world").to_dict()
    assert sent == [(0x20, " world")] and out["method"] == "posted_chars"
    assert "real_input" not in calls


def test_typing_falls_back_only_when_read_back_proves_nothing_arrived(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    monkeypatch.setattr(A.bg, "is_edit_class", lambda h: False)
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: "unchanged"))
    with pytest.raises(A.NeedsRealInput):
        A.Ladder(FakeScope(), p, "background").type_text(None, "abc")
    assert "real_typing" not in calls
    out = A.Ladder(FakeScope(), p, "auto").type_text(None, "abc").to_dict()
    assert out["method"] == "real_typing" and "real_typing" in calls


def test_slow_app_is_given_time_before_concluding_nothing_arrived(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    monkeypatch.setattr(A, "LANDED_WAIT_S", 5.0)
    reads = iter(["x = 1", "x = 1", "x = 1", "x = 1y = 2"])
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: next(reads)))
    out = A.Ladder(FakeScope(), p, "auto").type_text(None, "y = 2").to_dict()
    assert out["method"] == "posted_chars" and "real_typing" not in calls


def test_typing_never_double_types_when_read_back_is_unavailable(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: None))
    out = A.Ladder(FakeScope(), p, "auto").type_text(None, "abc").to_dict()
    assert out["method"] == "posted_chars" and "real_typing" not in calls


def test_typing_checks_the_target_window_as_a_typing_target(env, monkeypatch):
    calls, p = env
    seen = []
    p["background_target"] = lambda h, typing=False: seen.append((h, typing))
    monkeypatch.setattr(A.bg, "post_text", lambda *a, **k: None)
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: None))
    A.Ladder(FakeScope(), p, "background").type_text(None, "x")
    assert seen == [(0x20, True)]


def test_hotkey_is_posted_and_never_retried_with_real_keys(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_hotkey", calls.hook("posted_keys"))
    out = A.Ladder(FakeScope(), p, "auto").hotkey("ctrl+s").to_dict()
    assert out["method"] == "posted_keys" and "real_keys" not in calls


def test_input_method_goes_straight_to_real_input(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_click", calls.hook("posted_click"))
    out = A.Ladder(FakeScope(), p, "input").press(None, POINT).to_dict()
    assert out["method"] == "real_click" and "posted_click" not in calls


def test_empty_document_counts_as_readable(monkeypatch):
    monkeypatch.setattr(A.uia, "read", lambda el: {"name": "Text editor", "value": ""})
    assert A.Ladder._read(object(), 0) == ""
    assert A._landed("", "abc", "abc") is True


def test_landed_detection():
    assert A._landed("ab", "abxyz", "xyz") is True
    assert A._landed("ab", "ab", "xyz") is False
    assert A._landed(None, "ab", "x") is None
    assert A._landed("ab", "ab\rcd", "\ncd") is True
    assert A._landed("ab", "abyxz", "xyz") == A.MISMATCH  # changed, but not into the typed text



def _stealing_guard(monkeypatch):
    class StealingGuard:
        def __init__(self, owns):
            self.result = bg.GuardResult(stolen=True, restored=True)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(A.bg, "ForegroundGuard", StealingGuard)


def test_a_focus_steal_is_restored_and_reported_but_not_refused(env, monkeypatch):
    calls, p = env
    _stealing_guard(monkeypatch)
    monkeypatch.setattr(A.uia, "invoke", calls.hook("uia_invoke"))
    for _ in range(2):
        out = A.Ladder(FakeScope(), p, "background").pattern("uia_invoke", lambda: A.uia.invoke(None))
        assert out.focus == {"stolen": True, "restored": True, "person_active": False}
    assert calls.count("uia_invoke") == 2


def test_background_chromium_typing_is_not_trusted_to_posted_messages(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "is_chromium", lambda h: True)
    monkeypatch.setattr(A.Ladder, "_scope_in_front", lambda self: False)
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    with pytest.raises(A.NeedsRealInput):
        A.Ladder(FakeScope(), p, "background").type_text(None, "abc")
    assert "posted_chars" not in calls
    out = A.Ladder(FakeScope(), p, "auto").type_text(None, "abc").to_dict()
    assert out["method"] == "real_typing"
    monkeypatch.setattr(A.Ladder, "_scope_in_front", lambda self: True)
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: None))
    out = A.Ladder(FakeScope(), p, "background").type_text(None, "abc").to_dict()
    assert out["method"] == "posted_chars"  # in front, posted characters are fine


def test_real_typing_opens_the_borrow_before_focusing_anything(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.uia, "focus", calls.hook("uia_focus"))
    out = A.Ladder(FakeScope(), p, "input").type_text(object(), "abc").to_dict()
    assert calls.index("borrow") < calls.index("uia_focus") < calls.index("real_input") \
        < calls.index("real_typing") < calls.index("borrow_end")
    assert out["borrow"] == {"mode": "full", "waited_s": 0.0}


def test_a_refused_input_mode_leaves_the_persons_foreground_alone(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.uia, "focus", calls.hook("uia_focus"))

    def never():
        raise A.NeedsRealInput("input mode is never")

    p["borrow"] = never
    with pytest.raises(A.NeedsRealInput):
        A.Ladder(FakeScope(), p, "input").type_text(object(), "abc")
    assert "uia_focus" not in calls and "real_typing" not in calls


def test_real_clicks_and_keys_happen_inside_a_borrow(env):
    calls, p = env
    A.Ladder(FakeScope(), p, "input").press(None, POINT)
    A.Ladder(FakeScope(), p, "input").hotkey("ctrl+s")
    assert calls == ["borrow", "real_input", "real_click", "borrow_end",
                     "borrow", "real_input", "real_keys", "borrow_end"]


def test_edit_controls_get_the_whole_text_in_one_message(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "is_edit_class", lambda h: True)
    monkeypatch.setattr(A.bg, "replace_selection", calls.hook("replace_selection"))
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    reads = iter(["x = 1", "x = 1{y}"])
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: next(reads)))
    out = A.Ladder(FakeScope(), p, "background").type_text(None, "{y}").to_dict()
    assert out["method"] == "replace_selection" and "posted_chars" not in calls


def test_line_breaks_go_as_posted_characters_so_enter_keeps_its_meaning(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "is_edit_class", lambda h: True)
    monkeypatch.setattr(A.bg, "replace_selection", calls.hook("replace_selection"))
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    reads = iter(["", "a\rb"])
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: next(reads)))
    out = A.Ladder(FakeScope(), p, "background").type_text(None, "a\nb").to_dict()
    assert out["method"] == "posted_chars" and "replace_selection" not in calls


def test_text_that_arrives_garbled_is_an_error_not_a_success(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    monkeypatch.setattr(A, "LANDED_WAIT_S", 0.0)
    reads = iter(["print() ", "print() x{}"])  # typed "{x}", characters swapped on the way
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: next(reads)))
    with pytest.raises(A.TypingMismatch):
        A.Ladder(FakeScope(), p, "auto").type_text(None, "{x}")
    assert "real_typing" not in calls  # part of it arrived, so nothing is typed again


def test_partial_text_is_waited_for_not_reported_as_garbled(env, monkeypatch):
    calls, p = env
    monkeypatch.setattr(A.bg, "post_text", calls.hook("posted_chars"))
    monkeypatch.setattr(A, "LANDED_WAIT_S", 5.0)
    reads = iter(["x = 1", "x = 1y", "x = 1y =", "x = 1y = 2"])
    monkeypatch.setattr(A.Ladder, "_read", staticmethod(lambda el, h: next(reads)))
    out = A.Ladder(FakeScope(), p, "auto").type_text(None, "y = 2").to_dict()
    assert out["method"] == "posted_chars" and out["attempts"][-1]["detail"] == "read back"
