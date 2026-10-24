"""The .osp profile format: one item per line, small enough for an agent to read as it is.

    #osp 1 made_with=Overshoulder/0.1.0
    app Microsoft.WindowsNotepad_8wekyb3d8bbwe exe=Notepad.exe ver=11.2607.14.0 rev=7 "Notepad"
    ctl add-tab main Button aid=AddButton seen=5 ok=5 "Add New Tab"
    how close-unsaved ok=2 "Close a tab without saving" : click close-tab ; click dont-save
    end sha256=<hex digest of every line above, each ending in a newline>

An item line is `kind id [positional ...] [field=value ...] ["text"] [: step ; step ...]`. The quoted
text is the only free text in a file, and whoever reads it treats it as untrusted. A line that does not
parse is an error, never skipped, because profiles come from other people's PCs.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote

FORMAT_VERSION = 1
MAX_BYTES = 256 * 1024
MAX_ITEMS = 2000
MAX_LINE = 600
MAX_TEXT = 300
MAX_FIELDS = 16
MAX_STEPS = 20

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._@+~-]{0,119}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
VALUE_RE = re.compile(r'^[!#-~]{1,200}$')  # printable ASCII without space or double quote
_TOKEN = re.compile(r'\s*("(?:[^"\\\x00-\x1f\x7f]|\\["\\])*"|[^\s"]+)')
_HEADER = re.compile(r"^#osp (\d+)(?: made_with=([!#-~]{1,80}))?$")
_END = re.compile(r"^end sha256=([0-9a-f]{64})$")


class OspError(ValueError):
    code = "bad_profile"


def enc(text: str) -> str:
    """A field value from any text (a file name with spaces, a non-ASCII title): percent-encoded."""
    return quote(text, safe="!#$&'()*+,-./:;<=>?@[]^_`{|}~")


def dec(value: str) -> str:
    return unquote(value)


@dataclass(frozen=True)
class Spec:
    positional: tuple[str, ...] = ()
    required: int = 0
    text: bool = True
    steps: bool = False


# Positional names document each kind; fields are open (NAME_RE) so a later Overshoulder can add one
# without older readers refusing the file. A new kind needs a new FORMAT_VERSION.
KINDS: dict[str, Spec] = {
    "app": Spec(),
    "view": Spec(("type",)),
    "ctl": Spec(("view", "type"), required=2),
    "way": Spec(("method",), required=1, text=False),
    "key": Spec(),
    "quirk": Spec(),
    "how": Spec(steps=True),
    "paint": Spec(("view",), required=1),
    "note": Spec(),
    "rev": Spec(),
}
KIND_ORDER = {k: i for i, k in enumerate(KINDS)}


@dataclass
class Item:
    kind: str
    id: str
    args: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    text: str | None = None
    steps: list[list[str]] = field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        return self.kind, self.id

    def num(self, name: str, default: int = 0) -> int:
        v = self.fields.get(name, "")
        return int(v) if v.isdigit() else default

    def check(self) -> None:
        spec = KINDS.get(self.kind)
        if spec is None:
            raise OspError(f"unknown kind {self.kind!r}")
        if not ID_RE.match(self.id):
            raise OspError(f"{self.kind}: bad id {self.id!r}")
        if not spec.required <= len(self.args) <= len(spec.positional):
            raise OspError(f"{self.kind} {self.id}: takes {spec.required} to {len(spec.positional)} positional "
                           f"values, got {len(self.args)}")
        for a in self.args:
            if not ID_RE.match(a):
                raise OspError(f"{self.kind} {self.id}: bad value {a!r}")
        if len(self.fields) > MAX_FIELDS:
            raise OspError(f"{self.kind} {self.id}: more than {MAX_FIELDS} fields")
        for k, v in self.fields.items():
            if not NAME_RE.match(k) or not VALUE_RE.match(v):
                raise OspError(f"{self.kind} {self.id}: bad field {k}={v!r}")
        if self.text is not None:
            if not spec.text:
                raise OspError(f"{self.kind} {self.id}: takes no text")
            if len(self.text) > MAX_TEXT or re.search(r"[\x00-\x1f\x7f]", self.text):
                raise OspError(f"{self.kind} {self.id}: text is longer than {MAX_TEXT} or has control characters")
        if self.steps:
            if not spec.steps:
                raise OspError(f"{self.kind} {self.id}: takes no steps")
            if len(self.steps) > MAX_STEPS:
                raise OspError(f"{self.kind} {self.id}: more than {MAX_STEPS} steps")
            for step in self.steps:
                if not 1 <= len(step) <= 3 or not all(VALUE_RE.match(t) and t not in (":", ";") for t in step):
                    raise OspError(f"{self.kind} {self.id}: bad step {' '.join(step)!r}")

    def line(self) -> str:
        self.check()
        parts = [self.kind, self.id, *self.args, *(f"{k}={v}" for k, v in self.fields.items())]
        if self.text is not None:
            parts.append('"' + self.text.replace("\\", "\\\\").replace('"', '\\"') + '"')
        if self.steps:
            parts.append(":")
            parts.append(" ; ".join(" ".join(s) for s in self.steps))
        out = " ".join(parts)
        if len(out) > MAX_LINE:
            raise OspError(f"{self.kind} {self.id}: line longer than {MAX_LINE}")
        return out


def parse_line(line: str) -> Item:
    if len(line) > MAX_LINE:
        raise OspError(f"line longer than {MAX_LINE}")
    tokens: list[str] = []
    pos, steps_raw = 0, None
    while pos < len(line):
        m = _TOKEN.match(line, pos)
        if not m:
            if line[pos:].strip():
                raise OspError(f"can't read {line[pos:pos + 20]!r}")
            break
        tok, pos = m.group(1), m.end()
        if tok == ":":
            steps_raw = line[pos:]
            break
        tokens.append(tok)
    if len(tokens) < 2:
        raise OspError("an item needs a kind and an id")
    kind, item_id, rest = tokens[0], tokens[1], tokens[2:]
    item = Item(kind, item_id)
    for i, tok in enumerate(rest):
        if tok.startswith('"'):
            if i != len(rest) - 1:
                raise OspError(f"{kind} {item_id}: text must come last")
            item.text = re.sub(r'\\(["\\])', r"\1", tok[1:-1])
        elif "=" in tok:
            k, v = tok.split("=", 1)
            if k in item.fields:
                raise OspError(f"{kind} {item_id}: field {k} given twice")
            item.fields[k] = v
        elif item.fields:
            raise OspError(f"{kind} {item_id}: positional value {tok!r} after a field")
        else:
            item.args.append(tok)
    if steps_raw is not None:
        item.steps = [s.split() for s in steps_raw.split(";")]
        if not all(item.steps):
            raise OspError(f"{kind} {item_id}: empty step")
    item.check()
    return item


@dataclass
class Profile:
    app: Item
    items: list[Item] = field(default_factory=list)
    made_with: str = ""
    version: int = FORMAT_VERSION

    def __post_init__(self):
        if self.app.kind != "app":
            raise OspError("a profile starts with its app item")

    @property
    def app_id(self) -> str:
        return self.app.id

    @property
    def name(self) -> str:
        return self.app.text or self.app.id

    @property
    def revision(self) -> int:
        return self.app.num("rev")

    def get(self, kind: str, item_id: str) -> Item | None:
        return next((x for x in self.items if x.key == (kind, item_id)), None)

    def of_kind(self, kind: str) -> list[Item]:
        return [x for x in self.items if x.kind == kind]

    def put(self, item: Item) -> Item:
        """Add the item, or replace the one with the same kind and id in place."""
        if item.kind == "app":
            raise OspError("the app item is Profile.app")
        item.check()
        for i, x in enumerate(self.items):
            if x.key == item.key:
                self.items[i] = item
                return item
        if len(self.items) >= MAX_ITEMS - 1:
            raise OspError(f"a profile holds at most {MAX_ITEMS} items")
        self.items.append(item)
        return item

    def remove(self, kind: str, item_id: str) -> bool:
        before = len(self.items)
        self.items = [x for x in self.items if x.key != (kind, item_id)]
        return len(self.items) != before

    def body(self) -> str:
        header = f"#osp {self.version}" + (f" made_with={self.made_with}" if self.made_with else "")
        ordered = sorted(self.items, key=lambda x: KIND_ORDER[x.kind])  # stable: keeps order within a kind
        return "".join(line + "\n" for line in [header, self.app.line(), *(x.line() for x in ordered)])

    def dumps(self) -> str:
        body = self.body()
        out = body + f"end sha256={hashlib.sha256(body.encode('utf-8')).hexdigest()}\n"
        if len(out.encode("utf-8")) > MAX_BYTES:
            raise OspError(f"profile is larger than {MAX_BYTES} bytes")
        return out


def loads(text: str) -> Profile:
    """Parse and check a whole file: header, app item first, one item per line, end line with the hash."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise OspError(f"file is larger than {MAX_BYTES} bytes")
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    if len(lines) < 3:
        raise OspError("a profile needs a header, an app line and an end line")
    head = _HEADER.match(lines[0])
    if not head:
        raise OspError("line 1: not an .osp header")
    if int(head.group(1)) != FORMAT_VERSION:
        raise OspError(f"format version {head.group(1)}; this Overshoulder reads version {FORMAT_VERSION}")
    end = _END.match(lines[-1])
    if not end:
        raise OspError(f"line {len(lines)}: the last line must be 'end sha256=<hash>'")
    body = "".join(line + "\n" for line in lines[:-1])
    if hashlib.sha256(body.encode("utf-8")).hexdigest() != end.group(1):
        raise OspError("the hash on the end line does not match: the file was changed after it was written")
    if len(lines) - 3 >= MAX_ITEMS:
        raise OspError(f"more than {MAX_ITEMS} items")
    items, seen = [], set()
    for n, line in enumerate(lines[1:-1], 2):
        try:
            item = parse_line(line)
        except OspError as e:
            raise OspError(f"line {n}: {e}") from None
        if item.key in seen:
            raise OspError(f"line {n}: {item.kind} {item.id} appears twice")
        if (n == 2) != (item.kind == "app"):
            raise OspError(f"line {n}: the app item must be line 2, and only line 2")
        seen.add(item.key)
        items.append(item)
    return Profile(items[0], items[1:], made_with=head.group(2) or "", version=FORMAT_VERSION)
