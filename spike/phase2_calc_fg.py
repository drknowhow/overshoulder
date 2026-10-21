"""Why does Calculator end up in front? Step by step, with the foreground logged after each step."""

import time

import phase2_background as S


def fg_name():
    h = S.user32.GetForegroundWindow()
    return f"{S.w.title(h)!r}" if h else "none"


def main():
    S.COUNTER = S.InjectedCounter()
    S.SENTINEL = S.start_sentinel()
    print("sentinel forced:", S.force_foreground(S.SENTINEL), fg_name(), flush=True)
    S.launch_noactivate(["calc.exe"])
    win = S.wait_window(r"^Calculator$")
    for i in range(6):
        time.sleep(1)
        print(f"t+{i + 1}s fg={fg_name()}", flush=True)
    print("re-force sentinel:", S.force_foreground(S.SENTINEL), fg_name(), flush=True)
    for i in range(3):
        time.sleep(1)
        print(f"idle t+{i + 1}s fg={fg_name()}", flush=True)
    h = win.hwnd
    el = S.find_el(h, auto_id="num3Button")
    print("found button; fg:", fg_name(), flush=True)
    S.uia.invoke(el)
    time.sleep(0.8)
    print("after one UIA Invoke fg:", fg_name(), "injected:", S.COUNTER.injected, flush=True)
    S.force_foreground(S.SENTINEL)
    core = S.child_by_class(h, "Windows.UI.Core.CoreWindow")
    S.post_chars(core, "4")
    time.sleep(0.8)
    print("after WM_CHAR fg:", fg_name(), flush=True)
    S.close_window(h)


if __name__ == "__main__":
    main()
