"""What an app's profile suggests while an agent works: a short brief when it takes a scope, and which
methods to try first.

A profile is knowledge, not permission. Nothing here allows, approves or skips a check: order() only
changes the order of methods an action may already use, and the agent reads the brief as information.
"""

from __future__ import annotations

from overshoulder.learn import way_id
from overshoulder.osp import Item, Profile, enc

# Fields kept for learning that tell an agent nothing: which session saw an item last, name hashes and
# session counts, where an item came from, when the profile was saved, and a vision box's pixels hash.
BOOKKEEPING = frozenset({"last", "nh", "nses", "ses", "src", "at", "exe", "box", "sz", "dh"})
BRIEF_LINES = 120  # take_scope's brief; profile(op="get") has no limit
# A method that has failed this often in an app and never worked there is tried after the others.
DEMOTE_FAILS = 2
_ORDER = ("view", "way", "ctl", "key", "quirk", "how", "paint", "note")


def _rank(item: Item) -> int:
    """Most used first: what the agent is likeliest to need."""
    if item.kind == "way":
        return item.num("ok") + item.num("fail")
    if item.kind == "ctl":
        return item.num("seen") + item.num("ok") + item.num("ses")
    if item.kind == "paint":
        return item.num("hit") + item.num("ses")
    return item.num("n")


def _trim(item: Item, version: str) -> Item:
    fields = {k: v for k, v in item.fields.items() if k not in BOOKKEEPING}
    if fields.get("ver") == version:
        del fields["ver"]  # seen on the running version; a ver= left on an item means it is not yet
    return Item(item.kind, item.id, list(item.args), fields, item.text, [list(s) for s in item.steps])


def brief(prof: Profile, version: str = "", limit: int | None = BRIEF_LINES) -> str:
    """The profile as .osp lines without bookkeeping, most used items first. version is the running app's
    version: an item still carrying ver= was last seen on an older one."""
    ver = enc(version) if version else ""
    head = _trim(prof.app, "")
    if ver:
        head.fields["ver"] = ver
    lines = [head.line()]
    learned_on = prof.app.fields.get("ver", "")
    if ver and learned_on and learned_on != ver:
        lines.append(f"# learned on version {learned_on}; this is {ver}: items with ver= are not yet seen on it")
    items = []
    for kind in _ORDER:
        items += sorted(prof.of_kind(kind), key=_rank, reverse=True)
    for n, item in enumerate(items):
        if limit is not None and len(lines) >= limit:
            lines.append(f'# {len(items) - n} more items: profile(op="get") has them all')
            break
        lines.append(_trim(item, ver).line())
    return "\n".join(lines)


def order(prof: Profile, action: str, where: str, rungs: list[str]) -> list[str]:
    """rungs with the ones that keep failing here (an action on this kind of target in this app) moved to
    the end; the rest keep the ladder's own order."""
    def failing(method: str) -> bool:
        it = prof.get("way", way_id(action, where, method))
        return it is not None and it.num("ok") == 0 and it.num("fail") >= DEMOTE_FAILS

    return [m for m in rungs if not failing(m)] + [m for m in rungs if failing(m)]
