"""Registry signatures: a signed profile shows who published it, any change after signing is refused, and
an unsigned profile still imports with a warning."""

import hashlib
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from overshoulder import cli, settings_page, share, sign
from overshoulder.osp import Item, OspError, loads

CALCULATOR = Path(__file__).resolve().parents[1] / "docs" / "evidence" / "phase5-e2e" / "calculator.osp"


@pytest.fixture
def key(monkeypatch):
    k = Ed25519PrivateKey.generate()
    monkeypatch.setattr(sign, "REGISTRY_KEYS", {"reg-1": sign.public_hex(k)})
    return k


def _signed(key, **kw) -> str:
    prof = loads(CALCULATOR.read_text(encoding="utf-8"))
    args = {"key_id": "reg-1", "by": "yepgent", "pr": 12, "at": "2026-11-02", **kw}
    return sign.sign(prof, key, **args).dumps()


def _rehash(text: str) -> str:
    """What someone editing a file by hand would do: fix the end line so the plain hash checks out."""
    body = text[:text.rindex("end sha256=")]
    return body + f"end sha256={hashlib.sha256(body.encode('utf-8')).hexdigest()}\n"


def test_a_signed_profile_says_who_published_it(key):
    text = _signed(key)
    lines = text.splitlines()
    assert lines[-2].startswith("sig registry key=reg-1 by=yepgent pr=12 at=2026-11-02 ed25519=")
    assert lines[-1].startswith("end sha256=")
    prof = share.read(text.encode("utf-8"))
    assert sign.verify(prof) == sign.Signed("reg-1", "yepgent", 12, "2026-11-02")
    p = share.preview(prof)
    assert p["from_registry"]
    assert p["registry"] == "Reviewed on the registry, published by @yepgent in pull request #12 on 2026-11-02"
    assert prof.sig is not None and all(x.kind != "sig" for x in prof.items)


@pytest.mark.parametrize("change", [
    lambda t: t.replace("ok=29 fail=0", "ok=29 fail=1"),          # a profile line
    lambda t: t.replace('"Calculator"', '"Calculator Pro"'),      # the app's name
    lambda t: t.replace("made_with=Overshoulder/", "made_with=Other/"),  # the header
    lambda t: t.replace("by=yepgent", "by=someone"),              # the publisher
    lambda t: t.replace("pr=12", "pr=13"),                        # the pull request
    lambda t: t.replace("at=2026-11-02", "at=2026-11-03"),        # the date
])
def test_any_change_after_signing_is_refused(key, change):
    text = _rehash(change(_signed(key)))
    loads(text)  # the plain hash checks out
    with pytest.raises(sign.SignatureError, match="changed after the registry signed it"):
        share.read(text.encode("utf-8"))


def test_a_signature_from_another_key_is_refused(key):
    text = _signed(Ed25519PrivateKey.generate())  # same key id, wrong key
    with pytest.raises(sign.SignatureError, match="does not match"):
        share.read(text.encode("utf-8"))


def test_a_key_this_overshoulder_doesnt_know_is_refused(key):
    text = _signed(key, key_id="reg-9")
    with pytest.raises(sign.SignatureError, match="registry key reg-9, which this Overshoulder doesn't know"):
        share.read(text.encode("utf-8"))


def test_an_unsigned_profile_still_imports_with_a_warning(key, tmp_path):
    from overshoulder.profiles import ProfileStore
    prof = share.read(CALCULATOR.read_bytes())
    assert prof.sig is None and sign.verify(prof) is None
    p = share.preview(prof)
    assert not p["from_registry"] and p["registry"] == ""
    page = settings_page.render_import(p, "", "k" * 20, "ft", 0)
    assert "Not from the registry" in page and "Reviewed on the registry" not in page
    assert share.import_profile(ProfileStore(tmp_path), prof)["imported"] == 13


def test_the_import_page_names_the_publisher(key):
    p = share.preview(share.read(_signed(key).encode("utf-8")))
    page = settings_page.render_import(p, "", "k" * 20, "ft", 0)
    assert "Reviewed on the registry, published by @yepgent in pull request #12 on 2026-11-02" in page
    assert "Not from the registry" not in page


@pytest.mark.parametrize("mangle, why", [
    (lambda t: t.replace(" ed25519=", " note=x ed25519="), "exactly the fields"),
    (lambda t: t.replace("by=yepgent", "by=-bad-"), "by= is not well formed"),
    (lambda t: t.replace("sig registry", "sig someone"), "must be 'sig registry'"),
])
def test_a_malformed_sig_line_is_refused(key, mangle, why):
    with pytest.raises(sign.SignatureError, match=why):
        share.read(_rehash(mangle(_signed(key))).encode("utf-8"))


def test_the_sig_line_must_come_just_before_the_end_line(key):
    lines = _signed(key).splitlines()
    moved = "\n".join([lines[0], lines[1], lines[-2], *lines[2:-2], lines[-1]]) + "\n"
    with pytest.raises(OspError, match="sig line must come just before the end line"):
        loads(_rehash(moved))


def test_a_sig_is_never_an_item_and_never_exported(key):
    prof = share.read(_signed(key).encode("utf-8"))
    with pytest.raises(OspError):
        prof.put(Item("sig", "registry", fields={"key": "reg-1"}))
    shared, _ = share.export(prof)
    assert shared.sig is None and "\nsig " not in shared.dumps()


def test_signing_again_replaces_the_signature(key):
    prof = share.read(_signed(key).encode("utf-8"))
    again = sign.sign(prof, key, "reg-1", "yepgent", 14, "2026-11-05").dumps()
    assert again.count("\nsig ") == 1 and sign.verify(loads(again)).pr == 14


def test_the_command_line_signs_and_verifies(key, tmp_path, monkeypatch, capsys):
    f = tmp_path / "calculator.osp"
    f.write_bytes(CALCULATOR.read_bytes())
    assert cli.main(["profile", "verify", str(f)]) == 1
    assert "Not from the registry" in capsys.readouterr().out
    seed = key.private_bytes_raw().hex()
    monkeypatch.setenv("OVERSHOULDER_SIGNING_KEY", seed)
    assert cli.main(["profile", "sign", str(f), "--by", "yepgent", "--pr", "12", "--at", "2026-11-02"]) == 0
    capsys.readouterr()
    assert cli.main(["profile", "verify", str(f)]) == 0
    assert "published by @yepgent in pull request #12 on 2026-11-02 (key reg-1)" in capsys.readouterr().out
    f.write_text(_rehash(f.read_text(encoding="utf-8").replace("ok=29", "ok=30")), encoding="utf-8", newline="\n")
    assert cli.main(["profile", "verify", str(f)]) == 2
    assert "changed after the registry signed it" in capsys.readouterr().err


def test_the_shipped_registry_keys_are_ed25519_public_keys():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    assert sign.REGISTRY_KEYS, "Overshoulder ships at least one registry key"
    for key_id, pub in sign.REGISTRY_KEYS.items():
        assert sign.KEY_ID_RE.match(key_id)
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(pub))
