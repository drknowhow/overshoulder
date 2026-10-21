"""Calculator re-measure for the phase 2 spike: wait out its launch activation, then measure."""

import json
import time

import phase2_background as S


def main():
    S.COUNTER = S.InjectedCounter()
    S.SENTINEL = S.start_sentinel()
    S.force_foreground(S.SENTINEL)
    S.launch_noactivate(["calc.exe"])
    win = S.wait_window(r"^Calculator$")
    time.sleep(5.0)
    stole = S.user32.GetForegroundWindow() == win.hwnd
    print("calc took the foreground at launch:", stole, flush=True)
    S.ROWS.append({"app": "calc", "action": "launch", "method": "SW_SHOWNOACTIVATE", "effect": not stole,
                   "detail": "took the foreground" if stole else "stayed in the background"})
    S.force_foreground(S.SENTINEL)
    time.sleep(0.5)
    h = win.hwnd
    core = S.child_by_class(h, "Windows.UI.Core.CoreWindow")

    def display():
        el = S.find_el(h, auto_id="CalculatorResults")
        return S.uia.read(el)["name"] if el else ""

    def inv(a):
        S.uia.invoke(S.find_el(h, auto_id=a))

    def post_click_button(aid):
        r = S.uia.element_state(S.find_el(h, auto_id=aid))["rect"]
        S.post_click(h, *r.center())

    S.measure("calc", "press buttons", "UIA Invoke",
              lambda: [inv(a) for a in ("clearButton", "num7Button", "multiplyButton", "num6Button", "equalButton")],
              lambda: (display().strip().endswith("42"), display()))
    S.measure("calc", "type digits", "WM_CHAR post to CoreWindow",
              lambda: (inv("clearButton"), time.sleep(0.3), S.post_chars(core, "58")),
              lambda: (display().strip().endswith("58"), display()))
    S.measure("calc", "press button", "posted click",
              lambda: (inv("clearButton"), time.sleep(0.3), post_click_button("num9Button")),
              lambda: (display().strip().endswith("9"), display()))
    S.close_window(h)
    (S.OUT / "phase2_calc.json").write_text(json.dumps(S.ROWS, indent=1), encoding="utf-8")


if __name__ == "__main__":
    main()
