"""Registry signatures on .osp files: proof that a profile was reviewed on the registry, and by whom.

The registry (github.com/drknowhow/overshoulder-profiles) signs a profile when its pull request merges:

    sig registry key=reg-1 by=<GitHub login> pr=<number> at=<YYYY-MM-DD> ed25519=<128 hex>

The Ed25519 signature covers DOMAIN, every line above the sig line, and the sig line itself without its
ed25519 field, so neither a profile line nor the publisher, pull request or date can change without
breaking it. The end line's hash still covers the whole file, sig line included.

Overshoulder ships the registry's public keys (REGISTRY_KEYS). A signed file whose signature does not
check out, or that names a key this build doesn't know, is refused. An unsigned file can still be
imported; the import preview says it is not from the registry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from overshoulder.osp import Item, OspError, Profile

DOMAIN = b"overshoulder registry signature 1\n"
SIGNER = "registry"
FIELDS = ("key", "by", "pr", "at", "ed25519")

# Key id -> raw 32-byte Ed25519 public key, hex. The private half lives only in the registry's GitHub
# secret (environment "signing", main only); it was never written to disk, so if it is lost the registry
# moves to a new id and re-signs. A new key gets a new id; an old id stays here while files signed with it
# are in use.
REGISTRY_KEYS: dict[str, str] = {
    "reg-1": "f9e83d9b1ec3045d0816e34d58ec77092daf9beaaf4cf808cdc30d13de87620a",  # made 2026-10-24
}

LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
PR_RE = re.compile(r"^[1-9]\d{0,8}$")
KEY_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,30}$")
SIG_RE = re.compile(r"^[0-9a-f]{128}$")


class SignatureError(OspError):
    code = "bad_signature"


@dataclass(frozen=True)
class Signed:
    """Who published a profile on the registry, from a signature that checked out."""
    key: str
    by: str
    pr: int
    at: str

    def words(self) -> str:
        return f"Reviewed on the registry, published by @{self.by} in pull request #{self.pr} on {self.at}"


def _message(prof: Profile, sig: Item) -> bytes:
    unsigned = Item("sig", sig.id, fields={k: v for k, v in sig.fields.items() if k != "ed25519"})
    return DOMAIN + (prof.unsigned_body() + unsigned.line() + "\n").encode("utf-8")


def _fields(sig: Item) -> dict[str, str]:
    if sig.id != SIGNER or sig.args or set(sig.fields) != set(FIELDS):
        raise SignatureError(f"the sig line must be 'sig {SIGNER}' with exactly the fields {', '.join(FIELDS)}")
    f = sig.fields
    for name, rx in (("key", KEY_ID_RE), ("by", LOGIN_RE), ("pr", PR_RE), ("at", DATE_RE), ("ed25519", SIG_RE)):
        if not rx.match(f[name]):
            raise SignatureError(f"the sig line's {name}= is not well formed")
    return f


def private_key(seed_hex: str):
    """An Ed25519 private key from its 32-byte seed in hex (how the registry's secret holds it)."""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    try:
        seed = bytes.fromhex(seed_hex.strip())
    except ValueError:
        raise ValueError("the signing key must be 64 hex characters") from None
    if len(seed) != 32:
        raise ValueError("the signing key must be 64 hex characters")
    return Ed25519PrivateKey.from_private_bytes(seed)


def public_hex(key) -> str:
    """The raw public key of an Ed25519 private key, in hex, as REGISTRY_KEYS holds it."""
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    return key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()


def sign(prof: Profile, key, key_id: str, by: str, pr: int, at: str) -> Profile:
    """Sign prof in place (replacing any signature it had) and return it."""
    sig = Item("sig", SIGNER, fields={"key": key_id, "by": by, "pr": str(pr), "at": at, "ed25519": "0" * 128})
    _fields(sig)
    sig.fields["ed25519"] = key.sign(_message(prof, sig)).hex()
    prof.sig = sig
    return prof


def verify(prof: Profile, keys: dict[str, str] | None = None) -> Signed | None:
    """None for an unsigned profile, Signed for a good signature; SignatureError for anything else."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    if prof.sig is None:
        return None
    f = _fields(prof.sig)
    known = REGISTRY_KEYS if keys is None else keys
    if f["key"] not in known:
        raise SignatureError(f"it is signed with registry key {f['key']}, which this Overshoulder doesn't know; "
                             "update Overshoulder, or get the file from the registry again")
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(known[f["key"]])).verify(
            bytes.fromhex(f["ed25519"]), _message(prof, prof.sig))
    except InvalidSignature:
        raise SignatureError("the registry's signature does not match: the file was changed after the "
                             "registry signed it") from None
    return Signed(f["key"], f["by"], int(f["pr"]), f["at"])
