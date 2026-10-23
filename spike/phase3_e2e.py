"""Phase 3 end to end: a headless Claude agent narrows its own session, drags in the background and records
the drag as a tutorial with a Shotcut project, over MCP, with the person's input mode set to "never".

Like phase2_e2e_never.py: a separate daemon on its own port and data directory leaves the person's real
Overshoulder setup alone, a sentinel window holds the foreground for the person, and a sampler watches
the foreground window and the cursor every 50 ms. Afterwards this script checks where the box landed,
renders the agent's tutorial.mlt with Shotcut's melt and compares it with tutorial.mp4 frame by frame.
Phone approvals are proven against the live gateway instead, because they need the person's phone.

    python spike/phase3_e2e.py [--model sonnet] [--evidence]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "spike"))
import bghelpers as H  # noqa: E402
from phase2_e2e_never import Sampler, summarize, wait_port, wait_window  # noqa: E402
from test_shotcut import MELT  # noqa: E402

import overshoulder  # noqa: E402,F401  (DPI awareness)
from overshoulder.driver import windows as w  # noqa: E402
from overshoulder.gateway.clients import ClientRegistry  # noqa: E402

PORT = 47698
EVIDENCE = ROOT / "docs" / "evidence" / "phase3-e2e"

PROMPT = """You are testing Overshoulder, a desktop-control MCP server, as an ordinary agent. Use ONLY the
overshoulder tools. The person at this PC is working in another window and has set Overshoulder's input mode
to "never": agents must work in the background and never use their mouse or keyboard. Two windows are open
behind theirs: "Overshoulder drag test", a window with one blue box you can drag (the box's centre is at
screen pixel ({x0}, {y0}); the drop point is ({x1}, {y1})), and Character Map. Do these in order, and keep a
short log of every tool result that matters:

1. status(): record input_mode.
2. windows(): note both windows' hwnds. take_scope("window:<drag test hwnd>", read_only=true), then try
   act(action="click", x={x0}, y={y0}). Record the error code and the limit it names. release_scope().
3. take_scope("window:<Character Map hwnd>", deny_processes=["charmap.exe"]). Record the result or the
   error code. If it gave you a scope, release_scope().
4. take_scope("window:<drag test hwnd>", log_text=true, deny_processes=["charmap.exe"]). Record the limits
   in the result. Call windows() and record whether Character Map is listed.
5. record_start(name="drag-box"). act(action="drag", x={x0}, y={y0}, to_x={x1}, to_y={y1},
   method="background"); record result.method, result.background and the verify result. Then
   record_stop(narrate=true, gif=false, mlt=true) and record the tutorial's video, project and frames.
6. release_scope().

Do not try to work around refusals; a refusal is a valid result. Finish with a compact report: for each step,
ok or the error code, the methods used, and the values read back."""


def read_json(path: Path, key: str, timeout: float = 15.0) -> dict:
    end = time.time() + timeout
    while time.time() < end:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if data.get(key):
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise SystemExit(f"{path.name} never had {key}")


def psnr_per_frame(a: Path, b: Path) -> list[float]:
    out = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(a), "-i", str(b), "-lavfi",
                          "[0:v][1:v]psnr=stats_file=-", "-f", "null", "-"], capture_output=True, text=True,
                         check=True).stdout
    return [float(x.split(":")[1]) for line in out.splitlines() for x in line.split() if x.startswith("psnr_avg:")]


def compare_sheet(melt: Path, ours: Path, frames: list[int], dest: Path) -> None:
    """Melt's render on the left, tutorial.mp4 on the right, one row per frame."""
    pick = "+".join(f"eq(n\\,{n})" for n in frames)
    tile = f"tile=1x{len(frames)}"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(melt), "-i", str(ours),
                    "-filter_complex", f"[0:v]select={pick},scale=480:-1,{tile}[a];"
                    f"[1:v]select={pick},scale=480:-1,{tile}[b];[a][b]hstack", "-frames:v", "1",
                    "-update", "1", str(dest)], check=True)


def scrub(text: str, *paths: Path) -> str:
    for p, name in paths:
        text = text.replace(str(p), name).replace(str(p).replace("\\", "\\\\"), name)
    return text


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--evidence", action="store_true")
    a = ap.parse_args()
    if "charmap.exe" in subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower():
        raise SystemExit("Character Map is already running; close it first so the test cannot touch your window")

    home = Path(tempfile.mkdtemp(prefix="os_e2e3_home_"))
    (home / "settings.json").write_text(json.dumps({"input_mode": "never"}), encoding="utf-8")
    token = ClientRegistry(home / "clients.json").add("e2e-agent")
    work = Path(tempfile.mkdtemp(prefix="os_e2e3_work_"))
    cfg = work / "mcp.json"
    cfg.write_text(json.dumps({"mcpServers": {"overshoulder": {
        "type": "http", "url": f"http://127.0.0.1:{PORT}/mcp",
        "headers": {"Authorization": f"Bearer {token}"}}}}), encoding="utf-8")
    drag_out = work / "drag.json"

    env = {**os.environ, "OVERSHOULDER_HOME": str(home)}
    daemon = subprocess.Popen([str(ROOT / ".venv" / "Scripts" / "overshoulder.exe"), "--port", str(PORT), "serve"],
                              env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    windows, procs = [], []
    try:
        wait_port(PORT)
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 4  # SW_SHOWNOACTIVATE
        procs.append(subprocess.Popen(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                                       str(ROOT / "tests" / "dragapp.ps1"), str(drag_out)],
                                      startupinfo=si, creationflags=0x08000000))  # CREATE_NO_WINDOW: no console
        windows.append(wait_window(lambda t: t == "Overshoulder drag test"))
        ready = read_json(drag_out, "ready")
        (x0, y0), (x1, y1) = ready["start"], ready["drop"]
        procs.append(H.launch_noactivate(["charmap.exe"]))
        windows.append(wait_window(lambda t: t == "Character Map"))
        time.sleep(2.0)

        counter = H.InjectedCounter()
        sentinel = H.start_sentinel()
        assert H.take_foreground(sentinel), "the sentinel could not take the foreground"
        time.sleep(0.5)
        counter.injected = counter.physical = 0
        cursor0 = H.cursor()
        sampler = Sampler()
        sampler.start()
        on_top = w.window_at(x0, y0)
        covered_by = "" if on_top == windows[0] else (w.title(on_top) or w.class_name(on_top))
        prompt = PROMPT.format(x0=x0, y0=y0, x1=x1, y1=y1)
        t0 = time.time()
        run = subprocess.run(
            ["claude", "-p", "--model", a.model, "--mcp-config", str(cfg), "--strict-mcp-config",
             "--allowedTools", "mcp__overshoulder"],
            input=prompt, cwd=str(work), capture_output=True, text=True, encoding="utf-8", timeout=1200)
        took = time.time() - t0
        sampler.stop = True
        sampler.join(2)

        result = summarize(sampler.samples, sentinel, cursor0)
        result.update({"injected_input_events": counter.injected, "physical_input_events": counter.physical,
                       "agent_seconds": round(took, 1), "agent_exit_code": run.returncode,
                       "drag_start_covered_by": covered_by})
        done = json.loads(drag_out.read_text(encoding="utf-8-sig"))
        want = [30 + x1 - x0, 30 + y1 - y0]
        result["box"] = {"released": bool(done.get("released")), "moves": done.get("moves"),
                         "top_left": done.get("box", [None, None])[:2],
                         "expected_top_left": want, "landed_on_the_drop_point": done.get("box", [0, 0])[:2] == want}

        tutorials = sorted(home.glob("recordings/*/tutorial"))
        tut = tutorials[-1] if tutorials else None
        project = tut / "tutorial.mlt" if tut else None
        result["project_written"] = bool(project and project.is_file())
        if result["project_written"] and MELT:
            melt = work / "melt.mp4"
            subprocess.run([MELT, "-silent", str(project), "-consumer", f"avformat:{melt}", "vcodec=libx264",
                            "crf=12", "pix_fmt=yuv420p", "acodec=aac"], capture_output=True, check=True,
                           cwd=str(tut))
            psnr = psnr_per_frame(melt, tut / "tutorial.mp4")
            result["melt"] = {"frames_compared": len(psnr), "psnr_min_db": round(min(psnr), 1),
                              "psnr_mean_db": round(sum(psnr) / len(psnr), 1)}
            if a.evidence:
                EVIDENCE.mkdir(parents=True, exist_ok=True)
                n = len(psnr)
                compare_sheet(melt, tut / "tutorial.mp4", [n // 5, n // 2, (4 * n) // 5, n - 1],
                              EVIDENCE / "melt-vs-tutorial.png")
        report = run.stdout.strip() or run.stderr.strip()[-2000:]
        names = ((home, "<test home>"), (work, "<work>"), (Path.home(), "<user home>"))
        print(json.dumps(result, indent=2))
        print("---- agent report ----")
        print(report)
        if a.evidence:
            EVIDENCE.mkdir(parents=True, exist_ok=True)
            (EVIDENCE / "result.json").write_text(scrub(json.dumps(result, indent=2), *names) + "\n", encoding="utf-8")
            (EVIDENCE / "agent-report.md").write_text(scrub(report, *names) + "\n", encoding="utf-8")
            (EVIDENCE / "prompt.md").write_text(prompt + "\n", encoding="utf-8")
            log = sorted((home / "logs").glob("actions-*.jsonl"))
            if log:
                text = log[-1].read_text(encoding="utf-8")
                (EVIDENCE / "actions.jsonl").write_text(scrub(text, *names), encoding="utf-8")
            if tut:
                shutil.copy2(tut / "tutorial.mlt", EVIDENCE / "tutorial.mlt")
            print("evidence:", EVIDENCE)
    finally:
        for h in windows:
            try:
                H.close_discarding(h)
            except Exception:
                pass
        for p in procs:
            if p.poll() is None:
                p.terminate()
        daemon.terminate()
        cfg.unlink(missing_ok=True)  # holds the test client's token


if __name__ == "__main__":
    main()
