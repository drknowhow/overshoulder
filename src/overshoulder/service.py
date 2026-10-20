"""Agent-facing operations: what the MCP server and CLI expose.

Every method that touches the desktop goes through Gateway.run, so the lease, scope, hard denies,
approvals and the action log apply uniformly. Results are plain JSON-able dicts.
"""

from __future__ import annotations

import time
from pathlib import Path

from overshoulder import scope as sc
from overshoulder.driver import capture, uia
from overshoulder.driver import input as inp
from overshoulder.driver import windows as w
from overshoulder.driver.display import enable_dpi_awareness
from overshoulder.driver.windows import Rect
from overshoulder.gateway.clients import default_home
from overshoulder.gateway.core import Gateway, Target
from overshoulder.gateway.errors import GatewayError

ACTIONS = ("click", "double_click", "right_click", "invoke", "toggle", "select", "set_value", "type",
           "hotkey", "focus")
# Below this share of changed pixels a screenshot pair counts as "nothing visible happened".
CHANGE_EPSILON = 0.0002


class Service:
    def __init__(self, gateway: Gateway | None = None, shots_dir: Path | None = None):
        enable_dpi_awareness()
        self.gw = gateway or Gateway()
        self.cache = uia.ElementCache()
        self._cache_lease: str | None = None
        self.shots_dir = shots_dir or default_home() / "shots"

    # -- discovery and lease -------------------------------------------------------------------

    def windows(self, token: str, title_re: str | None = None, process: str | None = None) -> list[dict]:
        self.gw.registry.authenticate(token)
        return [{"hwnd": x.hwnd, "title": x.title, "process": x.process, "class": x.class_name,
                 "rect": [x.bounds.left, x.bounds.top, x.bounds.right, x.bounds.bottom],
                 "scope": f"window:{x.hwnd}"}
                for x in sc.find_windows(title_re=title_re, process=process)]

    def acquire(self, token: str, scope: str, ttl_s: float = 60) -> dict:
        out = self.gw.acquire(token, scope, ttl_s)
        if out["lease_id"] != self._cache_lease:
            self.cache.clear()
            self._cache_lease = out["lease_id"]
        return out

    def heartbeat(self, token: str, lease_id: str) -> dict:
        return self.gw.heartbeat(token, lease_id)

    def release(self, token: str, lease_id: str) -> dict:
        self.gw.release(token, lease_id)
        self.cache.clear()
        return {"released": True}

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
                elements += [uia.describe(e) for e in uia.walk(self.cache, root, max_nodes=max_nodes,
                                                                only_named=only_named)]
            out["elements"] = elements[:max_nodes]
            out["truncated"] = len(elements) > max_nodes
            if screenshot:
                out["screenshot"] = str(self._shot(scope.bounds(), "observe"))
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
                hits += [uia.describe(e) for e in uia.find(els, name, name_re, control_type, auto_id)]
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
            return {"path": str(self._shot(scope.bounds(), "shot"))}
        return self.gw.run(token, lease_id, "screenshot", perform, read_only=True)["result"]

    # -- actions -------------------------------------------------------------------------------

    def act(self, token: str, lease_id: str, action: str, ref: str | None = None, x: int | None = None,
            y: int | None = None, text: str | None = None, combo: str | None = None,
            value: str | None = None, expect: dict | None = None) -> dict:
        """Run one action. Target is an element ref from observe/find, or an x/y point in scope."""
        if action not in ACTIONS:
            raise ValueError(f"unknown action {action!r}; one of {ACTIONS}")
        target = self._element_target(ref) if ref else (
            Target("point", x=int(x), y=int(y)) if x is not None and y is not None else None)
        params = {"ref": ref, "x": x, "y": y, "combo": combo, "expect": expect}
        if text is not None:
            params["text"] = text
        if value is not None:
            params["value"] = value
        before: dict = {}

        def perform(t: Target | None, p: dict):
            scope = self._scope(lease_id)
            before["img"] = capture.grab(scope.bounds())
            el = self.cache.get(ref) if ref else None
            if action == "invoke":
                uia.invoke(_need(el, action))
            elif action == "toggle":
                return {"toggle": uia.toggle(_need(el, action))}
            elif action == "select":
                uia.select(_need(el, action))
            elif action == "set_value":
                uia.set_value(_need(el, action), value or "")
            elif action == "focus":
                uia.focus(_need(el, action))
            elif action in ("click", "double_click", "right_click"):
                if t is None:
                    raise ValueError(f"{action} needs a ref or x/y")
                px, py = t.point()
                inp.click(px, py, button="right" if action == "right_click" else "left",
                          count=2 if action == "double_click" else 1)
            elif action == "type":
                if el is not None:
                    uia.focus(el)
                    time.sleep(0.15)
                inp.type_text(self._focused_in_scope(scope), text or "", abort=p["abort"])
            elif action == "hotkey":
                p["abort"]()
                inp.hotkey(self._focused_in_scope(scope), combo or "")
            return None

        def verify(t: Target | None, p: dict, result):
            time.sleep(0.35)
            scope = self._scope(lease_id)
            after = capture.grab(scope.bounds())
            pixels = capture.changed_fraction(before["img"], after)
            verdict = {"ok": True, "changed": pixels > CHANGE_EPSILON, "pixels_changed": round(pixels, 5)}
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
    def _focused_in_scope(scope) -> int:
        fg = w.foreground()
        if not fg or not scope.owns_window(fg):
            raise sc.OutOfScope(f"foreground window {fg:#x} is not in scope")
        return fg

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
            roots = uia.roots_for_window(scope.binding.hwnd) if scope.kind == "window" else [w.foreground()]
            els = [e for r in roots for e in uia.walk(self.cache, r, max_nodes=2000)]
            if not uia.find(els, want.get("name"), want.get("name_re"), want.get("control_type"),
                            want.get("auto_id")):
                problems.append(f"no element matching {want}")
        return {"ok": not problems, "expect_failed": problems} if problems else {"expect": "met"}

    def _shot(self, rect: Rect, kind: str) -> Path:
        ms = int(time.time() * 1000) % 1000
        path = self.shots_dir / f"{time.strftime('%Y%m%d-%H%M%S')}-{ms:03d}-{kind}.png"
        return capture.save(capture.grab(rect), path)


def _need(el, action: str):
    if el is None:
        raise ValueError(f"{action} needs an element ref")
    return el


def _rect_list(r: Rect) -> list[int]:
    return [r.left, r.top, r.right, r.bottom]
