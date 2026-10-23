"""Agent-facing operations: what the MCP server and CLI expose.

Every method that touches the desktop goes through Gateway.run, so the lease, scope, hard denies,
approvals and the action log apply uniformly. Results are plain JSON-able dicts.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from overshoulder import scope as sc
from overshoulder.actions import Ladder
from overshoulder.driver import capture, uia
from overshoulder.driver import windows as w
from overshoulder.driver.display import enable_dpi_awareness
from overshoulder.driver.windows import Rect
from overshoulder.gateway.clients import default_home
from overshoulder.gateway.core import Gateway, Target
from overshoulder.gateway.errors import GatewayError
from overshoulder.gateway.limits import black_out, hidden_rects

ACTIONS = ("click", "double_click", "right_click", "invoke", "toggle", "select", "set_value", "type",
           "hotkey", "focus", "activate")
METHODS = ("auto", "background", "input")
# Below this share of changed pixels a screenshot pair counts as "nothing visible happened".
CHANGE_EPSILON = 0.0002
# What an agent may choose when it renders. Not engine or outside_audio: another TTS program and audio
# from anywhere on disk are for a person at the CLI.
RENDER_OPTIONS = ("pointer", "zoom", "keys", "gif", "narrate", "voice")
LAYER_OPS = ("list", "add", "edit", "remove")


class Service:
    def __init__(self, gateway: Gateway | None = None, shots_dir: Path | None = None):
        enable_dpi_awareness()
        self.gw = gateway or Gateway()
        self.cache = uia.ElementCache()
        self._cache_lease: str | None = None
        self.shots_dir = shots_dir or default_home() / "shots"
        self.recordings_dir = self.shots_dir.parent / "recordings"
        self._recordings: dict = {}  # lease id -> Recording
        self._rendering: set[str] = set()  # recording names being rendered now
        self._rendering_lock = threading.Lock()

    # -- discovery and lease -------------------------------------------------------------------

    def windows(self, token: str, title_re: str | None = None, process: str | None = None) -> list[dict]:
        client = self.gw.registry.authenticate(token)
        hidden = self.gw.hidden_for(client.name)
        return [{"hwnd": x.hwnd, "title": x.title, "process": x.process, "class": x.class_name,
                 "rect": [x.bounds.left, x.bounds.top, x.bounds.right, x.bounds.bottom],
                 "scope": f"window:{x.hwnd}"}
                for x in sc.find_windows(title_re=title_re, process=process) if not hidden.denies(x.process)]

    def acquire(self, token: str, scope: str, ttl_s: float = 60, log_text: bool = False,
                limits: dict | None = None) -> dict:
        """limits: {"read_only", "no_typing", "deny_processes"} to narrow this session (gateway.limits)."""
        out = self.gw.acquire(token, scope, ttl_s, log_text=log_text, limits=limits)
        if out["lease_id"] != self._cache_lease:
            self.cache.clear()
            self._cache_lease = out["lease_id"]
        return out

    def heartbeat(self, token: str, lease_id: str) -> dict:
        return self.gw.heartbeat(token, lease_id)

    def release(self, token: str, lease_id: str) -> dict:
        finished = self.stop_recordings_for(lease_id)
        self.gw.release(token, lease_id)
        self.cache.clear()
        return {"released": True, **({"recording": finished} if finished else {})}

    def status(self) -> dict:
        return self.gw.status()

    def _scope(self, lease_id: str):
        lease = self.gw.leases.active()
        if lease is None or lease.id != lease_id:
            return None
        return lease.scope

    # -- observation ---------------------------------------------------------------------------

    def observe(self, token: str, lease_id: str, max_nodes: int = 300, only_named: bool = True,
                screenshot: bool = False) -> dict:
        def perform(_t, p):
            scope = self._scope(lease_id)
            out: dict = {"scope": scope.describe(), "bounds": _rect_list(scope.bounds())}
            if scope.kind == "window":
                roots = uia.roots_for_window(scope.binding.hwnd)
            else:
                fg = w.foreground()
                roots = [fg] if fg and scope.owns_window(fg) else []
                out["windows"] = [{"hwnd": x.hwnd, "title": x.title, "process": x.process}
                                  for x in w.top_windows() if scope.owns_window(x.hwnd)]
            elements = []
            for root in roots:
                elements += uia.describe_all(uia.walk(self.cache, root, max_nodes=max_nodes,
                                                      only_named=only_named))
            out["elements"] = elements[:max_nodes]
            out["truncated"] = len(elements) > max_nodes
            if screenshot:
                out["screenshot"] = str(self._shot(scope, "observe"))
            return out

        return self.gw.run(token, lease_id, "observe", perform, read_only=True,
                           params={"max_nodes": max_nodes})["result"]

    def find(self, token: str, lease_id: str, name: str | None = None, name_re: str | None = None,
             control_type: str | None = None, auto_id: str | None = None,
             max_nodes: int = 2000) -> list[dict]:
        def perform(_t, p):
            scope = self._scope(lease_id)
            roots = uia.roots_for_window(scope.binding.hwnd) if scope.kind == "window" else \
                [h for h in [w.foreground()] if h and scope.owns_window(h)]
            hits = []
            for root in roots:
                els = uia.walk(self.cache, root, max_nodes=max_nodes)
                hits += uia.describe_all(uia.find(els, name, name_re, control_type, auto_id))
            return hits

        return self.gw.run(token, lease_id, "find", perform, read_only=True,
                           params={"name": name, "name_re": name_re, "control_type": control_type,
                                   "auto_id": auto_id})["result"]

    def read(self, token: str, lease_id: str, ref: str) -> dict:
        target = self._element_target(ref)
        return self.gw.run(token, lease_id, "read_value",
                           lambda t, p: uia.read(self.cache.get(ref)), target=target, read_only=True,
                           params={"ref": ref})["result"]

    def screenshot(self, token: str, lease_id: str) -> dict:
        def perform(_t, p):
            scope = self._scope(lease_id)
            return {"path": str(self._shot(scope, "shot"))}
        return self.gw.run(token, lease_id, "screenshot", perform, read_only=True)["result"]

    # -- recording -----------------------------------------------------------------------------

    def record_start(self, token: str, lease_id: str, fps: int = 10, name: str = "") -> dict:
        from overshoulder.recorder import Recording

        def perform(_t, p):
            if lease_id in self._recordings:
                raise ValueError("already recording this scope")
            lease = self.gw.leases.active()
            stamp = time.strftime("%Y%m%d-%H%M%S")
            safe = "".join(c for c in (name or lease.client) if c.isalnum() or c in "-_")[:40]
            rec = Recording(lease.scope, self.recordings_dir / f"{stamp}-{safe}", fps=max(1, min(int(fps), 30)),
                            lease_id=lease_id, client=lease.client,
                            alive=lambda: (self.gw.leases.active() or _NoLease).id == lease_id)
            rec.start()
            self._recordings[lease_id] = rec
            return {"recording": str(rec.dir), "fps": rec.fps, "size": rec.size}

        return self.gw.run(token, lease_id, "record_start", perform, read_only=True, params={"fps": fps})["result"]

    def record_stop(self, token: str, lease_id: str) -> dict:
        """Stop recording and save the action timeline. Rendering is separate (render_recording),
        so it can run without holding up the desktop."""
        def perform(_t, p):
            return self._finish_recording(lease_id)

        return self.gw.run(token, lease_id, "record_stop", perform, read_only=True)["result"]

    def _finish_recording(self, lease_id: str) -> dict:
        from overshoulder.recorder import collect_actions

        rec = self._recordings.pop(lease_id, None)
        if rec is None:
            raise ValueError("not recording")
        meta = rec.stop()
        actions = collect_actions(self.gw.log.path, lease_id, meta["start"], meta["end"])
        (rec.dir / "actions.json").write_text(json.dumps(actions, indent=1), encoding="utf-8")
        return {"recording": str(rec.dir), "frames": meta["frames"], "actions": len(actions),
                "error": meta.get("error")}

    def stop_recordings_for(self, lease_id: str) -> dict | None:
        """Called on release: stop a recording the agent forgot to stop. The caller renders it."""
        if lease_id in self._recordings:
            return self._finish_recording(lease_id)
        return None

    def redact(self, token: str, lease_id: str, ref: str, style: str = "blur") -> dict:
        """While recording, hide an element in every frame of the tutorial: its rect, in the recorded
        frame's pixels, goes into the recording's layer as a redaction."""
        from overshoulder.layer import Layer

        target = self._element_target(ref)

        def perform(t: Target, p):
            rec = self._recordings.get(lease_id)
            if rec is None:
                raise ValueError("not recording; call record_start first")
            b = self._scope(lease_id).bounds()
            r = t.rect
            item = {"kind": "redact", "rect": [r.left - b.left, r.top - b.top, r.right - b.left, r.bottom - b.top],
                    "style": style, "note": f"redacted by ref {ref}"}
            return {"ok": True, "recording": rec.dir.name, "item": Layer.for_recording(rec.dir).add(item)}

        return self.gw.run(token, lease_id, "redact", perform, target=target, read_only=True,
                           params={"ref": ref, "style": style})["result"]

    # -- tutorials (no desktop: callers run these without the desktop lock) ---------------------

    def own_recording(self, token: str, recording: str) -> Path:
        """One of the caller's recordings, by the name or path record_stop returned. Only its name is
        used, so nothing outside the recordings directory can be reached."""
        client = self.gw.registry.authenticate(token)
        name = Path(str(recording or "")).name
        rec = self.recordings_dir / name
        live = next((r for r in self._recordings.values() if r.dir.name == name), None)
        if live is not None:
            owner = live.meta.get("client")
        elif name and (rec / "meta.json").is_file():
            owner = json.loads((rec / "meta.json").read_text(encoding="utf-8")).get("client")
        else:
            raise ValueError(f"no recording named {name!r}")
        if owner != client.name:
            raise GatewayError(f"recording {name!r} belongs to another client")
        return rec

    def annotate(self, token: str, recording: str, op: str = "list", item: dict | None = None,
                 item_id: str | None = None, changes: dict | None = None) -> dict:
        """Read or edit a recording's annotation layer. list also returns the steps of its last render
        (ids, captions, targets in frame pixels) and the frame size, which is what items are placed by."""
        from overshoulder.layer import Layer

        if op not in LAYER_OPS:
            raise ValueError(f"op must be one of {LAYER_OPS}")
        rec = self.own_recording(token, recording)
        layer = Layer.for_recording(rec)
        if op == "list":
            steps = rec / "tutorial" / "steps.json"
            meta = rec / "meta.json"
            return {"ok": True, "recording": rec.name, "items": layer.items(),
                    "steps": json.loads(steps.read_text(encoding="utf-8")) if steps.is_file() else None,
                    "size": json.loads(meta.read_text(encoding="utf-8"))["size"] if meta.is_file() else None}
        if op == "add":
            return {"ok": True, "item": layer.add(dict(item or {}))}
        if not item_id:
            raise ValueError(f"{op} needs an id")
        if op == "edit":
            return {"ok": True, "item": layer.edit(item_id, dict(changes or {}))}
        return {"ok": True, "removed": layer.remove(item_id)}

    def render_recording(self, token: str, recording: str, **options) -> dict:
        """(Re)render one of the caller's stopped recordings with its layer."""
        from overshoulder import render as rnd

        bad = set(options) - set(RENDER_OPTIONS)
        if bad:
            raise ValueError(f"unknown render options {sorted(bad)}; allowed: {RENDER_OPTIONS}")
        rec = self.own_recording(token, recording)
        if any(r.dir.name == rec.name for r in self._recordings.values()):
            raise ValueError("still recording; call record_stop first")
        with self._rendering_lock:
            if rec.name in self._rendering:
                raise ValueError("this recording is already being rendered")
            self._rendering.add(rec.name)
        try:
            out = rnd.render(rec, **options)
        finally:
            with self._rendering_lock:
                self._rendering.discard(rec.name)
        self.gw.log.write("render", client=self.gw.registry.authenticate(token).name, recording=rec.name,
                          options=options, seconds=out.get("seconds"))
        return {"ok": True, **out}

    # -- actions -------------------------------------------------------------------------------

    def act(self, token: str, lease_id: str, action: str, ref: str | None = None, x: int | None = None,
            y: int | None = None, text: str | None = None, combo: str | None = None,
            value: str | None = None, expect: dict | None = None, method: str = "auto") -> dict:
        """Run one action. Target is an element ref from observe/find, or an x/y point in scope.

        method: "auto" (background first, the person's input only as a last resort), "background"
        (never the person's mouse or keyboard) or "input" (real input). The result says which ran.
        """
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; one of {ACTIONS}")
        if method not in METHODS:
            raise ValueError(f"method must be one of {METHODS}")
        target = self._element_target(ref) if ref else (
            Target("point", x=int(x), y=int(y)) if x is not None and y is not None else None)
        params = {"ref": ref, "x": x, "y": y, "combo": combo, "expect": expect, "method": method}
        if text is not None:
            params["text"] = text
        if value is not None:
            params["value"] = value
        before: dict = {}

        def perform(t: Target | None, p: dict):
            scope = self._scope(lease_id)
            before["img"] = self._snap(scope)
            el = self.cache.get(ref) if ref else None
            ladder = Ladder(scope, p, method)
            if action == "invoke":
                out = ladder.invoke(_need(el, action))
            elif action == "toggle":
                out = ladder.pattern("uia_toggle", lambda: {"toggle": uia.toggle(_need(el, action))})
            elif action == "select":
                out = ladder.pattern("uia_select", lambda: uia.select(_need(el, action)))
            elif action == "set_value":
                out = ladder.set_value(_need(el, action), value or "")
            elif action == "focus":
                out = ladder.pattern("uia_focus", lambda: uia.focus(_need(el, action)))
            elif action == "activate":
                if scope.kind != "window":
                    raise ValueError("activate needs a window scope")
                with p["borrow"]() as b:  # keeps the window in front, so only input mode "full" allows it
                    b.check()
                    uia.activate(scope.binding.hwnd)
                out = ladder._done("activate", background=False)
            elif action in ("click", "double_click", "right_click"):
                if t is None:
                    raise ValueError(f"{action} needs a ref or x/y")
                out = ladder.press(el, t, button="right" if action == "right_click" else "left",
                                   count=2 if action == "double_click" else 1)
            elif action == "type":
                out = ladder.type_text(el, text or "")
            else:  # hotkey
                out = ladder.hotkey(combo or "")
            return out.to_dict()

        def verify(t: Target | None, p: dict, result):
            time.sleep(0.35)
            scope = self._scope(lease_id)
            ran = {"method": (result or {}).get("method"), "background": (result or {}).get("background")}
            after = None if _gone(scope) else self._snap(scope)
            if after is None and _gone(scope):
                # The action closed the scope's own window (Ctrl+W on the last tab, a dialog's OK). That
                # is its effect, not a failure; an expectation about what the window shows can't be met.
                return {"ok": not expect, "changed": True, "window_closed": True, **ran,
                        "note": "the action closed the scope's window; release_scope, or take a new scope",
                        **({"expect_failed": ["the scope's window closed"]} if expect else {})}
            pixels = capture.changed_fraction(before["img"], after) if before["img"] and after else 0.0
            verdict = {"ok": True, "changed": pixels > CHANGE_EPSILON, "pixels_changed": round(pixels, 5), **ran}
            if ref and uia.element_state(self.cache.get(ref)) is not None:
                readback = uia.read(self.cache.get(ref))
                verdict["readback"] = {k: (v[:200] if isinstance(v, str) else v) for k, v in readback.items()}
            if expect:
                verdict.update(self._check_expect(expect, ref, lease_id, verdict))
            return verdict

        return self.gw.run(token, lease_id, action, perform, target=target, params=params, verify=verify)

    # -- helpers -------------------------------------------------------------------------------

    def _element_target(self, ref: str) -> Target:
        el = self.cache.get(ref)
        if el is None:
            raise GatewayError(f"unknown ref {ref!r}; observe or find first")

        def fresh() -> Target | None:
            st = uia.element_state(el)
            if st is None:
                return None
            return Target("element", rect=st["rect"], name=st["name"], control_type=st["type"],
                          automation_id=st["auto_id"], is_password=st["password"], pid=st["pid"],
                          hwnd=el.root_hwnd, ref=ref)

        t = fresh()
        if t is None:
            raise GatewayError(f"element {ref} no longer exists; observe again")
        return Target(**{**t.__dict__, "revalidate": fresh})

    @staticmethod
    def _snap(scope):
        """Pixels of the scope: the window's own (works when covered) or the screen rect. None when
        there is nothing to capture, such as a window closing as it is read."""
        bounds = scope.bounds()
        if bounds.empty:
            return None
        binding = getattr(scope, "binding", None)
        if binding is not None:
            img = capture.window_image(binding.hwnd, bounds)
            if img is not None:
                return img
        return capture.grab(bounds)

    def _check_expect(self, expect: dict, ref: str | None, lease_id: str, verdict: dict) -> dict:
        problems = []
        if expect.get("changed") and not verdict["changed"]:
            problems.append("nothing visibly changed")
        if (needle := expect.get("text_contains")) is not None:
            rb = verdict.get("readback") or {}
            hay = " ".join(str(rb.get(k, "")) for k in ("name", "value", "text"))
            if needle not in hay:
                problems.append(f"{needle!r} not found in the element's text")
        if (want := expect.get("element")) is not None:
            scope = self._scope(lease_id)
            roots = uia.roots_for_window(scope.binding.hwnd) if scope.kind == "window" else \
                [h for h in [w.foreground()] if h and scope.owns_window(h)]
            els = [e for r in roots for e in uia.walk(self.cache, r, max_nodes=2000)]
            if not uia.find(els, want.get("name"), want.get("name_re"), want.get("control_type"),
                            want.get("auto_id")):
                problems.append(f"no element matching {want}")
        return {"ok": not problems, "expect_failed": problems} if problems else {"expect": "met"}

    def _shot(self, scope, kind: str) -> Path:
        """A screenshot for the agent. A window scope gives the window's own pixels only, never the
        screen area it occupies, so whatever covers it (the person's other windows) is not captured."""
        binding = getattr(scope, "binding", None)
        if binding is not None:
            img = capture.window_image(binding.hwnd, scope.bounds())
            if img is None:
                raise GatewayError("could not capture the window's own pixels; try again")
        else:
            bounds = scope.bounds()
            img = black_out(capture.grab(bounds), bounds, hidden_rects(scope))
        ms = int(time.time() * 1000) % 1000
        path = self.shots_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{ms:03d}-{kind}.png"
        return capture.save(img, path)


class _NoLease:
    id = None


def _need(el, action: str):
    if el is None:
        raise ValueError(f"{action} needs an element ref")
    return el


def _gone(scope) -> bool:
    """A window scope whose window has closed, or whose process has gone, since it was taken."""
    if getattr(scope, "kind", None) != "window":
        return False
    try:
        scope.check_alive()
    except sc.ScopeStale:
        return True
    return False


def _rect_list(r: Rect) -> list[int]:
    return [r.left, r.top, r.right, r.bottom]
