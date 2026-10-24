"""Learn from use: when a lease ends, what its actions showed about each app goes into that app's profile.

Plain counting, no model. What is kept is the app's shape (its controls, which methods work on them, what
the app refuses, which controls asked the person), never the person's content:
- no window titles, typed text, field values or read-back, which the log rows carry and this never reads;
- a control is keyed by its automation id, else by its type and a hash of its name;
- a name is kept only after two sessions saw the same one, never when it looks like a file, path, address
  or number, and never for the kinds of control whose names are content (tabs, list, tree and data items,
  documents, text, links, images, windows), whose automation ids are dropped too.
"""

from __future__ import annotations

import hashlib
import re

from overshoulder.appid import AppIdentity, slug
from overshoulder.osp import MAX_TEXT, Item, OspError, Profile, dec, enc
from overshoulder.profiles import ProfileConflict, ProfileStore

ACTING = frozenset({"click", "double_click", "right_click", "drag", "invoke", "toggle", "select", "set_value",
                    "type", "hotkey", "focus", "activate"})
# Failures that say something about the app. Every other refusal (approval_denied aside) is the gateway's
# own rules or the person's limits, which a profile must not learn from.
APP_FAILS = frozenset({"needs_real_input", "typing_mismatch", "verify_failed", "keys_held", "target_stale"})
CONTENT_TYPES = frozenset({"tabitem", "listitem", "treeitem", "dataitem", "document", "text", "hyperlink",
                           "image", "window", "pane", "titlebar", "custom", "group"})
LOOKS_PERSONAL = re.compile(r"[\\/@]|://|\bwww\.|\.[a-z0-9]{1,5}(\s|$)|\d{3,}", re.I)
MAX_NAME = 40
MAX_ROWS = 5000  # rows a lease keeps for learning; more are not counted


def is_private(name: str, control_type: str) -> bool:
    return (control_type.lower() in CONTENT_TYPES or len(name) > MAX_NAME or not name.isprintable()
            or bool(LOOKS_PERSONAL.search(name)))


def name_hash(app_id: str, control_type: str, name: str) -> str:
    return hashlib.sha256(f"{app_id}\0{control_type}\0{name}".encode()).hexdigest()[:12]


def _bump(item: Item, key: str, by: int = 1) -> None:
    item.fields[key] = str(item.num(key) + by)


def _free_id(prof: Profile, kind: str, base: str) -> str:
    base = base or kind
    if prof.get(kind, base) is None:
        return base
    n = 2
    while prof.get(kind, f"{base}-{n}") is not None:
        n += 1
    return f"{base}-{n}"


def _where(t: dict | None) -> str:
    if not t:
        return "focus"
    if t.get("kind") == "element":
        return slug(t.get("control_type") or "", 20) or "element"
    return t.get("kind") or "point"


class _Session:
    """One lease's rows applied to one app's profile."""

    def __init__(self, prof: Profile, lease_id: str, version: str):
        self.prof = prof
        self.sess = hashlib.sha256(lease_id.encode()).hexdigest()[:10]  # which session, not its lease id
        self.version = enc(version) if version else ""
        self.touched: set[str] = set()
        self.stats = {"actions": 0, "controls": 0, "ways": 0, "new": 0, "dropped": 0}

    def _put(self, item: Item) -> Item | None:
        try:
            return self.prof.put(item)
        except OspError:
            self.stats["dropped"] += 1  # the profile is full; what it holds already stays
            return None

    def view(self, view: str) -> None:
        item = self.prof.get("view", view)
        if item is None:
            item = self._put(Item("view", view, ["window" if view == "main" else "dialog"], {"src": "use"}))
            if item is None:
                return
            self.stats["new"] += 1
        _bump(item, "n")

    def control(self, view: str, t: dict) -> Item | None:
        ctype = t.get("control_type") or "Unknown"
        if not re.fullmatch(r"[A-Za-z0-9]{1,40}", ctype):
            ctype = "Unknown"
        name = (t.get("name") or "").strip()
        aid = (t.get("automation_id") or "").strip()
        content = ctype.lower() in CONTENT_TYPES
        if content or len(aid) > 60 or not aid.isprintable():
            aid = ""  # an item's automation id can be the key of the content it shows
        private = bool(name) and is_private(name, ctype)
        nh = name_hash(self.prof.app_id, ctype, name) if name and not private else ""
        item = self._find(view, ctype, aid, nh, private or not name)
        if item is None:
            base = slug(aid, 32) if aid else (slug(ctype, 20) if (private or not name) else f"n-{nh[:8]}")
            fields = {"aid": enc(aid)} if aid else {}
            if private or not name:
                fields["name"] = "content" if private else "none"
            item = self._put(Item("ctl", _free_id(self.prof, "ctl", base), [view, ctype], {**fields, "src": "use"}))
            if item is None:
                return None
            self.stats["new"] += 1
        if item.id not in self.touched:
            self.touched.add(item.id)
            self.stats["controls"] += 1
            if item.fields.get("last") != self.sess:
                _bump(item, "ses")
                item.fields["last"] = self.sess
                self._name_seen(item, name, nh)
            if self.version:
                item.fields["ver"] = self.version
        _bump(item, "seen")
        return item

    def _find(self, view: str, ctype: str, aid: str, nh: str, nameless: bool) -> Item | None:
        for it in self.prof.of_kind("ctl"):
            if it.args != [view, ctype]:
                continue
            if aid:
                if dec(it.fields.get("aid", "")) == aid:
                    return it
            elif nh:
                if "aid" not in it.fields and it.fields.get("nh") == nh:
                    return it
            elif nameless and "aid" not in it.fields and it.fields.get("name") in ("content", "none"):
                return it
        return None

    def _name_seen(self, item: Item, name: str, nh: str) -> None:
        """A name becomes the item's text in the second session that sees the same one."""
        if not nh:
            return
        if item.fields.get("nh") != nh:
            item.fields["nh"], item.fields["nses"] = nh, "1"
            item.text = None
            return
        _bump(item, "nses")
        if item.num("nses") >= 2 and item.text is None:
            item.text = name[:MAX_TEXT]
            if item.id.startswith("n-"):  # a name-hash id gets the name's own slug once the name is kept
                new_id = _free_id(self.prof, "ctl", slug(name, 32))
                self.prof.remove("ctl", item.id)
                self.touched.discard(item.id)
                item.id = new_id
                self.touched.add(new_id)
                self._put(item)

    def way(self, action: str, where: str, method: str, ok: bool, why: str = "", ms: int | None = None) -> None:
        wid = f"{action}@{where}.{slug(method, 24).replace('-', '_') or 'any'}"
        item = self.prof.get("way", wid)
        if item is None:
            item = self._put(Item("way", wid, fields={"ok": "0", "fail": "0", "src": "use"}))
            if item is None:
                return
            self.stats["new"] += 1
            self.stats["ways"] += 1
        if ok:
            n = item.num("ok")
            if ms is not None:
                item.fields["ms"] = str(round((item.num("ms") * n + ms) / (n + 1)))
            _bump(item, "ok")
        else:
            _bump(item, "fail")
            if why:
                item.fields["why"] = why

    def key(self, combo: str) -> None:
        kid = re.sub(r"\s+", "", combo.lower())
        if not re.fullmatch(r"[a-z0-9][a-z0-9+]{0,40}", kid):
            return
        item = self.prof.get("key", kid) or self._put(Item("key", kid, fields={"src": "use"}))
        if item is not None:
            _bump(item, "n")

    def row(self, r: dict) -> None:
        action = r.get("action")
        ok = bool(r.get("ok"))
        code = (r.get("error") or {}).get("code", "")
        if ok and not (r.get("verify") or {}).get("method"):
            return  # an older row with no method recorded: nothing to learn
        if not ok and code not in APP_FAILS and code != "approval_denied":
            return
        self.stats["actions"] += 1
        view = r.get("view") or "main"
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", view):
            view = "main"
        self.view(view)
        t = r.get("target") or None
        ctl = self.control(view, t) if t and t.get("kind") == "element" else None
        asked = r.get("approved_by") is not None or code == "approval_denied"
        if ctl is not None:
            if asked:
                _bump(ctl, "ask")
            if ok:
                _bump(ctl, "ok")
            elif code != "approval_denied":
                _bump(ctl, "fail")
        if code == "approval_denied":
            return
        where = _where(t)
        if ok:
            method = (r.get("verify") or {}).get("method") or ""
            for a in r.get("attempts") or []:
                if not a.get("ok") and a.get("method") and a.get("method") != method:
                    self.way(action, where, a["method"], False, "next_rung")
            if method and method != "none":
                self.way(action, where, method, True, ms=r.get("ms") if isinstance(r.get("ms"), int) else None)
            if action == "hotkey" and isinstance((r.get("params") or {}).get("combo"), str):
                self.key(r["params"]["combo"])
        else:
            res = r.get("result") if isinstance(r.get("result"), dict) else {}
            tried = [a.get("method") for a in res.get("attempts") or [] if a.get("method")]
            self.way(action, where, res.get("method") or (tried[-1] if tried else "any"), False, code)


def apply(prof: Profile, lease_id: str, rows: list[dict], version: str = "") -> dict:
    """Fold one lease's action rows for this profile's app into it. Rows of other apps are skipped."""
    s = _Session(prof, lease_id, version)
    for r in rows:
        if r.get("event") == "action" and r.get("action") in ACTING \
                and ((r.get("scope") or {}).get("app") or {}).get("id") == prof.app_id:
            s.row(r)
    if version:
        prof.app.fields["ver"] = enc(version)
    return s.stats


def by_app(rows: list[dict]) -> dict[str, AppIdentity]:
    """The apps a lease's rows act on (a lease can be re-scoped from one app to another)."""
    out: dict[str, AppIdentity] = {}
    for r in rows:
        scope = r.get("scope") or {}
        app = scope.get("app") or {}
        if r.get("event") == "action" and r.get("action") in ACTING and app.get("id"):
            out[app["id"]] = AppIdentity(app["id"], app.get("name") or app["id"], scope.get("process") or "",
                                         version=app.get("version") or "")
    return out


def learn_lease(store: ProfileStore, lease_id: str, rows: list[dict]) -> dict:
    """Revise the profile of every app the lease acted on. Returns {app id: stats or error}."""
    out = {}
    for app_id, ident in by_app(rows).items():
        for attempt in (1, 2):
            try:
                prof = store.load_or_new(ident)
                stats = apply(prof, lease_id, rows, ident.version)
                if not stats["actions"]:
                    break  # only refusals by the gateway's own rules: nothing about the app
                summary = (f"use: {stats['actions']} actions, {stats['controls']} controls, "
                           f"{stats['new']} new items")
                store.save(prof, summary)
                out[app_id] = {**stats, "rev": prof.revision}
                break
            except ProfileConflict:
                if attempt == 2:
                    out[app_id] = {"error": "the profile kept changing while this session was saved"}
            except OspError as e:
                out[app_id] = {"error": str(e)}  # an unreadable profile is left as it is, never overwritten
                break
    return out
