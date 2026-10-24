"""Publishing a profile on the registry: the person reads every line, then GitHub opens with the file filled in.

Overshoulder never signs in to GitHub and holds no token. It opens GitHub's new-file page on the registry
repo; GitHub asks the person to sign in, makes their copy of the repo when they can't write to it, and
opens the pull request, where the registry's checks run. The GitHub user name only names the file:
apps/<app id>/<user name>.osp, the one place the registry lets that person write.

GitHub takes the file's text in the link only up to a point: on 2026-10-24 a 4,000-character link went
through and 7,000 got a server error. A longer profile opens the same page with only its name filled in,
and the person pastes the text. GitHub's upload page is no use here: it needs push access to the repo.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote, urlencode

from overshoulder import registry, share, sign
from overshoulder.osp import OspError, Profile

REPO_URL = f"https://github.com/{registry.REPO}"
BRANCH = "main"
MAX_LINK = 4000  # characters in the whole link; measured, see above


@dataclass
class Draft:
    app_id: str
    name: str
    text: str  # the export, exactly what will be public
    kept: int
    dropped: dict[str, int]
    login: str = ""
    path: str = ""
    link: str = ""  # GitHub's new-file page; holds the text only when fits
    fits: bool = False
    link_chars: int = 0  # how long the link with the text in it would be
    errors: list[str] = field(default_factory=list)  # what the registry's check would refuse
    flags: list[str] = field(default_factory=list)  # lines the registry's reviewer looks at


def login_ok(login: str) -> str:
    """A GitHub user name, without the @; OspError when it can't be one."""
    login = login.strip().removeprefix("@")
    if not sign.LOGIN_RE.match(login):
        raise OspError(f"{login!r} is not a GitHub user name: letters, digits and hyphens, at most 39")
    return login


def draft(prof: Profile, login: str = "") -> Draft:
    """The export to publish, and with a user name, where it goes, the registry's check and the link."""
    shared, st = share.export(prof)
    d = Draft(prof.app_id, prof.name, shared.dumps(), st["kept"], st["dropped"])
    if not login:
        return d
    d.login = login_ok(login)
    d.path = f"apps/{prof.app_id}/{d.login.lower()}.osp"
    rep = registry.check_file(d.path, d.text.encode("utf-8"))
    d.errors, d.flags = rep.errors, rep.flags
    full = new_file_link(d.path, d.text)
    d.link_chars, d.fits = len(full), len(full) <= MAX_LINK
    d.link = full if d.fits else new_file_link(d.path)
    return d


def new_file_link(path: str, text: str | None = None) -> str:
    params = {"filename": path} if text is None else {"filename": path, "value": text}
    return f"{REPO_URL}/new/{BRANCH}?{urlencode(params, quote_via=quote)}"


def next_steps(d: Draft) -> str:
    """What the person does on GitHub, in the words the CLI and the settings page both use."""
    paste = "" if d.fits else "Paste the profile's text into the empty file. "
    return (f"{paste}Sign in to GitHub if it asks, then choose Commit changes (Propose changes when you can't "
            "write to the registry). GitHub makes your copy of the registry and opens a pull request; the "
            "registry's checks run on it, and Yep reviews it as @yepgent before it is merged.")
