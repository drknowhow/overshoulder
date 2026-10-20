"""User settings, stored as JSON in the data directory and edited on the local settings page."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from overshoulder.gateway.clients import default_home

WAIT_KINDS = {
    "window_appears": "A window opens (title or process)",
    "window_closes": "A window closes",
    "title_changes": "A window's title changes, or matches a pattern",
    "element_appears": "A button, field or text appears in a window",
    "element_gone": "A button, field or text disappears from a window",
    "text_contains": "A control in a window shows some text",
    "file": "A file appears, changes or is deleted",
}


@dataclass
class Settings:
    waits_enabled: bool = True
    wait_kinds: list[str] = field(default_factory=lambda: sorted(WAIT_KINDS))
    poll_interval_ms: int = 500
    max_wait_minutes: int = 60
    beep_on_detect: bool = False
    # Only these processes may be watched by window/element waits; empty means any.
    wait_processes: list[str] = field(default_factory=list)

    def clamp(self) -> Settings:
        self.poll_interval_ms = max(100, min(int(self.poll_interval_ms), 10_000))
        self.max_wait_minutes = max(1, min(int(self.max_wait_minutes), 24 * 60))
        self.wait_kinds = [k for k in self.wait_kinds if k in WAIT_KINDS]
        self.wait_processes = sorted({p.strip().lower() for p in self.wait_processes if p.strip()})
        return self


def settings_path() -> Path:
    return default_home() / "settings.json"


def load(path: Path | None = None) -> Settings:
    p = path or settings_path()
    if not p.exists():
        return Settings()
    raw = json.loads(p.read_text(encoding="utf-8"))
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in raw.items() if k in known}).clamp()


def save(s: Settings, path: Path | None = None) -> Path:
    p = path or settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s.clamp()), indent=2), encoding="utf-8")
    tmp.replace(p)
    return p
