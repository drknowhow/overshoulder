"""Publishing a profile: the person sees every line, the file is named after their GitHub user name, the
registry's own check runs first, and GitHub opens with the file filled in, or with only its name when the
text is too long for a link. Overshoulder never signs in to GitHub and keeps no user name."""

import copy
import json
import sys
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from test_mcp_server import daemon  # noqa: F401  (fixture reuse)
from test_registry import CALC_ID, CALCULATOR

from overshoulder import cli, publish, registry, settings_page, share
from overshoulder.osp import Item, OspError, loads
from overshoulder.profiles import ProfileStore

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")
HOSTILE = "</textarea><script>alert(1)</script> & <b>bold</b>"


def calc():
    return loads(CALCULATOR.read_text(encoding="utf-8"))


def long_calc(n=40, prof=None):
    """The Calculator profile with n more controls: too long for a GitHub link, still a valid registry file."""
    prof = prof or calc()
    ctl = next(i for i in prof.items if i.kind == "ctl" and i.text)
    for k in range(n):
        more = copy.deepcopy(ctl)
        more.id, more.fields["aid"], more.text = f"extra{k}", f"Extra{k}", f"Extra button {k}"
        prof.items.append(more)
    return prof


def test_a_draft_is_the_export_named_after_the_person():
    d = publish.draft(calc())
    assert d.text == share.export(calc())[0].dumps() and d.link == "" and d.path == ""
    d = publish.draft(calc(), " @Pat-Smith ")
    assert d.login == "Pat-Smith" and d.path == f"apps/{CALC_ID}/pat-smith.osp"
    assert d.errors == [] and d.fits and d.link_chars == len(d.link) <= publish.MAX_LINK
    url = urlsplit(d.link)
    assert f"{url.scheme}://{url.netloc}{url.path}" == f"https://github.com/{registry.REPO}/new/main"
    q = parse_qs(url.query, keep_blank_values=True)
    assert q["filename"] == [d.path] and q["value"] == [d.text]
    assert registry.check_file(d.path, q["value"][0].replace("\n", "\r\n").encode()).errors == []  # a browser's CRLF


@pytest.mark.parametrize("login", ["", "a b", "-pat", "pat/../x", "x" * 40, "pat@example.com"])
def test_a_name_that_cant_be_a_github_user_is_refused(login):
    if login:
        with pytest.raises(OspError, match="not a GitHub user name"):
            publish.draft(calc(), login)
    else:
        assert publish.draft(calc(), login).path == ""


def test_a_long_profile_opens_github_with_only_its_name():
    d = publish.draft(long_calc(), "pat")
    assert not d.fits and d.link_chars > publish.MAX_LINK and d.errors == []
    q = parse_qs(urlsplit(d.link).query)
    assert q == {"filename": [d.path]}
    assert "Paste the profile's text" in publish.next_steps(d)
    assert "Paste" not in publish.next_steps(publish.draft(calc(), "pat"))


def test_what_the_registry_would_refuse_is_shown_before_github_opens(monkeypatch):
    monkeypatch.setattr(registry, "MAX_BYTES", 500)
    d = publish.draft(calc(), "pat")
    assert d.errors and "at most 500" in d.errors[0]
    page = settings_page.render_publish(d, "", "tok")
    assert "The registry would refuse this file" in page and "github.com" not in page.split("<textarea")[0]
    assert 'class="button" href="https://github.com' not in page


def test_the_publish_page_shows_every_line_escaped():
    prof = calc()
    prof.items.append(Item("quirk", "q-hostile", fields={"n": "2", "src": "agent"}, text=HOSTILE))
    d = publish.draft(prof)
    page = settings_page.render_publish(d, "", "tok")
    assert "<script>alert(1)" not in page and "&lt;/textarea&gt;&lt;script&gt;" in page
    body = page.split("<textarea")[1].split("</textarea>")[0]
    for line in d.text.splitlines():
        assert settings_page.html.escape(line) in body
    assert 'name="login"' in page and 'action="/settings/profiles/publish"' in page and "Open GitHub" not in page

    page = settings_page.render_publish(publish.draft(prof, "pat"), "", "tok")
    assert f"<code>apps/{CALC_ID}/pat.osp</code>" in page and "Open GitHub with this file</a>" in page
    assert 'target="_blank" rel="noopener noreferrer"' in page and 'name="login"' not in page
    assert "of about 4,000 characters fit" in page and "it isn't kept" in page

    page = settings_page.render_publish(publish.draft(long_calc(), "pat"), "", "tok")
    assert "too long for a GitHub link" in page and "Open GitHub</a>" in page and "Ctrl+A" in page

    page = settings_page.render_publish(publish.draft(prof), "'a b' is not a GitHub user name", "tok")
    assert 'class="bad"' in page and 'name="login"' in page


def test_profile_publish_on_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path))
    opened = []
    monkeypatch.setattr("webbrowser.open", opened.append)
    store = ProfileStore(tmp_path)
    store.save(calc(), "test")
    want = publish.draft(store.load(CALC_ID), "pat")

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["profile", "publish", "calculator", "--as", "pat"]) == 1 and opened == []
    out = capsys.readouterr().out
    assert out.startswith(want.text) and "That is all of it: 13 items" in out and "nothing published" in out

    assert cli.main(["profile", "publish", "calculator", "--as", "@pat", "--yes"]) == 0
    assert opened == [want.link] and "Commit changes" in capsys.readouterr().out
    assert cli.main(["profile", "publish", "calculator", "--as", "pat", "--yes", "--no-open"]) == 0
    assert capsys.readouterr().out.rstrip().splitlines()[-2] == want.link and len(opened) == 1
    assert cli.main(["profile", "publish", "calculator", "--as", "not a name", "--yes"]) == 2
    assert "not a GitHub user name" in capsys.readouterr().err and len(opened) == 1

    store.save(long_calc(prof=store.load(CALC_ID)), "more controls")
    assert cli.main(["profile", "publish", "calculator", "--as", "pat", "--yes"]) == 0
    saved = tmp_path / "publish" / CALC_ID / "pat.osp"
    assert saved.read_text(encoding="utf-8") == publish.draft(store.load(CALC_ID)).text
    assert "too long for a GitHub link" in capsys.readouterr().out
    assert parse_qs(urlsplit(opened[-1]).query) == {"filename": [f"apps/{CALC_ID}/pat.osp"]}


def test_nothing_about_github_is_kept(tmp_path, monkeypatch):
    from overshoulder import settings as st
    assert not any("github" in f.lower() or "login" in f.lower() for f in st.Settings.__dataclass_fields__)
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path))
    monkeypatch.setattr("webbrowser.open", lambda url: None)
    ProfileStore(tmp_path).save(calc(), "test")
    before = sorted(tmp_path.rglob("*"))
    assert cli.main(["profile", "publish", "calculator", "--as", "pat", "--yes"]) == 0
    assert sorted(tmp_path.rglob("*")) == before  # a profile that fits in the link writes nothing


@windows_only
def test_publish_on_the_settings_page(daemon):  # noqa: F811
    from test_settings_http import _http

    d, port, _ = daemon
    d.port_ref["port"] = port
    d.gw.profiles.save(calc(), "test")
    base = f"http://127.0.0.1:{port}"
    op = {"x-overshoulder-operator": d.operator_key, "content-type": "application/json"}
    url = json.loads(_http("POST", f"{base}/operator/settings_link", b"{}", op)[2])["url"]
    headers = _http("GET", url)[1]
    cookie = (headers.get("set-cookie") or headers["Set-Cookie"]).split(";")[0]
    page = _http("GET", f"{base}/settings", cookie=cookie)[2]
    ft = page.split('name="form_token" value="')[1].split('"')[0]
    assert f'href="/settings/profiles/publish?id={CALC_ID}&amp;ft={ft}">Publish</a>' in page

    assert _http("GET", f"{base}/settings/profiles/publish?id={CALC_ID}&ft=forged", cookie=cookie)[0] == 403
    status, _, page = _http("GET", f"{base}/settings/profiles/publish?id={CALC_ID}&ft={ft}", cookie=cookie)
    assert status == 200 and "Publish a profile" in page and "Your GitHub user name" in page
    assert _http("GET", f"{base}/settings/profiles/publish?id=nope&ft={ft}", cookie=cookie)[0] == 404

    form = {"content-type": "application/x-www-form-urlencoded"}
    body = urlencode({"form_token": "forged", "id": CALC_ID, "login": "pat"}).encode()
    assert _http("POST", f"{base}/settings/profiles/publish", body, form, cookie)[0] == 403
    body = urlencode({"form_token": ft, "id": CALC_ID, "login": "pat"}).encode()
    status, _, page = _http("POST", f"{base}/settings/profiles/publish", body, form, cookie)
    link = settings_page.html.escape(publish.draft(calc(), "pat").link)
    assert status == 200 and f'href="{link}"' in page and f"apps/{CALC_ID}/pat.osp" in page
    body = urlencode({"form_token": ft, "id": CALC_ID, "login": "a b"}).encode()
    page = _http("POST", f"{base}/settings/profiles/publish", body, form, cookie)[2]
    assert "is not a GitHub user name" in page and "Open GitHub" not in page
