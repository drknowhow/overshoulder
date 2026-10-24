"""The shared-profile registry's rules, and the files it publishes.

The registry is github.com/drknowhow/overshoulder-profiles: one profile per app per GitHub account, at
apps/<app id>/<login>.osp, added by pull request. Its CI runs this module from a pinned Overshoulder, so
the app and the registry agree on what a shared profile may hold:

    python -m overshoulder.registry check --author <login> --changes changes.tsv --root pr
    python -m overshoulder.registry build --root . --out dist --github-repo drknowhow/overshoulder-profiles

check: a pull request may add, change or remove only its author's own files (maintainers may touch any),
and every file it adds or changes must be exactly what an Overshoulder export writes: the .osp grammar
and end hash, no sig line (the registry signs on merge), nothing an export leaves out, only the fields
an export writes, no address, path or link in any text, and the app id of its folder. Text that reads
like an instruction to an agent is flagged for the reviewer, not refused: "click OK first" is a normal
how-to.

build: on every merge to main, each profile is signed with the registry key (by = the pull request's
author, pr, at = the day it merged) and written to the output folder with index.json, the catalog, and
index.json.sig, its detached signature. Overshoulder checks both before it shows the catalog.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from overshoulder import notes, share, sign
from overshoulder.appid import ID_RE
from overshoulder.osp import OspError, Profile, dec

REPO = "drknowhow/overshoulder-profiles"
MAX_BYTES = 64 * 1024  # what a registry profile may weigh; a typical one is a few KB
MAINTAINERS = frozenset({"yepgent", "drknowhow"})
INDEX_FORMAT = 1
PATH_RE = re.compile(r"^apps/([^/]+)/([^/]+)\.osp$")

_NUM = r"\d{1,9}"
_SRC = r"(?:use|look|agent|imported)(?:\+(?:use|look|agent|imported)){0,3}"
_ENC = r"[!#-~]{1,200}"
# Every field an export writes, per kind, and the shape of its value. A field Overshoulder starts
# writing must be added here; tests/test_registry.py exports a profile built by learn and notes and
# fails naming this table when one is missing.
FIELDS: dict[str, dict[str, str]] = {
    "view": {"src": _SRC, "n": _NUM},
    "ctl": {"aid": _ENC, "name": r"content|none", "src": _SRC, "ak": r"[!#-~]{1,20}", "ses": _NUM, "ver": _ENC,
            "seen": _NUM, "ok": _NUM, "fail": _NUM, "ask": _NUM, "hit": _NUM},
    "way": {"src": _SRC, "ok": _NUM, "fail": _NUM, "ms": _NUM, "why": r"[a-z_]{1,40}"},
    "key": {"src": _SRC, "n": _NUM, "of": r"[A-Za-z0-9][A-Za-z0-9._@+~-]{0,119}", "hit": _NUM},
    "paint": {"src": _SRC, "box": r"\d{1,5}(?:,\d{1,5}){3}", "sz": r"\d{1,5},\d{1,5}", "dh": r"[0-9a-f]{16}",
              "ver": _ENC, "ses": _NUM, "hit": _NUM},
    "quirk": {"src": _SRC, "n": _NUM},
    "how": {"src": _SRC, "n": _NUM, "used": _NUM},
    "note": {"src": _SRC, "n": _NUM, "url": _ENC},
}
APP_FIELDS = {"rev": r"0", "ver": _ENC}

# Words that suggest a note talks to agents rather than about the app. A flag for the reviewer only.
FLAG_RE = re.compile(
    r"\b(?:ignore|disregard|forget|override)\b.{0,40}\b(?:instructions?|rules?|prompts?|policy)\b"
    r"|\byou (?:are|must|should|will)\b|\b(?:system|developer) prompt\b"
    r"|\b(?:assistant|chatgpt|claude|gpt|llm|ai model)\b"
    r"|\b(?:powershell|cmd\.exe|curl|wget|iex|invoke-expression|regedit|bash)\b"
    r"|\b(?:password|passcode|token|api key|secret|credit card)\b", re.I)


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)


def where(path: str) -> tuple[str, str] | None:
    """(app id, login) for a profile's place in the registry, or None if the path is not one."""
    m = PATH_RE.match(path)
    if not m or not ID_RE.match(m.group(1)) or not sign.LOGIN_RE.match(m.group(2)):
        return None
    return m.group(1), m.group(2)


def _texts(prof: Profile):
    for item in prof.items:
        label = f"{item.kind} {item.id}"
        if item.text:
            yield label, item.text
        for step in item.steps:
            for tok in step[1:]:
                yield label, dec(tok)
        for name in ("aid", "ver"):
            if name in item.fields:
                yield label, dec(item.fields[name])
    if prof.app.text:
        yield "app", prof.app.text


def check_file(path: str, data: bytes) -> Report:
    """Whether one file may be published at path: everything but who may change it."""
    rep = Report()
    place = where(path)
    if place is None:
        rep.errors.append("a profile goes at apps/<app id>/<your GitHub login>.osp, with the login in lower case")
        return rep
    app_id, login = place
    if login != login.lower():
        rep.errors.append(f"name the file {login.lower()}.osp: logins are written in lower case here")
    if len(data) > MAX_BYTES:
        rep.errors.append(f"the file is {len(data):,} bytes; a registry profile may be at most {MAX_BYTES:,}")
        return rep
    try:
        prof = share.read(data)
    except OspError as e:
        rep.errors.append(f"not a valid profile: {e}")
        return rep
    if prof.sig is not None:
        rep.errors.append("leave out the sig line: the registry signs a profile when its pull request merges")
    if prof.app_id != app_id:
        rep.errors.append(f"this is the profile of {prof.app_id}, so it goes in apps/{prof.app_id}/")
    if not prof.made_with.startswith("Overshoulder/"):
        rep.errors.append("the header must say made_with=Overshoulder/<version>, as an export writes it")
    for name, value in prof.app.fields.items():
        if name not in APP_FIELDS or not re.fullmatch(APP_FIELDS[name], value):
            rep.errors.append(f"the app line has {name}={value}; an export writes only rev=0 and ver=")
    _, report = share.export(prof)
    for why, n in report["dropped"].items():
        rep.errors.append(f"{n} item{'s' if n > 1 else ''} an export leaves out ({why}); export the profile "
                          "from Overshoulder instead of editing it")
    for item in prof.items:
        allowed = FIELDS.get(item.kind, {})
        for name, value in item.fields.items():
            if name not in allowed:
                rep.errors.append(f"{item.kind} {item.id}: {name}= is a field an export never writes")
            elif not re.fullmatch(allowed[name], value):
                rep.errors.append(f"{item.kind} {item.id}: {name}={value} is not a value an export writes")
        if item.kind == "note" and "url" in item.fields:
            try:
                if notes._url(dec(item.fields["url"])) != item.fields["url"]:
                    raise ValueError
            except ValueError:
                rep.errors.append(f"note {item.id}: its link must be a public http(s) address with no query")
    for label, text in _texts(prof):
        if notes.PERSONAL.search(text):
            rep.errors.append(f"{label}: holds an e-mail address, a file path or a link: {text[:80]!r}")
        if FLAG_RE.search(text):
            rep.flags.append(f"{label}: reads like it talks to an agent; check it: {text[:120]!r}")
    return rep


def check_change(path: str, status: str, author: str, previous: str = "") -> list[str]:
    """Who may change what: contributors their own apps/<app>/<login>.osp files, maintainers anything."""
    if author.lower() in MAINTAINERS:
        return []
    errors = []
    for p in filter(None, (path, previous)):
        place = where(p)
        if place is None:
            errors.append(f"{p}: a pull request may only add, change or remove apps/<app id>/{author.lower()}.osp "
                          "files")
        elif place[1].lower() != author.lower():
            errors.append(f"{p}: this is @{place[1]}'s profile; yours goes at apps/{place[0]}/{author.lower()}.osp")
    return errors


def check_pull_request(root: Path, changes: list[tuple[str, str, str]], author: str) -> Report:
    """changes: (status, path, previous path) as the pull request files API gives them."""
    rep = Report()
    for status, path, previous in changes:
        for e in check_change(path, status, author, previous):
            rep.errors.append(e)
        if status != "removed" and path.endswith(".osp"):
            f = root / path
            r = check_file(path, f.read_bytes() if f.is_file() else b"")
            rep.errors += [f"{path}: {e}" for e in r.errors]
            rep.flags += [f"{path}: {x}" for x in r.flags]
    return rep


@dataclass
class Published:
    by: str
    pr: int
    at: str
    rev: int


def build(root: Path, out: Path, meta: dict[str, Published], key, key_id: str, commit: str = "") -> dict:
    """Sign every profile that arrived by a merged pull request and write the signed files and the catalog."""
    entries, skipped = [], []
    for f in sorted((root / "apps").glob("*/*.osp")):
        path = f.relative_to(root).as_posix()
        rep = check_file(path, f.read_bytes())
        if rep.errors or path not in meta:
            skipped.append({"path": path, "why": rep.errors[0] if rep.errors else "no merged pull request"})
            continue
        m = meta[path]
        prof = share.read(f.read_bytes())
        data = sign.sign(prof, key, key_id, m.by, m.pr, m.at).dumps().encode("utf-8")
        dest = out / path
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        kinds: dict[str, int] = {}
        for item in prof.items:
            kinds[item.kind] = kinds.get(item.kind, 0) + 1
        entries.append({"app": prof.app_id, "name": prof.name, "version": dec(prof.app.fields.get("ver", "")),
                        "path": path, "by": m.by, "pr": m.pr, "at": m.at, "rev": m.rev,
                        "items": len(prof.items), "kinds": kinds, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    entries.sort(key=lambda e: (e["name"].lower(), e["app"], e["by"]))
    index = {"format": INDEX_FORMAT, "registry": f"https://github.com/{REPO}", "commit": commit,
             "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "profiles": entries}
    data = (json.dumps(index, indent=1, ensure_ascii=False) + "\n").encode("utf-8")
    out.mkdir(parents=True, exist_ok=True)
    (out / "index.json").write_bytes(data)
    (out / "index.json.sig").write_text(sign.sign_index(data, key, key_id), encoding="utf-8", newline="\n")
    return {"published": len(entries), "skipped": skipped}


def read_index(data: bytes, sig_text: str, keys: dict[str, str] | None = None) -> dict:
    """The catalog, after its signature checks out: {format, registry, commit, built, profiles: [...]}."""
    sign.verify_index(data, sig_text, keys)
    index = json.loads(data.decode("utf-8"))
    if not isinstance(index, dict) or index.get("format") != INDEX_FORMAT or not isinstance(
            index.get("profiles"), list):
        raise OspError(f"the catalog is not format {INDEX_FORMAT}; update Overshoulder")
    return index


def _git(root: Path, *args: str) -> str:
    import subprocess
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


def meta_from_github(root: Path, repo: str, token: str) -> dict[str, Published]:
    """For each profile, the merged pull request that last changed it: who opened it, its number, the day."""
    import urllib.request
    out: dict[str, Published] = {}
    for f in sorted((root / "apps").glob("*/*.osp")):
        path = f.relative_to(root).as_posix()
        sha = _git(root, "log", "-1", "--format=%H", "--", path)
        rev = int(_git(root, "rev-list", "--count", "HEAD", "--", path) or 0)
        req = urllib.request.Request(f"https://api.github.com/repos/{repo}/commits/{sha}/pulls",
                                     headers={"Authorization": f"Bearer {token}",
                                              "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            pulls = json.load(r)
        merged = [p for p in pulls if p.get("merged_at") and p.get("base", {}).get("ref") == "main"]
        if merged:
            p = merged[0]
            out[path] = Published(p["user"]["login"], int(p["number"]), p["merged_at"][:10], rev)
    return out


def _annotate(level: str, text: str) -> None:
    path, _, msg = text.partition(": ")
    if where(path) or path.startswith(("apps/", ".github/")) or "/" in path:
        print(f"::{level} file={path}::{msg}")
    else:
        print(f"::{level}::{text}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m overshoulder.registry")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", help="check a pull request's changes")
    c.add_argument("--author", required=True)
    c.add_argument("--changes", required=True, help="a file of 'status<TAB>path<TAB>previous path' lines")
    c.add_argument("--root", default=".", help="where the pull request's files are checked out")
    b = sub.add_parser("build", help="sign every profile and write the catalog")
    b.add_argument("--root", default=".")
    b.add_argument("--out", default="dist")
    b.add_argument("--github-repo", default=REPO)
    b.add_argument("--key-id", default="reg-1")
    b.add_argument("--key-env", default="REGISTRY_SIGNING_KEY")
    a = ap.parse_args(argv)
    if a.cmd == "check":
        changes = []
        for line in Path(a.changes).read_text(encoding="utf-8").splitlines():
            if line.strip():
                status, path, *prev = line.split("\t")
                changes.append((status, path, prev[0] if prev else ""))
        rep = check_pull_request(Path(a.root), changes, a.author)
        for e in rep.errors:
            _annotate("error", e)
        for x in rep.flags:
            _annotate("warning", x)
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a", encoding="utf-8") as s:
                s.write(f"## Profile check\n\n{len(changes)} changed files, {len(rep.errors)} problems, "
                        f"{len(rep.flags)} flags for the reviewer.\n\n")
                s.writelines(f"- **Problem:** {e}\n" for e in rep.errors)
                s.writelines(f"- Flag: {x}\n" for x in rep.flags)
        print(f"{len(changes)} changed files: {len(rep.errors)} problems, {len(rep.flags)} flags")
        return 1 if rep.errors else 0
    root = Path(a.root)
    key = sign.private_key(os.environ[a.key_env])
    meta = meta_from_github(root, a.github_repo, os.environ["GITHUB_TOKEN"])
    res = build(root, Path(a.out), meta, key, a.key_id, commit=_git(root, "rev-parse", "HEAD"))
    for s in res["skipped"]:
        print(f"::warning file={s['path']}::not published: {s['why']}")
    print(f"published {res['published']} profiles; skipped {len(res['skipped'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
