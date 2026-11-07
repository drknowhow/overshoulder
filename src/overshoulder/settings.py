"""User settings, stored as JSON in the data directory and edited on the local settings page."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from overshoulder.gateway.clients import default_home
from overshoulder.vision import DEFAULT_LOCAL_MODEL, DEFAULT_LOCAL_URL, VISION_MODES

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
    # Agents may read text on screen with Windows OCR to find controls UI Automation can't see.
    ocr_enabled: bool = True
    # Agents may ask a vision model where a described control is (vision.py): "off", "local" or "cloud".
    vision_mode: str = "off"
    vision_local_url: str = DEFAULT_LOCAL_URL
    vision_local_model: str = DEFAULT_LOCAL_MODEL
    vision_cloud_url: str = ""
    vision_cloud_model: str = ""
    vision_cloud_key: str = field(default="", repr=False)
    # When a lease ends, what its actions showed about each app is added to that app's profile (learn.py).
    learn_profiles: bool = True
    # Once a week, load the registry's catalog to find newer revisions of profiles installed from it.
    # Off until the person turns it on: nothing goes online unless they ask (catalog.py).
    catalog_weekly_check: bool = False
    # The visible "Made with Overshoulder" mark on tutorials, GIFs, step images and annotated screenshots.
    # On for a new install; turning it off is free. The files' metadata says it either way (mark.py).
    show_mark: bool = True
    # Take MCP calls from the agent box (agentbox.py) on the WSL adapter; only /mcp is served there.
    listen_wsl: bool = False

    def clamp(self) -> Settings:
        self.poll_interval_ms = max(100, min(int(self.poll_interval_ms), 10_000))
        self.max_wait_minutes = max(1, min(int(self.max_wait_minutes), 24 * 60))
        self.wait_kinds = [k for k in self.wait_kinds if k in WAIT_KINDS]
        self.wait_processes = sorted({p.strip().lower() for p in self.wait_processes if p.strip()})
        if self.input_mode not in INPUT_MODES:
            self.input_mode = "never"  # an unknown mode never grants more than the person chose
        self.borrow_idle_s = max(1, min(int(self.borrow_idle_s), 120))
        if self.vision_mode not in VISION_MODES:
            self.vision_mode = "off"
        self.vision_local_url = self.vision_local_url.strip() or DEFAULT_LOCAL_URL
        self.vision_local_model = self.vision_local_model.strip() or DEFAULT_LOCAL_MODEL
        self.vision_cloud_url = self.vision_cloud_url.strip()
        self.vision_cloud_model = self.vision_cloud_model.strip()
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


# The last settings read from each file, so a file that breaks later keeps the person's own policy, and
# why each file could not be read, for status().
_last_good: dict[Path, Settings] = {}
_problems: dict[Path, str] = {}


def restrictive() -> Settings:
    """What applies when a settings file exists but can't be read and nothing was read from it before."""
    return Settings(input_mode="never", waits_enabled=False, vision_mode="off")


def load_or_default(path: Path | None = None) -> Settings:
    """load(), never raising. A missing file gives the defaults (a new install). A file that exists but
    can't be read gives the last settings read from it, else restrictive(), so damage never grants more
    than the person chose; problem() says what is wrong until the file reads again."""
    p = path or settings_path()
    try:
        s = load(p)
    except (OSError, ValueError, TypeError) as e:
        kept = _last_good.get(p)
        _problems[p] = (f"{p} could not be read ({type(e).__name__}); using "
                        + ("the last settings read from it" if kept else "restrictive settings (no real input, "
                           "no waits, no vision)") + " until it is fixed or saved again")
        return replace(kept) if kept else restrictive()
    _problems.pop(p, None)
    if p.exists():
        _last_good[p] = replace(s)
    return s


def problem(path: Path | None = None) -> str:
    """Why the settings file could not be read at the last load_or_default, or ""."""
    return _problems.get(path or settings_path(), "")


def save(s: Settings, path: Path | None = None) -> Path:
    p = path or settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(s.clamp()), indent=2), encoding="utf-8")
    tmp.replace(p)
    return p
