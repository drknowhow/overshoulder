"""Sharing app profiles between PCs: export a scrubbed copy, import someone else's into this PC's profile.

Export keeps what is about the app and was seen more than once, and nothing that only matters on this
PC:
- a control seen in only one session, or known only by a hash of its name, stays behind;
- a method tried fewer than twice, a key used once with no name, a painted control whose words were
  used in only one session, and the revision history stay behind;
- bookkeeping (which session saw an item last, name hashes, misses, when it was saved, the program's
  file name) is dropped;
- an agent's note is checked again for addresses, paths and links (notes.PERSONAL), and dropped if one
  got in.
An agent asking to export is asked about at the desk first (service.py); the person exporting from the
settings page or the command line has asked already.

Import reads a file as untrusted: the .osp grammar, its size limits and its end hash must all hold, and
a registry signature, if the file has one, must check out (sign.py), or nothing is taken. Then each
item this PC's profile lacks is added with src=imported, and an item it already has stays as it is: what
this PC learned wins. A file without a signature can be imported; the preview says it is not from the
registry. An export never carries a signature on, because its lines are this PC's.
"""

from __future__ import annotations

from overshoulder import __version__, notes, sign
from overshoulder.appid import AppIdentity
from overshoulder.osp import MAX_BYTES, Item, OspError, Profile, dec, loads

DROP_FIELDS = frozenset({"last", "nh", "nses", "at", "exe", "miss"})
NOTE_KINDS = frozenset({"quirk", "how", "note"})


def _copy(item: Item, **fields: str) -> Item:
    kept = {k: v for k, v in item.fields.items() if k not in DROP_FIELDS}
    return Item(item.kind, item.id, list(item.args), {**kept, **fields}, item.text, [list(s) for s in item.steps])


def _personal(item: Item) -> bool:
    texts = [item.text or ""] + [dec(t) for s in item.steps for t in s[1:]]
    return any(notes.PERSONAL.search(t) for t in texts)


def _why_not(item: Item) -> str | None:
    """Why an item stays on this PC, or None to share it."""
    if item.kind == "rev":
        return "revision history"
    if item.kind == "ctl" and (item.num("ses") < 2 or item.id.startswith("n-")):
        return "seen in one session only"
    if item.kind == "way" and item.num("ok") + item.num("fail") < 2:
        return "tried once only"
    if item.kind == "key" and item.num("n") < 2 and item.text is None:
        return "used once only"
    if item.kind == "paint" and item.text is None:
        return "described in one session only"
    if item.kind in NOTE_KINDS and _personal(item):
        return "holds an address, a path or a link"
    if item.kind not in {"view", "ctl", "way", "key", "paint", *NOTE_KINDS}:
        return "a kind this Overshoulder does not share"
    return None


def export(prof: Profile) -> tuple[Profile, dict]:
    """A copy of prof to share, and {kept, dropped: {reason: count}}."""
    app_fields = {"rev": "0", **({"ver": prof.app.fields["ver"]} if "ver" in prof.app.fields else {})}
    out = Profile(Item("app", prof.app_id, fields=app_fields, text=prof.app.text),
                  made_with=f"Overshoulder/{__version__}")
    dropped: dict[str, int] = {}
    for item in prof.items:
        why = _why_not(item)
        if why:
            dropped[why] = dropped.get(why, 0) + 1
        else:
            out.items.append(_copy(item))
    return out, {"kept": len(out.items), "dropped": dropped}


def read(data: bytes) -> Profile:
    """A file to import, checked: UTF-8, the .osp grammar, its limits, its end hash and any signature."""
    if len(data) > MAX_BYTES:
        raise OspError(f"file is larger than {MAX_BYTES} bytes")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        raise OspError("not a profile: the file is not UTF-8 text") from None
    prof = loads(text)
    sign.verify(prof)
    return prof


NOT_FROM_REGISTRY = ("Not from the registry: no one has reviewed this file. Import it only if you trust "
                     "where it came from.")


def preview(other: Profile) -> dict:
    """What an import would bring, for the person to look at before saying yes."""
    kinds: dict[str, int] = {}
    for item in other.items:
        kinds[item.kind] = kinds.get(item.kind, 0) + 1
    shown = [{"kind": x.kind, "text": x.text or "", "url": dec(x.fields.get("url", "")),
              "steps": [" ".join(dec(t) for t in s) for s in x.steps]}
             for x in other.items if x.kind in NOTE_KINDS]
    signed = sign.verify(other)
    return {"app": other.app_id, "name": other.name, "version": dec(other.app.fields.get("ver", "")),
            "made_with": other.made_with, "items": len(other.items), "kinds": kinds, "notes": shown,
            "registry": signed.words() if signed else "", "from_registry": signed is not None}


def merge(local: Profile, other: Profile) -> dict:
    """Add other's items that local lacks, marked src=imported. Local items stay as they are."""
    if other.app_id != local.app_id:
        raise OspError(f"that file is the profile of {other.app_id}, not {local.app_id}")
    stats = {"imported": 0, "kept_local": 0, "dropped": 0}
    for item in other.items:
        if item.kind == "rev":
            continue
        if local.get(item.kind, item.id) is not None:
            stats["kept_local"] += 1
            continue
        try:
            local.put(_copy(item, src="imported"))
            stats["imported"] += 1
        except OspError:
            stats["dropped"] += 1  # the profile is full
    return stats


def import_profile(store, other: Profile) -> dict:
    """Merge a checked file into this PC's profile of the same app and save it as the next revision."""
    from overshoulder.learn import revise
    ident = AppIdentity(other.app_id, other.name, "", version="")
    return revise(store, ident, lambda prof: merge(prof, other),
                  lambda st: f"import: {st['imported']} items, {st['kept_local']} already here")
