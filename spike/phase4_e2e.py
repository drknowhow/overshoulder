"""Phase 4 end to end: a headless Claude agent works a window whose buttons are only painted pixels, over MCP,
with text refs, vision refs, a consequential click that asks, and the check of the target's own area.

A separate daemon on its own port and data directory leaves the person's real Overshoulder setup alone. Its
settings: input mode "never", OCR on, the local vision model on. This script plays the person at the desk:
it watches for the daemon's approval boxes, screenshots each one, and answers No. Afterwards it reads the
drawn app's own record of what was clicked.

    python spike/phase4_e2e.py [--model sonnet] [--evidence]
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "spike"))
import bghelpers as H  # noqa: E402
from phase2_e2e_never import wait_port, wait_window  # noqa: E402
from phase3_e2e import read_json, scrub  # noqa: E402

import overshoulder  # noqa: E402,F401  (DPI awareness)
from overshoulder import vision  # noqa: E402
from overshoulder.approvals import BOX_TITLE  # noqa: E402
from overshoulder.driver import capture  # noqa: E402
from overshoulder.driver import windows as w  # noqa: E402
from overshoulder.gateway.clients import ClientRegistry  # noqa: E402

PORT = 47699
EVIDENCE = ROOT / "docs" / "evidence" / "phase4-e2e"
WM_CLOSE, WM_COMMAND, IDNO = 0x0010, 0x0111, 7

PROMPT = """You are testing Overshoulder, a desktop-control MCP server, as an ordinary agent. Use ONLY the
overshoulder tools. The person at this PC is working in another window and has set Overshoulder's input mode
to "never", so work in the background. A window titled "Overshoulder drawn test" is open. Its buttons are
painted pixels, so UI Automation does not see them. Do these in order, and keep a short log of every tool
result that matters:

1. status(): record input_mode.
2. windows(): find the drawn test window's hwnd. take_scope("window:<hwnd>").
3. observe(text=true): record how many elements it lists and every text it read, with ref and source.
4. find(text="Save"), then act(action="click", ref=<that ref>, method="background",
   expect={{"text_appears": "Clicked Save", "area_changed": true}}). Record verify.method, verify.area,
   verify.expect and verify.text_appeared.
5. find(describe='the "Open" button'). Record ref, rect, confirmed, confirmed_by and model. Then
   act(action="click", ref=<that ref>, method="background", expect={{"area_changed": true}}) and record
   whether it asked the person, verify.area and verify.expect.
6. find(describe="the small green square in the bottom-right corner"). Record ref, rect, confirmed and
   confirmed_by. Then act(action="click", ref=<that ref>, method="background"). Record the result or the
   error code and message.
7. find(text="Delete"), then act(action="click", ref=<that ref>, method="background"). Record the result or
   the error code and message.
8. release_scope().

When an action needs the person's approval, the person decides; record what came back. Do not try to work
around refusals; a refusal is a valid result. Finish with a compact report: for each step, ok or the error
code, and the values read back."""

user32 = ctypes.windll.user32
user32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
EnumChildProc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)


def descendants(pid: int) -> set[int]:
    query = 'Get-CimInstance Win32_Process | ForEach-Object { "$($_.ProcessId) $($_.ParentProcessId)" }'
    out = subprocess.run(["powershell.exe", "-NoProfile", "-Command", query],
                         capture_output=True, text=True, check=True).stdout
    kids: dict[int, list[int]] = {}
    for line in out.split("\n"):
        if line.strip():
            p, parent = (int(v) for v in line.split())
            kids.setdefault(parent, []).append(p)
    found, todo = {pid}, [pid]
    while todo:
        for k in kids.get(todo.pop(), []):
            if k not in found:
                found.add(k)
                todo.append(k)
    return found


def box_text(hwnd: int) -> str:
    """The message of a standard message box: the text of its Static children."""
    texts = []

    @EnumChildProc
    def cb(h, _):
        if w.class_name(h) == "Static" and w.title(h):
            texts.append(w.title(h))
        return True

    user32.EnumChildWindows(hwnd, cb, 0)
    return "\n".join(texts)


class Person(threading.Thread):
    """Answers the daemon's approval boxes with No, after a screenshot of each."""

    def __init__(self, pids: set[int], shots: Path):
        super().__init__(daemon=True)
        self.pids, self.shots, self.stop, self.boxes, self.seen = pids, shots, False, [], set()

    def run(self):
        while not self.stop:
            for x in w.top_windows():
                if x.title != BOX_TITLE or x.pid not in self.pids or x.hwnd in self.seen:
                    continue
                self.seen.add(x.hwnd)
                time.sleep(0.8)  # let it paint
                kind = "message box" if w.class_name(x.hwnd) == "#32770" else "picture box"
                n = len(self.boxes) + 1
                shot = capture.window_image(x.hwnd, w.frame_bounds(x.hwnd))
                name = f"box-{n}.png"
                if shot is not None:
                    shot.save(self.shots / name)
                text = box_text(x.hwnd) if kind == "message box" else ""
                if kind == "message box":
                    user32.PostMessageW(x.hwnd, WM_COMMAND, IDNO, 0)
                else:
                    user32.PostMessageW(x.hwnd, WM_CLOSE, 0, 0)  # the window's X, which is No
                self.boxes.append({"n": n, "kind": kind, "text": text, "answered": "No",
                                   "screenshot": name if shot is not None else None, "at": round(time.time(), 1)})
            time.sleep(0.1)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--evidence", action="store_true")
    a = ap.parse_args()
    if any(x.title == "Overshoulder drawn test" for x in w.top_windows()):
        raise SystemExit("a drawn test window is already open; close it first")

    home = Path(tempfile.mkdtemp(prefix="os_e2e4_home_"))
    (home / "settings.json").write_text(json.dumps({"input_mode": "never", "ocr_enabled": True,
                                                    "vision_mode": "local"}), encoding="utf-8")
    token = ClientRegistry(home / "clients.json").add("e2e-agent")
    work = Path(tempfile.mkdtemp(prefix="os_e2e4_work_"))
    shots = work / "boxes"
    shots.mkdir()
    cfg = work / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"overshoulder": {
        "type": "http", "url": f"http://127.0.0.1:{PORT}/mcp",
        "headers": {"Authorization": f"Bearer {token}"}}}}), encoding="utf-8")
    drawn_out = work / "drawn.json"

    env = {**os.environ, "OVERSHOULDER_HOME": str(home)}
    daemon = subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "overshoulder.exe"), "--port", str(PORT), "serve"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc, hwnd, person = None, None, None
    try:
        wait_port(PORT)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 4  # SW_SHOWNOACTIVATE
        proc = subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                 str(ROOT / "tests" / "drawnapp.ps1"), str(drawn_out)],
                                startupinfo=si, creationflags=0x08000000)  # CREATE_NO_WINDOW: no console
        hwnd = wait_window(lambda t: t == "Overshoulder drawn test")
        read_json(drawn_out, "ready")
        time.sleep(1.0)

        person = Person(descendants(daemon.pid), shots)
        person.start()
        counter = H.InjectedCounter()
        counter.injected = counter.physical = 0
        cursor0 = H.cursor()
        t0 = time.time()
        run = subprocess.run(
            ["claude", "-p", "--model", a.model, "--mcp-config", str(cfg), "--strict-mcp-config",
             "--allowedTools", "mcp__overshoulder"],
            input=PROMPT, cwd=str(work), capture_output=True, text=True, encoding="utf-8", timeout=1500)
        took = time.time() - t0
        person.stop = True
        person.join(2)

        done = json.loads(drawn_out.read_text(encoding="utf-8-sig"))
        result = {"agent_seconds": round(took, 1), "agent_exit_code": run.returncode,
                  "injected_input_events": counter.injected, "physical_input_events": counter.physical,
                  "cursor_moved": H.cursor() != cursor0, "clicked": done.get("clicked"),
                  "click_points": done.get("points"), "approval_boxes": person.boxes,
                  "delete_clicked": "Delete" in (done.get("clicked") or [])}
        report = run.stdout.strip() or run.stderr.strip()[-2000:]
        names = ((home, "<test home>"), (work, "<work>"), (Path.home(), "<user home>"))
        print(json.dumps(result, indent=2))
        print("---- agent report ----")
        print(report)
        if a.evidence:
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            (EVIDENCE / "result.json").write_text(scrub(json.dumps(result, indent=2), *names) + "\n", encoding="utf-8")
            (EVIDENCE / "agent-report.md").write_text(scrub(report, *names) + "\n", encoding="utf-8")
            (EVIDENCE / "prompt.md").write_text(PROMPT + "\n", encoding="utf-8")
            for b in person.boxes:
                if b["screenshot"]:
                    (EVIDENCE / b["screenshot"]).write_bytes((shots / b["screenshot"]).read_bytes())
            log = sorted((home / "logs").glob("actions-*.jsonl"))
            if log:
                text = log[-1].read_text(encoding="utf-8")
                (EVIDENCE / "actions.jsonl").write_text(scrub(text, *names), encoding="utf-8")
            print("evidence:", EVIDENCE)
    finally:
        if person is not None:
            person.stop = True
        if hwnd:
            try:
                H.close_discarding(hwnd)
            except Exception:
                pass
        if proc is not None and proc.poll() is None:
            proc.terminate()
        daemon.terminate()
        cfg.unlink(missing_ok=True)  # holds the test client's token
        try:
            vision.OllamaLocator().unload()  # give the graphics memory back now
        except vision.VisionError:
            pass


if __name__ == "__main__":
    main()
