"""Find profiles: the registry's catalog is loaded only when the person asks, its signature is checked before
anything in it is used, a profile is checked against the catalog's hash and its own signature before the
import preview, and the weekly check for newer revisions does nothing until it is turned on."""

import hashlib
import json
import shutil
import sys
import urllib.request
from urllib.parse import urlencode

import pytest
from test_hints import svc  # noqa: F401  (fixture reuse)
from test_learn import NOTEPAD
from test_mcp_server import daemon  # noqa: F401  (fixture reuse)
from test_registry import CALC_ID, CALCULATOR, _rich_export

from overshoulder import catalog, cli, registry, settings_page, sign
from overshoulder import settings as st
from overshoulder.profiles import ProfileStore

windows_only = pytest.mark.skipif(sys.platform != "win32", reason="Windows-only driver")

KEY = sign.private_key("22" * 32)
KEYS = {"test-1": sign.public_hex(KEY)}
BASE = "https://registry.test"
CALC_PATH = f"apps/{CALC_ID}/yepgent.osp"
NOTEPAD_PATH = f"apps/{NOTEPAD}/pat.osp"


def publish(root, files: dict[str, bytes], revs: dict[str, int] | None = None, key=KEY, key_id="test-1"):
    """What the registry's merge job writes: signed profiles and a signed catalog, in root/dist."""
    for path, data in files.items():
        (root / "repo" / path).parent.mkdir(parents=True, exist_ok=True)
        (root / "repo" / path).write_bytes(data)
    meta = {p: registry.Published(p.rsplit("/", 1)[1][:-4], 7, "2026-11-02", (revs or {}).get(p, 1))
            for p in files}
    registry.build(root / "repo", root / "dist", meta, key, key_id, commit="abc")
    return root / "dist"


class Web:
    """The registry's site, served from a dist folder; it counts what was fetched."""

    def __init__(self, dist):
        self.dist, self.got = dist, []

    def __call__(self, url: str, limit: int) -> bytes:
        assert url.startswith(BASE + "/"), url
        self.got.append(url[len(BASE) + 1:])
        f = self.dist / url[len(BASE) + 1:]
        if not f.is_file():
            raise catalog.CatalogError(f"the registry answered 404 for {url}")
        data = f.read_bytes()
        if len(data) > limit:
            raise catalog.CatalogError(f"{url} is larger than {limit:,} bytes")
        return data


@pytest.fixture
def reg(tmp_path, monkeypatch):
    """A published registry of two profiles, the keys this test build ships, and a catalog that reads it."""
    monkeypatch.setattr(sign, "REGISTRY_KEYS", KEYS)
    monkeypatch.setenv(catalog.URL_ENV, BASE)
    dist = publish(tmp_path, {CALC_PATH: CALCULATOR.read_bytes(), NOTEPAD_PATH: _rich_export()})
    web = Web(dist)
    return catalog.Catalog(tmp_path / "home", fetcher=web), web, dist


def test_load_checks_the_signature_and_keeps_the_catalog(reg):
    cat, web, _ = reg
    assert cat.cached() is None and web.got == []
    index = cat.load()
    assert web.got == ["index.json", "index.json.sig"]
    assert sorted(e["path"] for e in index["profiles"]) == sorted([CALC_PATH, NOTEPAD_PATH])
    assert cat.cached() == index and cat.loaded_at().endswith("Z")
    assert [e["path"] for e in cat.entries_for(CALC_ID)] == [CALC_PATH]
    assert cat.entry(NOTEPAD_PATH)["app"] == NOTEPAD and cat.entry("apps/x/y.osp") is None
    assert len(web.got) == 2  # reading it again stays on this PC


def test_a_changed_catalog_or_an_unknown_key_is_refused(reg, tmp_path):
    cat, web, dist = reg
    good = (dist / "index.json").read_bytes()
    (dist / "index.json").write_bytes(good.replace(b'"by": "yepgent"', b'"by": "mallory"'))
    with pytest.raises(sign.SignatureError, match="changed after"):
        cat.load()
    assert cat.cached() is None  # nothing that failed its check is kept
    (dist / "index.json").write_bytes(good)
    other = publish(tmp_path / "other", {CALC_PATH: CALCULATOR.read_bytes()}, key=sign.private_key("33" * 32),
                    key_id="other-1")
    with pytest.raises(sign.SignatureError, match="doesn't know"):
        catalog.Catalog(tmp_path / "home2", fetcher=Web(other)).load()
    cat.load()
    (cat.dir / "index.json").write_bytes(good.replace(b"yepgent", b"mallory"))  # edited on this PC
    assert cat.cached() is None and cat.entries_for(CALC_ID) == []


def test_get_checks_the_catalogs_hash_the_files_signature_and_the_app(reg):
    cat, web, dist = reg
    cat.load()
    entry = cat.entry(CALC_PATH)
    data, prof = cat.get(entry)
    assert prof.app_id == CALC_ID and sign.verify(prof).by == "yepgent"
    assert hashlib.sha256(data).hexdigest() == entry["sha256"] and web.got[-1] == CALC_PATH

    signed = (dist / CALC_PATH).read_bytes()
    (dist / CALC_PATH).write_bytes(signed.replace(b"Calculator", b"Calculater", 1))
    with pytest.raises(catalog.CatalogError, match="doesn't match the catalog"):
        cat.get(entry)
    (dist / CALC_PATH).write_bytes(CALCULATOR.read_bytes())  # unsigned, and listed with its own hash
    with pytest.raises(catalog.CatalogError, match="not signed by the registry"):
        cat.get({**entry, "sha256": hashlib.sha256(CALCULATOR.read_bytes()).hexdigest()})
    (dist / CALC_PATH).write_bytes(signed)
    with pytest.raises(catalog.CatalogError, match="holds the profile of"):
        cat.get({**entry, "app": NOTEPAD})


def test_search_puts_apps_on_this_pc_first(reg):
    cat, _, _ = reg
    index = cat.load()
    names = [e["app"] for e in catalog.search(index)]
    assert names == sorted(names, key=lambda a: a != CALC_ID)  # "Calculator" sorts before "Notepad"
    assert [e["app"] for e in catalog.search(index, "", {NOTEPAD})] == [NOTEPAD, CALC_ID]
    assert [e["app"] for e in catalog.search(index, "CALC")] == [CALC_ID]
    assert [e["app"] for e in catalog.search(index, "windowscalculator")] == [CALC_ID]
    assert catalog.search(index, "nothing like it") == [] and catalog.search(None, "x") == []


def test_a_newer_revision_of_an_installed_profile_is_found(reg, tmp_path):
    cat, web, _ = reg
    index = cat.load()
    assert cat.installed() == {} and cat.newer(index) == []
    cat.mark_installed(cat.entry(CALC_PATH))
    assert cat.installed()[CALC_ID]["rev"] == 1 and cat.newer(index) == []
    web.dist = publish(tmp_path / "v2", {CALC_PATH: CALCULATOR.read_bytes(), NOTEPAD_PATH: _rich_export()},
                       revs={CALC_PATH: 2})
    assert [e["path"] for e in cat.newer(cat.load())] == [CALC_PATH]


def test_the_weekly_check_stays_offline_until_it_is_turned_on(reg):
    cat, web, _ = reg
    t = 1_800_000_000.0
    assert cat.weekly_check(False, now=t) is None and web.got == []
    assert cat.weekly_check(True, now=t) == [] and web.got == ["index.json", "index.json.sig"]
    assert cat.weekly_check(True, now=t + 6 * 24 * 3600) is None and len(web.got) == 2
    assert cat.weekly_check(True, now=t + catalog.WEEK_S) == [] and len(web.got) == 4

    def offline(url, limit):
        raise catalog.CatalogError("can't reach the registry")
    cat.fetch = offline
    assert cat.weekly_check(True, now=t + 2 * catalog.WEEK_S) is None  # offline this week; no retry storm
    assert cat._read("checked.json")["t"] == t + 2 * catalog.WEEK_S


def test_the_brief_line_names_the_registry_and_who_may_install(reg):
    cat, _, _ = reg
    cat.load()
    line = catalog.brief_line(cat.entries_for(CALC_ID))
    assert "overshoulder.yepgent.com" in line and "@yepgent (13 items)" in line
    assert "agents can't install profiles" in line


def test_the_settings_page_shows_the_catalog_from_disk(reg):
    cat, _, _ = reg
    page = settings_page.render_page(st.Settings(), "tok", [], "logs", False, find={"index": None})
    assert "Load the catalog</button>" in page and "/settings/catalog/load" in page
    index = cat.load()
    index["profiles"][0]["name"] = "<script>alert(1)</script>"
    cat.mark_installed(cat.entry(NOTEPAD_PATH))
    find = {"index": index, "q": "", "local": {NOTEPAD}, "installed": cat.installed(), "newer": [],
            "loaded_at": cat.loaded_at()}
    page = settings_page.render_page(st.Settings(), "tok", [], "logs", False, find=find)
    assert "<script>alert(1)" not in page and "&lt;script&gt;alert(1)" in page
    assert page.index("Notepad") < page.index("&lt;script&gt;")  # the app on this PC first
    assert '<span class="tag">on this PC</span> <span class="tag">installed</span>' in page
    assert f'name="path" value="{NOTEPAD_PATH}"' in page and "Load again</button>" in page
    assert 'name="catalog_weekly_check" >' in page  # off by default
    page = settings_page.render_page(st.Settings(), "tok", [], "logs", False, find={**find, "q": "zzz"})
    assert "Nothing matches." in page


def test_profile_search_and_get_on_the_command_line(reg, tmp_path, monkeypatch, capsys):
    _, web, _ = reg
    monkeypatch.setenv("OVERSHOULDER_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(catalog, "fetch", web)
    assert cli.main(["profile", "search", "--cached"]) == 2
    assert "no catalog on this PC yet" in capsys.readouterr().err
    assert cli.main(["profile", "search"]) == 0
    out = capsys.readouterr().out
    assert f"Calculator  {CALC_ID}  by @yepgent, 13 items, revision 1" in out and "2 of 2 profiles" in out

    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    assert cli.main(["profile", "get", "calculator"]) == 1
    store = ProfileStore(tmp_path / "home")
    assert store.load(CALC_ID) is None and catalog.Catalog(tmp_path / "home").installed() == {}
    assert cli.main(["profile", "get", "o", "--yes"]) == 2  # matches both
    assert "2 profiles match 'o'" in capsys.readouterr().err
    assert cli.main(["profile", "get", "calc", "--by", "@yepgent", "--yes"]) == 0
    out = capsys.readouterr().out
    assert "Reviewed on the registry, published by @yepgent in pull request #7" in out and "imported 13" in out
    assert store.load(CALC_ID) is not None
    assert catalog.Catalog(tmp_path / "home").installed()[CALC_ID]["path"] == CALC_PATH
    fetched = len(web.got)
    assert cli.main(["profile", "search", "calc", "--cached"]) == 0 and len(web.got) == fetched
    assert "[on this PC, installed]" in capsys.readouterr().out


def no_network(*a, **k):
    raise AssertionError("went online")


def test_reading_the_catalog_stays_on_this_pc(reg, monkeypatch):
    """Only load() and get() fetch; everything that reads the catalog reads the copy on disk."""
    cat, _, _ = reg
    cat.load()
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    cat.fetch = no_network
    cat._cache = None
    assert cat.cached()["profiles"] and cat.entries_for(CALC_ID) and cat.newer() == []
    assert cat.weekly_check(False) is None


@windows_only
def test_take_scope_says_a_shared_profile_exists_without_going_online(svc, reg, monkeypatch):  # noqa: F811
    s, token, _, _ = svc
    cat, _, _ = reg
    monkeypatch.setattr(catalog, "fetch", no_network)
    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    first = s.acquire(token, "window:16")
    assert "shared" not in first["profile"]  # no catalog on this PC yet
    s.release(token, first["lease_id"])
    cat.load()
    shutil.copytree(cat.dir, s.gw.profiles.dir.parent / "catalog")
    out = s.acquire(token, "window:16")
    assert out["profile"]["shared"] == catalog.brief_line(cat.entries_for(NOTEPAD))
    assert "@pat" in out["profile"]["shared"]


def test_agents_cant_install_a_profile():
    """No MCP tool reaches Catalog.get: installing is the person's, on the settings page or the CLI."""
    from overshoulder import server, service
    assert set(service.PROFILE_OPS) == {"get", "learn", "add", "export"}
    for f in (server.__file__, service.__file__):
        text = open(f, encoding="utf-8").read()
        assert "catalog.get(" not in text and "catalog.load(" not in text, f


@windows_only
def test_find_profiles_on_the_settings_page(daemon, reg):  # noqa: F811
    from test_settings_http import _http

    d, port, _ = daemon
    _, web, dist = reg
    d.port_ref["port"] = port
    cat = d.svc.catalog
    cat.fetch = web  # the routes hold this same catalog
    base = f"http://127.0.0.1:{port}"
    op = {"x-overshoulder-operator": d.operator_key, "content-type": "application/json"}
    url = json.loads(_http("POST", f"{base}/operator/settings_link", b"{}", op)[2])["url"]
    headers = _http("GET", url)[1]
    cookie = (headers.get("set-cookie") or headers["Set-Cookie"]).split(";")[0]
    page = _http("GET", f"{base}/settings", cookie=cookie)[2]
    assert "Find profiles" in page and "Load the catalog</button>" in page and web.got == []
    ft = page.split('name="form_token" value="')[1].split('"')[0]
    form = {"content-type": "application/x-www-form-urlencoded"}

    assert _http("POST", f"{base}/settings/catalog/load", urlencode({"form_token": "forged"}).encode(), form,
                 cookie)[0] == 403 and web.got == []
    status, headers, _ = _http("POST", f"{base}/settings/catalog/load", urlencode({"form_token": ft}).encode(),
                               form, cookie)
    assert status == 303 and headers["location"] == "/settings?msg=loaded#find"
    page = _http("GET", f"{base}/settings?msg=loaded", cookie=cookie)[2]
    assert "Loaded the catalog." in page and f'value="{CALC_PATH}"' in page and "signature checks out" in page

    status, headers, _ = _http("POST", f"{base}/settings/catalog/get",
                               urlencode({"form_token": ft, "path": "apps/x/evil.osp"}).encode(), form, cookie)
    assert status == 303 and "apps/x/evil.osp" not in web.got
    assert "not in the catalog loaded last" in _http("GET", f"{base}/settings", cookie=cookie)[2]

    status, headers, _ = _http("POST", f"{base}/settings/catalog/get",
                               urlencode({"form_token": ft, "path": CALC_PATH}).encode(), form, cookie)
    assert status == 303 and headers["location"].startswith("/settings/profiles/import?p=")
    key = headers["location"].split("p=")[1]
    preview = _http("GET", f"{base}{headers['location']}", cookie=cookie)[2]
    assert "Import</button>" in preview and "Reviewed on the registry, published by @yepgent" in preview
    assert d.gw.profiles.load(CALC_ID) is None  # nothing is added before the person confirms
    status, headers, _ = _http("POST", f"{base}/settings/profiles/import/confirm",
                               urlencode({"form_token": ft, "p": key}).encode(), form, cookie)
    assert status == 303 and "msg=imported" in headers["location"]
    assert d.gw.profiles.load(CALC_ID) is not None and cat.installed()[CALC_ID]["path"] == CALC_PATH
    assert '<span class="tag">on this PC</span> <span class="tag">installed</span>' in \
        _http("GET", f"{base}/settings", cookie=cookie)[2]

    (dist / "index.json").write_bytes(b"{}")
    _http("POST", f"{base}/settings/catalog/load", urlencode({"form_token": ft}).encode(), form, cookie)
    page = _http("GET", f"{base}/settings", cookie=cookie)[2]
    assert "The catalog couldn&#x27;t be loaded" in page and f'value="{CALC_PATH}"' in page  # the old one stays
