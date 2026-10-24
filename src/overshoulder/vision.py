"""Finding a control by how it looks: a vision model is shown the scope's pixels and a description such
as "the gear icon", and answers with the box it sees there.

Off by default. On the settings page the person picks the local model (Qwen3-VL 8B in Ollama on this
PC, about 13 GB of graphics memory while it is loaded) or a cloud model behind an OpenAI-compatible chat
endpoint, which means screenshots of the scope leave the PC. The local model is asked to stay loaded
for UNLOAD_AFTER after each use and Ollama unloads it then, so the graphics memory comes back when
agents stop asking.

Both are asked for the box on a 0-1000 grid over the image's width and height, which is how Qwen-VL
models answer, and that keeps the answer independent of how a server resizes the image. The models'
own confidence is not used: measured with qwen3-vl:8b it was 0.99 for right and wrong boxes alike.
"""

from __future__ import annotations

import base64
import io
import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Protocol

from PIL import Image

from overshoulder.driver.windows import Rect

VISION_MODES = {
    "off": ("Off", "Agents find controls only through Windows and, if allowed above, the text on screen."),
    "local": ("On this PC", "A vision model in Ollama on this PC looks at the agent's window. Nothing is "
                            "sent anywhere. It takes about 13 GB of graphics memory while loaded and is "
                            "unloaded after 5 minutes without use."),
    "cloud": ("A cloud model", "Screenshots of the agent's window are sent to the service below. Use only "
                               "one you trust with what is on screen."),
}
DEFAULT_LOCAL_URL = "http://127.0.0.1:11434"
DEFAULT_LOCAL_MODEL = "qwen3-vl:8b-instruct"
UNLOAD_AFTER = "5m"
TIMEOUT_S = 180  # a cold local model takes a while to load
GRID = 1000
# Longest side of the image sent. A full 4K screen is more than a model needs to find a button.
MAX_SIDE = 2048

PROMPT = ('Find {description} in this screenshot. Answer only with JSON: {{"bbox_2d": [x1, y1, x2, y2]}} '
          "with coordinates from 0 to {grid} across the image's width and height. If it is not in the "
          'screenshot, answer {{"bbox_2d": null}}.')


# Words that describe a control's kind rather than which one it is; "the Save button" is confirmed by
# the text "Save".
FILLER = frozenset("the a an this that button icon link field box control menu item tab label text called "
                   "named with".split())


def _content(s: str) -> set[str]:
    return {w for w in re.findall(r"[^\W_]+", s.casefold()) if w not in FILLER}


def agrees(seen: str, description: str) -> bool:
    """Whether text or a control name seen in a box confirms the description: every word of it, apart
    from words like "button", is in the description. Strict on purpose: "Save As" does not confirm
    "the Save button", and an unconfirmed box is asked about."""
    words = _content(seen)
    return bool(words) and words <= _content(description)


class VisionError(Exception):
    """The model could not be asked, or answered with something that is not a box."""


class Locator(Protocol):
    name: str

    def locate(self, img: Image.Image, description: str) -> Rect | None: ...


def _png_b64(img: Image.Image) -> str:
    if max(img.size) > MAX_SIDE:
        k = MAX_SIDE / max(img.size)
        img = img.resize((max(1, round(img.width * k)), max(1, round(img.height * k))), Image.Resampling.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGB").save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _post_json(url: str, payload: dict, headers: dict | None = None, timeout: float = TIMEOUT_S) -> dict:
    req = urllib.request.Request(url, json.dumps(payload).encode(),
                                 {"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        raise VisionError(f"{url} answered {e.code}: {e.read()[:300].decode(errors='replace')}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise VisionError(f"could not reach {url}: {e}") from e
    except ValueError as e:
        raise VisionError(f"{url} did not answer with JSON") from e


def parse_box(reply: str, size: tuple[int, int]) -> Rect | None:
    """The box in a model's reply, in pixels of an image of `size`. None when the model says it is not
    there; VisionError when the reply holds no box at all."""
    m = re.search(r'"bbox_2d"\s*:\s*(null|\[[^\]]*\])', reply)
    if m and m.group(1) == "null":
        return None
    nums = re.findall(r"-?\d+(?:\.\d+)?", m.group(1) if m else reply)
    if len(nums) < 4:
        raise VisionError(f"the model's answer has no box: {reply[:200]!r}")
    x1, y1, x2, y2 = (float(v) for v in nums[:4])
    w, h = size
    left, right = sorted((round(x1 * w / GRID), round(x2 * w / GRID)))
    top, bottom = sorted((round(y1 * h / GRID), round(y2 * h / GRID)))
    left, top, right, bottom = max(left, 0), max(top, 0), min(right, w), min(bottom, h)
    if right - left < 2 or bottom - top < 2:
        return None  # a box with no area points at nothing
    return Rect(left, top, right, bottom)


@dataclass
class OllamaLocator:
    url: str = DEFAULT_LOCAL_URL
    model: str = DEFAULT_LOCAL_MODEL
    keep_alive: str = UNLOAD_AFTER

    @property
    def name(self) -> str:
        return f"local {self.model}"

    def locate(self, img: Image.Image, description: str) -> Rect | None:
        out = _post_json(f"{self.url.rstrip('/')}/api/chat", {
            "model": self.model, "stream": False, "keep_alive": self.keep_alive,
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": PROMPT.format(description=description, grid=GRID),
                          "images": [_png_b64(img)]}]})
        return parse_box(str((out.get("message") or {}).get("content", "")), img.size)

    def unload(self) -> None:
        """Free the graphics memory now instead of after keep_alive."""
        _post_json(f"{self.url.rstrip('/')}/api/generate", {"model": self.model, "keep_alive": 0}, timeout=30)


@dataclass
class CloudLocator:
    """An OpenAI-compatible /chat/completions endpoint; url is the base, e.g. https://host/v1."""
    url: str
    model: str
    key: str = ""

    @property
    def name(self) -> str:
        return f"cloud {self.model}"

    def locate(self, img: Image.Image, description: str) -> Rect | None:
        out = _post_json(f"{self.url.rstrip('/')}/chat/completions", {
            "model": self.model, "temperature": 0,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": PROMPT.format(description=description, grid=GRID)},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{_png_b64(img)}"}}]}]},
            headers={"Authorization": f"Bearer {self.key}"} if self.key else None)
        try:
            reply = out["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as e:
            raise VisionError(f"unexpected answer from {self.url}: {str(out)[:200]}") from e
        return parse_box(str(reply), img.size)


def unavailable(settings) -> str | None:
    """Why controls can't be found by how they look under these settings, or None."""
    mode = getattr(settings, "vision_mode", "off")
    if mode == "local":
        return None
    if mode == "cloud":
        if not (settings.vision_cloud_url and settings.vision_cloud_model):
            return "the cloud vision model's address or name is missing on the settings page"
        return None
    return "finding controls by how they look is turned off on the settings page"


def locator_for(settings) -> Locator:
    """The locator the settings ask for; call unavailable() first."""
    if settings.vision_mode == "cloud":
        return CloudLocator(settings.vision_cloud_url, settings.vision_cloud_model, settings.vision_cloud_key)
    return OllamaLocator(settings.vision_local_url, settings.vision_local_model)
