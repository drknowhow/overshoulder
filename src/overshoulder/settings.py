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

# How much agents may use the person's real mouse and keyboard: (label, explanation) per mode.
INPUT_MODES = {
    "never": ("Never", "Agents work only in the background. Anything that needs your mouse or keyboard is "
                       "refused, and the agent has to ask you instead."),
    "borrow": ("Only while I'm idle", "An agent may borrow your mouse and keyboard after you have left them "
                                     "alone for the number of seconds below."),
    "full": ("Full control", "An agent may use your mouse and keyboard whenever it needs to. Moving the "
                             "mouse or typing still pauses it."),
}
DEFAULT_INPUT_MODE = "borrow"


@dataclass
class Settings:
    waits_enabled: bool = True
    wait_kinds: list[str] = field(default_factory=lambda: sorted(WAIT_KINDS))
    poll_interval_ms: int = 500
    max_wait_minutes: int = 60
    beep_on_detect: bool = False
    # Only these processes may be watched by window/element waits; empty means any.
    wait_processes: list[str] = field(default_factory=list)
    input_mode: str = DEFAULT_INPUT_MODE
    borrow_idle_s: int = 5

    def clamp(self) -> Settings:
        self.poll_interval_ms = max(100, min(int(self.poll_interval_ms), 10_000))
        self.max_wait_minutes = max(1, min(int(self.max_wait_minutes), 24 * 60))
        self.wait_kinds = [k for k in self.wait_kinds if k in WAIT_KINDS]
        self.wait_processes = sorted({p.strip().lower() for p in self.wait_processes if p.strip()})
        if self.input_mode not in INPUT_MODES:
            self.input_mode = DEFAULT_INPUT_MODE
        self.borrow_idle_s = max(1, min(int(self.borrow_idle_s), 120))
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


def load_or_default(path: Path | None = None) -> Settings:
    """load(), but a missing or unreadable file gives the defaults: the gateway reads this before every
    real-input action, and a hand-edited file must not break driving."""
    try:
        return load(path)
    except (OSError, ValueError, TypeError):
        return Settings()


def save(s: Settings, path: Path | None = None) -> Path:
    p = path or settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s.clamp()), indent=2), encoding="utf-8")
    tmp.replace(p)
    return p
