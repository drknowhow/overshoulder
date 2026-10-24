"""Notes an agent adds to an app's profile (profile(op="add")): what it found out about the app.

  quirk  something the app does that an agent should know ("Find opens a panel, not a dialog")
  key    a shortcut and what it does
  how    steps that get something done, each an action and what it acts on: [["hotkey", "ctrl+p"],
         ["click", "Print"]]; its "used" count rises when a later session's actions follow the steps
  note   what a guide or the app's documentation says, with the link to it

A note is written by an agent, so it is information, never an instruction, and it grants nothing; the
brief keeps its src=agent. Notes describe the app, not the person: text that holds an email address, a
path or a link (a guide's link goes in url) is refused, and so are typed values in steps.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import quote, urlsplit, urlunsplit

from overshoulder.appid import slug
from overshoulder.osp import MAX_STEPS, MAX_TEXT, Item, OspError, Profile, dec, enc

KINDS = ("quirk", "key", "how", "note")
# Agent notes a profile keeps (quirks, how-tos and guide notes together); past it, adds are refused.
MAX_NOTES = 200
MAX_TARGET = 60
STEP_ACTIONS = frozenset({"click", "double_click", "right_click", "drag", "invoke", "toggle", "select",
                          "set_value", "type", "hotkey", "focus", "activate"})
PERSONAL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+|\b[a-z]:\\|\\users\\|/home/|/users/|://", re.I)
_KEY = re.compile(r"[a-z0-9][a-z0-9+]{0,40}")


def _text(value, what: str = "text", required: bool = True) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, str) or not " ".join(value.split()):
        raise ValueError(f"{what} is required")
    text = " ".join(value.split())
    if len(text) > MAX_TEXT or not text.isprintable():
        raise ValueError(f"{what} must be at most {MAX_TEXT} printable characters")
    if PERSONAL.search(text):
        raise ValueError(f"{what} holds an address, a path or a link: notes describe the app, not the "
                         "person's files or accounts (a guide's link goes in url)")
    return text


def _token(text: str) -> str:
    return quote(text, safe="!#$&'()*+,-./<=>?@[]^_`{|}~")  # enc() without : and ;, which mark steps


def _steps(value) -> list[list[str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_STEPS:
        raise ValueError(f"steps must be a list of 1 to {MAX_STEPS} steps")
    out = []
    for step in value:
        if isinstance(step, str):
            step = [step]
        if not isinstance(step, list) or not 1 <= len(step) <= 2 or not all(isinstance(t, str) for t in step):
            raise ValueError('each step is [action] or [action, what it acts on], e.g. ["click", "Print"]; '
                             "typed values are not kept")
        action = step[0].strip().lower()
        if action not in STEP_ACTIONS:
            raise ValueError(f"step action {step[0]!r} is not one of {', '.join(sorted(STEP_ACTIONS))}")
        tokens = [action]
        if len(step) == 2:
            target = " ".join(step[1].split())
            if not target or len(target) > MAX_TARGET or not target.isprintable() or PERSONAL.search(target):
                raise ValueError(f"step target {step[1]!r} must be a control's name or a key combination, "
                                 f"at most {MAX_TARGET} characters, with no address, path or link")
            tokens.append(_token(target))
        out.append(tokens)
    return out


def _url(value) -> str:
    if not isinstance(value, str):
        raise ValueError("a guide note needs url, the link to the guide")
    parts = urlsplit(value.strip())
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password \
            or parts.hostname in ("localhost", "127.0.0.1", "::1"):
        raise ValueError("url must be a public http(s) link")
    url = urlunsplit((parts.scheme, parts.netloc, parts.path, "", parts.fragment))  # a query can carry a token
    if len(enc(url)) > 200:
        raise ValueError("url is longer than a profile keeps (200 characters)")
    return enc(url)


def _short(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()[:10]


def add(prof: Profile, note: dict) -> tuple[Item, bool]:
    """Add an agent's note to the profile. Returns (item, new). The same note again counts it (n) rather
    than adding a copy. Raises ValueError for a note the profile cannot keep."""
    if not isinstance(note, dict):
        raise ValueError("item must be an object such as {\"kind\": \"quirk\", \"text\": \"...\"}")
    kind = note.get("kind")
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    fields: dict[str, str] = {}
    steps: list[list[str]] = []
    if kind == "key":
        iid = re.sub(r"\s+", "", str(note.get("combo") or "").lower())
        if not _KEY.fullmatch(iid):
            raise ValueError('a key note needs combo, such as "ctrl+shift+n"')
        text = _text(note.get("text"))
    elif kind == "quirk":
        text = _text(note.get("text"))
        iid = f"q-{_short(text)}"
    elif kind == "how":
        text = _text(note.get("text"), "text (what the steps do)")
        steps = _steps(note.get("steps"))
        iid = f"h-{slug(text, 40) or _short(text)}"
    else:
        text = _text(note.get("text"), "text (what the guide says)")
        fields["url"] = _url(note.get("url"))
        iid = f"g-{_short(dec(fields['url']) + ' ' + text)}"
    item = prof.get(kind, iid)
    new = item is None
    if new:
        if kind != "key" and sum(len(prof.of_kind(k)) for k in ("quirk", "how", "note")) >= MAX_NOTES:
            raise ValueError(f"this profile keeps {MAX_NOTES} notes and has them all")
        item = Item(kind, iid, fields=fields)
    if kind != "key":  # a key's n counts its uses
        item.fields["n"] = str(item.num("n") + 1)
    srcs = {*item.fields.get("src", "").split("+"), "agent"} - {""}
    item.fields["src"] = "+".join(sorted(srcs))
    if kind == "key":
        item.text = item.text or text  # a name the app showed for the key stays; agents only fill a gap
    else:
        item.text = text
    if steps:
        item.steps = steps
    try:
        prof.put(item)
    except OspError as e:
        raise ValueError(f"the profile can't keep this note: {e}") from e
    return item, new


def _matches(step: list[str], row: dict) -> bool:
    if row.get("action") != step[0]:
        return False
    if len(step) == 1:
        return True
    want = dec(step[1]).casefold()
    if step[0] == "hotkey":
        combo = (row.get("params") or {}).get("combo")
        return isinstance(combo, str) and re.sub(r"\s+", "", combo).casefold() == re.sub(r"\s+", "", want)
    t = row.get("target") or {}
    return want in ((t.get("name") or "").casefold(), (t.get("automation_id") or "").casefold())


def follow(prof: Profile, rows: list[dict]) -> int:
    """Count a how-to as used (its "used" field) when this session's successful actions took its steps
    in order, other actions in between allowed. Returns how many matched."""
    done = [r for r in rows if r.get("event") == "action" and r.get("ok")]
    matched = 0
    for how in prof.of_kind("how"):
        i = 0
        for r in done:
            if i < len(how.steps) and _matches(how.steps[i], r):
                i += 1
        if how.steps and i == len(how.steps):
            how.fields["used"] = str(how.num("used") + 1)
            matched += 1
    return matched
