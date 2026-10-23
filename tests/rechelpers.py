"""Synthetic recordings for renderer tests: a clean video, its frame track, meta and actions."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw

HAVE_FFMPEG = shutil.which("ffmpeg") is not None
ORIGIN = (100, 200)  # where the recorded window sits on screen


def frame(i: int, size: tuple[int, int]) -> Image.Image:
    img = Image.new("RGB", size, (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.rectangle((20, 20, 120, 60), fill=(220, 30, 30))  # a red "secret" the tests redact
    d.rectangle((200, 150, 260, 180), fill=(30, 30, 200))  # a blue button the actions click
    d.text((10, size[1] - 20), f"frame {i}", fill=(0, 0, 0))
    return img


def make_recording(root: Path, seconds: float = 4.0, fps: int = 10, size: tuple[int, int] = (320, 240),
                   actions: list[dict] | None = None) -> Path:
    rec = root / "rec"
    rec.mkdir(parents=True, exist_ok=True)
    n = int(seconds * fps)
    enc = subprocess.Popen([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                            "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-r", str(fps), "-i", "-",
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "10", "-pix_fmt", "yuv420p",
                            str(rec / "video.mp4")], stdin=subprocess.PIPE)
    with open(rec / "frames.jsonl", "w", encoding="utf-8") as track:
        for i in range(n):
            enc.stdin.write(frame(i, size).tobytes())
            track.write(json.dumps({"i": i, "t": i / fps, "bounds": [ORIGIN[0], ORIGIN[1], ORIGIN[0] + size[0],
                                                                     ORIGIN[1] + size[1]],
                                    "cursor": [0, 0]}) + "\n")
    enc.stdin.close()
    enc.wait()
    (rec / "meta.json").write_text(json.dumps({"fps": fps, "size": list(size), "start": 1000.0, "end": 1000.0 + seconds,
                                               "frames": n}), encoding="utf-8")
    if actions is None:
        button = [ORIGIN[0] + 200, ORIGIN[1] + 150, ORIGIN[0] + 260, ORIGIN[1] + 180]
        actions = [{"event": "action", "action": "click", "ok": True, "t_act": 1001.0, "ts": 1001.2, "ms": 200,
                    "target": {"kind": "element", "name": "Go", "rect": button}},
                   {"event": "action", "action": "hotkey", "ok": True, "t_act": 1002.5, "ts": 1002.6, "ms": 100,
                    "params": {"combo": "ctrl+s"}}]
    (rec / "actions.json").write_text(json.dumps(actions), encoding="utf-8")
    return rec


def red_pixels(img: Image.Image) -> int:
    raw = img.convert("RGB").tobytes()
    return sum(1 for i in range(0, len(raw), 3) if raw[i] > 180 and raw[i + 1] < 80 and raw[i + 2] < 80)


def video_frames(path: Path, size: tuple[int, int]) -> list[Image.Image]:
    raw = subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-i", str(path), "-f",
                          "rawvideo", "-pix_fmt", "rgb24", "-"], capture_output=True, check=True).stdout
    n = size[0] * size[1] * 3
    return [Image.frombytes("RGB", size, raw[i:i + n]) for i in range(0, len(raw) - n + 1, n)]
