"""Record a background-mode Calculator session and render it as a tutorial with the agent's pointer.

A sentinel window keeps the foreground (standing in for the person's own work); every action uses
method="background". Prints the tutorial paths and the injected-input count, cursor and foreground
checks, and writes them to docs/evidence/phase2-pointer/ when --evidence is passed.
"""

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "tests"))
import bghelpers as H  # noqa: E402

import overshoulder  # noqa: E402,F401
from overshoulder import settings as st  # noqa: E402
from overshoulder.gateway.core import gateway_for_tests  # noqa: E402
from overshoulder.service import Service  # noqa: E402


def main(evidence: bool) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="os_pointer_"))
    s = Service(gateway_for_tests(tmp, settings_source=lambda: st.Settings(input_mode="never")),
                shots_dir=tmp / "shots")
    token = s.gw.registry.add("tutorial-agent")
    counter = H.InjectedCounter()
    sentinel = H.start_sentinel()
    H.launch_noactivate(["calc.exe"])
    hwnd = None
    for _ in range(120):
        from overshoulder.driver import windows as w
        hits = [x.hwnd for x in w.top_windows() if x.title == "Calculator"]
        if hits:
            hwnd = hits[0]
            break
        time.sleep(0.25)
    time.sleep(5.0)
    try:
        lease = s.acquire(token, f"window:{hwnd}", log_text=True)["lease_id"]
        s.act(token, lease, "invoke", ref=s.find(token, lease, auto_id="clearButton")[0]["ref"],
              method="background")
        assert H.take_foreground(sentinel)
        time.sleep(1.5)  # the recording takes actions from a second before it starts; keep Clear out
        counter.injected = 0
        cursor = H.cursor()
        s.record_start(token, lease, fps=10, name="calc-6x7")
        time.sleep(1.0)
        for auto_id in ("num7Button", "multiplyButton", "num6Button", "equalButton"):
            ref = s.find(token, lease, auto_id=auto_id)[0]["ref"]
            s.act(token, lease, "invoke", ref=ref, method="background")
            time.sleep(1.2)
        time.sleep(1.0)
        out = s.record_stop(token, lease, render=True)
        shown = s.read(token, lease, s.find(token, lease, auto_id="CalculatorResults")[0]["ref"])["name"]
        result = {
            "what": "Calculator 7 x 6 = in input mode 'never', every action method='background', with a sentinel "
                    "window holding the foreground; rendered with the agent's pointer",
            "calculator_shows": shown,
            "injected_input_events": counter.injected,
            "cursor_unchanged": H.cursor() == cursor,
            "persons_window_in_front": H.user32.GetForegroundWindow() == sentinel,
            "tutorial": out.get("tutorial"),
        }
        print(json.dumps(result, indent=2))
        if evidence:
            dest = Path(__file__).parents[1] / "docs" / "evidence" / "phase2-pointer"
            dest.mkdir(parents=True, exist_ok=True)
            tut = Path(out["tutorial"]["video"]).parent
            for f in tut.iterdir():
                if f.suffix in (".png", ".md", ".mp4"):
                    shutil.copy2(f, dest / f.name)
            (dest / "session.json").write_text(json.dumps({k: v for k, v in result.items() if k != "tutorial"},
                                                          indent=2), encoding="utf-8")
            print("evidence:", dest)
    finally:
        H.close_discarding(hwnd)


if __name__ == "__main__":
    main("--evidence" in sys.argv)
