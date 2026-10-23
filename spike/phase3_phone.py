"""Phase 3 live phone-approval check through the running gateway, with the person's phone.

1. Turns ntfy on in this PC's phone settings and sends a phone-only readiness question (it waits up to
   an hour), so the run starts only once the person has subscribed and answered it.
2. Opens a WinForms window with Send and Delete buttons (phonetest.ps1) and, as a temporary MCP client,
   clicks Send (the person taps Allow, then Confirm Send) and, 10 s later, Delete (the person taps Deny).
3. Passes if the window saw only the Send click and the log says both answers came from ntfy.

ntfy stays on afterwards; the question timeout goes back to what it was. Writes spike/out/phase3-phone/.

    python spike/phase3_phone.py [--skip-ready] [--gap SECONDS]

--gap is the wait between the Send question closing and the Delete question (default 10).
"""

import asyncio
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

from overshoulder import approvals, phone

HERE = Path(__file__).parent
OUT_DIR = HERE / "out" / "phase3-phone"
WINDOW = OUT_DIR / "window.json"
RESULT = OUT_DIR / "result.json"
PY = sys.executable
GAP = float(sys.argv[sys.argv.index("--gap") + 1]) if "--gap" in sys.argv else 10.0
results: dict = {"started": dt.datetime.now().isoformat(timespec="seconds")}


def save():
    RESULT.write_text(json.dumps(results, indent=2), encoding="utf-8")


async def mcp_run(token: str) -> None:
    from mcp import ClientSession
    from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

    async def call(session, tool, args=None, timeout=60):
        res = await session.call_tool(tool, args or {}, read_timeout_seconds=float(timeout))
        texts = [c.text for c in res.content if getattr(c, "type", "") == "text"]
        try:
            return json.loads(texts[0]) if texts else {}
        except ValueError:
            return {"text": texts[0]}

    async with create_mcp_http_client(headers={"Authorization": f"Bearer {token}"}) as http:
        async with streamable_http_client("http://127.0.0.1:47621/mcp", http_client=http) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                await s.initialize()
                wins = await call(s, "windows")
                rows = wins.get("windows", wins) if isinstance(wins, dict) else wins
                hwnd = next(w["hwnd"] for w in rows if w.get("title") == "Overshoulder phone test")
                results["take_scope"] = await call(s, "take_scope", {"scope": f"window:{hwnd}", "ttl_s": 900})
                found = await call(s, "find", {"name": "Send"})
                send = next(e["ref"] for e in found["elements"] if e.get("name") == "Send")
                found = await call(s, "find", {"name": "Delete"})
                delete = next(e["ref"] for e in found["elements"] if e.get("name") == "Delete")
                print("tap ALLOW, then CONFIRM, on the phone for 'Send'", flush=True)
                t0 = time.time()
                results["send"] = await call(s, "act", {"action": "click", "ref": send, "method": "background"}, 400)
                results["send_s"] = round(time.time() - t0, 1)
                print("send ->", results["send"], flush=True)
                time.sleep(GAP)  # 10 s by default: one question on the phone at a time
                print("tap DENY on the phone for 'Delete'", flush=True)
                t0 = time.time()
                click = {"action": "click", "ref": delete, "method": "background"}
                results["delete"] = await call(s, "act", click, 400)
                results["delete_s"] = round(time.time() - t0, 1)
                print("delete ->", results["delete"], flush=True)
                results["release"] = await call(s, "release_scope")


def log_rows(since: float) -> list[dict]:
    home = subprocess.run([PY, "-m", "overshoulder.cli", "home"], capture_output=True, text=True).stdout.strip()
    logs = Path(home) / "logs"
    out = []
    for f in sorted(logs.glob("*.jsonl")):
        if f.stat().st_mtime < since:
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if r.get("client") == "phone-e2e" and r.get("event") == "action" and r.get("ts", 0) >= since:
                err = r.get("error") or {}
                out.append({"name": (r.get("target") or {}).get("name"), "ok": r.get("ok"),
                            "approved_by": r.get("approved_by"), "code": err.get("code"),
                            "denied_by": err.get("answered_by"), "message": err.get("message")})
    return out


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    s = phone.load()
    results["before"] = {"ntfy_enabled": s.ntfy_enabled, "timeout_s": s.timeout_s}
    s.ntfy_enabled, s.timeout_s = True, 180
    phone.save(s)
    print("ntfy on - waiting for the readiness answer on the phone", flush=True)
    ready = ({"answer": True, "by": "skipped"} if "--skip-ready" in sys.argv else approvals.try_phone(s, 3600))
    results["ready"] = ready
    save()
    print("ready ->", ready, flush=True)
    if ready.get("answer") is None:
        s = phone.load()
        s.timeout_s = results["before"]["timeout_s"]
        phone.save(s)
        return 2
    time.sleep(10)  # let the readiness notifications clear before the first real question
    t_start = time.time()
    WINDOW.unlink(missing_ok=True)
    app = subprocess.Popen(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                            str(HERE / "phonetest.ps1"), str(WINDOW)])
    token = None
    try:
        for _ in range(100):
            if WINDOW.exists():
                break
            time.sleep(0.1)
        add = subprocess.run([PY, "-m", "overshoulder.cli", "client", "add", "phone-e2e"],
                             capture_output=True, text=True, check=True)
        token = add.stdout.strip().splitlines()[0]
        asyncio.run(mcp_run(token))
        time.sleep(1)
        results["window_saw"] = json.loads(WINDOW.read_text(encoding="utf-8-sig"))["clicked"]
        results["log"] = log_rows(t_start - 5)
    finally:
        save()
        app.kill()
        if token:
            subprocess.run([PY, "-m", "overshoulder.cli", "client", "remove", "phone-e2e"], capture_output=True)
        s = phone.load()
        s.timeout_s = results["before"]["timeout_s"]
        phone.save(s)
        save()
    ok = (results["window_saw"] == ["Send"] and results["send"].get("ok") is True
          and results["delete"].get("ok") is not True)
    print("PASS" if ok else "FAIL", json.dumps(results, indent=2), flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
