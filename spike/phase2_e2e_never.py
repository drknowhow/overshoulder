"""Phase 2 end to end: a headless Claude agent does tasks in three background windows over MCP, with the
person's input mode set to "never", while a sampler watches the person's side of the desk.

A separate daemon runs on its own port with its own data directory (settings: input_mode "never"), so
the person's real Overshoulder setup is untouched. A sentinel window stands in for the person's work and
holds the foreground. Every 50 ms the sampler records the foreground window and the cursor position;
low-level hooks count injected and physical input for the whole run.

    python spike/phase2_e2e_never.py [--model sonnet] [--evidence]
"""

import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import bghelpers as H  # noqa: E402

import overshoulder  # noqa: E402,F401  (DPI awareness)
from overshoulder.driver import windows as w  # noqa: E402
from overshoulder.gateway.clients import ClientRegistry  # noqa: E402

PORT = 47699
FILE_NAME = "e2e_never.txt"
TYPED = "Typed in the background by an agent (6 * 7 = 42)"

PROMPT = f"""You are testing Overshoulder, a desktop-control MCP server, as an ordinary agent. Use ONLY the
overshoulder tools. The person at this PC is working in another window and has set Overshoulder's input mode
to "never": agents must work in the background and never use their mouse or keyboard. Three windows are open
behind theirs: a Notepad editing "{FILE_NAME}", Calculator, and Character Map. Do these in order, and keep a
short log of every tool result that matters:

1. status(): record input_mode.
2. windows(), then take_scope("window:<hwnd>") on the Notepad whose title contains e2e_never.
   find(control_type="Document"), then act(action="type", ref=<its ref>, text="{TYPED}",
   expect={{"text_contains": "Typed in the background"}}), then act(action="hotkey", combo="ctrl+s").
   read the Document and record what it shows. release_scope().
3. take_scope on Calculator. act(action="type", text="12*12=") with no ref (the keys go to Calculator itself).
   read the element with auto_id CalculatorResults and record what it shows. Then find(auto_id="num7Button")
   and try act(action="click", x=<the centre x of its rect>, y=<the centre y of its rect>). Record the result
   or the error code exactly. release_scope().
4. take_scope on Character Map. find(control_type="Edit") (the "Characters to copy" box) and
   act(action="set_value", ref=<its ref>, value="Overshoulder"). Then find(name="Select", control_type="Button")
   and act(action="click", ref=<its ref>). read the Edit box and record its value. release_scope().

Every act result has result.method and result.background; record them. Do not try to work around refusals; a
refusal is a valid result. Finish with a compact report: for each step, ok or the error code, the methods
used, and the values read back."""


class Sampler(threading.Thread):
    def __init__(self, period: float = 0.05):
        super().__init__(daemon=True)
        self.period, self.samples, self.stop = period, [], False

    def run(self):
        while not self.stop:
            self.samples.append((time.time(), H.user32.GetForegroundWindow(), H.cursor()))
            time.sleep(self.period)


def wait_window(match, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        hits = [x.hwnd for x in w.top_windows() if match(x.title)]
        if hits:
            return hits[0]
        time.sleep(0.25)
    raise SystemExit("window did not appear")


def wait_port(port, timeout=30):
    end = time.time() + timeout
    while time.time() < end:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.25)
    raise SystemExit("test daemon did not start")


def summarize(samples, sentinel, cursor0):
    away = [(t, fg) for t, fg, _ in samples if fg != sentinel]
    excursions, start = [], None
    for (t, fg, _), nxt in zip(samples, samples[1:] + [None], strict=True):
        if fg != sentinel and start is None:
            start = (t, fg)
        if start and (nxt is None or nxt[1] == sentinel):
            excursions.append({"from_s": round(start[0] - samples[0][0], 2),
                               "seconds": round((nxt[0] if nxt else t) - start[0], 2),
                               "window": w.title(start[1]) or w.class_name(start[1])})
            start = None
    cursors = sorted({c for _, _, c in samples})
    return {"samples": len(samples), "seconds": round(samples[-1][0] - samples[0][0], 1),
            "period_ms": 50, "foreground_was_the_persons_window": len(away) == 0,
            "samples_with_another_foreground": len(away), "foreground_excursions": excursions,
            "cursor_positions_seen": [list(c) for c in cursors],
            "cursor_unchanged": cursors == [cursor0]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--evidence", action="store_true")
    a = ap.parse_args()
    for exe in ("notepad.exe", "CalculatorApp.exe", "charmap.exe"):
        if exe.lower() in subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower():
            raise SystemExit(f"{exe} is already running; close it first so the test cannot touch your window")

    home = Path(tempfile.mkdtemp(prefix="os_e2e_home_"))
    (home / "settings.json").write_text(json.dumps({"input_mode": "never"}), encoding="utf-8")
    token = ClientRegistry(home / "clients.json").add("e2e-agent")
    work = Path(tempfile.mkdtemp(prefix="os_e2e_work_"))
    cfg = work / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"overshoulder": {
        "type": "http", "url": f"http://127.0.0.1:{PORT}/mcp",
        "headers": {"Authorization": f"Bearer {token}"}}}}), encoding="utf-8")
    note = work / FILE_NAME
    note.write_text("", encoding="utf-8")

    env = {**os.environ, "OVERSHOULDER_HOME": str(home)}
    daemon = subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "overshoulder.exe"), "--port", str(PORT), "serve"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    windows = []
    try:
        wait_port(PORT)
        H.launch_noactivate(["notepad.exe", str(note)])
        windows.append(wait_window(lambda t: "e2e_never" in t))
        H.launch_noactivate(["calc.exe"])
        windows.append(wait_window(lambda t: t == "Calculator"))
        cm = H.launch_noactivate(["charmap.exe"])
        windows.append(wait_window(lambda t: t == "Character Map"))
        time.sleep(5.0)  # Calculator takes the foreground when it starts; the sentinel takes it back below

        counter = H.InjectedCounter()
        sentinel = H.start_sentinel()
        assert H.take_foreground(sentinel), "the sentinel could not take the foreground"
        time.sleep(0.5)
        counter.injected = counter.physical = 0
        cursor0 = H.cursor()
        sampler = Sampler()
        sampler.start()
        t0 = time.time()
        run = subprocess.run(
            ["claude", "-p", "--model", a.model, "--mcp-config", str(cfg), "--strict-mcp-config",
             "--allowedTools", "mcp__overshoulder"],
            input=PROMPT, cwd=str(work), capture_output=True, text=True, encoding="utf-8", timeout=1200)
        took = time.time() - t0
        sampler.stop = True
        sampler.join(2)
        summary = summarize(sampler.samples, sentinel, cursor0)
        summary.update({"injected_input_events": counter.injected, "physical_input_events": counter.physical,
                        "agent_seconds": round(took, 1), "agent_exit_code": run.returncode,
                        "file_on_disk": note.read_text(encoding="utf-8")})
        print(json.dumps(summary, indent=2))
        print("---- agent report ----")
        print(run.stdout.strip() or run.stderr.strip()[-2000:])
        if a.evidence:
            dest = ROOT / "docs" / "evidence" / "phase2-e2e-never"
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "sampler.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
            (dest / "agent-report.md").write_text(run.stdout.strip() + "\n", encoding="utf-8")
            (dest / "prompt.md").write_text(PROMPT + "\n", encoding="utf-8")
            log = sorted((home / "logs").glob("actions-*.jsonl"))
            if log:
                shutil.copy2(log[-1], dest / "actions.jsonl")
            print("evidence:", dest)
    finally:
        for h in windows:
            try:
                H.close_discarding(h)
            except Exception:
                pass
        daemon.terminate()
        cfg.unlink(missing_ok=True)  # holds the test client's token
        if "cm" in locals() and cm.poll() is None:
            cm.terminate()


if __name__ == "__main__":
    main()
