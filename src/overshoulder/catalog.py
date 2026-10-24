"""The registry's catalog on this PC: what people published on overshoulder.yepgent.com.

Nothing here goes online on its own. load() runs when the person presses Load on the settings page's
Find profiles, or runs `overshoulder profile search` or `profile get`; the weekly check for newer
revisions runs only after the person turns it on. The catalog's signature is checked with the key
Overshoulder ships before anything in it is used, and a profile is checked against the catalog's hash
and its own signature before it reaches the import preview, where the person still decides.

The last catalog loaded stays in <home>/catalog/, so a take_scope brief can say a shared profile of the
app exists without going online. Agents can't install a profile: no MCP tool calls get().
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote

from overshoulder import __version__, registry, share, sign
from overshoulder.osp import OspError, Profile

SITE = "https://overshoulder.yepgent.com"
URL_ENV = "OVERSHOULDER_REGISTRY_URL"  # another address for the same files: a test server, a mirror
MAX_INDEX = 4 * 1024 * 1024
TIMEOUT_S = 20
WEEK_S = 7 * 24 * 3600
CHECK_EVERY_S = 3600  # how often the weekly check looks at whether a week has passed


class CatalogError(OspError):
    code = "catalog"


def base_url() -> str:
    return (os.environ.get(URL_ENV) or SITE).rstrip("/")


def fetch(url: str, limit: int) -> bytes:
    """GET one file from the registry's site, at most limit bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": f"Overshoulder/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            data = r.read(limit + 1)
    except urllib.error.HTTPError as e:
        raise CatalogError(f"the registry answered {e.code} for {url}") from None
    except (urllib.error.URLError, OSError) as e:
        reason = getattr(e, "reason", None) or e
        raise CatalogError(f"can't reach the registry at {base_url()}: {reason}") from None
    if len(data) > limit:
        raise CatalogError(f"{url} is larger than {limit:,} bytes")
    return data


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Catalog:
    def __init__(self, home: Path, fetcher=None):
        self.dir = Path(home) / "catalog"
        self.fetch = fetcher or fetch  # tests pass a fake: nothing else here touches the network
        self._lock = threading.Lock()
        self._cache: tuple[float, dict | None] | None = None  # (mtime of index.json, the checked index)

    # -- the catalog ---------------------------------------------------------------------------

    def load(self) -> dict:
        """Fetch index.json and its signature, check the signature, keep both. Goes online."""
        base = base_url()
        data = self.fetch(f"{base}/index.json", MAX_INDEX)
        sig = self.fetch(f"{base}/index.json.sig", 4096)
        try:
            index = registry.read_index(data, sig.decode("utf-8"))
        except UnicodeDecodeError:
            raise CatalogError("the catalog's signature is not text") from None
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            for name, body in (("index.json.sig", sig), ("index.json", data)):
                tmp = self.dir / f"{name}.tmp"
                tmp.write_bytes(body)
                os.replace(tmp, self.dir / name)
            self._write("loaded.json", {"at": _now(), "from": base})
            self._cache = None
        return index

    def cached(self) -> dict | None:
        """The catalog loaded last, its signature checked again; None if there is none. Offline."""
        f = self.dir / "index.json"
        try:
            mtime = f.stat().st_mtime
        except OSError:
            return None
        with self._lock:
            if self._cache is not None and self._cache[0] == mtime:
                return self._cache[1]
            try:
                index = registry.read_index(f.read_bytes(), (self.dir / "index.json.sig").read_text("utf-8"))
            except (OSError, ValueError):  # OspError is a ValueError
                index = None
            self._cache = (mtime, index)
            return index

    def loaded_at(self) -> str:
        return str(self._read("loaded.json").get("at", ""))

    def entries_for(self, app_id: str) -> list[dict]:
        index = self.cached()
        return [e for e in (index or {}).get("profiles", []) if e.get("app") == app_id]

    def entry(self, path: str) -> dict | None:
        """The cached catalog's entry for a file path: the only paths get() will fetch."""
        index = self.cached()
        return next((e for e in (index or {}).get("profiles", []) if e.get("path") == path), None)

    # -- one profile ---------------------------------------------------------------------------

    def get(self, entry: dict) -> tuple[bytes, Profile]:
        """Download one profile the catalog lists and check it: the catalog's hash, the file's own
        signature, the app. Goes online. The bytes go to the import preview; nothing is imported here."""
        data = self.fetch(f"{base_url()}/{quote(entry['path'])}", registry.MAX_BYTES)
        if hashlib.sha256(data).hexdigest() != entry.get("sha256"):
            raise CatalogError(f"{entry['path']} doesn't match the catalog: it changed after the catalog was "
                               "signed. Load the catalog again.")
        prof = share.read(data)
        if sign.verify(prof) is None:
            raise CatalogError(f"{entry['path']} is not signed by the registry")
        if prof.app_id != entry.get("app"):
            raise CatalogError(f"{entry['path']} holds the profile of {prof.app_id}, not {entry.get('app')}")
        return data, prof

    # -- what this PC installed from the registry, and newer revisions of it -------------------

    def installed(self) -> dict[str, dict]:
        """app id -> {path, by, rev, sha256, at} of the registry profile last installed for it."""
        return self._read("installed.json")

    def mark_installed(self, entry: dict) -> None:
        with self._lock:
            got = self._read("installed.json")
            got[entry["app"]] = {"path": entry["path"], "by": entry.get("by", ""), "rev": entry.get("rev", 0),
                                 "sha256": entry.get("sha256", ""), "at": _now()}
            self._write("installed.json", got)

    def newer(self, index: dict | None = None) -> list[dict]:
        """Catalog entries that are a newer revision of a profile installed from the registry."""
        index = index if index is not None else self.cached()
        got = self.installed()
        out = []
        for e in (index or {}).get("profiles", []):
            mine = got.get(e.get("app", ""))
            if mine and mine.get("path") == e.get("path") and int(e.get("rev", 0)) > int(mine.get("rev", 0)):
                out.append(e)
        return out

    def weekly_check(self, enabled: bool, now: float | None = None) -> list[dict] | None:
        """If the person turned the check on and a week has passed since the last one, load the catalog
        and return the newer revisions; otherwise None and nothing goes online."""
        if not enabled:
            return None
        now = time.time() if now is None else now
        last = float(self._read("checked.json").get("t", 0) or 0)
        if now - last < WEEK_S:
            return None
        self._write("checked.json", {"t": now, "at": _now()})
        try:
            return self.newer(self.load())
        except OspError:
            return None  # offline this week; the settings page shows the catalog loaded last

    # -- files ---------------------------------------------------------------------------------

    def _read(self, name: str) -> dict:
        try:
            got = json.loads((self.dir / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return got if isinstance(got, dict) else {}

    def _write(self, name: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        tmp = self.dir / f"{name}.tmp"
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.replace(tmp, self.dir / name)


def search(index: dict | None, text: str = "", local: set[str] | frozenset[str] = frozenset()) -> list[dict]:
    """Entries whose app name or id contains text (any case): apps with a profile on this PC first."""
    q = text.strip().lower()
    rows = [e for e in (index or {}).get("profiles", [])
            if not q or q in str(e.get("name", "")).lower() or q in str(e.get("app", "")).lower()]
    return sorted(rows, key=lambda e: (e.get("app") not in local, str(e.get("name", "")).lower(), e.get("by", "")))


def tags(entry: dict, local, installed: dict, newer_paths: set[str]) -> list[str]:
    """What Find profiles and `profile search` say about one entry: on this PC, installed, newer revision."""
    out = ["on this PC"] if entry.get("app") in local else []
    mine = installed.get(entry.get("app", ""))
    if mine and mine.get("path") == entry.get("path"):
        out.append("newer revision" if entry.get("path") in newer_paths else "installed")
    return out


def brief_line(entries: list[dict]) -> str:
    """What a take_scope brief says when the app has no profile here but the catalog has one."""
    who = ", ".join(f"@{e.get('by')} ({e.get('items', 0)} items)" for e in entries[:3])
    return (f"A shared profile of this app is on the registry at {SITE}, by {who}. The person can install it "
            "from Find profiles on the settings page; agents can't install profiles.")


def start_weekly_check(catalog: Catalog, settings_source) -> threading.Thread:
    """The gateway's background check: once an hour it asks weekly_check, which does nothing while the
    setting is off. settings_source() returns the current Settings."""
    def loop():
        while True:
            try:
                catalog.weekly_check(bool(getattr(settings_source(), "catalog_weekly_check", False)))
            except Exception:  # the check never takes the gateway down
                pass
            time.sleep(CHECK_EVERY_S)

    t = threading.Thread(target=loop, name="catalog-weekly-check", daemon=True)
    t.start()
    return t
