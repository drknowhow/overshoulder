"""The person's config, as they last accepted it, and a check on every change that gives agents more.

Three files decide what agents may do: settings.json (input mode, detection, vision), phone.json (where
approvals are answered) and clients.json (who holds a token, and with what grants and limits). Anything
running as the person can write them, so the gateway keeps the version the person accepted and compares
the files with it before each use.

- A change that only narrows what agents may do is accepted and logged.
- A change that widens it pauses every agent and asks the person in a box on their own screen. Yes keeps
  it; No (or no answer) puts the accepted version back.
- The settings page goes through propose(), which asks before it writes.

This catches mistakes and a program that edits the files while the gateway runs. It does not stop one
that can also stop the gateway, change the files, and start it again: the accepted version lives only in
the gateway's memory.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

CONFIG_PAUSE = "settings changed outside Overshoulder; waiting for the person to accept them on screen"

_INPUT_RANK = {"never": 0, "borrow": 1, "full": 2}
_GRANT_RANK = {"deny": 0, "approve": 1, "allow": 2}


def _settings_defaults() -> dict:
    from overshoulder.settings import Settings
    return asdict(Settings())


def _phone_defaults() -> dict:
    from overshoulder.phone import PhoneSettings
    return asdict(PhoneSettings())


def widened_settings(old: dict, new: dict) -> list[str]:
    base = _settings_defaults()
    o, n = {**base, **old}, {**base, **new}
    out = []
    if _INPUT_RANK.get(n["input_mode"], 0) > _INPUT_RANK.get(o["input_mode"], 0):
        out.append(f"input mode {o['input_mode']} -> {n['input_mode']}")
    if n["waits_enabled"] and not o["waits_enabled"]:
        out.append("detection switched on")
    added = sorted(set(n["wait_kinds"] or []) - set(o["wait_kinds"] or []))
    if added:
        out.append(f"detection kinds added: {', '.join(added)}")
    ow, nw = set(o["wait_processes"] or []), set(n["wait_processes"] or [])
    if ow and (not nw or nw - ow):
        out.append("detection may watch more programs")
    if n["vision_mode"] != "off" and n["vision_mode"] != o["vision_mode"]:
        out.append(f"vision model {o['vision_mode']} -> {n['vision_mode']} (screenshots go to it)")
    elif n["vision_mode"] != "off" and any(n[k] != o[k] for k in (
            "vision_local_url", "vision_local_model", "vision_cloud_url", "vision_cloud_model",
            "vision_cloud_key")):
        out.append("where screenshots are sent for vision changed")
    if n["ocr_enabled"] and not o["ocr_enabled"]:
        out.append("reading the screen with OCR switched on")
    if n["catalog_weekly_check"] and not o["catalog_weekly_check"]:
        out.append("weekly catalog check switched on")
    return out


def widened_phone(old: dict, new: dict) -> list[str]:
    base = _phone_defaults()
    o, n = {**base, **old}, {**base, **new}
    out = []
    for key, label in (("ntfy_enabled", "approvals on the phone (ntfy)"), ("webhook_enabled", "approvals by webhook")):
        if n[key] and not o[key]:
            out.append(f"{label} switched on")
    routed = [k for k in ("ntfy_server", "ntfy_topic", "ntfy_reply_topic", "ntfy_token")
              if n["ntfy_enabled"] and o[k] and n[k] != o[k]]
    routed += [k for k in ("webhook_url", "webhook_poll_url", "webhook_secret")
               if n["webhook_enabled"] and n[k] != o[k] and (o[k] or k != "webhook_secret")]
    if (n["ntfy_enabled"] or n["webhook_enabled"]) and o["answer_key"] and n["answer_key"] != o["answer_key"]:
        routed.append("answer_key")
    if routed:
        out.append(f"where approvals are asked and answered changed ({', '.join(routed)})")
    for key, label in (("share_title", "window titles"), ("share_screenshot", "screenshots")):
        if n[key] and not o[key]:
            out.append(f"{label} are sent with phone questions")
    return out


def widened_clients(old: dict, new: dict) -> list[str]:
    o = {c.get("name"): c for c in (old.get("clients") or [])}
    out = []
    for c in new.get("clients") or []:
        name = c.get("name")
        before = o.get(name)
        if before is None:
            grants = f"window={c.get('window', 'approve')}, screen={c.get('screen', 'approve')}"
            out.append(f"new client {name!r} ({grants})")
            continue
        if c.get("token_sha256") != before.get("token_sha256"):
            out.append(f"new token for {name!r}")
        for grant in ("window", "screen"):
            if _GRANT_RANK.get(c.get(grant, "approve"), 0) > _GRANT_RANK.get(before.get(grant, "approve"), 0):
                out.append(f"{name!r} {grant} scopes {before.get(grant)} -> {c.get(grant)}")
        lo, ln = before.get("limits") or {}, c.get("limits") or {}
        for flag in ("read_only", "no_typing"):
            if lo.get(flag) and not ln.get(flag):
                out.append(f"{name!r} is no longer {flag.replace('_', '-')}")
        lifted = sorted(set(lo.get("deny_processes") or []) - set(ln.get("deny_processes") or []))
        if lifted:
            out.append(f"{name!r} may now reach {', '.join(lifted)}")
        allowed = sorted(set(ln.get("allow_builtin") or []) - set(lo.get("allow_builtin") or []))
        if allowed:
            out.append(f"{name!r} allowed {', '.join(allowed)} (denied to every client by default)")
    return out


WIDENED: dict[str, Callable[[dict, dict], list[str]]] = {
    "settings": widened_settings, "phone": widened_phone, "clients": widened_clients}


def _parse(raw: bytes | None) -> dict:
    if not raw:
        return {}
    try:
        data = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        return {}  # the loaders treat an unreadable file as the most restrictive one
    return data if isinstance(data, dict) else {}


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def _write(path: Path, raw: bytes | None) -> None:
    if raw is None:
        path.unlink(missing_ok=True)
        return
    tmp = path.with_suffix(".restore")
    tmp.write_bytes(raw)
    tmp.replace(path)


class ConfigGuard:
    """files: name ("settings" | "phone" | "clients") -> path. ask(request) -> bool asks the person at the
    desk only. pause(reason) and resume(only_if) are the gateway's; log(event, **fields) its action log."""

    def __init__(self, files: dict[str, Path], ask: Callable[[dict], bool], pause: Callable[[str], None],
                 resume: Callable[[str], bool], log: Callable[..., None] = lambda *a, **k: None):
        self.files = dict(files)
        self._ask, self._pause, self._resume, self._log = ask, pause, resume, log
        self._lock = threading.Lock()
        self._accepted = {name: _read(p) for name, p in self.files.items()}
        self._asking: threading.Thread | None = None

    def asking(self) -> bool:
        return self._asking is not None and self._asking.is_alive()

    def check(self) -> list[str]:
        """Compare the files with the accepted versions. Narrowing changes are accepted. A widening one
        pauses agents and starts a question; returns what widened (empty when nothing did)."""
        with self._lock:
            if self.asking():
                return ["a question about changed settings is still open"]
            changed = {}
            for name, path in self.files.items():
                now = _read(path)
                if now == self._accepted[name]:
                    continue
                why = WIDENED[name](_parse(self._accepted[name]), _parse(now))
                if why:
                    changed[name] = (now, why)
                else:
                    self._accepted[name] = now
                    self._log("config_accepted", file=name, widened=False)
            if not changed:
                return []
            reasons = [r for _, why in changed.values() for r in why]
            self._pause(CONFIG_PAUSE)
            self._asking = threading.Thread(target=self._decide, args=(changed, reasons), daemon=True,
                                            name="overshoulder-config-question")
            self._asking.start()
            return reasons

    def _decide(self, changed: dict, reasons: list[str]) -> None:
        req = {"kind": "config", "client": "A program on this PC",
               "reason": "change Overshoulder's settings outside its settings page: " + "; ".join(reasons)}
        try:
            yes = bool(self._ask(req))
        except Exception:
            yes = False
        with self._lock:
            for name, (seen, why) in changed.items():
                if yes:
                    self._accepted[name] = seen
                else:
                    _write(self.files[name], self._accepted[name])
                self._log("config_accepted" if yes else "config_restored", file=name, widened=True, why=why)
        self._resume(CONFIG_PAUSE)

    def wait(self, timeout: float | None = None) -> None:
        t = self._asking
        if t is not None:
            t.join(timeout)

    def propose(self, name: str, new: dict, write: Callable[[], None]) -> list[str] | None:
        """A change from the settings page: asks the person first when it widens anything, then writes
        it and accepts the result. Returns None when written, or what widened when the person said no."""
        with self._lock:
            why = WIDENED[name](_parse(self._accepted[name]), new)
        if why:
            req = {"kind": "config", "client": "The settings page",
                   "reason": "change Overshoulder's settings: " + "; ".join(why)}
            try:
                yes = bool(self._ask(req))
            except Exception:
                yes = False
            if not yes:
                self._log("config_refused", file=name, why=why)
                return why
        with self._lock:
            write()
            self._accepted[name] = _read(self.files[name])
            self._log("config_accepted", file=name, widened=bool(why), why=why)
        return None
