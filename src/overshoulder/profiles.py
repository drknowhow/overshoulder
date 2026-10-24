"""Where app profiles live: one .osp file per app under <home>/profiles, with past revisions kept beside it.

A profile is knowledge, not permission. Nothing here is read by approvals, denies, scope, limits or input
mode; a profile only helps an agent find its way around an app it has already been allowed to use.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from overshoulder import __version__
from overshoulder.appid import ID_RE, AppIdentity
from overshoulder.osp import Item, OspError, Profile, enc, loads

KEEP_REVS = 20  # rev items in a profile, and old files in its history folder
INBOX_TTL_S = 24 * 3600  # a file waiting for the person to look at it before importing
_INBOX_ID = re.compile(r"[A-Za-z0-9_-]{16,64}")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_profile(app: AppIdentity) -> Profile:
    fields = {"exe": enc(app.exe)} if app.exe else {}  # a field's value can't be empty
    fields["rev"] = "0"
    if app.version:
        fields["ver"] = enc(app.version)
    return Profile(Item("app", app.id, fields=fields, text=app.name[:300] or None))


class ProfileConflict(OspError):
    code = "profile_conflict"


class ProfileStore:
    def __init__(self, home: Path):
        self.dir = Path(home) / "profiles"
        self._lock = threading.Lock()

    def path(self, app_id: str) -> Path:
        if not ID_RE.match(app_id):
            raise OspError(f"bad app id {app_id!r}")
        return self.dir / f"{app_id}.osp"

    def load(self, app_id: str) -> Profile | None:
        p = self.path(app_id)
        if not p.exists():
            return None
        prof = loads(p.read_bytes().decode("utf-8"))
        if prof.app_id != app_id:
            raise OspError(f"{p.name} holds the profile of {prof.app_id}")
        return prof

    def load_or_new(self, app: AppIdentity) -> Profile:
        return self.load(app.id) or new_profile(app)

    def save(self, prof: Profile, summary: str = "") -> Profile:
        """Write the next revision. The file on disk must still be the revision `prof` was loaded at
        (or absent, for a new profile); otherwise ProfileConflict, and the caller reloads and merges."""
        with self._lock:
            p = self.path(prof.app_id)
            base = prof.revision
            on_disk = loads(p.read_bytes().decode("utf-8")) if p.exists() else None
            if (on_disk.revision if on_disk else 0) != base:
                raise ProfileConflict(f"{prof.app_id} is at revision {on_disk.revision if on_disk else 0} on disk, "
                                      f"not {base}")
            rev, at = base + 1, now()
            prof.app.fields["rev"] = str(rev)
            prof.app.fields["at"] = at
            prof.made_with = f"Overshoulder/{__version__}"
            revs = prof.of_kind("rev")
            for old in revs[:max(0, len(revs) - KEEP_REVS + 1)]:
                prof.remove("rev", old.id)
            prof.put(Item("rev", str(rev), fields={"at": at, "items": str(len(prof.items) + 1)},
                          text=summary[:300] or None))
            text = prof.dumps()  # checks every item before anything is written
            self.dir.mkdir(parents=True, exist_ok=True)
            if on_disk is not None:
                self._keep(p, prof.app_id, base)
            tmp = p.with_suffix(".tmp")
            tmp.write_bytes(text.encode("utf-8"))  # bytes: write_text would turn LF into CRLF on Windows
            os.replace(tmp, p)
            return prof

    def _keep(self, p: Path, app_id: str, rev: int) -> None:
        hist = self.dir / "history" / app_id
        hist.mkdir(parents=True, exist_ok=True)
        (hist / f"rev-{rev}.osp").write_bytes(p.read_bytes())
        old = sorted(hist.glob("rev-*.osp"), key=lambda f: int(f.stem[4:]) if f.stem[4:].isdigit() else -1)
        for f in old[:max(0, len(old) - KEEP_REVS)]:
            f.unlink(missing_ok=True)

    def remove(self, app_id: str) -> bool:
        """Take an app's profile off this PC. Its last revision stays in its history folder."""
        with self._lock:
            p = self.path(app_id)
            if not p.exists():
                return False
            hist = self.dir / "history" / app_id
            hist.mkdir(parents=True, exist_ok=True)
            os.replace(p, hist / f"removed-{time.strftime('%Y%m%d-%H%M%S')}.osp")
            return True

    # A file someone wants to import waits in the inbox until the person has looked at it.

    def inbox_put(self, data: bytes) -> str:
        box = self.dir / "inbox"
        box.mkdir(parents=True, exist_ok=True)
        for old in box.glob("*.osp"):
            if time.time() - old.stat().st_mtime > INBOX_TTL_S:
                old.unlink(missing_ok=True)
        key = secrets.token_urlsafe(18)
        (box / f"{key}.osp").write_bytes(data)
        return key

    def inbox_get(self, key: str) -> bytes | None:
        f = self.dir / "inbox" / f"{key}.osp"
        return f.read_bytes() if _INBOX_ID.fullmatch(key or "") and f.exists() else None

    def inbox_drop(self, key: str) -> None:
        if _INBOX_ID.fullmatch(key or ""):
            (self.dir / "inbox" / f"{key}.osp").unlink(missing_ok=True)

    def history(self, app_id: str) -> list[int]:
        hist = self.dir / "history" / self.path(app_id).stem
        return sorted(int(f.stem[4:]) for f in hist.glob("rev-*.osp") if f.stem[4:].isdigit())

    def list(self) -> list[dict]:
        out = []
        for f in sorted(self.dir.glob("*.osp")) if self.dir.exists() else []:
            row = {"id": f.stem, "path": str(f)}
            try:
                prof = loads(f.read_bytes().decode("utf-8"))
            except (OspError, UnicodeDecodeError) as e:
                row["error"] = str(e)
            else:
                row.update(name=prof.name, rev=prof.revision, items=len(prof.items),
                           version=prof.app.fields.get("ver", ""), at=prof.app.fields.get("at", ""))
            out.append(row)
        return sorted(out, key=lambda r: str(r.get("name", r["id"])).lower())

    def find(self, query: str) -> Profile:
        """The profile whose id is `query`, or the only one whose id or name contains it (any case)."""
        if ID_RE.match(query) and self.path(query).exists():
            return self.load(query)
        q = query.lower()
        hits = [r for r in self.list() if q in r["id"].lower() or q in str(r.get("name", "")).lower()]
        if len(hits) != 1:
            names = ", ".join(r["id"] for r in hits[:8]) or "none"
            raise LookupError(f"{len(hits)} profiles match {query!r}: {names}")
        return self.load(hits[0]["id"])
